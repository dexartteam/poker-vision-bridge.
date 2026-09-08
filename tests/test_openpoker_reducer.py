from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Settings
from app.coordinator import canonical_hash
from app.models import ObservedAction, ObservedActionType, Position, Street
from app.openpoker import OpenPokerRunner


class RecordingCoordinator:
    def __init__(self) -> None:
        self.events = []

    async def process_platform(self, event):
        self.events.append(event)


def make_runner() -> OpenPokerRunner:
    return OpenPokerRunner(Settings(), RecordingCoordinator())


def players(*, hero_seat: int = 0) -> list[dict]:
    return [
        {
            "seat": seat,
            "name": "hero" if seat == hero_seat else f"bot-{seat}",
            "stack": 2000,
            "status": "active",
        }
        for seat in range(6)
    ]


async def start_full_hand(runner: OpenPokerRunner, *, hero_seat: int = 0) -> None:
    await runner._handle_message(
        {
            "type": "table_joined",
            "table_id": "table-a",
            "seat": hero_seat,
            "players": players(hero_seat=hero_seat),
        }
    )
    await runner._handle_message(
        {
            "type": "hand_start",
            "table_id": "table-a",
            "table_seq": 1,
            "hand_id": "hand-a",
            "seat": hero_seat,
            "dealer_seat": 0,
            "blinds": {"small_blind": 10, "big_blind": 20},
        }
    )


@pytest.mark.asyncio
async def test_hand_reset_is_deterministic_blinds_are_bets_and_positions_do_not_shift():
    runner = make_runner()
    joined = players()
    joined[4]["status"] = "away"
    joined[5].update(status="empty", name=None, stack=0)
    await runner._handle_message(
        {
            "type": "table_joined",
            "table_id": "table-a",
            "seat": 0,
            "players": joined,
        }
    )
    await runner._handle_message(
        {
            "type": "hand_start",
            "table_id": "table-a",
            "table_seq": 1,
            "hand_id": "hand-a",
            "seat": 0,
            "dealer_seat": 0,
            "blinds": {"small_blind": 10, "big_blind": 20},
        }
    )

    assert runner.hero_seat == 0
    assert runner.seats[4]["in_hand"] is False
    assert runner.seats[5]["in_hand"] is False
    assert runner.seats[1]["bet"] == Decimal("10")
    assert runner.seats[2]["bet"] == Decimal("20")
    frozen = runner._positions()
    assert frozen[3] == Position.CO

    await runner._handle_message(
        {
            "type": "player_action",
            "table_id": "table-a",
            "table_seq": 2,
            "hand_id": "hand-a",
            "seat": 3,
            "action": "fold",
            "amount": None,
            "street": "preflop",
            "pot": 30,
        }
    )
    assert runner._positions() == frozen
    assert runner.actions[-1].position == Position.CO


@pytest.mark.asyncio
async def test_table_change_uses_full_reset_and_preserves_explicit_zero_seat():
    runner = make_runner()
    await start_full_hand(runner)
    runner.board = ["2c", "3d", "4h"]
    runner.hole_cards = ["As", "Kh"]
    runner.authority = object()  # type: ignore[assignment]

    await runner._handle_message(
        {
            "type": "table_joined",
            "table_id": "table-b",
            "seat": 0,
            "players": [{"seat": 0, "name": "hero", "stack": 2000}],
        }
    )

    assert runner.table_id == "table-b"
    assert runner.hero_seat == 0
    assert runner.hand_id is None
    assert runner.street == Street.IDLE
    assert runner.board == []
    assert runner.hole_cards == []
    assert runner.actions == []
    assert runner.authority is None
    assert runner._last_table_seq == 0
    assert set(runner.seats) == {0}


@pytest.mark.asyncio
async def test_first_postflop_raise_is_normalized_to_bet_then_raise():
    runner = make_runner()
    await start_full_hand(runner)
    runner.hole_cards = ["As", "Kh"]
    await runner._handle_message(
        {
            "type": "community_cards",
            "table_id": "table-a",
            "table_seq": 2,
            "hand_id": "hand-a",
            "street": "flop",
            "cards": ["2c", "3d", "4h"],
        }
    )
    await runner._handle_message(
        {
            "type": "player_action",
            "table_id": "table-a",
            "table_seq": 3,
            "hand_id": "hand-a",
            "seat": 1,
            "street": "flop",
            "action": "raise",
            "amount": 40,
            "amount_mode": "to_total",
            "bet_after": 40,
            "pot_before": 30,
            "pot_after": 70,
        }
    )
    await runner._handle_message(
        {
            "type": "player_action",
            "table_id": "table-a",
            "table_seq": 4,
            "hand_id": "hand-a",
            "seat": 2,
            "street": "flop",
            "action": "raise",
            "amount": 120,
            "amount_mode": "to_total",
            "bet_after": 120,
            "pot_before": 70,
            "pot_after": 190,
        }
    )

    assert runner.actions[-2].action == ObservedActionType.BET
    assert runner.actions[-2].contribution == Decimal("40")
    assert runner.actions[-1].action == ObservedActionType.RAISE
    assert runner.actions[-1].contribution == Decimal("120")
    assert runner._flop_node_id() == "root/BET_2/RAISE_6"


