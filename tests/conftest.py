from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import VisionEvent


SAMPLE = Path(__file__).parents[1] / "examples" / "preflop_observation.json"


@pytest.fixture
def event_factory():
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))

    def make(seq: int = 1, *, event_id: str | None = None) -> VisionEvent:
        item = copy.deepcopy(raw)
        item["event_id"] = event_id or f"test-event-{seq:08d}"
        item["source"]["boot_id"] = "test-boot"
        item["source"]["seq"] = seq
        item["frame"]["seq"] = seq
        item["frame"]["hash"] = f"sha256:test-frame-{seq:08d}"
        item["captured_at"] = datetime.now(timezone.utc).isoformat()
        return VisionEvent.model_validate(item)

    return make
