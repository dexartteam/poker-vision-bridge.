from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from pydantic import SecretStr

import app.openpoker as openpoker_module
from app.config import Settings
from app.coordinator import canonical_hash
from app.models import (
    DecisionView,
    LegalActionType,
    ObservedActionType,
    Position,
    SelectedAction,
    StrategyAction,
    utc_now,
)
from app.openpoker import Authority, OpenPokerExecutionError, OpenPokerRunner


def _settings(tmp_path, **updates) -> Settings:
    values = {
        "database_path": tmp_path / "test.db",
        "receiver_token": SecretStr("receiver-unique-test-secret"),
        **updates,
    }
    return Settings(**values)


def _runner(event_factory, tmp_path) -> tuple[OpenPokerRunner, object, Authority, DecisionView]:
    observation = event_factory().observation
    state_hash = canonical_hash(observation.model_dump(mode="json"))
    authority = Authority(
        hand_id=observation.hand_id,
        turn_token="turn-token-1",
        state_hash=state_hash,
        valid_actions=[
            {"action": "fold"},
            {"action": "call", "amount": 20},
            {"action": "raise", "min": 40, "max": 2000},
        ],
        observation=observation,
    )
    runner = OpenPokerRunner(_settings(tmp_path), object())
    runner.table_id = "demo-table-01"
    runner.hand_id = observation.hand_id
    runner.ws = object()  # execute only requires a live marker when _send is stubbed.
    runner.authority = authority
    now = utc_now()
    provider_action = StrategyAction(action="call", frequency=1)
    decision = DecisionView(
        decision_id="decision-1",
        action_key="action-key-1",
        status="ready",
        source_kind="platform",
        table_id="demo-table-01",
        hand_id=observation.hand_id,
        street=observation.street,
        state_hash=state_hash,
        selected=SelectedAction(
            provider_action=provider_action,
            platform_action=LegalActionType.CALL,
            valid=True,
        ),
        execution_status="dry_run",
        created_at=now,
        updated_at=now,
    )
    return runner, observation, authority, decision


def _scale_chip_units(observation, divisor: Decimal):
    def scaled(value):
        return value / divisor if value is not None else None

    seats = [
        seat.model_copy(
            update={"stack": scaled(seat.stack), "bet_to": scaled(seat.bet_to)}
        )
        for seat in observation.seats
    ]
    actions = [
        action.model_copy(
            update={
                "contribution": scaled(action.contribution),
                "raise_to": scaled(action.raise_to),
            }
        )
        for action in observation.action_history.actions
    ]
    legal_actions = [
        action.model_copy(
            update={
                "call_amount": scaled(action.call_amount),
                "min_to": scaled(action.min_to),
                "max_to": scaled(action.max_to),
            }
        )
        for action in observation.legal_actions
    ]
    return observation.model_copy(
        update={
            "blinds": observation.blinds.model_copy(
                update={
                    "small": scaled(observation.blinds.small),
                    "big": scaled(observation.blinds.big),
                    "ante": scaled(observation.blinds.ante),
                }
            ),
            "pot": scaled(observation.pot),
            "seats": seats,
            "action_history": observation.action_history.model_copy(update={"actions": actions}),
            "legal_actions": legal_actions,
        }
    )


def test_reconcile_accepts_a_consistent_change_of_chip_units(event_factory):
    vision = event_factory().observation
    platform = _scale_chip_units(vision, Decimal("20"))

    assert OpenPokerRunner._reconcile(vision, platform) is None


