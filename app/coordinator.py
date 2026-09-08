from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.config import Settings
from app.models import (
    DecisionView,
    IngestItemResult,
    Observation,
    VisionEvent,
)
from app.solver import (
    PreparedRequest,
    Solver,
    SolverError,
    SolverUnsupported,
    select_mixed_action,
    validate_selected_action,
)
from app.store import ProjectionRecord, Store


logger = logging.getLogger(__name__)


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        normalized = "0" if value == 0 else format(value.normalize(), "f")
        return {"$decimal": normalized}
    if isinstance(value, dict):
        return {key: _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def canonical_observation_hash(observation: Observation) -> str:
    value = observation.model_dump(mode="python")
    value["seats"] = sorted(value["seats"], key=lambda seat: seat["seat"])
    value["legal_actions"] = sorted(
        value["legal_actions"],
        key=lambda action: json.dumps(
            _canonicalize(action),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return canonical_hash(value)


class Notifier(Protocol):
    async def send(self, decision: DecisionView, observation: Observation) -> None: ...


class Executor(Protocol):
    async def execute(
        self,
        decision: DecisionView,
        observation: Observation,
        source_kind: Literal["vision", "platform"],
    ) -> str: ...


class DecisionBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[DecisionView]] = set()

    def subscribe(self) -> asyncio.Queue[DecisionView]:
        queue: asyncio.Queue[DecisionView] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[DecisionView]) -> None:
        self._subscribers.discard(queue)

    def publish(self, decision: DecisionView) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(decision)


class Coordinator:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        solver: Solver,
        notifier: Notifier,
        executor: Executor | None = None,
        broker: DecisionBroker | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.solver = solver
        self.notifier = notifier
        self.executor = executor
        self.broker = broker or DecisionBroker()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def set_executor(self, executor: Executor) -> None:
        self.executor = executor

    async def process_vision(self, event: VisionEvent) -> IngestItemResult:
        return await self._process(event, "vision")

    async def process_platform(self, event: VisionEvent) -> IngestItemResult:
        return await self._process(event, "platform")

    async def _process(
        self,
        event: VisionEvent,
        source_kind: Literal["vision", "platform"],
    ) -> IngestItemResult:
        lock_key = f"{source_kind}:{event.source.agent_id}:{event.table.table_id}"
        async with self._locks[lock_key]:
            observation_json = event.observation.model_dump(mode="json")
            state_hash = canonical_observation_hash(event.observation)
            inserted = await self.store.insert_event(event, source_kind, state_hash)
            result = IngestItemResult(
                event_id=event.event_id,
                status=inserted.status,
                highest_seq=inserted.highest_seq,
                resync_required=inserted.resync_required,
                reason=inserted.reason,
            )
            if inserted.status == "rejected":
                return result

            previous = await self.store.get_projection(
                source_kind,
                event.source.agent_id,
                event.table.table_id,
            )
            if inserted.status == "duplicate" and previous is not None:
                same_boot = previous.boot_id == event.source.boot_id
                superseded_in_same_boot = same_boot and previous.source_seq > event.source.seq
                superseded_by_new_boot = (
                    not same_boot
                    and inserted.received_at is not None
                    and previous.updated_at > inserted.received_at
                )
                if superseded_in_same_boot or superseded_by_new_boot:
                    result.reason = "duplicate event was already superseded by a newer projection"
                    return result

            projection_is_current = (
                previous is not None
                and previous.boot_id == event.source.boot_id
                and previous.source_seq == event.source.seq
                and previous.frame_seq == event.frame.seq
                and previous.frame_hash == event.frame.hash
                and previous.hand_id == event.observation.hand_id
                and previous.state_hash == state_hash
            )
            if projection_is_current:
                stable_count = previous.stable_count
            else:
                stable_count = self._next_stable_count(
                    event,
                    source_kind,
                    state_hash,
                    previous,
                )
                await self.store.upsert_projection(
                    source_kind=source_kind,
                    source_id=event.source.agent_id,
                    table_id=event.table.table_id,
                    boot_id=event.source.boot_id,
                    frame_seq=event.frame.seq,
                    frame_hash=event.frame.hash,
                    hand_id=event.observation.hand_id,
                    source_seq=event.source.seq,
                    state_hash=state_hash,
                    stable_count=stable_count,
                    observation=observation_json,
                    confidence=event.confidence.model_dump(mode="json"),
                )

            if self.settings.decision_source != source_kind:
                return result
            not_ready = self._not_ready_reason(event, source_kind, stable_count)
            if not_ready:
                result.reason = not_ready
                return result

            decision = await self._decide(event, source_kind, state_hash)
            result.decision = decision
            return result

    def _next_stable_count(
        self,
        event: VisionEvent,
        source_kind: Literal["vision", "platform"],
        state_hash: str,
        previous: ProjectionRecord | None,
    ) -> int:
        if source_kind != "vision":
            same_candidate = (
                previous is not None
                and previous.boot_id == event.source.boot_id
                and previous.hand_id == event.observation.hand_id
                and previous.state_hash == state_hash
            )
            return previous.stable_count + 1 if same_candidate else 1

        if (
            not event.observation.hero_turn
            or not event.observation.action_history.complete
            or self._vision_quality_reason(event) is not None
        ):
            return 0

        same_candidate = (
            previous is not None
            and previous.boot_id == event.source.boot_id
            and previous.hand_id == event.observation.hand_id
            and previous.state_hash == state_hash
        )
        distinct_new_frame = previous is not None and event.frame.seq > previous.frame_seq
        previous_is_recent = (
            previous is not None
            and (datetime.now(timezone.utc) - previous.updated_at).total_seconds()
            <= self.settings.max_observation_age_seconds
        )
        if same_candidate and distinct_new_frame and previous_is_recent:
            return previous.stable_count + 1
        return 1

    def _vision_quality_reason(self, event: VisionEvent) -> str | None:
        age = (datetime.now(timezone.utc) - event.captured_at).total_seconds()
        if age > self.settings.max_observation_age_seconds:
            return (
                "observation stored; captured_at is stale "
                f"({age:.2f}s > {self.settings.max_observation_age_seconds:.2f}s)"
            )
        max_future_skew = getattr(self.settings, "max_future_clock_skew_seconds", 2.0)
        if age < -max_future_skew:
            return (
                "observation stored; captured_at is too far in the future "
                f"({-age:.2f}s > {max_future_skew:.2f}s)"
            )
        if event.confidence.overall < self.settings.min_vision_confidence:
            return "observation stored; overall confidence is below threshold"
        critical_names = {"hero.cards", "board", "pot", "hero_turn", "legal_actions"}
        missing = critical_names - set(event.confidence.fields)
        if missing:
            return "observation stored; missing critical confidence fields: " + ", ".join(
                sorted(missing)
            )
        if any(
            event.confidence.fields[name] < self.settings.min_vision_confidence
            for name in critical_names
        ):
            return "observation stored; a critical field is below confidence threshold"
        return None

    def _not_ready_reason(
        self,
        event: VisionEvent,
        source_kind: Literal["vision", "platform"],
        stable_count: int,
    ) -> str | None:
        obs = event.observation
        if not obs.hero_turn:
            return "observation stored; Hero is not acting"
        if not obs.action_history.complete:
            return "observation stored; ordered action history is incomplete"
        if source_kind == "vision":
            quality_reason = self._vision_quality_reason(event)
            if quality_reason:
                return quality_reason
            if stable_count < self.settings.min_stable_frames:
                return (
                    f"observation stored; waiting for stable frame "
                    f"{stable_count}/{self.settings.min_stable_frames}"
                )
        return None

    async def _decide(
        self,
        event: VisionEvent,
        source_kind: Literal["vision", "platform"],
        state_hash: str,
    ) -> DecisionView:
        obs = event.observation
        identity = {
            "source_kind": source_kind,
            "source_id": event.source.agent_id,
            "table_id": event.table.table_id,
            "hand_id": obs.hand_id,
            "street": obs.street.value,
            "turn_id": obs.turn_id,
            "state_hash": state_hash,
        }
        action_key = canonical_hash(identity)

        prepared: PreparedRequest | None = None
        prepare_error: Exception | None = None
        try:
            prepared = self.solver.prepare(obs)
            request_log = prepared.as_log_dict()
        except (SolverUnsupported, SolverError) as exc:
            prepare_error = exc
            request_log = {"kind": "unsupported", "reason": str(exc)}

        decision_id = str(uuid.uuid4())
        decision, created = await self.store.create_pending_decision(
            decision_id=decision_id,
            action_key=action_key,
            source_kind=source_kind,
            source_id=event.source.agent_id,
            table_id=event.table.table_id,
            hand_id=obs.hand_id,
            street=obs.street,
            state_hash=state_hash,
            solver_request=request_log,
        )
        if not created:
            return decision
        # A retry reclaims the original row so its id, not the fresh candidate
        # generated above, remains the durable identity for this action.
        decision_id = decision.decision_id

        if prepare_error is not None:
            decision = await self.store.complete_decision(
                decision_id,
                status="unsupported",
                error=str(prepare_error),
                execution_status="not_executed",
            )
            await self._after_decision(decision, obs)
            return decision

        assert prepared is not None
        try:
            solver_result = await self.solver.solve(prepared)
            provider_action = select_mixed_action(
                solver_result,
                action_key=action_key,
                secret=self.settings.sampling_secret.get_secret_value(),
            )
            selected = validate_selected_action(provider_action, obs)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(
                    self.store.complete_decision(
                        decision_id,
                        status="interrupted",
                        error="solver task was cancelled",
                        execution_status="not_executed",
                    )
                )
            except Exception:
                logger.exception("could not mark cancelled decision %s interrupted", decision_id)
            raise
        except SolverUnsupported as exc:
            decision = await self.store.complete_decision(
                decision_id,
                status="unsupported",
                error=str(exc),
                execution_status="not_executed",
            )
            await self._after_decision(decision, obs)
            return decision
        except SolverError as exc:
            decision = await self.store.complete_decision(
                decision_id,
                status="solver_failed",
                error=str(exc),
                execution_status="not_executed",
            )
            await self._after_decision(decision, obs)
            return decision
        except Exception as exc:
            logger.exception("unexpected solver failure for decision %s", decision_id)
            decision = await self.store.complete_decision(
                decision_id,
                status="solver_failed",
                error=f"unexpected solver failure: {type(exc).__name__}: {exc}",
                execution_status="not_executed",
            )
            await self._after_decision(decision, obs)
            return decision

        status = "ready" if selected.valid else "invalid"
        execution_status = "dry_run" if selected.valid else "not_executed"
        decision = await self.store.complete_decision(
            decision_id,
            status=status,
            solver_result=solver_result,
            selected=selected,
            execution_status=execution_status,
            error=selected.invalid_reason,
        )

        if selected.valid and self.settings.auto_execute:
            if self.executor is None:
                decision = await self.store.update_execution(
                    decision_id,
                    "failed",
                    error="AUTO_EXECUTE is enabled but no executor is connected",
                )
            else:
                try:
                    execution_status = await self.executor.execute(decision, obs, source_kind)
                    decision = await self.store.update_execution(decision_id, execution_status)
                except Exception as exc:  # execution failure must never cause a second solver choice
                    logger.exception("execution failed for decision %s", decision_id)
                    decision = await self.store.update_execution(
                        decision_id,
                        "failed",
                        error=f"execution failed: {exc}",
                    )

        await self._after_decision(decision, obs)
        return decision

    async def _after_decision(self, decision: DecisionView, observation: Observation) -> None:
        self.broker.publish(decision)
        try:
            await self.notifier.send(decision, observation)
        except Exception:
            logger.exception("Telegram notification failed for %s", decision.decision_id)
