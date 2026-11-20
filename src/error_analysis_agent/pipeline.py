"""End-to-end analysis: Slack thread -> Sentry -> Git snapshot -> Codex -> Slack.

Runs on a worker thread. Every failure mode ends with one safe Korean message
in the same Slack thread; raw server errors stay in the log.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import NoReturn

from . import formatters
from .codex_runner import CodexRunner, CodexRunnerError
from .config import AppConfig
from .job_manager import JobRegistry, JobRequest
from .link_parser import IssueRef, LinkParseError, extract_issue_ref
from .repository import RepositoryError, RepositoryManager, Snapshot
from .sentry_client import SentryClient, SentryError, build_context, render_context
from .slack_client import SlackError, SlackThreadClient

log = logging.getLogger(__name__)


@dataclass
class _Job:
    request: JobRequest
    reply_ts: str
    issue_ref: IssueRef | None = None
    issue_key: tuple[str, ...] | None = None
    snapshot: Snapshot | None = None


class AnalysisPipeline:
    def __init__(
        self,
        config: AppConfig,
        *,
        slack: SlackThreadClient,
        sentry: SentryClient,
        repositories: RepositoryManager,
        codex: CodexRunner,
        registry: JobRegistry,
    ) -> None:
        self._config = config
        self._slack = slack
        self._sentry = sentry
        self._repos = repositories
        self._codex = codex
        self._registry = registry

    def handle(self, request: JobRequest) -> None:
        started = time.monotonic()
        job = _Job(request=request, reply_ts=request.thread_ts)
        log.info(
            "job=%s start channel=%s thread_ts=%s user=%s event_id=%s",
            request.job_id,
            request.channel,
            request.thread_ts,
            request.user,
            request.event_id,
        )
        try:
            self._post(job, formatters.start_message(self._config.slack.command, request.user))
            self._resolve_thread(job)
            self._analyze(job)
        except _Reported:
            pass
        except Exception:  # noqa: BLE001 - last-resort guard, details only in the log
            log.exception("job=%s unexpected failure", request.job_id)
            self._post(
                job,
                formatters.error_message(
                    "알 수 없는 오류로 분석을 완료하지 못했습니다. 관리자가 로그를 확인해야 합니다.",
                    job_id=request.job_id,
                ),
            )
        finally:
            if job.snapshot is not None:
                job.snapshot.cleanup()
            if job.issue_key is not None:
                self._registry.release(job.issue_key)
            log.info(
                "job=%s finished in %.1fs issue_id=%s",
                request.job_id,
                time.monotonic() - started,
                job.issue_ref.issue_id if job.issue_ref else None,
            )

    # -- steps ------------------------------------------------------------- #

    def _resolve_thread(self, job: _Job) -> None:
        """Read the parent Sentry alert and pin down which issue to analyse."""
        try:
            parent = self._slack.fetch_parent(job.request.channel, job.request.thread_ts)
        except SlackError as exc:
            log.warning("job=%s parent lookup failed: %s", job.request.job_id, exc)
            self._fail(job, exc.user_message)
        # Always answer on the parent message's ts, which is the thread anchor.
        job.reply_ts = parent.ts

        try:
            job.issue_ref = extract_issue_ref(
                parent.raw,
                allowed_hosts=self._config.sentry.allowed_hosts,
                organization=self._config.sentry.org,
            )
        except LinkParseError as exc:
            self._fail(job, str(exc))

        key = (job.request.channel, parent.ts, job.issue_ref.issue_id)
        if not self._registry.claim(key):
            self._post(job, formatters.duplicate_message(job.issue_ref.issue_id))
            raise _Reported
        job.issue_key = key
        log.info(
            "job=%s issue_id=%s issue_url=%s",
            job.request.job_id,
            job.issue_ref.issue_id,
            job.issue_ref.url,
        )

    def _analyze(self, job: _Job) -> None:
        assert job.issue_ref is not None  # _resolve_thread raised otherwise
        issue_ref = job.issue_ref
        analysis = self._config.analysis

        try:
            issue = self._sentry.get_issue(issue_ref.issue_id)
            event = self._sentry.get_latest_event(issue_ref.issue_id)
        except SentryError as exc:
            log.warning("job=%s sentry failed: %s", job.request.job_id, exc)
            self._fail(job, exc.user_message)

        context = build_context(issue, event, issue_ref=issue_ref)
        rendered = render_context(context, max_bytes=analysis.max_context_bytes)

        try:
            project = self._repos.resolve_project(context.project_slug)
            revision = self._repos.resolve_revision(project, context.revision)
            job.snapshot = self._repos.create_snapshot(project, revision, job.request.job_id)
        except RepositoryError as exc:
            log.warning("job=%s repository step failed: %s", job.request.job_id, exc)
            self._fail(job, exc.user_message)

        log.info(
            "job=%s revision=%s source=%s verified=%s context_bytes=%s",
            job.request.job_id,
            revision.short,
            revision.source,
            revision.verified,
            len(rendered.text.encode("utf-8")),
        )

        try:
            result = self._codex.analyze(
                snapshot_dir=job.snapshot.path,
                context_text=rendered.text,
                job_id=job.request.job_id,
                timeout_seconds=analysis.timeout_seconds,
            )
        except CodexRunnerError as exc:
            log.warning("job=%s codex failed: %s", job.request.job_id, exc)
            self._fail(job, exc.user_message, retryable=exc.retryable)

        header = formatters.analysis_header(
            dataclasses.replace(context, truncated=rendered.truncated),
            revision,
            job.snapshot,
            job_id=job.request.job_id,
        )
        try:
            self._slack.post_analysis(
                job.request.channel, job.reply_ts, header=header, body=result.text
            )
        except SlackError as exc:
            log.warning("job=%s posting result failed: %s", job.request.job_id, exc)
            raise _Reported from exc

        log.info(
            "job=%s analysis posted thread_id=%s mode=%s tokens=%s",
            job.request.job_id,
            result.thread_id,
            result.input_mode,
            result.total_tokens,
        )

    # -- helpers ----------------------------------------------------------- #

    def _post(self, job: _Job, text: str) -> None:
        try:
            self._slack.post(job.request.channel, job.reply_ts, text)
        except SlackError as exc:
            log.warning("job=%s could not post to slack: %s", job.request.job_id, exc)

    def _fail(self, job: _Job, summary: str, *, retryable: bool = False) -> NoReturn:
        """Tell the user why the analysis stopped, then unwind quietly."""
        self._post(
            job,
            formatters.error_message(summary, job_id=job.request.job_id, retryable=retryable),
        )
        raise _Reported


class _Reported(Exception):
    """Internal signal: the user already got a message, stop quietly."""