@pytest.mark.asyncio
async def test_flop_context_keeps_hero_role_when_hero_seat_is_zero():
    runner = make_runner()
    await start_full_hand(runner, hero_seat=0)
    for seat, state in runner.seats.items():
        state["folded"] = seat not in {0, 3}
    runner.actions.extend(
        [
            ObservedAction(
                order=2,
                street=Street.PREFLOP,
                seat=3,
                position=Position.UTG,
                action=ObservedActionType.RAISE,
                contribution=Decimal("60"),
                raise_to=Decimal("60"),
            ),
            ObservedAction(
                order=3,
                street=Street.PREFLOP,
                seat=0,
                position=Position.BTN,
                action=ObservedActionType.CALL,
                contribution=Decimal("60"),
            ),
        ]
    )
    runner.street = Street.FLOP
    runner.board = ["2c", "3d", "4h"]

    context = runner._pokerai_context()

    assert context.pot_type == "SRP"
    assert context.roles["hero"] == Position.BTN


@pytest.mark.asyncio
async def test_ambiguous_nonzero_prior_bet_fails_closed_without_advancing_watermark():
    runner = make_runner()
    await start_full_hand(runner)

    await runner._handle_message(
        {
            "type": "player_action",
            "table_id": "table-a",
            "table_seq": 2,
            "hand_id": "hand-a",
            "seat": 1,
            "street": "preflop",
            "action": "raise",
            "amount": 60,
        }
    )

    assert runner.history_complete is False
    assert len(runner.actions) == 2
    assert runner._last_table_seq == 1


@pytest.mark.asyncio
async def test_incremental_amount_mode_converts_blind_raise_to_total_and_checks_aliases():
    runner = make_runner()
    await start_full_hand(runner)

    await runner._handle_message(
        {
            "type": "player_action",
            "table_id": "table-a",
            "table_seq": 2,
            "hand_id": "hand-a",
            "seat": 1,
            "street": "preflop",
            "action": "raise",
            "amount": 50,
            "amount_mode": "incremental",
            "contribution_delta": 50,
            "stack_before": 1990,
            "player_stack_before": 1990,
            "stack_after": 1940,
            "player_stack_after": 1940,
            "bet_after": 60,
            "pot_before": 30,
            "pot_after": 80,
        }
    )

    action = runner.actions[-1]
    assert action.action == ObservedActionType.RAISE
    assert action.contribution == Decimal("50")
    assert action.raise_to == Decimal("60")
    assert runner.seats[1]["bet"] == Decimal("60")
    assert runner.history_complete is True


@pytest.mark.asyncio
async def test_duplicate_and_regressive_table_sequences_are_not_reduced_twice():
    runner = make_runner()
    await start_full_hand(runner)
    fold = {
        "type": "player_action",
        "table_id": "table-a",
        "table_seq": 2,
        "hand_id": "hand-a",
        "seat": 3,
        "street": "preflop",
        "action": "fold",
        "amount": None,
        "pot": 30,
    }
    await runner._handle_message(fold)
    await runner._handle_message(dict(fold))
    await runner._handle_message({**fold, "table_seq": 1, "seat": 4})

    assert len(runner.actions) == 3
    assert runner._last_table_seq == 2


