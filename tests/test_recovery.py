from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.coordinator import Coordinator, canonical_observation_hash
from app.solver import FakeSolver, SolverError
from app.store import Store
from app.telegram import CapturingNotifier


class CountingSolver(FakeSolver):
    def __init__(self) -> None:
        self.calls = 0

    async def solve(self, request):
        self.calls += 1
        return await super().solve(request)


class FlakyUnexpectedSolver(FakeSolver):
    def __init__(self) -> None:
        self.calls = 0

    async def solve(self, request):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary internal failure")
        return await super().solve(request)


class AlwaysFailSolver(FakeSolver):
    def __init__(self) -> None:
        self.calls = 0

    async def solve(self, request):
        self.calls += 1
        raise SolverError("temporary provider failure")


class BlockingSolver(FakeSolver):
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def solve(self, request):
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def settings_for(tmp_path, *, min_stable_frames: int = 1) -> Settings:
    return Settings(
        database_path=tmp_path / "recovery.db",
        receiver_token=SecretStr("test-receiver-token"),
        sampling_secret=SecretStr("test-sampling-secret"),
        min_stable_frames=min_stable_frames,
    )


def coordinator_for(settings: Settings, store: Store, solver):
    return Coordinator(
        settings=settings,
        store=store,
        solver=solver,
        notifier=CapturingNotifier(),
    )


