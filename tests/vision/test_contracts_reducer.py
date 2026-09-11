import pytest
from pydantic import ValidationError

from app.vision.contracts import Amount, Card, Observation
from app.vision.reducer import initial_state, invalidate, reduce_observation, view


def state():
    return initial_state("epoch-0001", "calibration-1")


def test_contract_unknown_amount_is_distinct_from_zero():
    assert Amount(value=None, raw=None).value is None
    assert Amount(value=0, raw="0").value == 0
    with pytest.raises(ValidationError):
        Amount(value=1.25, raw="1.25")
    with pytest.raises(ValidationError):
        Amount(value=-1, raw="-1")


@pytest.mark.parametrize(
    "status,value",
    [("hidden", "As"), ("visible", None), ("empty", "Kh"), ("visible", "10s")],
)
def test_contract_rejects_inconsistent_cards(status, value):
    with pytest.raises(ValidationError):
        Card(status=status, value=value)


def test_contract_rejects_duplicate_cards_and_seats(observation):
    raw = observation.model_dump()
    raw["hero_cards"][1]["value"] = "As"
    with pytest.raises(ValidationError):
        Observation.model_validate(raw)
    raw = observation.model_dump()
    raw["seats"][1]["seat"] = 0
    with pytest.raises(ValidationError):
        Observation.model_validate(raw)


def test_contract_rejects_street_mismatch(observation):
    raw = observation.model_dump()
    raw["street"] = "flop"
    with pytest.raises(ValidationError):
        Observation.model_validate(raw)


def test_reducer_requires_two_distinct_fresh_captures(frame_factory, observation):
    first, accepted = reduce_observation(state(), frame_factory(), observation, 1000)
    assert not accepted and first["status"] == "uncertain"
    duplicate, accepted = reduce_observation(first, frame_factory(), observation, 1001)
    assert not accepted and duplicate == first
    second, accepted = reduce_observation(first, frame_factory(2, 2000), observation, 2000)
    assert accepted and second["status"] == "confirmed"
    assert second["decision_ready"] is False and second["action_history"]["status"] == "unknown"
    assert state()["observation"] is None


def test_reducer_change_immediately_invalidates_and_old_reply_cannot_confirm(
    frame_factory, observation
):
    first, _ = reduce_observation(state(), frame_factory(), observation, 1000)
    changed = invalidate(first, 1)
    assert changed["observation"] is None
    result, accepted = reduce_observation(changed, frame_factory(2, 2000), observation, 2000)
    assert result == changed and not accepted


def test_reducer_capture_epoch_and_calibration_fence_old_results(frame_factory, observation):
    frame = frame_factory()
    frame.source.capture_epoch = "epoch-old"
    assert reduce_observation(state(), frame, observation, 1000) == (state(), False)
    frame = frame_factory()
    frame.source.calibration_id = "calibration-old"
    assert reduce_observation(state(), frame, observation, 1000) == (state(), False)


def test_reducer_ttl_does_not_refresh_on_heartbeat(frame_factory, observation):
    first, _ = reduce_observation(state(), frame_factory(), observation, 1000)
    second, _ = reduce_observation(first, frame_factory(2, 2000), observation, 2000)
    assert view(second, 7000)["status"] == "confirmed"
    assert view(second, 7001)["status"] == "stale"
    assert view(second, 9000)["observed_at"] == 2000


def test_reducer_old_and_future_capture_rejected(frame_factory, observation):
    assert reduce_observation(state(), frame_factory(), observation, 7000) == (
        state(),
        False,
    )
    assert reduce_observation(state(), frame_factory(at=5000), observation, 1000) == (
        state(),
        False,
    )


def test_reducer_unstable_and_low_confidence_never_confirm(frame_factory, observation):
    first, _ = reduce_observation(state(), frame_factory(), observation, 1000)
    result, accepted = reduce_observation(
        first, frame_factory(2, 2000, stable=False), observation, 2000
    )
    assert not accepted and result["status"] == "uncertain"
    observation.confidence = 0.4
    assert not reduce_observation(first, frame_factory(2, 2000), observation, 2000)[1]


def test_reducer_does_not_splice_dynamic_groups(frame_factory, observation):
    first, _ = reduce_observation(state(), frame_factory(), observation, 1000)
    observation.pot.value = 100
    observation.actor_seat = 3
    result, accepted = reduce_observation(first, frame_factory(2, 2000), observation, 2000)
    assert not accepted and result["observation"]["pot"]["value"] == 100
    assert result["observation"]["actor_seat"] == 3


def test_reducer_two_frames_far_apart_are_not_confirmation(frame_factory, observation):
    first, _ = reduce_observation(state(), frame_factory(), observation, 1000)
    result, accepted = reduce_observation(first, frame_factory(2, 7000), observation, 7000)
    assert not accepted and result["status"] == "uncertain"


def test_reducer_null_critical_fields_cannot_confirm(frame_factory, observation):
    observation.pot.value = None
    first, _ = reduce_observation(state(), frame_factory(), observation, 1000)
    second, accepted = reduce_observation(first, frame_factory(2, 2000), observation, 2000)
    assert (
        not accepted
        and second["status"] == "uncertain"
        and not view(second, 2000)["needs_confirmation"]
    )


def test_reducer_stale_does_not_trigger_confirmation_loop(frame_factory, observation):
    first, _ = reduce_observation(state(), frame_factory(), observation, 1000)
    second, _ = reduce_observation(first, frame_factory(2, 2000), observation, 2000)
    assert view(second, 8000)["status"] == "stale" and not view(second, 8000)["needs_confirmation"]
