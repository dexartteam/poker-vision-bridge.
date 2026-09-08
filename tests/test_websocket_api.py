from __future__ import annotations

import warnings
from pathlib import Path
from threading import Event

import pytest
from pydantic import SecretStr
from starlette.exceptions import StarletteDeprecationWarning
from starlette.websockets import WebSocketDisconnect

# Starlette supports the installed httpx fallback, but warns at import time
# unless its optional httpx2 dependency is installed.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.solver import FakeSolver
from app.telegram import CapturingNotifier


AUTH_HEADERS = {"Authorization": "Bearer receiver-secret"}


def websocket_app(database_path: Path):
    settings = Settings(
        database_path=database_path,
        receiver_token=SecretStr("receiver-secret"),
        sampling_secret=SecretStr("sampling-secret"),
        min_stable_frames=2,
    )
    return create_app(
        settings,
        solver_override=FakeSolver(),
        notifier_override=CapturingNotifier(),
    )


def test_stream_rejects_an_unauthenticated_client(tmp_path):
    with TestClient(websocket_app(tmp_path / "unauthorized.db")) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/v1/stream"):
                pass

    assert exc_info.value.code == 4401
    assert exc_info.value.reason == "invalid receiver token"


def test_stream_recovers_after_an_invalid_event(event_factory, tmp_path):
    with TestClient(websocket_app(tmp_path / "recovery.db")) as client:
        with client.websocket_connect(
            "/v1/stream",
            headers=AUTH_HEADERS,
        ) as websocket:
            websocket.send_text("{}")
            error = websocket.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_event"

            websocket.send_text(event_factory(1).model_dump_json())
            recovered = websocket.receive_json()

    assert recovered["results"][0]["status"] == "accepted"
    assert recovered["results"][0]["decision"] is None


def test_two_stable_events_produce_a_decision(event_factory, tmp_path):
    with TestClient(websocket_app(tmp_path / "stability.db")) as client:
        with client.websocket_connect(
            "/v1/stream",
            headers=AUTH_HEADERS,
        ) as websocket:
            websocket.send_text(event_factory(1).model_dump_json())
            first = websocket.receive_json()

            websocket.send_text(event_factory(2).model_dump_json())
            second = websocket.receive_json()

    assert first["results"][0]["status"] == "accepted"
    assert first["results"][0]["decision"] is None
    assert second["results"][0]["status"] == "accepted"
    assert second["results"][0]["decision"]["status"] == "ready"


def test_decision_stream_delivers_a_completed_decision(event_factory, tmp_path):
    app = websocket_app(tmp_path / "decisions.db")
    with TestClient(app) as client:
        subscribed = Event()
        subscribe = app.state.broker.subscribe

        def tracked_subscribe():
            queue = subscribe()
            subscribed.set()
            return queue

        app.state.broker.subscribe = tracked_subscribe
        with client.websocket_connect(
            "/v1/decision-stream",
            headers=AUTH_HEADERS,
        ) as decisions:
            assert subscribed.wait(timeout=1)
            with client.websocket_connect(
                "/v1/stream",
                headers=AUTH_HEADERS,
            ) as events:
                events.send_text(event_factory(1).model_dump_json())
                first = events.receive_json()
                assert first["results"][0]["decision"] is None

                events.send_text(event_factory(2).model_dump_json())
                completed = events.receive_json()
                published = decisions.receive_json()

    response_decision = completed["results"][0]["decision"]
    assert response_decision["status"] == "ready"
    assert published["decision_id"] == response_decision["decision_id"]
    assert published["status"] == "ready"
