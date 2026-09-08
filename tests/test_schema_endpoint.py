from __future__ import annotations

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.solver import FakeSolver
from app.telegram import CapturingNotifier


async def test_authenticated_vision_schema_endpoint(tmp_path):
    settings = Settings(
        database_path=tmp_path / "schema.db",
        receiver_token=SecretStr("receiver-secret"),
    )
    app = create_app(
        settings,
        solver_override=FakeSolver(),
        notifier_override=CapturingNotifier(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            unauthorized = await client.get("/v1/schema/vision-event")
            assert unauthorized.status_code == 401

            response = await client.get(
                "/v1/schema/vision-event",
                headers={"Authorization": "Bearer receiver-secret"},
            )
            assert response.status_code == 200
            schema = response.json()
            assert schema["title"] == "VisionEvent"
            assert "observation" in schema["required"]
