"""Slack Socket Mode entry point.

The event listener does no network I/O beyond handing the job to the worker
pool: Slack expects an acknowledgement within three seconds, while an analysis
takes minutes.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from typing import Any, Mapping

from .codex_runner import CodexRunner
from .config import AppConfig, ConfigError, load_config
from .event_handler import Verdict, decide
from .job_manager import JobManager, JobRegistry, JobRequest, MemoryEventStore, SqliteEventStore
from .logging_setup import configure_logging
from .pipeline import AnalysisPipeline
from .repository import RepositoryManager
from .sentry_client import SentryClient
from .slack_client import SlackError, SlackThreadClient

log = logging.getLogger(__name__)


def build_runtime(config: AppConfig) -> tuple[Any, JobManager, SentryClient]:
    """Wire the Slack app, the worker pool and the analysis pipeline together."""
    from slack_bolt import App  # noqa: PLC0415 - keeps import cost out of tests

    # Socket Mode needs no signing secret. Token verification is done below so
    # that a bad token produces our own message instead of a Bolt traceback.
    app = App(
        token=config.slack.bot_token,
        token_verification_enabled=False,
        raise_error_for_unhandled_request=False,
    )
    slack = SlackThreadClient(app.client, max_message_chars=config.slack.max_message_chars)

    try:
        bot_user_id = slack.bot_user_id()
    except SlackError as exc:
        raise SystemExit(
            f"Slack 인증에 실패했습니다. SLACK_BOT_TOKEN 을 확인해 주세요: {exc}"
        ) from exc
    log.info("authenticated as bot_user_id=%s", bot_user_id)

    event_store = (
        SqliteEventStore(config.state_db_path) if config.state_db_path else MemoryEventStore()
    )
    registry = JobRegistry(event_store)
    sentry = SentryClient(config.sentry)
    pipeline = AnalysisPipeline(
        config,
        slack=slack,
        sentry=sentry,
        repositories=RepositoryManager(config),
        codex=CodexRunner(config.codex),
        registry=registry,
    )
    jobs = JobManager(
        pipeline.handle, registry=registry, max_parallel=config.analysis.max_parallel_jobs
    )

    @app.event("message")
    def on_message(event: Mapping[str, Any], body: Mapping[str, Any]) -> None:
        handle_message_event(event, body, config=config, slack=slack, jobs=jobs, bot_user_id=bot_user_id)

    return app, jobs, sentry


def handle_message_event(
    event: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    config: AppConfig,
    slack: SlackThreadClient,
    jobs: JobManager,
    bot_user_id: str | None,
) -> None:
    """Classify one Slack message event and queue the analysis. Returns fast."""
    decision = decide(event, config.slack, bot_user_id=bot_user_id)

    if decision.verdict is Verdict.ignore:
        log.debug("ignoring event reason=%s", decision.reason.value)
        return

    event_id = body.get("event_id") if isinstance(body, Mapping) else None

    if decision.verdict is Verdict.reject:
        log.info(
            "rejected command reason=%s channel=%s user=%s event_id=%s",
            decision.reason.value,
            decision.channel,
            decision.user,
            event_id,
        )
        message = decision.user_message(config.slack.command)
        if message and decision.channel and decision.thread_ts:
            try:
                slack.post(decision.channel, decision.thread_ts, f"⚠️ {message}")
            except SlackError as exc:
                log.warning("could not post rejection: %s", exc)
        return

    assert decision.channel and decision.thread_ts  # guaranteed by Verdict.accept
    request = JobRequest.new(
        event_id=str(event_id) if event_id else None,
        channel=decision.channel,
        thread_ts=decision.thread_ts,
        user=decision.user,
        command_ts=decision.event_ts,
    )
    result = jobs.submit(request)
    log.info(
        "submit status=%s job_id=%s channel=%s thread_ts=%s event_id=%s",
        result.status.value,
        result.job_id,
        request.channel,
        request.thread_ts,
        request.event_id,
    )

    if result.status.value in ("already_running", "queue_full"):
        from . import formatters  # noqa: PLC0415 - avoid a module-level cycle

        text = (
            formatters.duplicate_message(None)
            if result.status.value == "already_running"
            else "⏳ 분석 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요."
        )
        try:
            slack.post(request.channel, request.thread_ts, text)
        except SlackError as exc:
            log.warning("could not post queue notice: %s", exc)


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[설정 오류] {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    log.info(
        "starting error-analysis-agent projects=%s max_parallel=%s timeout=%ss require_revision=%s",
        [project.name for project in config.projects],
        config.analysis.max_parallel_jobs,
        config.analysis.timeout_seconds,
        config.analysis.require_release_revision,
    )

    app, jobs, sentry = build_runtime(config)

    from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: PLC0415

    handler = SocketModeHandler(app, config.slack.app_token)
    stopping = threading.Event()

    def shutdown(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("signal %s received, shutting down", signum)
        try:
            handler.close()
        finally:
            jobs.shutdown(wait=True)
            sentry.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)

    try:
        handler.start()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        if not stopping.is_set():
            jobs.shutdown(wait=True)
            sentry.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
