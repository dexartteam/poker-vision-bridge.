from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.config import Settings
from app.coordinator import Coordinator, DecisionBroker
from app.models import IngestResponse, VisionEvent
from app.openpoker import OpenPokerRunner
from app.solver import build_solver
from app.store import Store
from app.telegram import TelegramNotifier


logger = logging.getLogger(__name__)
EVENT_INPUT = TypeAdapter(VisionEvent | list[VisionEvent])


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def create_app(
    settings: Settings | None = None,
    *,
    solver_override: Any | None = None,
    notifier_override: Any | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()
    logging.basicConfig(
        level=getattr(logging, runtime_settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime_settings.ensure_directories()
        async with AsyncExitStack() as stack:
            store = Store(runtime_settings.database_path)
            await store.open()
            stack.push_async_callback(store.close)

            solver = solver_override or build_solver(runtime_settings)
            stack.push_async_callback(solver.close)

            notifier = notifier_override or TelegramNotifier(runtime_settings)
            close_notifier = getattr(notifier, "close", None)
            if close_notifier is not None:
                stack.push_async_callback(close_notifier)

            broker = DecisionBroker()
            coordinator = Coordinator(
                settings=runtime_settings,
                store=store,
                solver=solver,
                notifier=notifier,
                broker=broker,
            )
            openpoker = OpenPokerRunner(runtime_settings, coordinator)
            coordinator.set_executor(openpoker)
            await openpoker.start()
            stack.push_async_callback(openpoker.stop)

            app.state.settings = runtime_settings
            app.state.store = store
            app.state.solver = solver
            app.state.notifier = notifier
            app.state.coordinator = coordinator
            app.state.broker = broker
            app.state.openpoker = openpoker
            yield

    app = FastAPI(
        title="Poker Vision Bridge",
        version="0.1.0",
        description=(
            "Receives stable bot-only poker observations, requests PokerAI strategy, "
            "publishes decisions, and optionally executes through Open Poker's official WebSocket."
        ),
        lifespan=lifespan,
    )

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        supplied = _bearer_token(authorization)
        expected = runtime_settings.receiver_token.get_secret_value()
        if supplied is None or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid receiver token")

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "solver_mode": runtime_settings.solver_mode,
            "decision_source": runtime_settings.decision_source,
            "openpoker_enabled": runtime_settings.openpoker_enabled,
            "auto_execute": runtime_settings.auto_execute,
        }

    @app.post("/v1/events", response_model=IngestResponse, dependencies=[Depends(require_token)])
    async def ingest(body: VisionEvent | list[VisionEvent]) -> IngestResponse:
        events = body if isinstance(body, list) else [body]
        if not 1 <= len(events) <= 100:
            raise HTTPException(status_code=422, detail="batch must contain 1..100 events")
        results = []
        for event in events:
            results.append(await app.state.coordinator.process_vision(event))
        return IngestResponse(results=results)

    @app.get("/v1/projections/{source_kind}/{source_id}/{table_id}", dependencies=[Depends(require_token)])
    async def projection(source_kind: str, source_id: str, table_id: str) -> dict[str, Any]:
        if source_kind not in {"vision", "platform"}:
            raise HTTPException(status_code=422, detail="source_kind must be vision|platform")
        record = await app.state.store.get_projection(source_kind, source_id, table_id)
        if record is None:
            raise HTTPException(status_code=404, detail="projection not found")
        return {
            "source_kind": record.source_kind,
            "source_id": record.source_id,
            "table_id": record.table_id,
            "hand_id": record.hand_id,
            "source_seq": record.source_seq,
            "state_hash": record.state_hash,
            "stable_count": record.stable_count,
            "observation": record.observation,
            "confidence": record.confidence,
            "updated_at": record.updated_at,
        }

    @app.get("/v1/decisions", dependencies=[Depends(require_token)])
    async def decisions(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:
        rows = await app.state.store.list_decisions(limit)
        return [row.model_dump(mode="json") for row in rows]

    @app.get("/v1/decisions/{decision_id}", dependencies=[Depends(require_token)])
    async def decision(decision_id: str) -> dict[str, Any]:
        row = await app.state.store.get_decision(decision_id)
        if row is None:
            raise HTTPException(status_code=404, detail="decision not found")
        return row.model_dump(mode="json")

    @app.get("/v1/openpoker/status", dependencies=[Depends(require_token)])
    async def openpoker_status() -> dict[str, Any]:
        return app.state.openpoker.status()

    @app.get("/v1/schema/vision-event", dependencies=[Depends(require_token)])
    async def vision_event_schema() -> dict[str, Any]:
        return VisionEvent.model_json_schema()

    async def websocket_auth(websocket: WebSocket) -> bool:
        header_token = _bearer_token(websocket.headers.get("authorization"))
        query_token = websocket.query_params.get("token")
        supplied = header_token or query_token
        expected = runtime_settings.receiver_token.get_secret_value()
        if supplied is None or not secrets.compare_digest(supplied, expected):
            await websocket.close(code=4401, reason="invalid receiver token")
            return False
        return True

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        if not await websocket_auth(websocket):
            return
        await websocket.accept()
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    parsed = EVENT_INPUT.validate_json(raw)
                    events = parsed if isinstance(parsed, list) else [parsed]
                    if not 1 <= len(events) <= 100:
                        raise ValueError("batch must contain 1..100 events")
                    results = [
                        await app.state.coordinator.process_vision(event) for event in events
                    ]
                    await websocket.send_text(IngestResponse(results=results).model_dump_json())
                except (ValidationError, ValueError) as exc:
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_event", "detail": str(exc)}
                    )
        except WebSocketDisconnect:
            return

    @app.websocket("/v1/decision-stream")
    async def decision_stream(websocket: WebSocket) -> None:
        if not await websocket_auth(websocket):
            return
        await websocket.accept()
        queue = app.state.broker.subscribe()
        queue_task = asyncio.create_task(queue.get())
        receive_task = asyncio.create_task(websocket.receive())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {queue_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task in done:
                    message = receive_task.result()
                    if message["type"] == "websocket.disconnect":
                        return
                    receive_task = asyncio.create_task(websocket.receive())
                if queue_task in done:
                    item = queue_task.result()
                    await websocket.send_text(item.model_dump_json())
                    queue_task = asyncio.create_task(queue.get())
        except WebSocketDisconnect:
            return
        finally:
            for task in (queue_task, receive_task):
                task.cancel()
            await asyncio.gather(queue_task, receive_task, return_exceptions=True)
            app.state.broker.unsubscribe(queue)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
