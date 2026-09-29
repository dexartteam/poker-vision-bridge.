from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models import Observation, SolverResult, StrictModel


class ReplayState(Observation):
    """Poker fields only: no live transport, TableRef, platform, or execution authority."""


class RecordedSource(StrictModel):
    kind: Literal["recording", "camera"]
    epoch: str = Field(min_length=1, max_length=128)
    calibration_id: str = Field(min_length=1, max_length=128)
    frame_seq: int = Field(ge=0)
    revision: int = Field(ge=0)
    captured_at: float
    media_time: float | None = Field(default=None, ge=0)


class CardRead(StrictModel):
    status: Literal["visible", "empty", "unreadable"]
    value: str | None


class PotDisplay(StrictModel):
    value: float | None = Field(ge=0)
    raw: str | None
    unit: Literal["chips"]


class SeatRead(StrictModel):
    seat_index: int = Field(ge=0, le=5)
    is_hero: bool
    name: str | None
    stack: float | None = Field(ge=0)
    bet: None = None
    chip_display: float | None = Field(default=None, ge=0)
    chip_visibility: Literal["visible", "empty", "unknown"]
    action_label: str | None
    status: Literal["folded", "all_in", "unknown"]


class LocalObservation(StrictModel):
    schema_version: Literal["local-vision.observation.v2"]
    layout: Literal["pokerstars-classic"]
    source: RecordedSource
    status: Literal["partial", "stale"]
    evidence: Literal["single_frame", "repeated", "moving"]
    stable: bool
    decision_ready: Literal[False]
    pot_includes_current_bets: None = None
    ui_mode: Literal["live", "replay", "unknown"]
    hand_number: int | None
    ui_street: Literal["preflop", "flop", "turn", "river", "showdown", "unknown"]
    pot_display: PotDisplay
    board: list[CardRead] = Field(max_length=5)
    hero_cards: list[CardRead] = Field(max_length=2)
    hero_turn: bool | None
    seats: list[SeatRead] = Field(max_length=6)
    dealer_seat: int | None = Field(ge=0, le=5)
    available_actions: list[str]
    action_history: dict[str, Any]
    diagnostics: dict[str, Any]


class ConfirmedContext(StrictModel):
    provenance: Literal["manual"]
    source_epoch: str
    frame_seq: int = Field(ge=0)
    revision: int = Field(ge=0)
    media_time: float = Field(ge=0)
    history_confirmed: bool = False
    values: dict[str, Any]


class ReplayRequest(StrictModel):
    schema_version: Literal["replay.request.v1"] = "replay.request.v1"
    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    action: Literal["diagnose", "solve"] = "diagnose"
    observation: LocalObservation
    confirmed_context: ConfirmedContext | None = None


class FrameRef(StrictModel):
    source_epoch: str
    frame_seq: int
    revision: int
    media_time: float | None


class Issue(StrictModel):
    code: str
    field: str
    message: str
    kind: Literal["missing", "invalid", "unsupported", "configuration", "warning"]


class SolverInfo(StrictModel):
    provider: Literal["pokerai.bet"] = "pokerai.bet"
    configured: bool


class ReplayResponse(StrictModel):
    schema_version: Literal["replay.response.v1"] = "replay.response.v1"
    session_id: str
    request_id: str
    frame: FrameRef
    status: Literal[
        "missing_data",
        "invalid",
        "unsupported",
        "ready",
        "not_configured",
        "solved",
        "solver_failed",
    ]
    issues: list[Issue]
    solver: SolverInfo
    execution: Literal["disabled"] = "disabled"
    draft_observation: dict[str, Any]
    normalized_observation: dict[str, Any] | None = None
    solver_request: dict[str, Any] | None = None
    solver_result: SolverResult | None = None
    manual_fields: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0
