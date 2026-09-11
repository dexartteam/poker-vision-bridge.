import asyncio

import httpx
import pytest
from pydantic import SecretStr

from app.vision.api import VisionSettings, create_app
from app.vision.provider import MockProvider


@pytest.fixture
async def client():
    app = create_app(
        VisionSettings(receiver_token=SecretStr("test-receiver")),
        provider_override=MockProvider(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app


async def session(client, profile):
    response = await client.post(
        "/v1/vision/sessions",
        headers={"Authorization": "Bearer test-receiver"},
        json={"profile": profile.model_dump(), "capture_epoch": "epoch-0001"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["session_id"], {"X-Vision-Session": body["session_token"]}


async def test_vision_api_auth_and_session_ownership(client, profile):
    c, app = client
    response = await c.post(
        "/v1/vision/sessions",
        json={"profile": profile.model_dump(), "capture_epoch": "epoch-0001"},
    )
    assert response.status_code == 401
    sid, headers = await session(c, profile)
    other, other_headers = await session(c, profile)
    assert (await c.get(f"/v1/vision/sessions/{sid}", headers=other_headers)).status_code == 401
    assert (await c.get(f"/v1/vision/sessions/{sid}", headers=headers)).status_code == 200


async def test_vision_api_frame_pipeline_and_recording_excludes_tokens(
    client, profile, frame_factory
):
    c, app = client
    sid, headers = await session(c, profile)
    frame = frame_factory(at=app.state.scheduler.clock())
    result = await c.post(
        f"/v1/vision/sessions/{sid}/frames", headers=headers, json=frame.model_dump()
    )
    assert result.status_code == 202 and result.json()["baseline_accepted"] is False
    # Yield to the real scheduler task without wall-clock sleeps.
    for _ in range(50):
        if app.state.scheduler.sessions[sid].outcomes:
            break
        await asyncio.sleep(0)
    state = (await c.get(f"/v1/vision/sessions/{sid}", headers=headers)).json()
    assert state["state"]["decision_ready"] is False and state["state"]["status"] == "uncertain"
    assert state["metrics"]["requests"] == 1
    recording = await c.get(f"/v1/vision/sessions/{sid}/recording", headers=headers)
    assert (
        headers["X-Vision-Session"] not in recording.text and "test-receiver" not in recording.text
    )
    assert any(e["type"] == "observation" for e in recording.json()["events"])
    assert recording.json()["detector_source_included"] is False


async def test_vision_api_has_no_bridge_execution_routes_or_services(client):
    c, app = client
    for path in ("/v1/events", "/v1/decisions", "/v1/stream"):
        assert (await c.post(path, json={})).status_code in (404, 405)
    assert (
        not hasattr(app.state, "coordinator")
        and not hasattr(app.state, "solver")
        and not hasattr(app.state, "openpoker")
    )


@pytest.mark.parametrize(
    "image",
    [
        "data:image/jpeg;base64,bm90LWltYWdl",
        "https://attacker.test/image.jpg",
        "data:image/svg+xml;base64,AAAA",
    ],
)
async def test_vision_api_rejects_non_image_inputs(client, profile, frame_factory, image):
    c, app = client
    sid, headers = await session(c, profile)
    frame = frame_factory(at=app.state.scheduler.clock()).model_dump()
    frame["image"] = image
    response = await c.post(f"/v1/vision/sessions/{sid}/frames", headers=headers, json=frame)
    assert response.status_code == 422 and app.state.scheduler.sessions[sid].requests == 0


async def test_vision_api_rejects_mismatched_image_dimensions(client, profile, frame_factory):
    c, app = client
    profile.width = 128
    sid, headers = await session(c, profile)
    response = await c.post(
        f"/v1/vision/sessions/{sid}/frames",
        headers=headers,
        json=frame_factory(at=app.state.scheduler.clock()).model_dump(),
    )
    assert response.status_code == 422


async def test_vision_api_body_limit_and_no_echo_of_invalid_input(client):
    c, app = client
    assert (await c.post("/v1/vision/sessions", content=b"x" * 2_850_001)).status_code == 413
    response = await c.post("/v1/vision/sessions", json={"secret": "DO-NOT-ECHO"})
    assert response.status_code == 422 and "DO-NOT-ECHO" not in response.text


async def test_vision_api_change_invalidation_and_profile_reset(client, profile):
    c, app = client
    sid, headers = await session(c, profile)
    response = await c.post(
        f"/v1/vision/sessions/{sid}/changes",
        headers=headers,
        json={
            "capture_epoch": "epoch-0001",
            "calibration_id": profile.calibration_id,
            "visual_revision": 3,
        },
    )
    assert response.status_code == 200
    state = (await c.get(f"/v1/vision/sessions/{sid}", headers=headers)).json()["state"]
    assert state["visual_revision"] == 3 and state["observation"] is None
    reset = await c.put(
        f"/v1/vision/sessions/{sid}",
        headers=headers,
        json={"profile": profile.model_dump(), "capture_epoch": "epoch-0002"},
    )
    assert reset.status_code == 200
    assert app.state.scheduler.sessions[sid].state["visual_revision"] == 0


async def test_vision_api_close_removes_session(client, profile):
    c, app = client
    sid, headers = await session(c, profile)
    assert (await c.delete(f"/v1/vision/sessions/{sid}", headers=headers)).status_code == 200
    assert (await c.get(f"/v1/vision/sessions/{sid}", headers=headers)).status_code == 401


def test_vision_live_requires_key_and_private_receiver_token(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_app(VisionSettings(_env_file=None, vision_mode="live", openai_api_key=None))
    with pytest.raises(ValueError, match="private RECEIVER_TOKEN"):
        create_app(
            VisionSettings(
                _env_file=None,
                vision_mode="live",
                openai_api_key=SecretStr("test-secret"),
            )
        )