def test_reconcile_normalizes_omitted_pokerai_versions_to_configured_defaults(
    event_factory,
):
    vision = event_factory().observation
    platform = _scale_chip_units(vision, Decimal("20"))
    platform = platform.model_copy(
        update={
            "pokerai": platform.pokerai.model_copy(
                update={"preflop_version": None, "flop_version": None}
            )
        }
    )

    assert (
        OpenPokerRunner._reconcile(
            vision,
            platform,
            default_preflop_version="6max",
            default_flop_version="6max",
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        (
            "blinds",
            lambda state: state.model_copy(
                update={
                    "blinds": state.blinds.model_copy(update={"small": Decimal("0.25")})
                }
            ),
        ),
        (
            "hero position",
            lambda state: state.model_copy(
                update={"hero": state.hero.model_copy(update={"position": "BTN"})}
            ),
        ),
        (
            "action history",
            lambda state: state.model_copy(
                update={
                    "action_history": state.action_history.model_copy(
                        update={
                            "actions": [
                                *state.action_history.actions[:2],
                                state.action_history.actions[2].model_copy(
                                    update={"action": ObservedActionType.CHECK}
                                ),
                                *state.action_history.actions[3:],
                            ]
                        }
                    )
                }
            ),
        ),
        (
            "seat state",
            lambda state: state.model_copy(
                update={
                    "seats": [
                        *state.seats[:4],
                        state.seats[4].model_copy(
                            update={"stack": state.seats[4].stack - Decimal("1")}
                        ),
                        *state.seats[5:],
                    ]
                }
            ),
        ),
        (
            "legal actions",
            lambda state: state.model_copy(
                update={
                    "legal_actions": [
                        action.model_copy(update={"call_amount": Decimal("2")})
                        if action.action == LegalActionType.CALL
                        else action
                        for action in state.legal_actions
                    ]
                }
            ),
        ),
        (
            "legal actions",
            lambda state: state.model_copy(
                update={
                    "legal_actions": [
                        action
                        for action in state.legal_actions
                        if action.action != LegalActionType.RAISE
                    ]
                }
            ),
        ),
    ],
)
def test_reconcile_rejects_every_execution_critical_difference(
    event_factory, field, mutate
):
    vision = event_factory().observation
    platform = mutate(_scale_chip_units(vision, Decimal("20")))

    assert OpenPokerRunner._reconcile(vision, platform) == field


@pytest.mark.parametrize(
    "context_update",
    [
        {"preflop_version": "6max_RC_40bb"},
        {"flop_version": "different-flop-tree"},
        {"pot_type": "SRP"},
        {"roles": {"hero": Position.BTN}},
        {"node_id": "root/CHECK"},
    ],
)
def test_reconcile_rejects_changed_pokerai_context(event_factory, context_update):
    vision = event_factory().observation
    platform = _scale_chip_units(vision, Decimal("20"))
    platform = platform.model_copy(
        update={
            "pokerai": platform.pokerai.model_copy(update=context_update),
        }
    )

    assert OpenPokerRunner._reconcile(vision, platform) == "PokerAI context"


async def test_execute_rejects_wrong_table_id_before_send(event_factory, tmp_path, monkeypatch):
    runner, observation, _, decision = _runner(event_factory, tmp_path)
    decision = decision.model_copy(
        update={"table_id": "another-table", "source_kind": "vision"}
    )
    sent = []

    async def fake_send(payload):
        sent.append(payload)
        await runner._handle_message(
            {
                "type": "action_ack",
                "client_action_id": decision.decision_id,
                "status": "accepted",
            }
        )

    monkeypatch.setattr(runner, "_send", fake_send)

    with pytest.raises(OpenPokerExecutionError, match="table_id"):
        await runner.execute(decision, observation, "vision")
    assert sent == []


async def test_camera_decision_executes_after_full_normalized_reconciliation(
    event_factory, tmp_path, monkeypatch
):
    runner, vision, authority, decision = _runner(event_factory, tmp_path)
    platform = _scale_chip_units(vision, Decimal("20"))
    runner.authority = Authority(
        hand_id=authority.hand_id,
        turn_token=authority.turn_token,
        state_hash=canonical_hash(platform.model_dump(mode="json")),
        valid_actions=[
            {"action": "fold"},
            {"action": "call", "amount": 1},
            {"action": "raise", "min": 2, "max": 100},
        ],
        observation=platform,
    )
    decision = decision.model_copy(update={"source_kind": "vision"})
    sent = []

    async def fake_send(payload):
        sent.append(payload)
        await runner._handle_message(
            {
                "type": "action_ack",
                "client_action_id": decision.decision_id,
                "status": "accepted",
            }
        )

    monkeypatch.setattr(runner, "_send", fake_send)

    assert await runner.execute(decision, vision, "vision") == "confirmed"
    assert sent == [
        {
            "type": "action",
            "hand_id": vision.hand_id,
            "action": "call",
            "client_action_id": decision.decision_id,
            "turn_token": authority.turn_token,
        }
    ]


