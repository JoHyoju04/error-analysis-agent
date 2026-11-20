from __future__ import annotations

import threading
from pathlib import Path

from error_analysis_agent.job_manager import (
    JobManager,
    JobRegistry,
    JobRequest,
    MemoryEventStore,
    SqliteEventStore,
    SubmitStatus,
)


def make_request(**overrides) -> JobRequest:
    defaults = {"event_id": "Ev1", "channel": "C100", "thread_ts": "1700.1", "user": "U100"}
    defaults.update(overrides)
    return JobRequest.new(**defaults)


def test_job_ids_are_unique():
    assert make_request().job_id != make_request().job_id


def test_same_slack_event_runs_once():
    done = threading.Event()
    manager = JobManager(lambda _request: done.set(), max_parallel=1)
    try:
        first = manager.submit(make_request())
        done.wait(timeout=5)
        second = manager.submit(make_request())
    finally:
        manager.shutdown()

    assert first.status is SubmitStatus.queued
    assert second.status is SubmitStatus.duplicate_event


def test_events_without_an_id_are_not_deduplicated():
    manager = JobManager(lambda _request: None, max_parallel=1)
    try:
        assert manager.submit(make_request(event_id=None, thread_ts="a")).queued
        assert manager.submit(make_request(event_id=None, thread_ts="b")).queued
    finally:
        manager.shutdown()


def test_second_command_in_a_running_thread_is_refused():
    started = threading.Event()
    release = threading.Event()

    def handler(_request: JobRequest) -> None:
        started.set()
        release.wait(timeout=5)

    manager = JobManager(handler, max_parallel=2)
    try:
        assert manager.submit(make_request(event_id="Ev1")).queued
        assert started.wait(timeout=5)
        again = manager.submit(make_request(event_id="Ev2"))
        assert again.status is SubmitStatus.already_running
    finally:
        release.set()
        manager.shutdown()


def test_claim_is_released_when_the_handler_raises():
    def handler(_request: JobRequest) -> None:
        raise RuntimeError("boom")

    manager = JobManager(handler, max_parallel=1)
    try:
        assert manager.submit(make_request(event_id="Ev1")).queued
        manager.shutdown(wait=True)
    finally:
        pass
    assert manager.registry.active_count == 0


def test_queue_is_bounded():
    release = threading.Event()
    manager = JobManager(
        lambda _request: release.wait(timeout=5), max_parallel=1, max_queued=2
    )
    try:
        statuses = [
            manager.submit(make_request(event_id=f"Ev{index}", thread_ts=f"t{index}")).status
            for index in range(4)
        ]
    finally:
        release.set()
        manager.shutdown()

    assert statuses[:2] == [SubmitStatus.queued, SubmitStatus.queued]
    assert SubmitStatus.queue_full in statuses[2:]


def test_parallelism_is_capped():
    running = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()
    entered = threading.Semaphore(0)

    def handler(_request: JobRequest) -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        entered.release()
        release.wait(timeout=5)
        with lock:
            running -= 1

    manager = JobManager(handler, max_parallel=2, max_queued=10)
    try:
        for index in range(4):
            manager.submit(make_request(event_id=f"Ev{index}", thread_ts=f"t{index}"))
        for _ in range(2):
            assert entered.acquire(timeout=5)
        release.set()
    finally:
        manager.shutdown()

    assert peak <= 2


def test_submit_after_shutdown_is_refused():
    manager = JobManager(lambda _request: None, max_parallel=1)
    manager.shutdown()
    assert manager.submit(make_request()).status is SubmitStatus.shutting_down


# --------------------------------------------------------------------------- #
# stores
# --------------------------------------------------------------------------- #


def test_memory_store_is_bounded():
    store = MemoryEventStore(capacity=2)
    assert store.seen("a") is False
    assert store.seen("a") is True
    store.seen("b")
    store.seen("c")  # evicts "a"
    assert store.seen("a") is False


def test_sqlite_store_survives_a_restart(tmp_path: Path):
    path = tmp_path / "state" / "jobs.sqlite3"
    assert SqliteEventStore(path).seen("Ev1") is False
    # a fresh instance stands in for a restarted container
    assert SqliteEventStore(path).seen("Ev1") is True
    assert SqliteEventStore(path).seen("Ev2") is False


def test_registry_claim_and_release():
    registry = JobRegistry()
    key = ("C1", "1700.1", "4242")
    assert registry.claim(key) is True
    assert registry.claim(key) is False
    assert registry.is_active(key) is True
    registry.release(key)
    assert registry.claim(key) is True
    assert registry.active_count == 1
