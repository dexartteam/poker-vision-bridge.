from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.models import SolverResult, StrategyAction
from app.replay.api import create_app
from app.replay.settings import ReplaySettings
from app.solver import SolverError

TOKEN = "receiver-token-for-replay-tests-only"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class RecordingSolver:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.error = error
        self.closed = False
        self.result = result or SolverResult(
            provider="pokerai.bet",
            strategy=[
                StrategyAction(action="fold", frequency=0.25),
                StrategyAction(action="call", frequency=0.25),
                StrategyAction(action="raise", frequency=0.5, amount_bb=Decimal("3")),
            ],
        )

    async def solve(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


def settings(configured=True, **kwargs):
    return ReplaySettings(
        _env_file=None,
        receiver_token=SecretStr(TOKEN),
        pokerai_api_key=SecretStr("gto-test-server-secret") if configured else None,
        **kwargs,
    )


@pytest.fixture
def request_body(event_factory):
    state = event_factory().observation.model_dump(mode="json")
    return {
        "schema_version": "replay.request.v1",
        "session_id": "test-session",
        "request_id": "test-request",
        "action": "solve",
        "observation": {
            "schema_version": "local-vision.observation.v2",
            "layout": "pokerstars-classic",
            "source": {
                "kind": "recording",
                "epoch": "test-epoch",
                "calibration_id": "test-calibration",
                "frame_seq": 7,
                "revision": 2,
                "captured_at": 123,
                "media_time": 12.5,
            },
            "status": "partial",
            "evidence": "single_frame",
            "stable": True,
            "decision_ready": False,
            "pot_includes_current_bets": None,
            "ui_mode": "unknown",
            "hand_number": None,
            "ui_street": "preflop",
            "pot_display": {"value": 30, "raw": "Pot: 30", "unit": "chips"},
            "board": [{"status": "empty", "value": None} for _ in range(5)],
            "hero_cards": [{"status": "visible", "value": c} for c in state["hero"]["cards"]],
            "hero_turn": True,
            "seats": [
                {
                    "seat_index": s["seat"],
                    "is_hero": s["seat"] == state["hero"]["seat"],
                    "name": s["name"],
                    "stack": float(s["stack"]),
                    "bet": None,
                    "chip_display": float(s["bet_to"]),
                    "chip_visibility": "visible",
                    "action_label": "fold" if s["folded"] else None,
                    "status": "folded" if s["folded"] else "unknown",
                }
                for s in state["seats"]
            ],
            "dealer_seat": None,
            "available_actions": ["fold", "call", "raise"],
            "action_history": {"status": "incomplete", "events": []},
            "diagnostics": {},
        },
        "confirmed_context": {
            "provenance": "manual",
            "source_epoch": "test-epoch",
            "frame_seq": 7,
            "revision": 2,
            "media_time": 12.5,
            "history_confirmed": True,
            "values": state,
        },
    }


def test_partial_frame_names_all_gaps_without_inventing_bets(request_body):
    solver = RecordingSolver()
    request_body.pop("confirmed_context")
    with TestClient(create_app(settings(False), solver_override=solver)) as client:
        response = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "missing_data"
    missing = {i["field"] for i in body["issues"]}
    assert {
        "blinds.big",
        "blinds.small",
        "blinds.ante",
        "dealer_seat",
        "hero.position",
        "pot",
        "action_history",
        "turn_id",
        "seats.0.bet_to",
    } <= missing
    assert body["draft_observation"]["pot"] is None
    assert body["draft_observation"]["seats"][0]["bet_to"] is None
    assert body["execution"] == "disabled"
    assert body["solver_result"] is None
    assert not solver.calls


def test_diagnose_prepares_without_call_and_missing_key_never_uses_fallback(request_body):
    solver = RecordingSolver()
    with TestClient(create_app(settings(False), solver_override=solver)) as client:
        body = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS).json()
        assert body["status"] == "not_configured"
        assert body["solver_request"]["kind"] == "preflop"
        assert body["solver_result"] is None
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        request_body["action"] = "diagnose"
        body = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS).json()
        assert body["status"] == "ready"
    assert not solver.calls


def test_real_solver_contract_and_idempotent_retransmission(request_body):
    solver = RecordingSolver()
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        one = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS)
        two = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS)
        assert one.status_code == two.status_code == 200
        assert one.json() == two.json()
        body = one.json()
        assert body["status"] == "solved"
        assert body["solver_result"]["provider"] == "pokerai.bet"
        assert len(body["solver_result"]["strategy"]) == 3
        assert body["frame"]["media_time"] == 12.5
        assert body["execution"] == "disabled"
        assert "platform" not in body["normalized_observation"]
        assert "table" not in body["normalized_observation"]
        request_body["confirmed_context"]["values"]["turn_id"] = "a-new-turn"
        assert (
            client.post("/v1/replay/analyze", json=request_body, headers=HEADERS).status_code == 409
        )
    assert len(solver.calls) == 1
    assert solver.calls[0].payload["request"]["hole_cards"] == "AhKh"
    assert solver.closed


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda r: r["observation"]["source"].update(kind="camera"), "recording_required"),
        (lambda r: r["confirmed_context"].update(frame_seq=8), "context_frame_mismatch"),
        (lambda r: r["confirmed_context"].update(revision=3), "context_frame_mismatch"),
        (lambda r: r["confirmed_context"].update(media_time=12.6), "context_frame_mismatch"),
        (lambda r: r["confirmed_context"].update(history_confirmed=False), "history_unconfirmed"),
        (lambda r: r["observation"].update(status="stale"), "stale_frame"),
        (lambda r: r["observation"].update(stable=False), "unstable_frame"),
        (
            lambda r: r["confirmed_context"]["values"]["hero"].update(cards=["As", "Kh"]),
            "recognized_conflict",
        ),
        (lambda r: r["confirmed_context"]["values"].update(pot="31"), "pot_history_mismatch"),
        (
            lambda r: r["confirmed_context"]["values"]["seats"][0].update(bet_to="0"),
            "bet_history_mismatch",
        ),
        (
            lambda r: r["confirmed_context"]["values"]["legal_actions"][1].update(call_amount="1"),
            "call_amount_mismatch",
        ),
    ],
)
def test_ambiguous_or_inconsistent_input_never_reaches_solver(request_body, change, code):
    solver = RecordingSolver()
    change(request_body)
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        response = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS)
    assert response.status_code == 200
    assert code in {i["code"] for i in response.json()["issues"]}
    assert not solver.calls


