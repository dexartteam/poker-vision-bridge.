import asyncio

import pytest

from app.vision.provider import RecognitionError
from app.vision.scheduler import Scheduler


class Provider:
    def __init__(self, observation):
        self.observation = observation
        self.calls = []
        self.error = None

    async def recognize(self, frame, profile):
        self.calls.append(frame.source.frame_id)
        if self.error:
            raise RecognitionError(self.error)
        return self.observation, {"fixture": True}

    async def close(self):
        pass


def build(observation):
    clock = [1000]
    provider = Provider(observation)
    return (
        Scheduler(provider, clock=lambda: clock[0], monotonic=lambda: clock[0]),
        provider,
        clock,
    )


async def test_scheduler_one_pending_replaces_older_frames(profile, frame_factory, observation):
    scheduler, provider, clock = build(observation)
    session = scheduler.create(profile, "epoch-0001")
    for i in range(1, 5):
        scheduler.enqueue(session, frame_factory(i))
    assert len(scheduler.pending) == 1
    await scheduler.run_one()
    assert provider.calls == ["frame-00000004"]
    assert session.seen["frame-00000001"] == "replaced"


async def test_scheduler_same_frame_is_idempotent_even_after_completion(
    profile, frame_factory, observation
):
    scheduler, provider, clock = build(observation)
    session = scheduler.create(profile, "epoch-0001")
    frame = frame_factory()
    scheduler.enqueue(session, frame)
    await scheduler.run_one()
    assert scheduler.enqueue(session, frame) == "recognized"
    assert not scheduler.pending and len(provider.calls) == 1


async def test_scheduler_two_identical_images_can_confirm_with_distinct_captures(
    profile, frame_factory, observation
):
    scheduler, provider, clock = build(observation)
    session = scheduler.create(profile, "epoch-0001")
    scheduler.enqueue(session, frame_factory())
    await scheduler.run_one()
    clock[0] = 2000
    scheduler.enqueue(session, frame_factory(2, 2000, reason="confirmation"))
    await scheduler.run_one()
    assert session.state["status"] == "confirmed" and len(provider.calls) == 2


async def test_scheduler_global_limit_counts_all_reasons_and_attempts(
    profile, frame_factory, observation
):
    scheduler, provider, clock = build(observation)
    scheduler.min_interval_ms = 0
    scheduler.limit = 2
    first = scheduler.create(profile, "epoch-0001")
    second = scheduler.create(profile, "epoch-0001")
    scheduler.enqueue(first, frame_factory())
    await scheduler.run_one()
    scheduler.enqueue(second, frame_factory())
    await scheduler.run_one()
    scheduler.enqueue(first, frame_factory(2, reason="manual"))
    assert not await scheduler.run_one() and scheduler.delay_ms() == 60000
    clock[0] = 61000
    assert scheduler.delay_ms() == 0


async def test_scheduler_min_interval_is_global(profile, frame_factory, observation):
    scheduler, provider, clock = build(observation)
    s = scheduler.create(profile, "epoch-0001")
    scheduler.enqueue(s, frame_factory())
    await scheduler.run_one()
    clock[0] = 1500
    assert scheduler.delay_ms() == 500


async def test_scheduler_timeout_uses_backoff_without_accepting_baseline(
    profile, frame_factory, observation
):
    scheduler, provider, clock = build(observation)
    s = scheduler.create(profile, "epoch-0001")
    provider.error = "provider_timeout"
    scheduler.enqueue(s, frame_factory())
    await scheduler.run_one()
    assert s.state["observation"] is None and s.errors == 1 and len(scheduler.attempts) == 1
    assert scheduler.delay_ms() == 2000 and s.outcomes[-1]["baseline_accepted"] is False


async def test_scheduler_active_slot_held_and_old_reply_fenced_after_change(
    profile, frame_factory, observation
):
    scheduler, provider, clock = build(observation)
    s = scheduler.create(profile, "epoch-0001")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(frame, profile):
        entered.set()
        await release.wait()
        return observation, {}

    provider.recognize = slow
    scheduler.enqueue(s, frame_factory())
    task = asyncio.create_task(scheduler.run_one())
    await entered.wait()
    scheduler.change(s, 1)
    scheduler.enqueue(s, frame_factory(2, revision=1))
    clock[0] = 2000
    assert not await scheduler.run_one() and scheduler.active is not None
    release.set()
    await task
    assert s.state["observation"] is None and not s.outcomes[-1]["baseline_accepted"]


async def test_scheduler_new_epoch_not_blocked_by_active_old_sequence(
    profile, frame_factory, observation
):
    scheduler, provider, clock = build(observation)
    s = scheduler.create(profile, "epoch-0001")
    scheduler.active = (s.id, frame_factory(100))
    scheduler.reset(s, profile, "epoch-0002")
    new = frame_factory()
    new.source.capture_epoch = "epoch-0002"
    assert scheduler.enqueue(s, new) == "queued"


async def test_scheduler_stale_pending_expires_without_charging(
    profile, frame_factory, observation
):
    scheduler, provider, clock = build(observation)
    s = scheduler.create(profile, "epoch-0001")
    scheduler.enqueue(s, frame_factory())
    clock[0] = 7000
    await scheduler.run_one()
    assert not provider.calls and s.outcomes[-1]["status"] == "expired_capture"


def test_scheduler_revision_mismatch_and_out_of_order_rejected(profile, frame_factory, observation):
    scheduler, provider, clock = build(observation)
    s = scheduler.create(profile, "epoch-0001")
    with pytest.raises(ValueError, match="obsolete_source"):
        scheduler.enqueue(s, frame_factory(revision=1))
    scheduler.enqueue(s, frame_factory(2))
    with pytest.raises(ValueError, match="out_of_order"):
        scheduler.enqueue(s, frame_factory(1))


def test_scheduler_session_and_recording_memory_are_bounded(profile, observation):
    scheduler, provider, clock = build(observation)
    for _ in range(8):
        s = scheduler.create(profile, "epoch-0001")
    with pytest.raises(ValueError, match="session_limit"):
        scheduler.create(profile, "epoch-0001")
    for _ in range(120):
        s.record({"type": "test", "value": "x" * 200000})
    assert s.record_bytes <= 16000000 and s.dropped_records > 0 and len(s.records) <= 100


def test_scheduler_invalidation_discards_pending_old_revision(profile, frame_factory, observation):
    scheduler, provider, clock = build(observation)
    s = scheduler.create(profile, "epoch-0001")
    scheduler.enqueue(s, frame_factory())
    scheduler.change(s, 1)
    assert not scheduler.pending and s.outcomes[-1]["status"] == "superseded"
