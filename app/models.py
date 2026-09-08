from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CARD_RE = re.compile(r"^(?:[2-9TJQKA][hdcs])$")
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Street(StrEnum):
    IDLE = "idle"
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


class Position(StrEnum):
    SB = "SB"
    BB = "BB"
    UTG = "UTG"
    MP = "MP"
    CO = "CO"
    BTN = "BTN"


class ObservedActionType(StrEnum):
    SMALL_BLIND = "small_blind"
    BIG_BLIND = "big_blind"
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"
    RAISE = "raise"
    ALL_IN = "all_in"


class LegalActionType(StrEnum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    RAISE = "raise"
    ALL_IN = "all_in"


class SourceRef(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    boot_id: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=0)


class TableRef(StrictModel):
    platform: Literal["openpoker"] = "openpoker"
    table_id: str = Field(min_length=1, max_length=256)
    mode: Literal["bot_only"] = "bot_only"


class FrameRef(StrictModel):
    seq: int = Field(ge=0)
    hash: str = Field(min_length=8, max_length=256)


class Confidence(StrictModel):
    overall: float = Field(ge=0, le=1)
    fields: dict[str, float] = Field(default_factory=dict)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, float]) -> dict[str, float]:
        for key, score in value.items():
            if not 0 <= score <= 1:
                raise ValueError(f"confidence for {key!r} must be between 0 and 1")
        return value


class Blinds(StrictModel):
    small: PositiveDecimal
    big: PositiveDecimal
    ante: NonNegativeDecimal = Decimal("0")

    @model_validator(mode="after")
    def small_not_above_big(self) -> "Blinds":
        if self.small > self.big:
            raise ValueError("small blind cannot exceed big blind")
        return self


class Hero(StrictModel):
    seat: int = Field(ge=0, le=20)
    position: Position
    cards: list[str] = Field(min_length=0, max_length=2)

    @field_validator("cards")
    @classmethod
    def validate_cards(cls, cards: list[str]) -> list[str]:
        for card in cards:
            if not CARD_RE.fullmatch(card):
                raise ValueError(f"invalid card: {card}")
        if len(cards) != len(set(cards)):
            raise ValueError("duplicate hero cards")
        return cards


class Seat(StrictModel):
    seat: int = Field(ge=0, le=20)
    position: Position | None = None
    name: str | None = Field(default=None, max_length=128)
    stack: NonNegativeDecimal
    bet_to: NonNegativeDecimal = Decimal("0")
    status: Literal["active", "away", "sitting_out", "disconnected", "empty"] = "active"
    in_hand: bool = True
    folded: bool = False


class ObservedAction(StrictModel):
    order: int = Field(ge=0)
    street: Street
    seat: int = Field(ge=0, le=20)
    position: Position
    action: ObservedActionType
    contribution: NonNegativeDecimal = Decimal("0")
    raise_to: NonNegativeDecimal | None = None
    all_in: bool = False
    base_action: Literal["call", "raise"] | None = None
    node_label: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_amounts(self) -> "ObservedAction":
        if self.action in {
            ObservedActionType.SMALL_BLIND,
            ObservedActionType.BIG_BLIND,
            ObservedActionType.CALL,
            ObservedActionType.BET,
            ObservedActionType.RAISE,
        } and self.contribution <= 0:
            raise ValueError(f"{self.action} requires a positive incremental contribution")
        if self.action in {ObservedActionType.BET, ObservedActionType.RAISE} and self.raise_to is None:
            raise ValueError(f"{self.action} requires raise_to")
        if self.action == ObservedActionType.ALL_IN and self.base_action is None:
            raise ValueError("all_in requires base_action=call|raise")
        return self


