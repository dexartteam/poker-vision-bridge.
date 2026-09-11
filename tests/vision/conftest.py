import base64
from io import BytesIO

import pytest
from PIL import Image

from app.vision.contracts import Frame, Observation, Profile, unknown_observation


@pytest.fixture
def profile():
    return Profile(
        calibration_id="calibration-1",
        width=64,
        height=64,
        unit="chips",
        scale=0,
        hero_seat=0,
        regions=[dict(name="table", x=0.0, y=0.0, w=1.0, h=1.0, ignore=False)],
    )


@pytest.fixture
def frame_factory():
    out = BytesIO()
    Image.new("RGB", (64, 64), (30, 70, 40)).save(out, format="JPEG")
    image = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()

    def make(seq=1, at=1000, revision=0, **kwargs):
        return Frame.model_validate(
            dict(
                source=dict(
                    capture_epoch="epoch-0001",
                    calibration_id="calibration-1",
                    frame_id=f"frame-{seq:08}",
                    frame_seq=seq,
                    visual_revision=revision,
                    captured_at=at,
                ),
                reason=kwargs.pop("reason", "initial"),
                stable=kwargs.pop("stable", True),
                image=image,
                **kwargs,
            )
        )

    return make


@pytest.fixture
def observation():
    raw = unknown_observation().model_dump()
    raw.update(street="preflop", confidence=0.98, warnings=[], hand_number="1001")
    raw["hero_cards"] = [
        dict(status="visible", value="As"),
        dict(status="visible", value="Kh"),
    ]
    raw["board"] = [dict(status="empty", value=None) for _ in range(5)]
    raw["pot"] = {"value": 80, "raw": "80"}
    raw["dealer_seat"] = 1
    raw["hero_turn"] = False
    for seat in raw["seats"]:
        seat.update(
            status="active",
            stack={"value": 1000, "raw": "1000"},
            street_bet={"value": 0, "raw": "0"},
        )
    return Observation.model_validate(raw)
