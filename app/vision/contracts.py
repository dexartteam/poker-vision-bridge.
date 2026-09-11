"""Version 1 wire contract. Transport metadata is never supplied by the model."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Card(StrictModel):
    status: Literal["visible", "empty", "hidden", "unreadable"]
    value: str | None = Field(pattern=r"^[2-9TJQKA][cdhs]$")

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "visible") != (self.value is not None):
            raise ValueError("only visible cards have values")
        return self


class Amount(StrictModel):
    # Integer minimal units, never a binary floating point monetary value.
    value: int | None = Field(ge=0, le=9_007_199_254_740_991)
    raw: str | None = Field(max_length=64)


class Seat(StrictModel):
    seat: int = Field(ge=0, le=5)
    status: Literal["active", "folded", "empty", "sitting_out", "unknown"]
    stack: Amount
    street_bet: Amount


class Observation(StrictModel):
    hand_number: str | None = Field(max_length=80)
    street: Literal["preflop", "flop", "turn", "river", "unknown"]
    hero_cards: list[Card] = Field(min_length=2, max_length=2)
    board: list[Card] = Field(min_length=5, max_length=5)
    seats: list[Seat] = Field(min_length=6, max_length=6)
    dealer_seat: int | None = Field(ge=0, le=5)
    actor_seat: int | None = Field(ge=0, le=5)
    hero_turn: bool | None
    pot: Amount
    pot_includes_current_bets: bool | None
    side_pots_complete: bool | None
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def consistent(self):
        if sorted(s.seat for s in self.seats) != list(range(6)):
            raise ValueError("six distinct fixed seats required")
        cards = [c.value for c in self.hero_cards + self.board if c.value]
        if len(cards) != len(set(cards)):
            raise ValueError("duplicate visible cards")
        visible = sum(c.status == "visible" for c in self.board)
        expected = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}.get(self.street)
        if expected is not None and visible != expected:
            raise ValueError("street and visible board disagree")
        return self


class Region(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    ignore: bool

    @model_validator(mode="after")
    def bounds(self):
        if self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError("region outside image")
        return self


class Profile(StrictModel):
    calibration_id: str = Field(min_length=8, max_length=80)
    width: int = Field(ge=64, le=4096)
    height: int = Field(ge=64, le=4096)
    unit: Literal["chips", "USD", "EUR", "BB"]
    scale: int = Field(ge=0, le=6)
    hero_seat: int = Field(ge=0, le=5)
    regions: list[Region] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def usable(self):
        if not any(not r.ignore for r in self.regions):
            raise ValueError("at least one observed region required")
        if len({r.name for r in self.regions}) != len(self.regions):
            raise ValueError("region names must be unique")
        return self


class Source(StrictModel):
    capture_epoch: str = Field(min_length=8, max_length=80)
    calibration_id: str = Field(min_length=8, max_length=80)
    frame_id: str = Field(min_length=8, max_length=80)
    frame_seq: int = Field(ge=1)
    visual_revision: int = Field(ge=0)
    captured_at: int = Field(ge=0)


class Frame(StrictModel):
    source: Source
    reason: Literal[
        "initial",
        "significant_change",
        "confirmation",
        "safety_poll",
        "retry",
        "manual",
        "hero_turn",
    ]
    stable: bool
    image: str = Field(max_length=2_800_000)


class Change(StrictModel):
    capture_epoch: str = Field(min_length=8, max_length=80)
    calibration_id: str = Field(min_length=8, max_length=80)
    visual_revision: int = Field(ge=0)


def unknown_observation() -> Observation:
    return Observation(
        hand_number=None,
        street="unknown",
        hero_cards=[Card(status="unreadable", value=None) for _ in range(2)],
        board=[Card(status="unreadable", value=None) for _ in range(5)],
        seats=[
            Seat(
                seat=i,
                status="unknown",
                stack=Amount(value=None, raw=None),
                street_bet=Amount(value=None, raw=None),
            )
            for i in range(6)
        ],
        dealer_seat=None,
        actor_seat=None,
        hero_turn=None,
        pot=Amount(value=None, raw=None),
        pot_includes_current_bets=None,
        side_pots_complete=None,
        confidence=0.0,
        warnings=["mock_provider_no_recognition"],
    )
