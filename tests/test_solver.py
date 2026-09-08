from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.models import LegalAction, LegalActionType, StrategyAction
from app.solver import (
    PokerAISolver,
    SolverResult,
    select_mixed_action,
    validate_selected_action,
)


def test_preflop_request_uses_incremental_bb(event_factory, tmp_path):
    event = event_factory()
    settings = Settings(
        database_path=tmp_path / "test.db",
        solver_mode="pokerai",
        pokerai_api_key=SecretStr("gto_test"),
        receiver_token=SecretStr("receiver-test"),
        sampling_secret=SecretStr("sampling-test"),
    )
    solver = PokerAISolver(settings)
    try:
        prepared = solver.prepare(event.observation)
    finally:
        # Client has not opened a connection; closing is covered by runtime tests.
        pass

    assert prepared.kind == "preflop"
    body = prepared.payload["request"]
    assert body["hole_cards"] == "AhKh"
    assert body["positions"] == {"hero": "CO"}
    assert body["preflop_actions"][:2] == [
        {"position": "SB", "action": "small blind", "amount": 0.5},
        {"position": "BB", "action": "big blind", "amount": 1},
    ]
    assert body["preflop_actions"][2:] == [
        {"position": "UTG", "action": "fold"},
        {"position": "MP", "action": "fold"},
    ]


def test_mixed_sampling_is_deterministic():
    result = SolverResult(
        provider="test",
        strategy=[
            StrategyAction(action="fold", frequency=0.2),
            StrategyAction(action="call", frequency=0.3),
            StrategyAction(action="raise", frequency=0.5, amount_bb=Decimal("3")),
        ],
    )
    choices = [
        select_mixed_action(result, action_key="same-turn", secret="secret") for _ in range(20)
    ]
    assert len({choice.model_dump_json() for choice in choices}) == 1


def test_raise_is_absolute_and_must_be_inside_bounds(event_factory):
    observation = event_factory().observation
    chosen = StrategyAction(action="raise", frequency=1, amount_bb=Decimal("3"))
    selected = validate_selected_action(chosen, observation)
    assert selected.valid
    assert selected.amount_bb == Decimal("3")

    illegal_observation = observation.model_copy(
        update={
            "legal_actions": [
                LegalAction(
                    action=LegalActionType.RAISE,
                    min_to=Decimal("80"),
                    max_to=Decimal("2000"),
                )
            ]
        }
    )
    invalid = validate_selected_action(chosen, illegal_observation)
    assert not invalid.valid
    assert "outside" in (invalid.invalid_reason or "")