class ActionHistory(StrictModel):
    complete: bool
    actions: list[ObservedAction] = Field(default_factory=list)

    @field_validator("actions")
    @classmethod
    def ordered_and_unique(cls, actions: list[ObservedAction]) -> list[ObservedAction]:
        orders = [item.order for item in actions]
        if orders != list(range(len(actions))):
            raise ValueError("action_history.actions order must be contiguous from zero")
        street_rank = {
            Street.PREFLOP: 0,
            Street.FLOP: 1,
            Street.TURN: 2,
            Street.RIVER: 3,
            Street.SHOWDOWN: 4,
            Street.IDLE: -1,
        }
        ranks = [street_rank[item.street] for item in actions]
        if ranks != sorted(ranks):
            raise ValueError("action_history streets cannot move backwards")
        return actions


class LegalAction(StrictModel):
    action: LegalActionType
    call_amount: NonNegativeDecimal | None = None
    min_to: NonNegativeDecimal | None = None
    max_to: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "LegalAction":
        if self.action == LegalActionType.RAISE:
            if self.min_to is None or self.max_to is None:
                raise ValueError("raise requires min_to and max_to")
            if self.min_to > self.max_to:
                raise ValueError("raise min_to cannot exceed max_to")
            if self.call_amount is not None:
                raise ValueError("raise cannot include call_amount")
        elif self.action == LegalActionType.CALL:
            if self.call_amount is None:
                raise ValueError("call requires call_amount")
            if self.min_to is not None or self.max_to is not None:
                raise ValueError("call cannot include min_to or max_to")
        elif self.action == LegalActionType.ALL_IN:
            if self.max_to is None:
                raise ValueError("all_in requires max_to")
            if self.call_amount is not None or self.min_to is not None:
                raise ValueError("all_in cannot include call_amount or min_to")
        elif any(value is not None for value in (self.call_amount, self.min_to, self.max_to)):
            raise ValueError(f"{self.action} cannot include amount fields")
        return self


class PokerAIContext(StrictModel):
    preflop_version: str | None = None
    flop_version: str | None = None
    pot_type: Literal["SRP", "3BET", "4BET", "LIMP"] | None = None
    roles: dict[str, Position] = Field(default_factory=dict)
    node_id: str | None = Field(default=None, max_length=512)


