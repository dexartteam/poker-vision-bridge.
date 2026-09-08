from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from app.config import Settings
from app.coordinator import Coordinator, canonical_hash
from app.models import (
    ActionHistory,
    Blinds,
    Confidence,
    DecisionView,
    FrameRef,
    Hero,
    LegalAction,
    LegalActionType,
    Observation,
    ObservedAction,
    ObservedActionType,
    PokerAIContext,
    Position,
    Seat,
    SourceRef,
    Street,
    TableRef,
    VisionEvent,
    utc_now,
)


logger = logging.getLogger(__name__)


TABLE_SCOPED_MESSAGE_TYPES = frozenset(
    {
        "hand_start",
        "hole_cards",
        "your_turn",
        "player_action",
        "community_cards",
        "hand_result",
        "action_ack",
        "table_state",
        "player_joined",
        "player_left",
    }
)

REPLAYABLE_MESSAGE_TYPES = frozenset(
    {
        "hand_start",
        "player_action",
        "community_cards",
        "hand_result",
        "player_joined",
        "player_left",
    }
)

STREET_RANK = {
    Street.IDLE: -1,
    Street.PREFLOP: 0,
    Street.FLOP: 1,
    Street.TURN: 2,
    Street.RIVER: 3,
    Street.SHOWDOWN: 4,
}


@dataclass(slots=True)
class Authority:
    hand_id: str
    turn_token: str
    state_hash: str
    valid_actions: list[dict[str, Any]]
    observation: Observation


class OpenPokerExecutionError(RuntimeError):
    pass


class OpenPokerOutcomeUnknown(OpenPokerExecutionError):
    """The action may have reached Open Poker, but its acknowledgement was lost."""


def position_map(dealer_seat: int, occupied_seats: list[int], table_slots: int = 6) -> dict[int, Position]:
    seats = sorted(set(occupied_seats), key=lambda seat: (seat - dealer_seat) % table_slots)
    if dealer_seat not in seats:
        return {}
    by_count: dict[int, list[Position]] = {
        2: [Position.BTN, Position.BB],
        3: [Position.BTN, Position.SB, Position.BB],
        4: [Position.BTN, Position.SB, Position.BB, Position.CO],
        5: [Position.BTN, Position.SB, Position.BB, Position.UTG, Position.CO],
        6: [Position.BTN, Position.SB, Position.BB, Position.UTG, Position.MP, Position.CO],
    }
    labels = by_count.get(len(seats))
    if labels is None:
        return {}
    return dict(zip(seats, labels, strict=True))


