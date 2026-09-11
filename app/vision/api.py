"""Standalone first-stage app: uvicorn app.vision.api:app --port 8001 --workers 1."""

from __future__ import annotations

import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .contracts import Change, Frame, Observation, Profile, StrictModel
from .provider import MockProvider, OpenAIProvider
from .scheduler import Scheduler


class VisionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    receiver_token: SecretStr = SecretStr("change-me-local-only")
    vision_mode: Literal["mock", "live"] = "mock"
    openai_api_key: SecretStr | None = None
    openai_vision_model: str = "gpt-4.1-mini"
    vision_timeout_seconds: float = Field(default=15, gt=0, le=60)


class Start(StrictModel):
    profile: Profile
    capture_epoch: str = Field(min_length=8, max_length=80)


def validate_image(frame: Frame, profile: Profile):
    try:
        prefix, encoded = frame.image.split(",", 1)
        if prefix not in ("data:image/jpeg;base64", "data:image/png;base64"):
            raise ValueError()
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 2_000_000:
            raise ValueError()
        with Image.open(BytesIO(data)) as image:
            expected = "PNG" if "png" in prefix else "JPEG"
            if image.format != expected or image.size != (
                profile.width,
                profile.height,
            ):
                raise ValueError()
            image.verify()
    except (
        ValueError,
        binascii.Error,
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
    ):
        raise HTTPException(422, "invalid_image") from None


def create_app(
    settings: VisionSettings | None = None,
    *,
    provider_override=None,
    scheduler_override=None,
):
    settings = settings or VisionSettings()
    if settings.vision_mode == "live" and not provider_override:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for VISION_MODE=live")
        if len(
            settings.receiver_token.get_secret_value()
        ) < 24 or settings.receiver_token.get_secret_value() in (
            "change-me-local-only",
            "replace-with-a-long-random-value",
        ):
            raise ValueError(
                "Set a private RECEIVER_TOKEN (at least 24 characters) for live recognition"
            )

    @asynccontextmanager
    async def lifespan(app):
        provider = provider_override or (
            MockProvider()
            if settings.vision_mode == "mock"
            else OpenAIProvider(
                settings.openai_api_key.get_secret_value(),
                settings.openai_vision_model,
                settings.vision_timeout_seconds,
            )
        )
        scheduler = scheduler_override or Scheduler(provider)
        app.state.scheduler = scheduler
        scheduler.start()
        try:
            yield
        finally:
            await scheduler.close()

    app = FastAPI(title="Poker Vision — stage 1", lifespan=lifespan)

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        if request.method in ("POST", "PUT"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2_850_000:
                    return JSONResponse({"detail": "body_too_large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=()"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "detail": "invalid_request",
                "fields": [list(e["loc"]) for e in exc.errors()],
            },
            status_code=422,
        )

    def owned(sid: str, token: str | None):
        session = app.state.scheduler.sessions.get(sid)
        if not session or not token or not secrets.compare_digest(session.token, token):
            raise HTTPException(401, "invalid_session")
        session.touched_at = app.state.scheduler.clock()
        return session

    @app.get("/v1/vision/status")
    async def status():
        return {
            "schema_version": "1.0",
            "mode": settings.vision_mode,
            "model": settings.openai_vision_model if settings.vision_mode == "live" else None,
            "decision_ready": False,
            "ttl_ms": 5000,
        }

    @app.get("/v1/vision/schema")
    async def schema():
        return Observation.model_json_schema()

    @app.post("/v1/vision/sessions", status_code=201)
    async def create_session(start: Start, authorization: str | None = Header(default=None)):
        if not secrets.compare_digest(
            authorization or "", "Bearer " + settings.receiver_token.get_secret_value()
        ):
            raise HTTPException(401, "unauthorized")
        try:
            session = app.state.scheduler.create(start.profile, start.capture_epoch)
        except ValueError:
            raise HTTPException(429, "session_limit") from None
        return {"session_id": session.id, "session_token": session.token}

    @app.put("/v1/vision/sessions/{sid}")
    async def reset(sid: str, start: Start, x_vision_session: str | None = Header(default=None)):
        session = owned(sid, x_vision_session)
        if start.capture_epoch == session.state["capture_epoch"]:
            raise HTTPException(409, "new_capture_epoch_required")
        app.state.scheduler.reset(session, start.profile, start.capture_epoch)
        return {"status": "reset"}

    @app.delete("/v1/vision/sessions/{sid}")
    async def close_session(sid: str, x_vision_session: str | None = Header(default=None)):
        owned(sid, x_vision_session)
        app.state.scheduler.pending.pop(sid, None)
        app.state.scheduler.sessions.pop(sid, None)
        return {"status": "closed"}

    @app.post("/v1/vision/sessions/{sid}/changes")
    async def change(sid: str, event: Change, x_vision_session: str | None = Header(default=None)):
        session = owned(sid, x_vision_session)
        if (
            event.capture_epoch != session.state["capture_epoch"]
            or event.calibration_id != session.profile.calibration_id
        ):
            raise HTTPException(409, "obsolete_source")
        if event.visual_revision < session.state["visual_revision"]:
            raise HTTPException(409, "obsolete_revision")
        app.state.scheduler.change(session, event.visual_revision)
        return {"status": "invalidated"}

    @app.post("/v1/vision/sessions/{sid}/frames", status_code=202)
    async def recognize(
        sid: str, frame: Frame, x_vision_session: str | None = Header(default=None)
    ):
        session = owned(sid, x_vision_session)
        validate_image(frame, session.profile)
        try:
            result = app.state.scheduler.enqueue(session, frame)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {
            "frame_id": frame.source.frame_id,
            "status": result,
            "baseline_accepted": False,
        }

    @app.get("/v1/vision/sessions/{sid}")
    async def state(sid: str, x_vision_session: str | None = Header(default=None)):
        return app.state.scheduler.status(owned(sid, x_vision_session))

    @app.get("/v1/vision/sessions/{sid}/recording")
    async def recording(sid: str, x_vision_session: str | None = Header(default=None)):
        session = owned(sid, x_vision_session)
        return {
            "schema_version": "1.0",
            "mode": settings.vision_mode,
            "model": settings.openai_vision_model if settings.vision_mode == "live" else None,
            "profile": session.profile.model_dump(),
            "dropped_records": session.dropped_records,
            "queue_at_export": app.state.scheduler.status(session)["queue"],
            "detector_source_included": False,
            "events": [e for e, _ in session.records],
        }

    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="console")
    return app


app = create_app()
