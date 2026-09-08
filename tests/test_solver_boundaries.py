from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.models import (
    LegalAction,
    LegalActionType,
    PokerAIContext,
    StrategyAction,
)
from app.solver import PokerAISolver, SolverUnsupported, validate_selected_action


def _solver(tmp_path) -> PokerAISolver:
    return PokerAISolver(
        Settings(
            database_path=tmp_path / "solver.db",
            receiver_token=SecretStr("private-receiver-token"),
            sampling_secret=SecretStr("private-sampling-secret"),
            solver_mode="pokerai",
            pokerai_api_key=SecretStr("gto_test"),
        )
    )


def test_preflop_rejects_ante_and_wrong_stack_depth(event_factory, tmp_path):
    solver = _solver(tmp_path)
    observation = event_factory().observation

    with_ante = observation.model_copy(
        update={"blinds": observation.blinds.model_copy(update={"ante": Decimal("2")})}
    )
    with pytest.raises(SolverUnsupported, match="ante"):
        solver.prepare(with_ante)

    wrong_depth = observation.model_copy(
        update={
            "seats": [
                seat.model_copy(update={"stack": Decimal("1000")})
                if seat.seat in {4, 5}
                else seat
                for seat in observation.seats
            ]
        }
    )
    with pytest.raises(SolverUnsupported, match="expects about 100bb"):
        solver.prepare(wrong_depth)


def test_preflop_accepts_documented_40bb_version(event_factory, tmp_path):
    solver = _solver(tmp_path)
    observation = event_factory().observation
    stacks = {0: "790", 1: "780", 2: "800", 3: "800", 4: "800", 5: "800"}
    forty = observation.model_copy(
        update={
            "seats": [
                seat.model_copy(update={"stack": Decimal(stacks[seat.seat])})
                for seat in observation.seats
            ],
            "pokerai": observation.pokerai.model_copy(
                update={"preflop_version": "6max_RC_40bb"}
            ),
        }
    )
    assert solver.prepare(forty).payload["request"]["preflop_version"] == "6max_RC_40bb"


def test_multiway_flop_is_fail_closed(event_factory, tmp_path):
    solver = _solver(tmp_path)
    observation = event_factory().observation
    flop = observation.model_copy(
        update={
            "street": "flop",
            "board": ["2c", "7d", "Js"],
            "pokerai": PokerAIContext(
                pot_type="SRP",
                roles={"hero": "CO", "raiser": "CO", "caller": "BTN"},
                node_id="root",
            ),
        }
    )
    with pytest.raises(SolverUnsupported, match="exactly two"):
        solver.prepare(flop)


def test_all_in_flag_on_call_remains_call(event_factory):
    observation = event_factory().observation.model_copy(
        update={
            "legal_actions": [
                LegalAction(action=LegalActionType.CALL, call_amount=Decimal("20")),
                LegalAction(action=LegalActionType.ALL_IN, max_to=Decimal("2000")),
            ]
        }
    )
    selected = validate_selected_action(
        StrategyAction(action="call", frequency=1, allin=True), observation
    )
    assert selected.valid
    assert selected.platform_action == LegalActionType.CALL