@pytest.mark.asyncio
async def test_resync_replays_in_order_deduplicates_then_installs_snapshot():
    runner = make_runner()
    await runner._handle_message(
        {
            "type": "table_joined",
            "table_id": "table-a",
            "seat": 0,
            "players": players(),
        }
    )
    hand_start = {
        "type": "hand_start",
        "table_id": "table-a",
        "table_seq": 1,
        "hand_id": "hand-a",
        "seat": 0,
        "dealer_seat": 0,
        "blinds": {"small_blind": 10, "big_blind": 20},
    }
    fold = {
        "type": "player_action",
        "table_id": "table-a",
        "table_seq": 2,
        "hand_id": "hand-a",
        "seat": 3,
        "street": "preflop",
        "action": "fold",
        "amount": None,
        "pot": 30,
    }
    snapshot_seats = []
    for item in players():
        seat = item["seat"]
        snapshot_seats.append(
            {
                **item,
                "bet": 10 if seat == 1 else 20 if seat == 2 else 0,
                "in_hand": True,
                "folded": seat == 3,
            }
        )
    snapshot = {
        "type": "table_state",
        "table_id": "table-a",
        "table_seq": 5,
        "hand_id": "hand-a",
        "street": "preflop",
        "dealer_seat": 0,
        "small_blind": 10,
        "big_blind": 20,
        "pot": 30,
        "actor_seat": 2,
        "board": [],
        "seats": snapshot_seats,
        "hero": {"seat": 0, "hole_cards": ["As", "Kh"]},
    }
    snapshot["state_hash"] = canonical_hash(
        {
            key: value
            for key, value in snapshot.items()
            if key not in {"ts", "table_seq", "hand_seq", "state_hash"}
        }
    )

    await runner._handle_message(
        {
            "type": "resync_response",
            "table_id": "table-a",
            "from_table_seq": 1,
            "to_table_seq": 5,
            "replayed_events": [fold, hand_start, dict(fold)],
            "snapshot": snapshot,
        }
    )

    assert runner._last_table_seq == 5
    assert runner.history_complete is True
    assert [action.action for action in runner.actions] == [
        ObservedActionType.SMALL_BLIND,
        ObservedActionType.BIG_BLIND,
        ObservedActionType.FOLD,
    ]
    assert runner.seats[3]["folded"] is True
    assert runner.hole_cards == ["As", "Kh"]


@pytest.mark.asyncio
async def test_cold_snapshot_without_replayed_hand_start_is_never_complete():
    runner = make_runner()
    await runner._handle_message(
        {
            "type": "table_joined",
            "table_id": "table-a",
            "seat": 0,
            "players": players(),
        }
    )
    snapshot = {
        "type": "table_state",
        "table_id": "table-a",
        "table_seq": 9,
        "hand_id": "hand-a",
        "street": "flop",
        "dealer_seat": 0,
        "small_blind": 10,
        "big_blind": 20,
        "pot": 120,
        "board": ["2c", "3d", "4h"],
        "seats": [
            {**item, "bet": 0, "in_hand": True, "folded": False}
            for item in players()
        ],
        "hero": {"seat": 0, "hole_cards": ["As", "Kh"]},
    }
    await runner._handle_message(
        {
            "type": "resync_response",
            "table_id": "table-a",
            "to_table_seq": 9,
            "replayed_events": [],
            "snapshot": snapshot,
        }
    )

    assert runner.hand_id == "hand-a"
    assert runner.street == Street.FLOP
    assert runner.history_complete is False
    assert runner.authority is None


@pytest.mark.asyncio
async def test_snapshot_replaces_seats_and_observation_filters_empty_seats():
    runner = make_runner()
    await start_full_hand(runner)
    runner.hole_cards = ["As", "Kh"]
    snapshot = {
        "type": "table_state",
        "table_id": "table-a",
        "table_seq": 2,
        "hand_id": "hand-a",
        "street": "preflop",
        "dealer_seat": 0,
        "small_blind": 10,
        "big_blind": 20,
        "pot": 30,
        "board": [],
        "seats": [
            {
                "seat": 0,
                "name": "hero",
                "stack": 1990,
                "bet": 0,
                "status": "active",
                "in_hand": True,
                "folded": False,
            },
            {
                "seat": 1,
                "name": "bot-1",
                "stack": 1980,
                "bet": 10,
                "status": "active",
                "in_hand": True,
                "folded": False,
            },
            {
                "seat": 2,
                "name": "bot-2",
                "stack": 1980,
                "bet": 20,
                "status": "active",
                "in_hand": True,
                "folded": False,
            },
            {"seat": 5, "name": None, "stack": 0, "bet": 0, "status": "empty"},
        ],
        "hero": {"seat": 0, "hole_cards": ["As", "Kh"]},
    }
    runner._apply_table_state(snapshot)
    observation = runner._make_observation(
        {
            "turn_token": "token",
            "valid_actions": [{"action": "check"}, {"action": "raise", "min": 40, "max": 1990}],
        }
    )

    assert set(runner.seats) == {0, 1, 2, 5}
    assert [seat.seat for seat in observation.seats] == [0, 1, 2]
    assert observation.hero.seat == 0
    assert observation.hero.position == Position.BTN


def test_malformed_advertised_action_fails_closed():
    with pytest.raises(ValueError, match="unsupported advertised"):
        OpenPokerRunner._legal_action({"action": "mystery"})
    with pytest.raises(ValueError, match="missing min/max"):
        OpenPokerRunner._legal_action({"action": "raise", "min": 40})
