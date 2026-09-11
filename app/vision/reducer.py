"""Pure whole-observation reconciliation; never splice dynamic fields across frames."""

from copy import deepcopy

from .contracts import Frame, Observation

TTL_MS = 5000


def initial_state(epoch: str, calibration: str) -> dict:
    return {
        "schema_version": "1.0",
        "capture_epoch": epoch,
        "calibration_id": calibration,
        "visual_revision": 0,
        "status": "unknown",
        "observation": None,
        "observed_at": None,
        "confirmed_frame_ids": [],
        "candidate": None,
        "candidate_frame_id": None,
        "candidate_at": None,
        "last_frame_seq": 0,
        "decision_ready": False,
        "blocking_reasons": ["consumer_profile_not_configured"],
        "action_history": {"status": "unknown", "items": []},
    }


def invalidate(state: dict, revision: int) -> dict:
    result = deepcopy(state)
    if revision > result["visual_revision"]:
        result.update(
            visual_revision=revision,
            status="uncertain",
            observation=None,
            observed_at=None,
            candidate=None,
            candidate_frame_id=None,
            candidate_at=None,
            confirmed_frame_ids=[],
        )
    return result


def view(state: dict, now: int) -> dict:
    result = deepcopy(state)
    if result["status"] == "confirmed" and now - result["observed_at"] > TTL_MS:
        result["status"] = "stale"
    result.pop("candidate", None)
    result.pop("candidate_frame_id", None)
    result.pop("candidate_at", None)
    result["needs_confirmation"] = (
        state["candidate"] is not None
        and state["status"] == "uncertain"
        and 0 <= now - state["candidate_at"] <= TTL_MS
    )
    return result


def complete(obs: Observation) -> bool:
    """Minimum readable dynamic group. Unknown optional semantics still block consumers."""
    return (
        obs.pot.value is not None
        and obs.dealer_seat is not None
        and obs.hero_turn is not None
        and all(c.status in ("visible", "empty") for c in obs.hero_cards + obs.board)
        and all(
            s.status != "unknown"
            and (
                s.status in ("empty", "sitting_out")
                or (s.stack.value is not None and s.street_bet.value is not None)
            )
            for s in obs.seats
        )
    )


def evidence_key(obs: dict) -> dict:
    return {k: v for k, v in obs.items() if k not in ("confidence", "warnings")}


def reduce_observation(state: dict, frame: Frame, obs: Observation, now: int) -> tuple[dict, bool]:
    s = frame.source
    if (
        s.capture_epoch != state["capture_epoch"]
        or s.calibration_id != state["calibration_id"]
        or s.visual_revision != state["visual_revision"]
        or s.frame_seq <= state["last_frame_seq"]
        or now - s.captured_at > TTL_MS
        or s.captured_at > now + 2000
    ):
        return state, False
    result = deepcopy(state)
    data = obs.model_dump()
    result["last_frame_seq"] = s.frame_seq
    if not frame.stable or obs.confidence < 0.9 or obs.street == "unknown" or not complete(obs):
        result.update(
            status="uncertain",
            candidate=None,
            candidate_frame_id=None,
            candidate_at=None,
            observation=data,
            observed_at=s.captured_at,
            confirmed_frame_ids=[],
        )
        return result, False
    same = (
        result["candidate"] is not None
        and evidence_key(result["candidate"]) == evidence_key(data)
        and result["candidate_frame_id"] != s.frame_id
        and s.captured_at > result["candidate_at"]
        and s.captured_at - result["candidate_at"] <= TTL_MS
    )
    if same:
        result.update(
            status="confirmed",
            observation=data,
            observed_at=s.captured_at,
            confirmed_frame_ids=[result["candidate_frame_id"], s.frame_id],
        )
    else:
        result.update(
            status="uncertain",
            observation=data,
            observed_at=s.captured_at,
            confirmed_frame_ids=[],
        )
    result.update(candidate=data, candidate_frame_id=s.frame_id, candidate_at=s.captured_at)
    return result, same
