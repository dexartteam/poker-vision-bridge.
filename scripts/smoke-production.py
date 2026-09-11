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
    cloud = client.get("/server.html")
    assert cloud.status_code == 200
    for asset in re.findall(r'(?:src|href)="(/assets/[^"]+)"', cloud.text):
        assert client.get(asset).status_code == 200
    assert client.get("/ocr/worker.min.js").status_code == 200
    for core in ("lstm", "simd-lstm", "relaxedsimd-lstm"):
        for suffix in ("wasm", "wasm.js"):
            response = client.head(f"/ocr/tesseract-core-{core}.{suffix}")
            assert response.status_code == 200 and int(response.headers["content-length"]) > 0
    response = client.head("/ocr/eng.traineddata.gz")
    assert response.status_code == 200 and int(response.headers["content-length"]) > 0
    assert client.get("/v1/vision/status").json()["mode"] == "mock"
    assert client.post("/v1/events", json={}).status_code in (404, 405)
print("Production ASGI and built assets passed")
