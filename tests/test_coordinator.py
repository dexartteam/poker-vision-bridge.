from __future__ import annotations

from datetime import datetime, timezone

from pydantic import SecretStr

from app.config import Settings
from app.coordinator import Coordinator
from app.solver import FakeSolver
from app.store import Store
from app.telegram import CapturingNotifier


class CountingSolver(FakeSolver):
    def __init__(self) -> None:
        self.calls = 0

    async def solve(self, request):
        self.calls += 1
        return await super().solve(request)


async def test_stability_and_duplicate_suppression(event_factory, tmp_path):
    settings = Settings(
        database_path=tmp_path / "test.db",
        receiver_token=SecretStr("test-token"),
        sampling_secret=SecretStr("sample-secret"),
        min_stable_frames=2,
    )
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    notifier = CapturingNotifier()
    coordinator = Coordinator(
        settings=settings,
        store=store,
        solver=solver,
        notifier=notifier,
    )
    try:
        first = await coordinator.process_vision(event_factory(1))
        assert first.status == "accepted"
        assert first.decision is None
        assert "1/2" in (first.reason or "")

        second_event = event_factory(2)
        second = await coordinator.process_vision(second_event)
        assert second.decision is not None
        assert second.decision.status == "ready"
        assert second.decision.execution_status == "dry_run"
        assert solver.calls == 1
        assert len(notifier.messages) == 1

        duplicate = await coordinator.process_vision(second_event)
        assert duplicate.status == "duplicate"
        assert solver.calls == 1

        third = await coordinator.process_vision(event_factory(3))
        assert third.decision is not None
        assert third.decision.decision_id == second.decision.decision_id
        assert solver.calls == 1
    finally:
        await store.close()


async def test_low_confidence_never_calls_solver(event_factory, tmp_path):
    settings = Settings(database_path=tmp_path / "test.db", min_stable_frames=1)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = Coordinator(
        settings=settings,
        store=store,
        solver=solver,
        notifier=CapturingNotifier(),
    )
    event = event_factory(1)
    event = event.model_copy(
        update={"confidence": event.confidence.model_copy(update={"overall": 0.5})}
    )
    try:
        result = await coordinator.process_vision(event)
        assert result.decision is None
        assert "confidence" in (result.reason or "")
        assert solver.calls == 0
    finally:
        await store.close()


async def test_missing_critical_confidence_never_calls_solver(event_factory, tmp_path):
    settings = Settings(database_path=tmp_path / "test.db", min_stable_frames=1)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = Coordinator(
        settings=settings,
        store=store,
        solver=solver,
        notifier=CapturingNotifier(),
    )
    event = event_factory(1)
    event = event.model_copy(
        update={"confidence": event.confidence.model_copy(update={"fields": {}})}
    )
    try:
        result = await coordinator.process_vision(event)
        assert result.decision is None
        assert "missing critical" in (result.reason or "")
        assert solver.calls == 0
    finally:
        await store.close()


async def test_stale_observation_never_calls_solver(event_factory, tmp_path):
    settings = Settings(database_path=tmp_path / "test.db", min_stable_frames=1)
    store = Store(settings.database_path)
    await store.open()
    solver = CountingSolver()
    coordinator = Coordinator(
        settings=settings,
        store=store,
        solver=solver,
        notifier=CapturingNotifier(),
    )
    event = event_factory(1).model_copy(
        update={"captured_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    )
    try:
        result = await coordinator.process_vision(event)
        assert result.decision is None
        assert "stale" in (result.reason or "")
        assert solver.calls == 0
    finally:
        await store.close()