class OpenPokerRunner:
    """Official Open Poker V2 WebSocket source and executor.

    It never clicks a screen. It keeps platform state separate from camera state and
    reconciles both immediately before executing a camera-derived decision.
    """

    def __init__(self, settings: Settings, coordinator: Coordinator):
        self.settings = settings
        self.coordinator = coordinator
        self.ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._decision_tasks: set[asyncio.Task[Any]] = set()
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._pending_acks: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._current_client_action_id: str | None = None
        self._boot_id = str(uuid.uuid4())
        self._local_seq = 0
        self._last_table_seq = 0

        self.agent_id = "openpoker-selfhosted"
        self.agent_name: str | None = None
        self.table_id: str | None = None
        self.hero_seat: int | None = None
        self.hand_id: str | None = None
        self.dealer_seat: int | None = None
        self.small_blind = Decimal("0")
        self.big_blind = Decimal("0")
        self.street = Street.IDLE
        self.board: list[str] = []
        self.hole_cards: list[str] = []
        self.pot = Decimal("0")
        self.seats: dict[int, dict[str, Any]] = {}
        self._hand_positions: dict[int, Position] = {}
        self.actions: list[ObservedAction] = []
        self.history_complete = False
        self.authority: Authority | None = None

    async def start(self) -> None:
        if not self.settings.openpoker_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="openpoker-runner")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.openpoker_enabled,
            "connected": self.ws is not None,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "table_id": self.table_id,
            "hand_id": self.hand_id,
            "street": self.street.value,
            "hero_turn_authority": self.authority is not None,
            "last_table_seq": self._last_table_seq,
            "history_complete": self.history_complete,
        }

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            await self.ws.close()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._decision_tasks:
            for task in self._decision_tasks:
                task.cancel()
            await asyncio.gather(*self._decision_tasks, return_exceptions=True)

    def _fail_pending_acks(self, reason: str) -> None:
        for future in self._pending_acks.values():
            if not future.done():
                future.set_exception(OpenPokerOutcomeUnknown(reason))

    def _reset_hand_state(self, *, hand_id: str | None = None) -> None:
        """Clear every hand-derived field before reducing a new hand or snapshot."""

        self.hand_id = hand_id
        self.dealer_seat = None
        self.small_blind = Decimal("0")
        self.big_blind = Decimal("0")
        self.street = Street.IDLE
        self.board = []
        self.hole_cards = []
        self.pot = Decimal("0")
        self.actions = []
        self._hand_positions = {}
        self.history_complete = False
        self.authority = None
        for seat in self.seats.values():
            seat["in_hand"] = False
            seat["folded"] = False
            seat["bet"] = 0

    def _reset_table_state(
        self,
        *,
        table_id: str | None = None,
        hero_seat: int | None = None,
        reason: str = "Open Poker table state reset",
    ) -> None:
        """Install an empty table scope; sequence watermarks never cross tables."""

        self._fail_pending_acks(reason)
        self._pending_acks.clear()
        self._current_client_action_id = None
        self.table_id = table_id
        self.hero_seat = hero_seat
        self.seats = {}
        self._last_table_seq = 0
        self._reset_hand_state()

    def _switch_table(self, table_id: str, hero_seat: int | None = None) -> None:
        if self.table_id != table_id:
            self._reset_table_state(
                table_id=table_id,
                hero_seat=hero_seat,
                reason="Open Poker changed tables before action acknowledgement",
            )
        elif hero_seat is not None:
            self.hero_seat = hero_seat

    @staticmethod
    def _state_hash_matches(message: dict[str, Any]) -> bool:
        expected = message.get("state_hash")
        if expected is None:
            return True
        if not isinstance(expected, str) or not expected.startswith("sha256:"):
            return False
        hashed = {
            key: value
            for key, value in message.items()
            if key not in {"ts", "table_seq", "hand_seq", "state_hash"}
        }
        return canonical_hash(hashed) == expected

    async def _request_resync(self) -> None:
        if self.ws is None or self.table_id is None:
            return
        await self._send(
            {
                "type": "resync_request",
                "table_id": self.table_id,
                "last_table_seq": self._last_table_seq,
            }
        )

    async def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                key = self.settings.openpoker_api_key
                if key is None:
                    raise RuntimeError("OPENPOKER_API_KEY is missing")
                async with websockets.connect(
                    self.settings.openpoker_ws_url,
                    additional_headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2**20,
                ) as ws:
                    self.ws = ws
                    backoff = 1
                    async for raw in ws:
                        if not isinstance(raw, str):
                            continue
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("Open Poker sent non-JSON text")
                            continue
                        await self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Open Poker connection failed; reconnecting")
            finally:
                self.ws = None
                self.authority = None
                self._fail_pending_acks(
                    "Open Poker disconnected before action acknowledgement"
                )
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        async with self._state_lock:
            await self._handle_message_locked(message)

    async def _handle_message_locked(
        self, message: dict[str, Any], *, replay: bool = False
    ) -> None:
        message_type = message.get("type")
        if replay and message_type not in REPLAYABLE_MESSAGE_TYPES:
            self.history_complete = False
            self.authority = None
            logger.warning("Non-replayable Open Poker message appeared in replay: %s", message_type)
            return
        if message_type == "resync_response":
            if replay:
                self.history_complete = False
                self.authority = None
                return
            await self._handle_resync_response(message)
            return

        if message_type == "table_joined":
            table_id = str(message["table_id"])
            hero_seat = int(message["seat"])
            self._switch_table(table_id, hero_seat)
        elif message_type == "table_state" and message.get("table_id") is not None:
            self._switch_table(str(message["table_id"]))
        elif message_type in TABLE_SCOPED_MESSAGE_TYPES and message.get("table_id") is not None:
            incoming_table = str(message["table_id"])
            if self.table_id is None:
                self._switch_table(incoming_table)
            elif incoming_table != self.table_id:
                logger.warning(
                    "Ignoring %s for stale Open Poker table %s (current %s)",
                    message_type,
                    incoming_table,
                    self.table_id,
                )
                return

        table_seq = message.get("table_seq")
        if isinstance(table_seq, bool):
            table_seq = None
        if table_seq is not None and not isinstance(table_seq, int):
            self.history_complete = False
            self.authority = None
            logger.warning("Ignoring Open Poker message with invalid table_seq: %s", message)
            if not replay:
                await self._request_resync()
            return
        if isinstance(table_seq, int) and table_seq <= self._last_table_seq:
            return
        if message_type in {
            "hole_cards",
            "your_turn",
            "player_action",
            "community_cards",
            "hand_result",
        } and message.get("hand_id") is not None:
            incoming_hand = str(message["hand_id"])
            if self.hand_id is None or incoming_hand != self.hand_id:
                self.history_complete = False
                self.authority = None
                logger.warning(
                    "Ignoring %s for stale Open Poker hand %s (current %s)",
                    message_type,
                    incoming_hand,
                    self.hand_id,
                )
                if not replay:
                    await self._request_resync()
                return
        if message_type == "table_state" and not self._state_hash_matches(message):
            self.history_complete = False
            self.authority = None
            logger.warning("Open Poker state_hash verification failed at table_seq=%s", table_seq)
            if not replay:
                await self._request_resync()
            return

        scope_before = self.table_id
        try:
            if message_type == "connected":
                self.agent_id = str(message.get("agent_id") or self.agent_id)
                self.agent_name = message.get("name")
                await self._resume_or_join()
            elif message_type == "lobby_joined":
                if self.settings.openpoker_auto_rebuy:
                    await self._send({"type": "set_auto_rebuy", "enabled": True})
            elif message_type == "table_joined":
                for player in message.get("players", []):
                    self._merge_seat(player)
            elif message_type == "player_joined":
                self._merge_seat(message)
            elif message_type == "player_left":
                seat = message.get("seat")
                if isinstance(seat, int):
                    if self.hand_id is None:
                        self.seats.pop(seat, None)
                    else:
                        current = self.seats.setdefault(seat, {"seat": seat})
                        current.update(status="empty", in_hand=False, bet=0)
            elif message_type == "hand_start":
                self._start_hand(message)
            elif message_type == "hole_cards":
                cards = message.get("cards")
                if not isinstance(cards, list):
                    raise ValueError("hole_cards.cards must be a list")
                self.hole_cards = list(cards)
            elif message_type == "player_action":
                self._record_action(message)
            elif message_type == "community_cards":
                self._record_board(message)
            elif message_type == "table_state":
                if replay:
                    self.history_complete = False
                    self.authority = None
                    return
                self._apply_table_state(message)
            elif message_type == "your_turn":
                if replay:
                    # Private action authority is intentionally excluded from replay.
                    self.history_complete = False
                    self.authority = None
                    return
                await self._handle_turn(message)
            elif message_type == "action_ack":
                client_id = message.get("client_action_id")
                future = self._pending_acks.get(str(client_id))
                if future is not None and not future.done():
                    future.set_result(message)
            elif message_type == "action_rejected":
                client_id = message.get("client_action_id") or self._current_client_action_id
                future = self._pending_acks.get(str(client_id)) if client_id else None
                if future is not None and not future.done():
                    future.set_exception(
                        OpenPokerExecutionError(
                            f"{message.get('code', 'action_rejected')}: "
                            f"{message.get('reason', '')}"
                        )
                    )
            elif message_type == "hand_result":
                self.street = Street.SHOWDOWN
                self.authority = None
            elif message_type == "table_closed":
                if replay:
                    self.history_complete = False
                    self.authority = None
                    return
                self._reset_table_state(reason="Open Poker table closed")
                await self._send(
                    {"type": "join_lobby", "buy_in": self.settings.openpoker_buy_in}
                )
            elif message_type == "season_ended":
                if replay:
                    self.history_complete = False
                    self.authority = None
                    return
                self._reset_table_state(reason="Open Poker season ended")
                await self._send(
                    {"type": "join_lobby", "buy_in": self.settings.openpoker_buy_in}
                )
            elif message_type == "error":
                await self._handle_error(message)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            self.history_complete = False
            self.authority = None
            logger.warning("Could not reduce Open Poker message: %s", message, exc_info=True)
            if not replay:
                await self._request_resync()
            return

        if isinstance(table_seq, int) and self.table_id == scope_before:
            self._last_table_seq = table_seq

    def _capture_reducer_state(self) -> dict[str, Any]:
        names = (
            "table_id",
            "hero_seat",
            "hand_id",
            "dealer_seat",
            "small_blind",
            "big_blind",
            "street",
            "board",
            "hole_cards",
            "pot",
            "seats",
            "_hand_positions",
            "actions",
            "history_complete",
            "authority",
            "_last_table_seq",
        )
        return {name: copy.deepcopy(getattr(self, name)) for name in names}

    def _restore_reducer_state(self, saved: dict[str, Any]) -> None:
        for name, value in saved.items():
            setattr(self, name, value)

    async def _handle_resync_response(self, message: dict[str, Any]) -> None:
        snapshot = message.get("snapshot")
        replayed = message.get("replayed_events")
        if not isinstance(snapshot, dict) or not isinstance(replayed, list):
            self.history_complete = False
            self.authority = None
            logger.warning("Malformed Open Poker resync_response")
            return

        response_table = message.get("table_id") or snapshot.get("table_id")
        if response_table is not None:
            response_table = str(response_table)
            if self.table_id is not None and response_table != self.table_id:
                logger.warning(
                    "Ignoring stale resync response for table %s (current %s)",
                    response_table,
                    self.table_id,
                )
                return
            self._switch_table(response_table)

        target_seq = message.get("to_table_seq", snapshot.get("table_seq"))
        if isinstance(target_seq, bool) or (
            target_seq is not None and not isinstance(target_seq, int)
        ):
            self.history_complete = False
            self.authority = None
            logger.warning("Malformed to_table_seq in Open Poker resync_response")
            return
        if isinstance(target_seq, int) and target_seq <= self._last_table_seq:
            return
        if not self._state_hash_matches(snapshot):
            self.history_complete = False
            self.authority = None
            logger.warning("Open Poker resync state_hash verification failed")
            return

        sequenced: list[tuple[int, dict[str, Any]]] = []
        seen: dict[int, str] = {}
        for replayed_message in replayed:
            if not isinstance(replayed_message, dict):
                self.history_complete = False
                self.authority = None
                logger.warning("Open Poker resync contains a non-object event")
                return
            sequence = replayed_message.get("table_seq")
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                self.history_complete = False
                self.authority = None
                logger.warning("Open Poker replay event is missing a valid table_seq")
                return
            fingerprint = canonical_hash(replayed_message)
            previous = seen.get(sequence)
            if previous is not None and previous != fingerprint:
                self.history_complete = False
                self.authority = None
                logger.warning("Conflicting Open Poker replay events at table_seq=%s", sequence)
                return
            seen[sequence] = fingerprint
            sequenced.append((sequence, replayed_message))

        sequenced.sort(key=lambda item: item[0])
        if isinstance(target_seq, int) and any(seq > target_seq for seq, _ in sequenced):
            self.history_complete = False
            self.authority = None
            logger.warning("Open Poker replay event exceeds to_table_seq")
            return

        saved = self._capture_reducer_state()
        try:
            self.authority = None
            for _, replayed_message in sequenced:
                await self._handle_message_locked(replayed_message, replay=True)

            self._apply_table_state(snapshot)
            highest_replay = max((seq for seq, _ in sequenced), default=self._last_table_seq)
            installed_seq = target_seq if isinstance(target_seq, int) else highest_replay
            self._last_table_seq = max(self._last_table_seq, installed_seq)

            final_snapshot = dict(snapshot)
            final_snapshot.setdefault("table_seq", self._last_table_seq)
            await self._emit_from_snapshot_if_turn(final_snapshot)
        except (KeyError, TypeError, ValueError, ArithmeticError, RuntimeError):
            self._restore_reducer_state(saved)
            self.history_complete = False
            self.authority = None
            logger.warning("Could not atomically reduce Open Poker resync", exc_info=True)

    async def _handle_error(self, message: dict[str, Any]) -> None:
        code = message.get("code")
        if code != "already_seated":
            logger.error("Open Poker error %s: %s", code, message.get("message"))
            return
        details = message.get("details") if isinstance(message.get("details"), dict) else {}
        table_id = message.get("table_id")
        if table_id is None:
            table_id = details.get("table_id")
        seat = message.get("seat")
        if seat is None:
            seat = details.get("seat")
        if table_id is not None:
            self._switch_table(str(table_id), int(seat) if seat is not None else None)
        elif seat is not None:
            self.hero_seat = int(seat)
        else:
            active = await self._discover_active_game()
            if active and active.get("playing") and active.get("table_id"):
                active_seat = active.get("seat")
                self._switch_table(
                    str(active["table_id"]),
                    int(active_seat) if active_seat is not None else None,
                )
            else:
                logger.error("already_seated did not identify a recoverable active game")
                return
        if self.table_id is not None:
            await self._request_resync()

    async def _discover_active_game(self) -> dict[str, Any] | None:
        key = self.settings.openpoker_api_key
        if key is None:
            return None
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.openpoker_rest_url.rstrip("/"),
                headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                timeout=10,
                trust_env=False,
            ) as client:
                response = await client.get("/me/active-game")
                response.raise_for_status()
                active = response.json()
            return active if isinstance(active, dict) else None
        except Exception:
            logger.warning("Could not query active Open Poker game", exc_info=True)
            return None

    async def _resume_or_join(self) -> None:
        active = await self._discover_active_game()
        if active and active.get("playing") and active.get("table_id"):
            active_seat = active.get("seat")
            self._switch_table(
                str(active["table_id"]),
                int(active_seat) if active_seat is not None else None,
            )
            await self._request_resync()
            return
        await self._send({"type": "join_lobby", "buy_in": self.settings.openpoker_buy_in})

    def _start_hand(self, message: dict[str, Any]) -> None:
        hand_id = str(message["hand_id"])
        if self.hand_id == hand_id and self.street != Street.IDLE:
            raise ValueError("duplicate logical hand_start with a new sequence")
        self._reset_hand_state(hand_id=hand_id)
        if message.get("seat") is not None:
            self.hero_seat = int(message["seat"])
        if self.hero_seat is None:
            raise ValueError("hand_start is missing Hero seat")
        self.dealer_seat = int(message["dealer_seat"])
        blinds = message.get("blinds") or {}
        self.small_blind = Decimal(str(blinds.get("small_blind", 0)))
        self.big_blind = Decimal(str(blinds.get("big_blind", 0)))
        if self.small_blind <= 0 or self.big_blind <= 0 or self.small_blind > self.big_blind:
            raise ValueError("hand_start contains invalid blinds")
        self.street = Street.PREFLOP
        self.pot = self.small_blind + self.big_blind
        self.history_complete = True
        for seat in self.seats.values():
            status = str(seat.get("status", "active"))
            seat["in_hand"] = status in {"active", "disconnected"}
            seat["folded"] = False
            seat["bet"] = 0
        participants = self._occupied_seats()
        self._hand_positions = position_map(self.dealer_seat, participants)
        if len(participants) < 2 or len(self._hand_positions) != len(participants):
            self.history_complete = False
        self._add_blind_actions()

    def _occupied_seats(self) -> list[int]:
        return [
            seat
            for seat, data in self.seats.items()
            if data.get("status", "active") != "empty" and data.get("in_hand", False)
        ]

    def _positions(self) -> dict[int, Position]:
        if self._hand_positions:
            return dict(self._hand_positions)
        if self.dealer_seat is None:
            return {}
        return position_map(self.dealer_seat, self._occupied_seats())

    def _add_blind_actions(self) -> None:
        positions = self._positions()
        inverse = {position: seat for seat, position in positions.items()}
        # Heads-up button posts the small blind even though its strategic label is BTN.
        sb = (
            self.dealer_seat
            if len(positions) == 2 and self.dealer_seat in positions
            else inverse.get(Position.SB)
        )
        bb = inverse.get(Position.BB)
        if sb is None or bb is None or self.small_blind <= 0 or self.big_blind <= 0:
            self.history_complete = False
            return
        self.seats[sb]["bet"] = self.small_blind
        self.seats[bb]["bet"] = self.big_blind
        self.actions.extend(
            [
                ObservedAction(
                    order=0,
                    street=Street.PREFLOP,
                    seat=sb,
                    position=positions[sb],
                    action=ObservedActionType.SMALL_BLIND,
                    contribution=self.small_blind,
                ),
                ObservedAction(
                    order=1,
                    street=Street.PREFLOP,
                    seat=bb,
                    position=Position.BB,
                    action=ObservedActionType.BIG_BLIND,
                    contribution=self.big_blind,
                ),
            ]
        )

    @staticmethod
    def _decimal_field(message: dict[str, Any], key: str) -> Decimal | None:
        value = message.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a non-negative amount")
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"{key} must be a finite non-negative amount")
        return parsed

    @staticmethod
    def _one_consistent_amount(label: str, values: list[Decimal | None]) -> Decimal | None:
        present = [value for value in values if value is not None]
        if not present:
            return None
        first = present[0]
        if any(value != first for value in present[1:]):
            raise ValueError(f"conflicting {label} fields")
        return first

    def _record_action(self, message: dict[str, Any]) -> None:
        seat = message.get("seat")
        if isinstance(seat, bool) or not isinstance(seat, int):
            raise ValueError("player_action.seat must be an integer")
        data = self.seats.get(seat)
        if data is None or not data.get("in_hand", False):
            raise ValueError("player_action refers to a seat outside the current hand")
        position = self._positions().get(seat)
        if position is None:
            raise ValueError("player_action has no frozen hand position")

        street = Street(str(message.get("street", self.street.value)))
        if street != self.street or street in {Street.IDLE, Street.SHOWDOWN}:
            raise ValueError("player_action street does not match reducer street")
        raw_action = ObservedActionType(str(message.get("action")))
        if raw_action in {ObservedActionType.SMALL_BLIND, ObservedActionType.BIG_BLIND}:
            raise ValueError("blind posts may only come from hand_start")

        prior_bet = self._decimal_field(data, "bet") or Decimal("0")
        current_bet = max(
            (
                self._decimal_field(item, "bet") or Decimal("0")
                for item in self.seats.values()
                if item.get("in_hand", False)
            ),
            default=Decimal("0"),
        )
        contribution_delta = self._decimal_field(message, "contribution_delta")
        stack_before = self._one_consistent_amount(
            "stack-before",
            [
                self._decimal_field(message, "stack_before"),
                self._decimal_field(message, "player_stack_before"),
            ],
        )
        stack_after = self._one_consistent_amount(
            "stack-after",
            [
                self._decimal_field(message, "stack_after"),
                self._decimal_field(message, "player_stack_after"),
            ],
        )
        if (stack_before is None) != (stack_after is None):
            raise ValueError("stack_before and stack_after must be supplied together")
        stack_delta = stack_before - stack_after if stack_before is not None else None
        if stack_delta is not None and stack_delta < 0:
            raise ValueError("player stack increased during an action")
        explicit_delta = self._one_consistent_amount(
            "contribution", [contribution_delta, stack_delta]
        )
        amount = self._decimal_field(message, "amount")
        bet_after = self._decimal_field(message, "bet_after")
        amount_mode = message.get("amount_mode")
        if amount_mode not in {None, "incremental", "to_total"}:
            raise ValueError("unsupported player_action.amount_mode")
        amount_delta: Decimal | None = None
        amount_total: Decimal | None = None
        if amount is not None:
            if amount_mode == "incremental":
                amount_delta = amount
            elif amount_mode == "to_total":
                amount_total = amount
            elif prior_bet == 0:
                # Both documented interpretations are identical from zero.
                amount_delta = amount
                amount_total = amount
            elif explicit_delta is not None:
                if amount == explicit_delta:
                    amount_delta = amount
                elif amount == prior_bet + explicit_delta:
                    amount_total = amount
                else:
                    raise ValueError("amount disagrees with explicit contribution fields")
            elif bet_after is not None and amount == bet_after:
                amount_total = amount
            else:
                raise ValueError(
                    "amount_mode is required when amount is ambiguous for a nonzero prior bet"
                )

        action = raw_action
        contribution: Decimal
        raise_to: Decimal | None = None
        base_action: Literal["call", "raise"] | None = None

        if raw_action in {ObservedActionType.FOLD, ObservedActionType.CHECK}:
            contribution = self._one_consistent_amount(
                "zero contribution", [explicit_delta, amount_delta, amount_total]
            ) or Decimal("0")
            if contribution != 0 or (bet_after is not None and bet_after != prior_bet):
                raise ValueError("fold/check cannot contribute chips")
        elif raw_action == ObservedActionType.CALL:
            from_total = amount_total - prior_bet if amount_total is not None else None
            if from_total is not None and from_total < 0:
                raise ValueError("call total is below the prior street bet")
            contribution = self._one_consistent_amount(
                "call contribution", [explicit_delta, amount_delta, from_total]
            )
            if contribution is None or contribution <= 0:
                raise ValueError("call requires an unambiguous positive contribution")
            expected_bet_after = prior_bet + contribution
            if bet_after is not None and bet_after != expected_bet_after:
                raise ValueError("call bet_after disagrees with its contribution")
        elif raw_action in {ObservedActionType.RAISE, ObservedActionType.BET}:
            raise_to = self._one_consistent_amount(
                "raise-to",
                [
                    bet_after,
                    amount_total,
                    prior_bet + amount_delta if amount_delta is not None else None,
                ],
            )
            if raise_to is None or raise_to <= prior_bet or raise_to <= current_bet:
                raise ValueError("aggression requires an unambiguous increasing raise-to")
            contribution = raise_to - prior_bet
            if explicit_delta is not None and explicit_delta != contribution:
                raise ValueError("raise-to disagrees with contribution fields")
            if street == Street.PREFLOP:
                if raw_action == ObservedActionType.BET:
                    raise ValueError("preflop aggression is a raise, never a bet")
                action = ObservedActionType.RAISE
            elif current_bet == 0:
                action = ObservedActionType.BET
            else:
                action = ObservedActionType.RAISE
        elif raw_action == ObservedActionType.ALL_IN:
            from_total = amount_total - prior_bet if amount_total is not None else None
            if from_total is not None and from_total < 0:
                raise ValueError("all-in total is below the prior street bet")
            contribution = self._one_consistent_amount(
                "all-in contribution", [explicit_delta, amount_delta, from_total]
            )
            if bet_after is not None:
                derived = bet_after - prior_bet
                if derived < 0:
                    raise ValueError("all-in bet_after is below the prior bet")
                contribution = self._one_consistent_amount(
                    "all-in contribution", [contribution, derived]
                )
            if contribution is None or contribution <= 0:
                raise ValueError("all-in requires an unambiguous positive contribution")
            inferred_to_call = max(current_bet - prior_bet, Decimal("0"))
            stated_to_call = self._decimal_field(message, "to_call_before")
            if stated_to_call is not None and stated_to_call != inferred_to_call:
                raise ValueError("to_call_before disagrees with retained street bets")
            to_call = stated_to_call if stated_to_call is not None else inferred_to_call
            base_action = "call" if contribution <= to_call else "raise"
            if base_action == "raise":
                raise_to = prior_bet + contribution
                if raise_to <= current_bet:
                    raise ValueError("all-in raise does not increase the current bet")
        else:  # pragma: no cover - enum exhaustiveness
            raise ValueError(f"unsupported player action: {raw_action}")

        pot_before = self._decimal_field(message, "pot_before")
        if pot_before is not None and pot_before != self.pot:
            raise ValueError("pot_before disagrees with retained pot")
        expected_pot = self.pot + contribution
        announced_pot = self._one_consistent_amount(
            "pot-after",
            [
                self._decimal_field(message, "pot_after"),
                self._decimal_field(message, "pot"),
            ],
        )
        if announced_pot is not None and announced_pot != expected_pot:
            raise ValueError("announced pot disagrees with action contribution")

        observed = ObservedAction(
            order=len(self.actions),
            street=street,
            seat=seat,
            position=position,
            action=action,
            contribution=contribution,
            raise_to=raise_to,
            all_in=raw_action == ObservedActionType.ALL_IN,
            base_action=base_action,
            node_label=message.get("node_label"),
        )
        self.actions.append(observed)
        self.pot = announced_pot if announced_pot is not None else expected_pot
        if action in {ObservedActionType.RAISE, ObservedActionType.BET} and raise_to is not None:
            data["bet"] = raise_to
        elif action in {ObservedActionType.CALL, ObservedActionType.ALL_IN}:
            data["bet"] = prior_bet + contribution
        final_stack = self._one_consistent_amount(
            "stack-after",
            [stack_after, self._decimal_field(message, "stack")],
        )
        if final_stack is not None:
            data["stack"] = final_stack
        if action == ObservedActionType.FOLD:
            data["folded"] = True

    def _record_board(self, message: dict[str, Any]) -> None:
        street = Street(str(message["street"]))
        expected_next = {
            Street.PREFLOP: Street.FLOP,
            Street.FLOP: Street.TURN,
            Street.TURN: Street.RIVER,
        }.get(self.street)
        if street != expected_next:
            raise ValueError("community_cards does not advance exactly one street")
        raw_cards = message.get("cards")
        if not isinstance(raw_cards, list):
            raise ValueError("community_cards.cards must be a list")
        cards = list(raw_cards)
        expected_count = 3 if street == Street.FLOP else 1
        if len(cards) != expected_count or len(cards) != len(set(cards)):
            raise ValueError("community_cards has the wrong number of unique cards")
        if any(card in self.board or card in self.hole_cards for card in cards):
            raise ValueError("community_cards duplicates a known card")
        if street == Street.FLOP:
            self.board = cards
        else:
            self.board.extend(cards)
        self.street = street
        for seat in self.seats.values():
            seat["bet"] = 0

    def _merge_seat(self, player: dict[str, Any]) -> None:
        seat = player.get("seat")
        if isinstance(seat, bool) or not isinstance(seat, int) or not 0 <= seat < 6:
            raise ValueError("Open Poker seat must be an integer from 0 through 5")
        current = self.seats.setdefault(seat, {"seat": seat})
        status = player.get("status", current.get("status", "active"))
        if status not in {"active", "away", "sitting_out", "disconnected", "empty"}:
            raise ValueError(f"unsupported Open Poker seat status: {status}")
        if "stack" in player:
            self._decimal_field(player, "stack")
        if "bet" in player:
            self._decimal_field(player, "bet")
        if "in_hand" in player and not isinstance(player["in_hand"], bool):
            raise ValueError("seat.in_hand must be boolean")
        if "folded" in player and not isinstance(player["folded"], bool):
            raise ValueError("seat.folded must be boolean")
        current.update(player)
        current["status"] = status
        if status == "empty":
            current.update(stack=0, bet=0, in_hand=False, folded=False)
        else:
            current.setdefault("stack", 0)
            current.setdefault("bet", 0)
            current.setdefault("in_hand", False)
            current.setdefault("folded", False)

    def _apply_table_state(self, message: dict[str, Any]) -> None:
        incoming_hand = message.get("hand_id")
        new_hand = incoming_hand is not None and str(incoming_hand) != self.hand_id
        if new_hand:
            self._reset_hand_state(hand_id=str(incoming_hand))
        elif message.get("street") == Street.IDLE.value and self.street != Street.IDLE:
            self._reset_hand_state()

        if message.get("dealer_seat") is not None:
            self.dealer_seat = int(message["dealer_seat"])
        if message.get("small_blind") is not None:
            self.small_blind = Decimal(str(message["small_blind"]))
        if message.get("big_blind") is not None:
            self.big_blind = Decimal(str(message["big_blind"]))
        incoming_street = self.street
        if message.get("street"):
            incoming_street = Street(str(message["street"]))
            if not new_hand and incoming_street != self.street:
                self.history_complete = False
            self.street = incoming_street
        if message.get("board") is not None:
            board = message["board"]
            if not isinstance(board, list):
                raise ValueError("table_state.board must be a list")
            if not new_hand and list(board) != self.board:
                self.history_complete = False
            self.board = list(board)
        if message.get("pot") is not None:
            pot = self._decimal_field(message, "pot")
            assert pot is not None
            if not new_hand and self.street != Street.IDLE and pot != self.pot:
                self.history_complete = False
            self.pot = pot
        if "seats" in message:
            raw_seats = message["seats"]
            if not isinstance(raw_seats, list):
                raise ValueError("table_state.seats must be a list")
            retained = self.seats
            installed: dict[int, dict[str, Any]] = {}
            seen_seats: set[int] = set()
            for raw in raw_seats:
                if not isinstance(raw, dict):
                    raise ValueError("table_state seat must be an object")
                seat_number = raw.get("seat")
                if isinstance(seat_number, bool) or not isinstance(seat_number, int):
                    raise ValueError("table_state seat is missing an integer index")
                if seat_number in seen_seats:
                    raise ValueError("table_state contains a duplicate seat")
                seen_seats.add(seat_number)
                previous = retained.get(seat_number, {})
                if not new_hand and previous.get("in_hand", False):
                    if "folded" in raw and bool(raw["folded"]) != bool(
                        previous.get("folded", False)
                    ):
                        self.history_complete = False
                    if "bet" in raw:
                        prior_bet = self._decimal_field(previous, "bet") or Decimal("0")
                        snapshot_bet = self._decimal_field(raw, "bet") or Decimal("0")
                        if prior_bet != snapshot_bet:
                            self.history_complete = False
                normalized = dict(raw)
                # Older snapshots can omit folded; retain it only within the same hand.
                if not new_hand and "folded" not in raw and "folded" in previous:
                    normalized["folded"] = previous["folded"]
                if (
                    self.hand_id is not None
                    and normalized.get("status", "active") != "empty"
                    and "in_hand" not in raw
                ):
                    self.history_complete = False
                temporary = self.seats
                try:
                    self.seats = installed
                    self._merge_seat(normalized)
                finally:
                    self.seats = temporary
            self.seats = installed
        hero = message.get("hero") or {}
        if not isinstance(hero, dict):
            raise ValueError("table_state.hero must be an object")
        if hero.get("seat") is not None:
            self.hero_seat = int(hero["seat"])
        if hero.get("hole_cards") is not None:
            if not isinstance(hero["hole_cards"], list):
                raise ValueError("hero.hole_cards must be a list")
            self.hole_cards = list(hero["hole_cards"])

        if self.hand_id is not None and self.dealer_seat is not None and not self._hand_positions:
            self._hand_positions = position_map(self.dealer_seat, self._occupied_seats())
        if new_hand:
            # A table snapshot is current state, never a substitute for action history.
            self.history_complete = False

    async def _emit_from_snapshot_if_turn(self, snapshot: dict[str, Any]) -> None:
        hero = snapshot.get("hero") or {}
        turn_token = hero.get("turn_token") or snapshot.get("turn_token")
        valid_actions = hero.get("valid_actions")
        actor_seat = snapshot.get("actor_seat")
        if (
            turn_token
            and isinstance(valid_actions, list)
            and valid_actions
            and isinstance(actor_seat, int)
            and actor_seat == self.hero_seat
        ):
            synthetic = {
                **snapshot,
                "type": "your_turn",
                "hand_id": snapshot.get("hand_id", self.hand_id),
                "community_cards": snapshot.get("board", self.board),
                "valid_actions": valid_actions,
                "turn_token": turn_token,
                "players": snapshot.get("seats", []),
            }
            await self._handle_turn(synthetic)

    async def _handle_turn(self, message: dict[str, Any]) -> None:
        if message.get("hand_id") is not None:
            incoming_hand = str(message["hand_id"])
            if self.hand_id is None or incoming_hand != self.hand_id:
                raise ValueError("your_turn arrived without the matching hand_start")
        if not isinstance(message.get("turn_token"), str) or not message["turn_token"]:
            raise ValueError("your_turn is missing turn_token")
        if not isinstance(message.get("valid_actions"), list) or not message["valid_actions"]:
            raise ValueError("your_turn is missing valid_actions")
        if message.get("community_cards") is not None:
            community_cards = message["community_cards"]
            if not isinstance(community_cards, list):
                raise ValueError("your_turn.community_cards must be a list")
            street = {0: Street.PREFLOP, 3: Street.FLOP, 4: Street.TURN, 5: Street.RIVER}.get(
                len(community_cards)
            )
            if street is None:
                raise ValueError("your_turn has an invalid community-card count")
            if list(community_cards) != self.board or street != self.street:
                self.history_complete = False
            self.board = list(community_cards)
            self.street = street
        if message.get("pot") is not None:
            pot = self._decimal_field(message, "pot")
            assert pot is not None
            if pot != self.pot:
                self.history_complete = False
            self.pot = pot
        players = message.get("players", [])
        if not isinstance(players, list):
            raise ValueError("your_turn.players must be a list")
        for player in players:
            if not isinstance(player, dict):
                raise ValueError("your_turn player must be an object")
            self._merge_seat(player)
        observation = self._make_observation(message)
        state_hash = canonical_hash(observation.model_dump(mode="json"))
        self.authority = Authority(
            hand_id=observation.hand_id,
            turn_token=str(message["turn_token"]),
            state_hash=state_hash,
            valid_actions=list(message.get("valid_actions") or []),
            observation=observation,
        )
        event = self._make_platform_event(message, observation, state_hash)
        task = asyncio.create_task(self.coordinator.process_platform(event))
        self._decision_tasks.add(task)
        task.add_done_callback(self._decision_tasks.discard)

    def _make_observation(self, message: dict[str, Any]) -> Observation:
        if self.table_id is None or self.hand_id is None or self.hero_seat is None:
            raise RuntimeError("Open Poker turn arrived before table/hand identity")
        if self.dealer_seat is None or self.big_blind <= 0:
            raise RuntimeError("Open Poker turn arrived before blinds/dealer state")
        positions = self._positions()
        hero_position = positions.get(self.hero_seat)
        if hero_position is None:
            self.history_complete = False
            raise RuntimeError("Hero is missing from the frozen hand position map")
        legal = [self._legal_action(item) for item in message.get("valid_actions", [])]
        legal_names = [item.action for item in legal]
        if len(legal_names) != len(set(legal_names)):
            raise ValueError("your_turn contains duplicate legal actions")
        seats = []
        for seat_number, raw in sorted(self.seats.items()):
            status = raw.get("status", "active")
            if status == "empty":
                continue
            seats.append(
                Seat(
                    seat=seat_number,
                    position=positions.get(seat_number),
                    name=raw.get("name"),
                    stack=Decimal(str(raw.get("stack") or 0)),
                    bet_to=Decimal(str(raw.get("bet") or 0)),
                    status=status,
                    in_hand=bool(raw.get("in_hand", False)),
                    folded=bool(raw.get("folded", False)),
                )
            )
        context = self._pokerai_context()
        token_hash = hashlib.sha256(str(message["turn_token"]).encode()).hexdigest()
        return Observation(
            hand_id=self.hand_id,
            street=self.street,
            dealer_seat=self.dealer_seat,
            acting_seat=self.hero_seat,
            blinds=Blinds(small=self.small_blind, big=self.big_blind),
            hero=Hero(seat=self.hero_seat, position=hero_position, cards=self.hole_cards),
            board=self.board,
            pot=self.pot,
            seats=seats,
            action_history=ActionHistory(complete=self.history_complete, actions=self.actions),
            legal_actions=legal,
            hero_turn=True,
            turn_id=f"openpoker:{token_hash}",
            pokerai=context,
        )

    def _pokerai_context(self) -> PokerAIContext:
        if self.street != Street.FLOP:
            return PokerAIContext()
        remaining = [
            seat
            for seat in self.seats.values()
            if seat.get("status", "active") != "empty"
            and seat.get("in_hand", False)
            and not seat.get("folded", False)
        ]
        if len(remaining) != 2:
            return PokerAIContext()
        preflop = [
            action
            for action in self.actions
            if action.street == Street.PREFLOP
            and action.action not in {ObservedActionType.SMALL_BLIND, ObservedActionType.BIG_BLIND}
        ]
        raises = [
            action
            for action in preflop
            if action.action in {ObservedActionType.RAISE, ObservedActionType.BET}
            or (action.action == ObservedActionType.ALL_IN and action.base_action == "raise")
        ]
        callers = [
            action
            for action in preflop
            if action.action == ObservedActionType.CALL
            or (action.action == ObservedActionType.ALL_IN and action.base_action == "call")
        ]
        hero_position = (
            self._positions().get(self.hero_seat) if self.hero_seat is not None else None
        )
        if hero_position is None:
            return PokerAIContext()
        roles: dict[str, Position] = {"hero": hero_position}
        pot_type = None
        if len(raises) == 1 and callers:
            pot_type = "SRP"
            roles.update(raiser=raises[0].position, caller=callers[-1].position)
        elif len(raises) == 2:
            pot_type = "3BET"
            roles.update(raiser=raises[0].position, three_bettor=raises[1].position)
        elif len(raises) == 3:
            pot_type = "4BET"
            roles.update(raiser=raises[0].position, three_bettor=raises[1].position)
        elif not raises and callers:
            pot_type = "LIMP"
            roles.update(limper=callers[0].position)
        node_id = self._flop_node_id()
        if pot_type is None or node_id is None:
            return PokerAIContext()
        return PokerAIContext(pot_type=pot_type, roles=roles, node_id=node_id)

    def _flop_node_id(self) -> str | None:
        labels = ["root"]
        for action in (item for item in self.actions if item.street == Street.FLOP):
            if action.node_label:
                labels.append(action.node_label)
            elif action.action == ObservedActionType.CHECK:
                labels.append("CHECK")
            elif action.action == ObservedActionType.CALL:
                labels.append("CALL")
            elif action.action == ObservedActionType.FOLD:
                labels.append("FOLD")
            elif action.action in {ObservedActionType.BET, ObservedActionType.RAISE}:
                if action.raise_to is None:
                    return None
                bb = action.raise_to / self.big_blind
                amount = int(bb) if bb == bb.to_integral_value() else float(bb)
                prefix = "BET" if action.action == ObservedActionType.BET else "RAISE"
                labels.append(f"{prefix}_{amount}")
            else:
                return None
        return "/".join(labels)

    @classmethod
    def _legal_action(cls, raw: dict[str, Any]) -> LegalAction:
        if not isinstance(raw, dict):
            raise ValueError("legal action must be an object")
        try:
            action = LegalActionType(raw["action"])
        except (KeyError, ValueError):
            raise ValueError("unsupported advertised legal action") from None
        if action == LegalActionType.RAISE:
            minimum = cls._decimal_field(raw, "min")
            maximum = cls._decimal_field(raw, "max")
            if minimum is None or maximum is None:
                raise ValueError("advertised raise is missing min/max")
            return LegalAction(action=action, min_to=minimum, max_to=maximum)
        if action == LegalActionType.CALL:
            amount = cls._decimal_field(raw, "amount")
            if amount is None:
                raise ValueError("advertised call is missing amount")
            return LegalAction(action=action, call_amount=amount)
        if action == LegalActionType.ALL_IN:
            amount = cls._decimal_field(raw, "amount")
            if amount is None:
                raise ValueError("advertised all-in is missing amount")
            return LegalAction(action=action, max_to=amount)
        return LegalAction(action=action)

    def _make_platform_event(
        self,
        message: dict[str, Any],
        observation: Observation,
        state_hash: str,
    ) -> VisionEvent:
        self._local_seq = max(self._local_seq + 1, int(message.get("table_seq") or 0))
        timestamp = message.get("ts")
        captured_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else utc_now()
        event_id = f"openpoker:{self.table_id}:{self._local_seq}:{observation.turn_id}"
        return VisionEvent(
            event_id=event_id,
            source=SourceRef(agent_id=self.agent_id, boot_id=self._boot_id, seq=self._local_seq),
            captured_at=captured_at,
            table=TableRef(table_id=self.table_id or "unknown"),
            frame=FrameRef(seq=self._local_seq, hash=state_hash),
            observation=observation,
            confidence=Confidence(
                overall=1,
                fields={
                    "hero.cards": 1,
                    "board": 1,
                    "pot": 1,
                    "hero_turn": 1,
                    "legal_actions": 1,
                },
            ),
            state_token=message.get("state_hash") or state_hash,
        )

    async def execute(
        self,
        decision: DecisionView,
        observation: Observation,
        source_kind: Literal["vision", "platform"],
    ) -> str:
        async with self._execution_lock:
            loop = asyncio.get_running_loop()
            async with self._state_lock:
                authority = self.authority
                if authority is None or self.ws is None:
                    raise OpenPokerExecutionError("no live Open Poker turn authority")
                if decision.source_kind != source_kind:
                    raise OpenPokerExecutionError(
                        "decision source_kind does not match executor input"
                    )
                if decision.table_id != self.table_id:
                    raise OpenPokerExecutionError("decision table_id is stale")
                if authority.hand_id != decision.hand_id:
                    raise OpenPokerExecutionError("decision hand_id is stale")
                if decision.state_hash != canonical_hash(observation.model_dump(mode="json")):
                    raise OpenPokerExecutionError(
                        "decision state_hash does not match its observation"
                    )
                if source_kind == "vision":
                    mismatch = self._reconcile(
                        observation,
                        authority.observation,
                        default_preflop_version=self.settings.preflop_version,
                        default_flop_version=self.settings.flop_version,
                    )
                    if mismatch:
                        raise OpenPokerExecutionError(
                            f"vision/platform mismatch: {mismatch}"
                        )
                elif decision.state_hash != authority.state_hash:
                    raise OpenPokerExecutionError(
                        "authoritative platform state changed after solving"
                    )
                if (
                    decision.selected is None
                    or not decision.selected.valid
                    or not decision.selected.platform_action
                ):
                    raise OpenPokerExecutionError("decision has no valid selected action")

                action_name = decision.selected.platform_action.value
                advertised = next(
                    (
                        item
                        for item in authority.valid_actions
                        if item.get("action") == action_name
                    ),
                    None,
                )
                if advertised is None:
                    raise OpenPokerExecutionError(f"{action_name} is no longer legal")
                payload: dict[str, Any] = {
                    "type": "action",
                    "hand_id": authority.hand_id,
                    "action": action_name,
                    "client_action_id": decision.decision_id,
                    "turn_token": authority.turn_token,
                }
                if action_name == "raise":
                    if decision.selected.amount_bb is None:
                        raise OpenPokerExecutionError("raise has no amount_bb")
                    native = (
                        decision.selected.amount_bb * authority.observation.blinds.big
                    )
                    if native != native.to_integral_value():
                        raise OpenPokerExecutionError(
                            "raise-to does not map to integer Open Poker chips"
                        )
                    amount = int(native)
                    minimum = int(advertised["min"])
                    maximum = int(advertised["max"])
                    if not minimum <= amount <= maximum:
                        raise OpenPokerExecutionError(
                            f"raise-to {amount} is outside authoritative "
                            f"[{minimum}, {maximum}]"
                        )
                    payload["amount"] = amount

                future: asyncio.Future[dict[str, Any]] = loop.create_future()
                self._pending_acks[decision.decision_id] = future
                self._current_client_action_id = decision.decision_id

            try:
                await self._send(payload)
                acknowledgement = await asyncio.wait_for(future, timeout=10)
                if acknowledgement.get("status") != "accepted":
                    raise OpenPokerExecutionError(
                        "Open Poker returned non-accepted action_ack status"
                    )
                async with self._state_lock:
                    if self.authority is authority:
                        self.authority = None
                return "confirmed"
            except asyncio.TimeoutError:
                async with self._state_lock:
                    if self.authority is authority:
                        self.authority = None
                try:
                    await self._request_resync()
                except Exception:
                    logger.warning(
                        "Could not request Open Poker resync after action ACK timeout",
                        exc_info=True,
                    )
                return "unknown_waiting_for_resync"
            except OpenPokerOutcomeUnknown:
                async with self._state_lock:
                    if self.authority is authority:
                        self.authority = None
                return "unknown_waiting_for_resync"
            except Exception:
                async with self._state_lock:
                    if self.authority is authority:
                        self.authority = None
                raise
            finally:
                async with self._state_lock:
                    self._pending_acks.pop(decision.decision_id, None)
                    if self._current_client_action_id == decision.decision_id:
                        self._current_client_action_id = None

    @staticmethod
    def _reconcile(
        vision: Observation,
        platform: Observation,
        *,
        default_preflop_version: str = "6max",
        default_flop_version: str = "6max",
    ) -> str | None:
        tolerance_bb = Decimal("0.05")

        def same_amount(
            vision_amount: Decimal,
            platform_amount: Decimal,
        ) -> bool:
            return (
                abs(
                    vision_amount / vision.blinds.big
                    - platform_amount / platform.blinds.big
                )
                <= tolerance_bb
            )

        def same_optional_amount(
            vision_amount: Decimal | None,
            platform_amount: Decimal | None,
        ) -> bool:
            if vision_amount is None or platform_amount is None:
                return vision_amount is None and platform_amount is None
            return same_amount(vision_amount, platform_amount)

        if vision.hand_id != platform.hand_id:
            return "hand_id"
        if vision.street != platform.street:
            return "street"
        if (
            vision.hero.seat != platform.hero.seat
            or vision.hero.position != platform.hero.position
        ):
            return "hero position"
        if sorted(vision.hero.cards) != sorted(platform.hero.cards):
            return "hero cards"
        if vision.board != platform.board:
            return "board"
        if not vision.hero_turn or not platform.hero_turn:
            return "Hero turn"
        if (
            vision.dealer_seat != platform.dealer_seat
            or vision.acting_seat != platform.acting_seat
        ):
            return "seat state"
        if not same_amount(vision.blinds.small, platform.blinds.small) or not same_amount(
            vision.blinds.ante, platform.blinds.ante
        ):
            return "blinds"
        if not same_amount(vision.pot, platform.pot):
            return "pot"

        vision_actions = vision.action_history.actions
        platform_actions = platform.action_history.actions
        if (
            not vision.action_history.complete
            or not platform.action_history.complete
            or len(vision_actions) != len(platform_actions)
        ):
            return "action history"
        for vision_action, platform_action in zip(
            vision_actions, platform_actions, strict=True
        ):
            if (
                vision_action.order != platform_action.order
                or vision_action.street != platform_action.street
                or vision_action.seat != platform_action.seat
                or vision_action.position != platform_action.position
                or vision_action.action != platform_action.action
                or vision_action.all_in != platform_action.all_in
                or vision_action.base_action != platform_action.base_action
                or not same_amount(
                    vision_action.contribution, platform_action.contribution
                )
                or not same_optional_amount(
                    vision_action.raise_to, platform_action.raise_to
                )
            ):
                return "action history"

        vision_seats = {
            seat.seat: seat for seat in vision.seats if seat.status != "empty"
        }
        platform_seats = {
            seat.seat: seat for seat in platform.seats if seat.status != "empty"
        }
        if vision_seats.keys() != platform_seats.keys():
            return "seat state"
        for seat_number, vision_seat in vision_seats.items():
            platform_seat = platform_seats[seat_number]
            if (
                vision_seat.position != platform_seat.position
                or vision_seat.status != platform_seat.status
                or vision_seat.in_hand != platform_seat.in_hand
                or vision_seat.folded != platform_seat.folded
                or not same_amount(vision_seat.stack, platform_seat.stack)
                or not same_amount(vision_seat.bet_to, platform_seat.bet_to)
            ):
                return "seat state"

        vision_legal = sorted(vision.legal_actions, key=lambda item: item.action.value)
        platform_legal = sorted(platform.legal_actions, key=lambda item: item.action.value)
        if len(vision_legal) != len(platform_legal):
            return "legal actions"
        for vision_action, platform_action in zip(
            vision_legal, platform_legal, strict=True
        ):
            if (
                vision_action.action != platform_action.action
                or not same_optional_amount(
                    vision_action.call_amount, platform_action.call_amount
                )
                or not same_optional_amount(vision_action.min_to, platform_action.min_to)
                or not same_optional_amount(vision_action.max_to, platform_action.max_to)
            ):
                return "legal actions"

        def pokerai_context(observation: Observation) -> tuple[Any, ...]:
            context = observation.pokerai
            return (
                context.preflop_version or default_preflop_version,
                context.flop_version or default_flop_version,
                context.pot_type,
                tuple(
                    sorted(
                        (name, position.value)
                        for name, position in context.roles.items()
                    )
                ),
                context.node_id,
            )

        if pokerai_context(vision) != pokerai_context(platform):
            return "PokerAI context"
        return None

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            raise OpenPokerExecutionError("Open Poker WebSocket is disconnected")
        async with self._send_lock:
            await self.ws.send(json.dumps(payload, separators=(",", ":")))
