import pytest

from app.vision.reducer import view
from app.vision.replay import replay
from app.vision.scheduler import Scheduler


async def test_replay_reconstructs_exact_state_and_transition(profile, frame_factory, observation):
    class Provider:
        async def recognize(self, frame, profile):
            return observation, {"fixture": True}

    clock = [1000]
    scheduler = Scheduler(Provider(), clock=lambda: clock[0], monotonic=lambda: clock[0])
    s = scheduler.create(profile, "epoch-0001")
    scheduler.enqueue(s, frame_factory())
    await scheduler.run_one()
    clock[0] = 2000
    scheduler.enqueue(s, frame_factory(2, 2000, reason="confirmation"))
    await scheduler.run_one()
    scheduler.change(s, 1)
    recording = {
        "schema_version": "1.0",
        "dropped_records": 0,
        "events": [e for e, _ in s.records],
    }
    first = replay(recording)
    second = replay(recording)
    assert first == second and first["state"] == view(s.state, 2000)
    assert first["observations_replayed"] == 2 and not first["detector_replay_available"]


def test_replay_rejects_unknown_schema():
    with pytest.raises(ValueError, match="unsupported_recording_version"):
        replay({"schema_version": "2.0"})