@pytest.mark.parametrize(
    "patch",
    [
        {"seats": [{"seat": []}]},
        {"legal_actions": [{"action": {}}]},
        {"legal_actions": [{"action": "all_in", "max_to": None}]},
        {"hero": []},
        {"pokerai": []},
        {"pokerai": {"roles": None}},
        {"street": "flop", "pokerai": {"pot_type": {}}},
        {"blinds": []},
        {"action_history": []},
        {"board": {}},
        {"hero_turn": None},
    ],
)
def test_malformed_manual_shapes_are_diagnostics_not_internal_errors(request_body, patch):
    solver = RecordingSolver()
    request_body["confirmed_context"]["values"].update(patch)
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        response = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] in {"invalid", "missing_data"}
    assert not solver.calls


def test_incomplete_fold_history_is_not_treated_as_full(request_body):
    solver = RecordingSolver()
    request_body["confirmed_context"]["values"]["action_history"]["actions"].pop()
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        body = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS).json()
    assert "fold_history_mismatch" in {i["code"] for i in body["issues"]}
    assert not solver.calls


def test_unsupported_depth_does_not_consume_quota(request_body):
    solver = RecordingSolver()
    for seat, raw in zip(
        request_body["confirmed_context"]["values"]["seats"],
        request_body["observation"]["seats"],
        strict=True,
    ):
        seat["stack"] = str(Decimal(seat["stack"]) / 2)
        raw["stack"] = float(seat["stack"])
    request_body["confirmed_context"]["values"]["legal_actions"][2]["max_to"] = "1000"
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        body = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS).json()
    assert body["status"] == "unsupported"
    assert not solver.calls


def test_solver_error_redacts_secrets_and_does_not_retry(request_body):
    solver = RecordingSolver(error=SolverError(f"failure gto-test-server-secret {TOKEN}"))
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        one = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS)
        two = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS)
    assert one.json()["status"] == "solver_failed"
    assert one.json() == two.json()
    assert "gto-test-server-secret" not in one.text
    assert TOKEN not in one.text
    assert len(solver.calls) == 1


@pytest.mark.parametrize(
    "strategy,status",
    [
        ([StrategyAction(action="fold", frequency=0.2)], "solver_failed"),
        ([StrategyAction(action="raise", frequency=1, amount_bb=Decimal("300"))], "invalid"),
        ([StrategyAction(action="check", frequency=1)], "invalid"),
    ],
)
def test_bad_provider_strategy_is_visible_but_not_solved(request_body, strategy, status):
    solver = RecordingSolver(result=SolverResult(provider="pokerai.bet", strategy=strategy))
    with TestClient(create_app(settings(), solver_override=solver)) as client:
        body = client.post("/v1/replay/analyze", json=request_body, headers=HEADERS).json()
    assert body["status"] == status
    assert body["solver_result"] is not None
    assert body["execution"] == "disabled"


def test_authentication_schema_cors_and_no_credentials_in_status(request_body):
    solver = RecordingSolver()
    config = settings(replay_allowed_origins=["https://dexartteam.github.io"])
    with TestClient(create_app(config, solver_override=solver)) as client:
        assert client.post("/v1/replay/analyze", json=request_body).status_code == 401
        assert client.get("/v1/replay/schema").status_code == 401
        schema = client.get("/v1/replay/schema", headers=HEADERS).json()
        assert "table" not in schema["properties"]
        public = client.get("/v1/replay/status")
        assert "gto-test-server-secret" not in public.text
        cors = client.options(
            "/v1/replay/analyze",
            headers={
                "Origin": "https://dexartteam.github.io",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )
        assert cors.status_code == 200
        assert cors.headers["access-control-allow-origin"] == "https://dexartteam.github.io"
        assert "access-control-allow-credentials" not in cors.headers
        assert (
            client.options(
                "/v1/replay/analyze",
                headers={
                    "Origin": "https://wrong.example",
                    "Access-Control-Request-Method": "POST",
                },
            ).status_code
            == 400
        )
    assert not solver.calls


def test_unknown_extra_execution_fields_and_false_manual_provenance_are_rejected(request_body):
    with TestClient(create_app(settings(False))) as client:
        bad = deepcopy(request_body)
        bad["confirmed_context"]["provenance"] = "auto"
        assert client.post("/v1/replay/analyze", json=bad, headers=HEADERS).status_code == 422
        bad = deepcopy(request_body)
        bad["confirmed_context"]["values"]["platform"] = "openpoker"
        body = client.post("/v1/replay/analyze", json=bad, headers=HEADERS).json()
        assert body["status"] == "invalid"