async def test_exact_duplicate_resumes_after_event_commit(event_factory, tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    event = event_factory(1)
    state_hash = canonical_observation_hash(event.observation)
    try:
        inserted = await store.insert_event(event, "vision", state_hash)
        assert inserted.status == "accepted"
        assert await store.get_projection("vision", event.source.agent_id, event.table.table_id) is None

        resumed = await coordinator.process_vision(event)

        assert resumed.status == "duplicate"
        assert resumed.decision is not None
        assert resumed.decision.status == "ready"
        assert solver.calls == 1
    finally:
        await store.close()


async def test_unqualified_frame_does_not_advance_stability(event_factory, tmp_path):
    settings = settings_for(tmp_path, min_stable_frames=2)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    low = event_factory(1).model_copy(
        update={"confidence": event_factory(1).confidence.model_copy(update={"overall": 0.1})}
    )
    try:
        first = await coordinator.process_vision(low)
        second = await coordinator.process_vision(event_factory(2))
        third = await coordinator.process_vision(event_factory(3))

        assert "confidence" in (first.reason or "")
        assert "1/2" in (second.reason or "")
        assert second.decision is None
        assert third.decision is not None
        assert solver.calls == 1
    finally:
        await store.close()


async def test_replayed_frame_sequence_does_not_advance_stability(event_factory, tmp_path):
    settings = settings_for(tmp_path, min_stable_frames=2)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    replayed = event_factory(2)
    replayed = replayed.model_copy(
        update={"frame": replayed.frame.model_copy(update={"seq": 1})}
    )
    try:
        first = await coordinator.process_vision(event_factory(1))
        second = await coordinator.process_vision(replayed)
        third = await coordinator.process_vision(event_factory(3))

        assert "1/2" in (first.reason or "")
        assert "1/2" in (second.reason or "")
        assert second.decision is None
        assert third.decision is not None
        assert solver.calls == 1
    finally:
        await store.close()


async def test_new_frame_sequence_advances_when_pixels_are_unchanged(event_factory, tmp_path):
    settings = settings_for(tmp_path, min_stable_frames=2)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    first_event = event_factory(1)
    relabelled = event_factory(2)
    relabelled = relabelled.model_copy(
        update={
            "frame": relabelled.frame.model_copy(update={"hash": first_event.frame.hash})
        }
    )
    try:
        first = await coordinator.process_vision(first_event)
        second = await coordinator.process_vision(relabelled)

        assert "1/2" in (first.reason or "")
        assert second.decision is not None
        assert second.decision.status == "ready"
        assert solver.calls == 1
    finally:
        await store.close()


async def test_semantically_unordered_fields_and_decimal_scale_keep_stability(
    event_factory, tmp_path
):
    settings = settings_for(tmp_path, min_stable_frames=2)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    equivalent = event_factory(2)
    observation = equivalent.observation
    equivalent_observation = observation.model_copy(
        update={
            "blinds": observation.blinds.model_copy(
                update={
                    "small": Decimal("10.00"),
                    "big": Decimal("20.000"),
                }
            ),
            "pot": Decimal("30.0000"),
            "seats": list(reversed(observation.seats)),
            "legal_actions": list(reversed(observation.legal_actions)),
        }
    )
    equivalent = equivalent.model_copy(update={"observation": equivalent_observation})
    try:
        first = await coordinator.process_vision(event_factory(1))
        second = await coordinator.process_vision(equivalent)

        assert "1/2" in (first.reason or "")
        assert second.decision is not None
        assert second.decision.status == "ready"
        assert solver.calls == 1
    finally:
        await store.close()


async def test_stability_resets_when_camera_boot_changes(event_factory, tmp_path):
    settings = settings_for(tmp_path, min_stable_frames=2)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    rebooted = event_factory(2)
    rebooted = rebooted.model_copy(
        update={"source": rebooted.source.model_copy(update={"boot_id": "new-boot"})}
    )
    after_reboot = event_factory(3)
    after_reboot = after_reboot.model_copy(
        update={"source": after_reboot.source.model_copy(update={"boot_id": "new-boot"})}
    )
    try:
        first = await coordinator.process_vision(event_factory(1))
        second = await coordinator.process_vision(rebooted)
        third = await coordinator.process_vision(after_reboot)

        assert "1/2" in (first.reason or "")
        assert "1/2" in (second.reason or "")
        assert second.decision is None
        assert third.decision is not None
        assert solver.calls == 1
    finally:
        await store.close()


async def test_far_future_observation_is_not_solver_eligible(event_factory, tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    event = event_factory(1).model_copy(
        update={"captured_at": datetime.now(timezone.utc) + timedelta(minutes=1)}
    )
    try:
        result = await coordinator.process_vision(event)

        assert result.decision is None
        assert "future" in (result.reason or "")
        assert solver.calls == 0
    finally:
        await store.close()


async def test_unexpected_solver_failure_is_retryable_on_duplicate(event_factory, tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.database_path)
    await store.open()
    solver = FlakyUnexpectedSolver()
    coordinator = coordinator_for(settings, store, solver)
    event = event_factory(1)
    try:
        failed = await coordinator.process_vision(event)
        recovered = await coordinator.process_vision(event)

        assert failed.decision is not None
        assert failed.decision.status == "solver_failed"
        assert recovered.decision is not None
        assert recovered.decision.status == "ready"
        assert recovered.decision.decision_id == failed.decision.decision_id
        assert recovered.decision.attempts == 2
        assert solver.calls == 2
    finally:
        await store.close()


async def test_corrected_state_with_reused_turn_id_gets_a_new_decision(event_factory, tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = coordinator_for(settings, store, solver)
    first_event = event_factory(1)
    corrected = event_factory(2)
    corrected = corrected.model_copy(
        update={
            "observation": corrected.observation.model_copy(update={"pot": Decimal("31")})
        }
    )
    try:
        first = await coordinator.process_vision(first_event)
        second = await coordinator.process_vision(corrected)

        assert first.decision is not None
        assert second.decision is not None
        assert first.decision.decision_id != second.decision.decision_id
        assert first.decision.action_key != second.decision.action_key
        assert solver.calls == 2
    finally:
        await store.close()


async def test_transient_failure_retries_at_most_three_times(event_factory, tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.database_path)
    await store.open()
    solver = AlwaysFailSolver()
    coordinator = coordinator_for(settings, store, solver)
    event = event_factory(1)
    try:
        results = [await coordinator.process_vision(event) for _ in range(4)]

        assert solver.calls == 3
        assert all(item.decision is not None for item in results)
        assert [item.decision.attempts for item in results if item.decision] == [1, 2, 3, 3]
        assert results[-1].decision is not None
        assert results[-1].decision.status == "solver_failed"
    finally:
        await store.close()


async def test_cancelled_solver_attempt_is_marked_and_can_resume(event_factory, tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.database_path)
    await store.open()
    blocking = BlockingSolver()
    coordinator = coordinator_for(settings, store, blocking)
    event = event_factory(1)
    try:
        task = asyncio.create_task(coordinator.process_vision(event))
        await blocking.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        [interrupted] = await store.list_decisions()
        assert interrupted.status == "interrupted"
        assert interrupted.attempts == 1

        coordinator.solver = FakeSolver()
        resumed = await coordinator.process_vision(event)
        assert resumed.decision is not None
        assert resumed.decision.status == "ready"
        assert resumed.decision.decision_id == interrupted.decision_id
        assert resumed.decision.attempts == 2
    finally:
        await store.close()
