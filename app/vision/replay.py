"""Deterministic reducer replay. No network and no provider calls."""

import argparse
import json
from pathlib import Path

from .contracts import Frame, Observation
from .reducer import invalidate, reduce_observation, view


def replay(recording: dict) -> dict:
    if recording.get("schema_version") != "1.0":
        raise ValueError("unsupported_recording_version")
    state = None
    last_at = 0
    processed = 0
    for event in recording["events"]:
        last_at = event["at"]
        if event["type"] == "reset":
            state = event["state"]
        elif state is None:
            # Bounded recordings may begin mid-session. A recorded post-reducer state is a checkpoint.
            if event["type"] == "observation":
                state = event["state"]
        elif event["type"] == "change":
            state = invalidate(state, event["revision"])
        elif event["type"] == "observation":
            state, _ = reduce_observation(
                state,
                Frame.model_validate(event["frame"]),
                Observation.model_validate(event["observation"]),
                last_at,
            )
            if state != event["state"]:
                raise ValueError("replay_state_mismatch")
            processed += 1
    return {
        "state": view(state, last_at) if state else None,
        "observations_replayed": processed,
        "truncated": recording.get("dropped_records", 0) > 0,
        "detector_replay_available": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    if args.recording.stat().st_size > 20_000_000:
        raise SystemExit("recording_too_large")
    result = replay(json.loads(args.recording.read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
