"""One process, one provider slot; one replaceable pending frame per session."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from .contracts import Frame, Profile
from .provider import RecognitionError
from .reducer import initial_state, invalidate, reduce_observation, view

_WALL_ORIGIN = time.time()
_MONOTONIC_ORIGIN = time.monotonic()


def now_ms() -> int:
    # UTC origin + monotonic elapsed time: NTP clock jumps cannot refresh old evidence.
    return int((_WALL_ORIGIN + time.monotonic() - _MONOTONIC_ORIGIN) * 1000)


@dataclass
class Session:
    id: str
    token: str
    profile: Profile
    state: dict
    touched_at: int
    seen: OrderedDict = field(default_factory=OrderedDict)
    outcomes: deque = field(default_factory=lambda: deque(maxlen=16))
    records: deque = field(default_factory=deque)
    record_bytes: int = 0
    dropped_records: int = 0
    requests: int = 0
    errors: int = 0

    def record(self, event: dict):
        size = len(json.dumps(event, ensure_ascii=False).encode())
        self.records.append((event, size))
        self.record_bytes += size
        while self.record_bytes > 16_000_000 or len(self.records) > 100:
            _, removed = self.records.popleft()
            self.record_bytes -= removed
            self.dropped_records += 1


class Scheduler:
    def __init__(self, provider, *, clock=now_ms, monotonic=None, min_interval_ms=1000, limit=60):
        self.provider = provider
        self.clock = clock
        self.monotonic = monotonic or (lambda: int(time.monotonic() * 1000))
        self.cooldown_until = 0
        self.consecutive_errors = 0
        self.min_interval_ms = min_interval_ms
        self.limit = limit
        self.sessions: dict[str, Session] = {}
        self.pending: OrderedDict[str, Frame] = OrderedDict()
        self.active: tuple[str, Frame] | None = None
        self.attempts: deque[int] = deque()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    def create(self, profile: Profile, epoch: str) -> Session:
        now = self.clock()
        for sid, session in list(self.sessions.items()):
            if now - session.touched_at > 1_800_000 and (not self.active or self.active[0] != sid):
                self.sessions.pop(sid)
                self.pending.pop(sid, None)
        if len(self.sessions) >= 8:
            raise ValueError("session_limit")
        session = Session(
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(32),
            profile,
            initial_state(epoch, profile.calibration_id),
            now,
        )
        self.sessions[session.id] = session
        session.record(
            {
                "type": "reset",
                "at": now,
                "state": session.state,
                "profile": profile.model_dump(),
            }
        )
        return session

    def change(self, session: Session, revision: int):
        session.state = invalidate(session.state, revision)
        session.record({"type": "change", "at": self.clock(), "revision": revision})
        queued = self.pending.get(session.id)
        if queued and queued.source.visual_revision < revision:
            self.pending.pop(session.id)
            self.finish(session, queued, "superseded")

    def reset(self, session: Session, profile: Profile, epoch: str):
        self.pending.pop(session.id, None)
        session.profile = profile
        session.state = initial_state(epoch, profile.calibration_id)
        session.seen.clear()
        session.outcomes.clear()
        session.record(
            {
                "type": "reset",
                "at": self.clock(),
                "state": session.state,
                "profile": profile.model_dump(),
            }
        )

    def enqueue(self, session: Session, frame: Frame) -> str:
        s = frame.source
        if s.frame_id in session.seen:
            return session.seen[s.frame_id]
        if len(session.seen) >= 4096:
            raise ValueError("session_frame_limit_restart_required")
        if (
            s.capture_epoch != session.state["capture_epoch"]
            or s.calibration_id != session.profile.calibration_id
            or s.visual_revision != session.state["visual_revision"]
        ):
            raise ValueError("obsolete_source")
        if s.frame_seq <= session.state["last_frame_seq"]:
            raise ValueError("obsolete_frame")
        if not -2000 <= self.clock() - s.captured_at <= 5000:
            raise ValueError("expired_capture")
        previous = self.pending.get(session.id)
        if previous and s.frame_seq <= previous.source.frame_seq:
            raise ValueError("out_of_order_frame")
        if (
            self.active
            and self.active[0] == session.id
            and self.active[1].source.capture_epoch == s.capture_epoch
            and self.active[1].source.calibration_id == s.calibration_id
            and s.frame_seq <= self.active[1].source.frame_seq
        ):
            raise ValueError("out_of_order_frame")
        if previous:
            self.finish(session, previous, "replaced")
        session.seen[s.frame_id] = "queued"
        self.pending[session.id] = frame
        session.record({"type": "frame", "at": self.clock(), "frame": frame.model_dump()})
        self.wake.set()
        return "queued"

    def finish(self, session: Session, frame: Frame, status: str, accepted=False):
        session.seen[frame.source.frame_id] = status
        session.outcomes.append(
            {
                "frame_id": frame.source.frame_id,
                "source": frame.source.model_dump(),
                "status": status,
                "baseline_accepted": accepted,
            }
        )

    def delay_ms(self) -> int:
        now = self.monotonic()
        while self.attempts and self.attempts[0] <= now - 60_000:
            self.attempts.popleft()
        delay = max(0, self.attempts[-1] + self.min_interval_ms - now) if self.attempts else 0
        if len(self.attempts) >= self.limit:
            delay = max(delay, self.attempts[0] + 60_000 - now)
        return max(delay, self.cooldown_until - now, 0)

    async def run_one(self) -> bool:
        if self.active or not self.pending or self.delay_ms():
            return False
        sid, frame = self.pending.popitem(last=False)
        session = self.sessions[sid]
        if self.clock() - frame.source.captured_at > 5000:
            self.finish(session, frame, "expired_capture")
            return True
        self.active = (sid, frame)
        profile = session.profile
        self.attempts.append(self.monotonic())
        session.requests += 1
        session.seen[frame.source.frame_id] = "recognizing"
        started = self.clock()
        try:
            observation, raw = await self.provider.recognize(frame, profile)
            self.consecutive_errors = 0
            session.state, accepted = reduce_observation(
                session.state, frame, observation, self.clock()
            )
            self.finish(session, frame, "recognized", accepted)
            session.record(
                {
                    "type": "observation",
                    "at": self.clock(),
                    "frame": frame.model_dump(),
                    "observation": observation.model_dump(),
                    "raw_response": raw,
                    "latency_ms": self.clock() - started,
                    "baseline_accepted": accepted,
                    "state": session.state,
                }
            )
        except RecognitionError as exc:
            session.errors += 1
            self.consecutive_errors += 1
            self.cooldown_until = self.monotonic() + min(
                30_000, 1000 * 2 ** min(self.consecutive_errors, 5)
            )
            self.finish(session, frame, str(exc))
            session.record(
                {
                    "type": "error",
                    "at": self.clock(),
                    "code": str(exc),
                    "frame_id": frame.source.frame_id,
                }
            )
        except Exception:
            # Provider exception strings may contain Authorization headers or image data.
            session.errors += 1
            self.finish(session, frame, "recognition_failed")
        finally:
            self.active = None
        return True

    async def run(self):
        while True:
            if await self.run_one():
                continue
            self.wake.clear()
            try:
                await asyncio.wait_for(
                    self.wake.wait(),
                    timeout=max(0.05, self.delay_ms() / 1000) if self.pending else 30,
                )
            except TimeoutError:
                pass

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.provider.close()

    def status(self, session: Session) -> dict:
        return {
            "state": view(session.state, self.clock()),
            "outcomes": list(session.outcomes),
            "queue": {
                "active": bool(self.active and self.active[0] == session.id),
                "pending": session.id in self.pending,
                "delay_ms": self.delay_ms(),
            },
            "metrics": {
                "requests": session.requests,
                "errors": session.errors,
                "record_bytes": session.record_bytes,
                "dropped_records": session.dropped_records,
            },
        }