class Observation(StrictModel):
    hand_id: str = Field(min_length=1, max_length=256)
    street: Street
    dealer_seat: int = Field(ge=0, le=20)
    acting_seat: int | None = Field(default=None, ge=0, le=20)
    blinds: Blinds
    hero: Hero
    board: list[str] = Field(default_factory=list, max_length=5)
    pot: NonNegativeDecimal
    seats: list[Seat] = Field(min_length=2, max_length=10)
    action_history: ActionHistory
    legal_actions: list[LegalAction] = Field(default_factory=list)
    hero_turn: bool
    turn_id: str | None = Field(default=None, max_length=256)
    pokerai: PokerAIContext = Field(default_factory=PokerAIContext)

    @model_validator(mode="after")
    def validate_state(self) -> "Observation":
        expected_board = {
            Street.IDLE: {0},
            Street.PREFLOP: {0},
            Street.FLOP: {3},
            Street.TURN: {4},
            Street.RIVER: {5},
            Street.SHOWDOWN: {0, 3, 4, 5},
        }[self.street]
        if len(self.board) not in expected_board:
            raise ValueError(f"street {self.street} cannot have {len(self.board)} board cards")
        cards = self.hero.cards + self.board
        for card in cards:
            if not CARD_RE.fullmatch(card):
                raise ValueError(f"invalid card: {card}")
        if len(cards) != len(set(cards)):
            raise ValueError("duplicate card across hero cards and board")
        seat_ids = [seat.seat for seat in self.seats]
        if len(seat_ids) != len(set(seat_ids)):
            raise ValueError("duplicate seat")
        seats_by_id = {seat.seat: seat for seat in self.seats}
        if self.hero.seat not in seats_by_id:
            raise ValueError("hero seat is missing from seats")
        if self.dealer_seat not in seats_by_id:
            raise ValueError("dealer seat is missing from seats")
        if self.acting_seat is not None and self.acting_seat not in seats_by_id:
            raise ValueError("acting seat is missing from seats")

        positioned_seats = [
            seat
            for seat in self.seats
            if seat.status != "empty" and seat.in_hand
        ]
        if any(seat.position is None for seat in positioned_seats):
            raise ValueError("every non-empty in-hand seat requires a position")
        positions = [seat.position for seat in positioned_seats]
        if len(positions) != len(set(positions)):
            raise ValueError("duplicate position among non-empty in-hand seats")

        for action in self.action_history.actions:
            action_seat = seats_by_id.get(action.seat)
            if action_seat is None:
                raise ValueError(f"action seat {action.seat} is missing from seats")
            if action_seat.position != action.position:
                raise ValueError(
                    f"action position {action.position} does not match seat "
                    f"{action.seat} position {action_seat.position}"
                )

        legal_action_types = [item.action for item in self.legal_actions]
        if len(legal_action_types) != len(set(legal_action_types)):
            raise ValueError("duplicate legal action type")

        if self.hero_turn:
            hero_seat = seats_by_id[self.hero.seat]
            if self.acting_seat != self.hero.seat:
                raise ValueError("hero_turn requires acting_seat to equal hero.seat")
            if not self.turn_id:
                raise ValueError("hero_turn requires turn_id")
            if len(self.hero.cards) != 2:
                raise ValueError("hero_turn requires two hero cards")
            if hero_seat.status not in {"active", "disconnected"}:
                raise ValueError("hero_turn requires an active or disconnected hero seat")
            if not hero_seat.in_hand:
                raise ValueError("hero_turn requires hero seat to be in_hand")
            if hero_seat.folded:
                raise ValueError("hero_turn requires hero seat not to be folded")
            if hero_seat.position != self.hero.position:
                raise ValueError("hero position does not match hero seat position")
        phase_rank = {
            Street.IDLE: -1,
            Street.PREFLOP: 0,
            Street.FLOP: 1,
            Street.TURN: 2,
            Street.RIVER: 3,
            Street.SHOWDOWN: 4,
        }
        if any(
            phase_rank[action.street] > phase_rank[self.street]
            for action in self.action_history.actions
        ):
            raise ValueError("action history contains a future street")
        return self


class VisionEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(min_length=8, max_length=256)
    type: Literal["table.observation"] = "table.observation"
    source: SourceRef
    captured_at: datetime
    table: TableRef
    frame: FrameRef
    observation: Observation
    confidence: Confidence
    state_token: str | None = Field(default=None, max_length=256)

    @field_validator("captured_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at must include a timezone")
        return value


class StrategyAction(StrictModel):
    action: Literal["fold", "check", "call", "bet", "raise"]
    frequency: float = Field(ge=0, le=1)
    amount_bb: Decimal | None = Field(default=None, ge=0)
    sizing_pot: float | None = None
    allin: bool = False


class SolverResult(StrictModel):
    provider: str
    provider_request_id: str | None = None
    situation: str | None = None
    strategy: list[StrategyAction] = Field(min_length=1)
    quota: dict[str, Any] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class SelectedAction(StrictModel):
    provider_action: StrategyAction
    platform_action: LegalActionType | None = None
    amount_bb: Decimal | None = None
    valid: bool
    invalid_reason: str | None = None


class DecisionView(StrictModel):
    decision_id: str
    action_key: str
    status: str
    source_kind: Literal["vision", "platform"]
    table_id: str
    hand_id: str
    street: Street
    state_hash: str
    attempts: int = 1
    solver_request: dict[str, Any] | None = None
    solver_result: SolverResult | None = None
    selected: SelectedAction | None = None
    execution_status: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class IngestItemResult(StrictModel):
    event_id: str
    status: Literal["accepted", "duplicate", "rejected"]
    highest_seq: int | None = None
    resync_required: bool = False
    reason: str | None = None
    decision: DecisionView | None = None


class IngestResponse(StrictModel):
    results: list[IngestItemResult]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