async def test_action_ack_does_not_clear_a_newer_turn_authority(
    event_factory, tmp_path, monkeypatch
):
    runner, observation, old_authority, decision = _runner(event_factory, tmp_path)
    newer_authority = Authority(
        hand_id=old_authority.hand_id,
        turn_token="turn-token-2",
        state_hash="sha256:newer-state",
        valid_actions=[{"action": "check"}],
        observation=old_authority.observation,
    )

    async def fake_send(_payload):
        await runner._handle_message(
            {
                "type": "action_ack",
                "client_action_id": decision.decision_id,
                "status": "accepted",
            }
        )
        runner.authority = newer_authority

    monkeypatch.setattr(runner, "_send", fake_send)

    assert await runner.execute(decision, observation, "platform") == "confirmed"
    assert runner.authority is newer_authority


async def test_nonaccepted_action_ack_fails_closed(event_factory, tmp_path, monkeypatch):
    runner, observation, _, decision = _runner(event_factory, tmp_path)

    async def fake_send(_payload):
        await runner._handle_message(
            {
                "type": "action_ack",
                "client_action_id": decision.decision_id,
                "status": "unexpected",
            }
        )

    monkeypatch.setattr(runner, "_send", fake_send)

    with pytest.raises(OpenPokerExecutionError, match="action_ack status"):
        await runner.execute(decision, observation, "platform")
    assert runner.authority is None


async def test_action_ack_timeout_requests_resync_without_retrying_action(
    event_factory, tmp_path, monkeypatch
):
    runner, observation, _, decision = _runner(event_factory, tmp_path)
    runner._last_table_seq = 37
    sent = []

    async def fake_send(payload):
        sent.append(payload)

    async def immediate_timeout(_future, *, timeout):
        assert timeout == 10
        raise asyncio.TimeoutError

    monkeypatch.setattr(runner, "_send", fake_send)
    monkeypatch.setattr(openpoker_module.asyncio, "wait_for", immediate_timeout)

    result = await runner.execute(decision, observation, "platform")

    assert result == "unknown_waiting_for_resync"
    assert runner.authority is None
    assert [payload["type"] for payload in sent] == ["action", "resync_request"]
    assert sent[0]["client_action_id"] == decision.decision_id
    assert sent[1] == {
        "type": "resync_request",
        "table_id": "demo-table-01",
        "last_table_seq": 37,
    }


async def test_disconnect_fails_all_pending_action_ack_waiters(tmp_path, monkeypatch):
    runner = OpenPokerRunner(
        _settings(
            tmp_path,
            openpoker_enabled=True,
            openpoker_api_key=SecretStr("op_test_key"),
        ),
        object(),
    )
    pending = asyncio.get_running_loop().create_future()
    runner._pending_acks["decision-1"] = pending

    class DroppingConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            runner._stop.set()
            raise ConnectionError("test disconnect")

    monkeypatch.setattr(
        openpoker_module.websockets,
        "connect",
        lambda *_args, **_kwargs: DroppingConnection(),
    )

    await runner._run()

    assert pending.done()
    with pytest.raises(OpenPokerExecutionError, match="disconnected"):
        pending.result()


async def test_disconnect_after_send_is_reported_as_unknown_outcome(
    event_factory, tmp_path, monkeypatch
):
    runner, observation, _, decision = _runner(event_factory, tmp_path)

    async def disconnect_after_send(_payload):
        runner._fail_pending_acks("Open Poker disconnected before action acknowledgement")

    monkeypatch.setattr(runner, "_send", disconnect_after_send)

    assert (
        await runner.execute(decision, observation, "platform")
        == "unknown_waiting_for_resync"
    )
    assert runner.authority is None
