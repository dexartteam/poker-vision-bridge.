"""uvicorn app.replay.api:app --host 127.0.0.1 --port 8002 --workers 1."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.solver import (
    PokerAIRequestPlanner,
    PokerAISolver,
    SolverError,
    SolverUnsupported,
    validate_selected_action,
)

from .contracts import Issue, ReplayRequest, ReplayResponse, ReplayState
from .diagnostics import diagnose
from .settings import ReplaySettings


def create_app(settings: ReplaySettings | None = None, *, solver_override=None) -> FastAPI:
    settings = settings or ReplaySettings()
    configured = bool(
        settings.pokerai_api_key and settings.pokerai_api_key.get_secret_value().strip()
    )
    planner = PokerAIRequestPlanner(settings)

    @asynccontextmanager
    async def lifespan(app):
        app.state.solver = solver_override or (PokerAISolver(settings) if configured else None)
        app.state.lock = asyncio.Lock()
        app.state.responses = OrderedDict()
        try:
            yield
        finally:
            if app.state.solver is not None:
                await app.state.solver.close()

    app = FastAPI(title="Poker video replay — diagnostics and comparison", lifespan=lifespan)
    if settings.replay_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.replay_allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        if request.method in {"POST", "PUT"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 256_000:
                    return JSONResponse({"detail": "body_too_large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=()"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Never reflect complete request values or credentials into an error response.
        return JSONResponse(
            {
                "detail": "invalid_request",
                "fields": [list(error["loc"]) for error in exc.errors()],
            },
            status_code=422,
        )

    def authenticate(authorization: str | None = Header(default=None)):
        if not secrets.compare_digest(
            authorization or "", "Bearer " + settings.receiver_token.get_secret_value()
        ):
            raise HTTPException(401, "invalid_receiver_token")

    @app.get("/healthz")
    @app.get("/v1/replay/status")
    async def status():
        return {
            "schema_version": "replay.response.v1",
            "provider": "pokerai.bet",
            "configured": configured,
            "execution": "disabled",
        }

    @app.get("/v1/replay/schema", dependencies=[Depends(authenticate)])
    async def schema():
        return ReplayState.model_json_schema()

    def safe_error(exc):
        message = str(exc)
        for secret in (settings.pokerai_api_key, settings.receiver_token):
            if secret and secret.get_secret_value():
                message = message.replace(secret.get_secret_value(), "[redacted]")
        return message[:1000]

    @app.post(
        "/v1/replay/analyze", response_model=ReplayResponse, dependencies=[Depends(authenticate)]
    )
    async def analyze(request: ReplayRequest):
        started = time.monotonic()
        response, state, prepared = diagnose(request, planner, configured=configured)
        if request.action == "diagnose" or response.status != "ready":
            response.elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            return response

        assert state is not None and prepared is not None and app.state.solver is not None
        # A retransmission of one request must not spend quota twice. No automatic solver retries.
        key = (request.session_id, request.request_id)
        digest = hashlib.sha256(
            json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        async with app.state.lock:
            saved = app.state.responses.get(key)
            if saved:
                if saved[0] != digest:
                    raise HTTPException(409, "request_id_reused_with_different_frame_or_context")
                app.state.responses.move_to_end(key)
                return saved[1]
            try:
                result = await app.state.solver.solve(prepared)
                response.solver_result = result
                total = sum(item.frequency for item in result.strategy)
                if abs(total - 1) > 0.01:
                    raise SolverError(f"strategy frequencies must sum to 1 (received {total:.6g})")
                response.status = "solved"
                for index, action in enumerate(result.strategy):
                    if action.frequency == 0:
                        continue
                    validated = validate_selected_action(action, state)
                    if not validated.valid:
                        response.status = "invalid"
                        response.issues.append(
                            Issue(
                                code="illegal_solver_action",
                                field=f"solver_result.strategy.{index}",
                                message=validated.invalid_reason or "Illegal solver action",
                                kind="invalid",
                            )
                        )
            except SolverUnsupported as exc:
                response.status = "unsupported"
                response.issues.append(
                    Issue(
                        code="solver_unsupported",
                        field="solver",
                        message=safe_error(exc),
                        kind="unsupported",
                    )
                )
            except SolverError as exc:
                response.status = "solver_failed"
                response.issues.append(
                    Issue(
                        code="solver_failed",
                        field="solver",
                        message=safe_error(exc),
                        kind="invalid",
                    )
                )
            except Exception:
                response.status = "solver_failed"
                response.issues.append(
                    Issue(
                        code="solver_failed",
                        field="solver",
                        message="Неожиданная ошибка ответа PokerAI; запрос автоматически не повторяется.",
                        kind="invalid",
                    )
                )
            response.elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            app.state.responses[key] = (digest, response)
            while len(app.state.responses) > 128:
                app.state.responses.popitem(last=False)
            return response

    static = Path(__file__).resolve().parents[1] / "vision" / "static"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="replay-console")
    return app


app = create_app()
