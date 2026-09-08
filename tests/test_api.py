from __future__ import annotations

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.solver import FakeSolver
from app.telegram import CapturingNotifier


async def test_http_receiver_auth_and_batch(event_factory, tmp_path):
    settings = Settings(
        database_path=tmp_path / "api.db",
        receiver_token=SecretStr("receiver-secret"),
        sampling_secret=SecretStr("sampling-secret"),
        min_stable_frames=2,
    )
    app = create_app(
        settings,
        solver_override=FakeSolver(),
        notifier_override=CapturingNotifier(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = await client.post("/v1/events", json=event_factory(1).model_dump(mode="json"))
            assert unauthorized.status_code == 401

            response = await client.post(
                "/v1/events",
                headers={"Authorization": "Bearer receiver-secret"},
                json=[
                    event_factory(1).model_dump(mode="json"),
                    event_factory(2).model_dump(mode="json"),
                ],
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["results"][0]["decision"] is None
            assert payload["results"][1]["decision"]["status"] == "ready"

            listed = await client.get(
                "/v1/decisions",
                headers={"Authorization": "Bearer receiver-secret"},
            )
            assert listed.status_code == 200
            assert len(listed.json()) == 1

