"""Check built static assets through the actual ASGI app, without browser or live API."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.vision.api import VisionSettings, create_app

assert (Path(__file__).parents[1] / "app/vision/static/index.html").exists(), (
    "Run npm run build first"
)
app = create_app(VisionSettings(_env_file=None, vision_mode="mock"))
with TestClient(app) as client:
    response = client.get("/")
    assert response.status_code == 200 and 'lang="ru"' in response.text
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', response.text)
    assert len(assets) >= 2
    for asset in assets:
        assert client.get(asset).status_code == 200
    assert client.get("/v1/vision/status").json()["mode"] == "mock"
    assert client.post("/v1/events", json={}).status_code in (404, 405)
print("Production ASGI and built assets passed")
