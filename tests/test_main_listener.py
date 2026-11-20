"""The Socket Mode listener: classify, enqueue, return. No analysis here."""

from __future__ import annotations

import threading

from conftest import slack_command_event

from error_analysis_agent.job_manager import JobManager, JobRequest
from error_analysis_agent.main import handle_message_event


class SpySlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    def post(self, channel: str, thread_ts: str, text: str) -> str:
        self.posts.append((channel, thread_ts, text))
        return "1700.9"


def run(config, event, *, body=None, handler=None, manager=None):
    slack = SpySlack()
    seen: list[JobRequest] = []
    manager = manager or JobManager(
        handler or seen.append, max_parallel=1, max_queued=4
    )
    handle_message_event(
        event,
        body if body is not None else {"event_id": "Ev1"},
        config=config,
        slack=slack,
        jobs=manager,
        bot_user_id="UBOT",
    )
    manager.shutdown(wait=True)
    return slack, seen


def test_accepted_command_is_queued_with_slack_metadata(config):
    slack, seen = run(config, slack_command_event())
    assert slack.posts == []  # the worker posts the start message, not the listener
    assert len(seen) == 1
    job = seen[0]
    assert (job.channel, job.thread_ts, job.user) == ("C100", "1700000000.000100", "U100")
    assert job.event_id == "Ev1"
    assert job.command_ts == "1700000100.000200"


def test_ignored_events_do_nothing(config):
    for event in (
        slack_command_event(text="안녕하세요"),
        slack_command_event(bot_id="B1"),
        slack_command_event(subtype="message_changed"),
        slack_command_event(channel="C999"),
        slack_command_event(user="UBOT"),
    ):
        slack, seen = run(config, event)
        assert (slack.posts, seen) == ([], [])


def test_rejected_command_is_answered_in_the_thread(config):
    slack, seen = run(config, slack_command_event(thread_ts=None))
    assert seen == []
    assert len(slack.posts) == 1
    assert "스레드" in slack.posts[0][2]


def test_unauthorized_user_is_answered(config):
    slack, seen = run(config, slack_command_event(user="U999"))
    assert seen == []
    assert "권한이 없습니다" in slack.posts[0][2]


def test_duplicate_while_running_tells_the_user(config):
    release = threading.Event()
    started = threading.Event()

    def handler(_request: JobRequest) -> None:
        started.set()
        release.wait(timeout=5)

    manager = JobManager(handler, max_parallel=1, max_queued=4)
    slack = SpySlack()
    try:
        handle_message_event(
            slack_command_event(),
            {"event_id": "Ev1"},
            config=config,
            slack=slack,
            jobs=manager,
            bot_user_id="UBOT",
        )
        assert started.wait(timeout=5)
        handle_message_event(
            slack_command_event(ts="1700000100.000300"),
            {"event_id": "Ev2"},
            config=config,
            slack=slack,
            jobs=manager,
            bot_user_id="UBOT",
        )
    finally:
        release.set()
        manager.shutdown(wait=True)

    assert any("이미 진행 중" in text for _c, _t, text in slack.posts)


def test_missing_event_id_still_queues(config):
    _slack, seen = run(config, slack_command_event(), body={})
    assert len(seen) == 1
    assert seen[0].event_id is None


def test_listener_does_not_block_on_the_analysis(config):
    """리스너는 분석을 기다리지 않고 즉시 반환해야 한다."""
    release = threading.Event()
    manager = JobManager(lambda _r: release.wait(timeout=5), max_parallel=1)
    slack = SpySlack()
    try:
        start = threading.Event()

        def call() -> None:
            handle_message_event(
                slack_command_event(),
                {"event_id": "Ev1"},
                config=config,
                slack=slack,
                jobs=manager,
                bot_user_id="UBOT",
            )
            start.set()

        thread = threading.Thread(target=call)
        thread.start()
        assert start.wait(timeout=2), "listener blocked while the analysis was running"
        thread.join(timeout=2)
    finally:
        release.set()
        manager.shutdown(wait=True)
