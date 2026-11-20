"""Job intake: duplicate suppression, a bounded worker pool and timeouts.

Slack retries event deliveries, developers double-paste commands and the same
issue can be reported in two threads, so three separate guards exist:

* ``event_id`` — one Slack delivery runs at most once (optionally persisted to
  SQLite so a restart does not replay the backlog).
* ``(channel, thread_ts)`` — claimed at intake, before the Sentry issue is known.
* ``(channel, thread_ts, issue_id)`` — claimed once the issue is identified.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

JobKey = tuple[str, ...]


@dataclass(frozen=True)
class JobRequest:
    job_id: str
    event_id: str | None
    channel: str
    thread_ts: str
    user: str | None
    command_ts: str | None = None
    received_at: float = field(default_factory=time.time)

    @staticmethod
    def new(
        *,
        event_id: str | None,
        channel: str,
        thread_ts: str,
        user: str | None,
        command_ts: str | None = None,
    ) -> "JobRequest":
        return JobRequest(
            job_id=uuid.uuid4().hex[:12],
            event_id=event_id,
            channel=channel,
            thread_ts=thread_ts,
            user=user,
            command_ts=command_ts,
        )

    @property
    def thread_key(self) -> JobKey:
        return (self.channel, self.thread_ts)


class SubmitStatus(str, Enum):
    queued = "queued"
    duplicate_event = "duplicate_event"
    already_running = "already_running"
    queue_full = "queue_full"
    shutting_down = "shutting_down"


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    job_id: str | None = None

    @property
    def queued(self) -> bool:
        return self.status is SubmitStatus.queued


class ProcessedEventStore(Protocol):
    def seen(self, event_id: str) -> bool:
        """Record ``event_id`` and return True when it was already present."""


class MemoryEventStore:
    """Bounded in-memory LRU of Slack event ids."""

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen:
                self._seen.move_to_end(event_id)
                return True
            self._seen[event_id] = time.time()
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return False


class SqliteEventStore:
    """Same contract as :class:`MemoryEventStore`, but survives a restart."""

    def __init__(self, path: Path, *, retention_seconds: int = 7 * 24 * 3600) -> None:
        self._path = path
        self._retention = retention_seconds
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS processed_events "
                "(event_id TEXT PRIMARY KEY, seen_at REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def seen(self, event_id: str) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM processed_events WHERE seen_at < ?", (now - self._retention,))
            try:
                conn.execute(
                    "INSERT INTO processed_events (event_id, seen_at) VALUES (?, ?)", (event_id, now)
                )
            except sqlite3.IntegrityError:
                return True
            return False


class JobRegistry:
    """Tracks which analyses are in flight."""

    def __init__(self, event_store: ProcessedEventStore | None = None) -> None:
        self._event_store = event_store or MemoryEventStore()
        self._active: set[JobKey] = set()
        self._lock = threading.Lock()

    def seen_event(self, event_id: str | None) -> bool:
        if not event_id:
            return False
        return self._event_store.seen(event_id)

    def claim(self, key: JobKey) -> bool:
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    def release(self, key: JobKey) -> None:
        with self._lock:
            self._active.discard(key)

    def is_active(self, key: JobKey) -> bool:
        with self._lock:
            return key in self._active

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)


class JobManager:
    """Bounded queue in front of the analysis pipeline."""

    def __init__(
        self,
        handler: Callable[[JobRequest], None],
        *,
        registry: JobRegistry | None = None,
        max_parallel: int = 2,
        max_queued: int | None = None,
    ) -> None:
        self._handler = handler
        self.registry = registry or JobRegistry()
        self._max_parallel = max(1, max_parallel)
        self._max_queued = max_queued if max_queued is not None else self._max_parallel * 4
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_parallel, thread_name_prefix="analysis"
        )
        self._pending = 0
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, request: JobRequest) -> SubmitResult:
        if self._closed:
            return SubmitResult(SubmitStatus.shutting_down)
        if self.registry.seen_event(request.event_id):
            log.info("duplicate slack event ignored event_id=%s", request.event_id)
            return SubmitResult(SubmitStatus.duplicate_event)
        if not self.registry.claim(request.thread_key):
            return SubmitResult(SubmitStatus.already_running)

        with self._lock:
            if self._pending >= self._max_queued:
                self.registry.release(request.thread_key)
                return SubmitResult(SubmitStatus.queue_full)
            self._pending += 1

        try:
            self._executor.submit(self._run, request)
        except RuntimeError:  # executor already shut down
            with self._lock:
                self._pending -= 1
            self.registry.release(request.thread_key)
            return SubmitResult(SubmitStatus.shutting_down)
        return SubmitResult(SubmitStatus.queued, job_id=request.job_id)

    def _run(self, request: JobRequest) -> None:
        try:
            self._handler(request)
        except BaseException:  # noqa: BLE001 - a worker must never die silently
            log.exception("job=%s handler crashed", request.job_id)
        finally:
            with self._lock:
                self._pending -= 1
            self.registry.release(request.thread_key)

    def shutdown(self, *, wait: bool = True) -> None:
        self._closed = True
        self._executor.shutdown(wait=wait)
