"""End-to-end pipeline tests with every external boundary faked."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ISSUE_URL, sentry_alert_message, sentry_event, sentry_issue

from error_analysis_agent.codex_runner import AnalysisResult, CodexQuotaExceeded, CodexTimeout
from error_analysis_agent.config import ProjectConfig
from error_analysis_agent.job_manager import JobRegistry, JobRequest
from error_analysis_agent.pipeline import AnalysisPipeline
from error_analysis_agent.repository import ResolvedRevision, RevisionUnavailable, Snapshot
from error_analysis_agent.sentry_client import SentryForbiddenError
from error_analysis_agent.slack_client import ParentMessage, SlackPermissionError

PARENT_TS = "1700000000.000100"
SHA = "1f2e3d4c5b6a7890abcdef1234567890abcdef12"

ANALYSIS_TEXT = """\
## 장애 요약
`OrderService.place` 에서 NullPointerException 이 발생했습니다.

## 가장 가능성 높은 원인과 확신도
**High** — 주문 조회 결과가 null 인 경로가 있습니다 (`com/example/order/OrderService.java:88`).
"""


class FakeSlack:
    def __init__(self, *, parent_ts: str = PARENT_TS, parent=None, fetch_error=None) -> None:
        self._parent_ts = parent_ts
        self._parent = parent if parent is not None else sentry_alert_message()
        self._fetch_error = fetch_error
        self.posts: list[tuple[str, str, str]] = []
        self.analyses: list[tuple[str, str, str, str]] = []

    def fetch_parent(self, channel: str, thread_ts: str) -> ParentMessage:
        if self._fetch_error:
            raise self._fetch_error
        return ParentMessage(ts=self._parent_ts, raw=self._parent)

    def post(self, channel: str, thread_ts: str, text: str) -> str:
        self.posts.append((channel, thread_ts, text))
        return "1700000200.000001"

    def post_analysis(self, channel: str, thread_ts: str, *, header: str, body: str) -> list[str]:
        self.analyses.append((channel, thread_ts, header, body))
        return ["1700000300.000001"]

    @property
    def all_text(self) -> str:
        return "\n".join(text for _c, _t, text in self.posts) + "\n".join(
            f"{header}\n{body}" for _c, _t, header, body in self.analyses
        )


class FakeSentry:
    def __init__(self, *, issue=None, event=None, error=None) -> None:
        self._issue = issue if issue is not None else sentry_issue()
        self._event = event if event is not None else sentry_event()
        self._error = error
        self.calls: list[str] = []

    def get_issue(self, issue_id: str):
        self.calls.append(f"issue:{issue_id}")
        if self._error:
            raise self._error
        return self._issue

    def get_latest_event(self, issue_id: str):
        self.calls.append(f"event:{issue_id}")
        if self._error:
            raise self._error
        return self._event


class FakeRepositories:
    def __init__(self, tmp_path: Path, *, revision_error=None) -> None:
        self._tmp = tmp_path
        self._revision_error = revision_error
        self.project = ProjectConfig(
            name="example-service",
            sentry_project_slug="example-service",
            repo_dir=tmp_path / "repo",
        )
        self.snapshots: list[Snapshot] = []
        self.cleaned = 0

    def resolve_project(self, slug):
        return self.project

    def resolve_revision(self, project, hints):
        if self._revision_error:
            raise self._revision_error
        return ResolvedRevision(sha=SHA, source="tag:commit", release_version=hints.release_version)

    def create_snapshot(self, project, revision, job_id) -> Snapshot:
        path = self._tmp / "snapshots" / job_id
        path.mkdir(parents=True, exist_ok=True)
        snapshot = _TrackingSnapshot(
            path=path, revision=revision, project=project, agents_md_source="template", owner=self
        )
        self.snapshots.append(snapshot)
        return snapshot


class _TrackingSnapshot(Snapshot):
    def __init__(self, *, owner: FakeRepositories, **kwargs) -> None:
        super().__init__(**kwargs)
        self._owner = owner

    def cleanup(self) -> None:
        self._owner.cleaned += 1


class FakeCodexRunner:
    def __init__(self, *, text: str = ANALYSIS_TEXT, error=None) -> None:
        self._text = text
        self._error = error
        self.calls: list[dict[str, object]] = []

    def analyze(self, *, snapshot_dir, context_text, job_id, timeout_seconds):
        self.calls.append(
            {
                "snapshot_dir": snapshot_dir,
                "context_text": context_text,
                "job_id": job_id,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._error:
            raise self._error
        return AnalysisResult(
            text=self._text, thread_id="thread_1", status="completed", input_mode="external_message"
        )


def build(config, tmp_path, *, slack=None, sentry=None, repos=None, codex=None, registry=None):
    slack = slack or FakeSlack()
    sentry = sentry or FakeSentry()
    repos = repos or FakeRepositories(tmp_path)
    codex = codex or FakeCodexRunner()
    registry = registry or JobRegistry()
    pipeline = AnalysisPipeline(
        config, slack=slack, sentry=sentry, repositories=repos, codex=codex, registry=registry
    )
    return pipeline, slack, sentry, repos, codex, registry


def request() -> JobRequest:
    return JobRequest.new(
        event_id="Ev1", channel="C100", thread_ts=PARENT_TS, user="U100", command_ts="1700000100.1"
    )


# --------------------------------------------------------------------------- #


def test_happy_path(config, tmp_path):
    pipeline, slack, sentry, repos, codex, registry = build(config, tmp_path)
    pipeline.handle(request())

    assert "AI 분석을 시작합니다" in slack.posts[0][2]
    assert sentry.calls == ["issue:4242", "event:4242"]

    assert len(slack.analyses) == 1
    channel, thread_ts, header, body = slack.analyses[0]
    assert (channel, thread_ts) == ("C100", PARENT_TS)
    assert body == ANALYSIS_TEXT
    assert "1f2e3d4c5b6a" in header
    assert ISSUE_URL in header

    assert codex.calls[0]["timeout_seconds"] == config.analysis.timeout_seconds
    assert codex.calls[0]["snapshot_dir"] == repos.snapshots[0].path
    assert repos.cleaned == 1
    assert registry.active_count == 0


def test_reply_always_goes_to_the_parent_message_ts(config, tmp_path):
    """thread_ts 와 부모 ts 가 다르게 보고되더라도 부모 ts 에 답글을 단다."""
    slack = FakeSlack(parent_ts="1699999999.000009")
    pipeline, slack, *_ = build(config, tmp_path, slack=slack)
    pipeline.handle(request())
    assert slack.analyses[0][1] == "1699999999.000009"
    # the start comment is posted before the parent lookup, so it uses thread_ts
    assert slack.posts[0][1] == PARENT_TS


def test_no_sentry_link_in_thread(config, tmp_path):
    slack = FakeSlack(parent={"type": "message", "ts": PARENT_TS, "text": "배포 완료했습니다"})
    pipeline, slack, sentry, _repos, codex, _registry = build(config, tmp_path, slack=slack)
    pipeline.handle(request())

    assert "Sentry Issue 링크를 찾지 못했습니다" in slack.posts[-1][2]
    assert sentry.calls == []
    assert codex.calls == []
    assert slack.analyses == []


def test_foreign_host_link_is_refused(config, tmp_path):
    slack = FakeSlack(
        parent={
            "type": "message",
            "ts": PARENT_TS,
            "text": "<https://evil.example.net/organizations/my-org/issues/1/|보기>",
        }
    )
    pipeline, slack, sentry, *_ = build(config, tmp_path, slack=slack)
    pipeline.handle(request())
    assert "허용된 Sentry" in slack.posts[-1][2]
    assert sentry.calls == []


def test_slack_permission_problem_is_explained(config, tmp_path):
    slack = FakeSlack(fetch_error=SlackPermissionError("missing_scope"))
    pipeline, slack, sentry, *_ = build(config, tmp_path, slack=slack)
    pipeline.handle(request())
    assert "Slack 권한" in slack.posts[-1][2]
    assert sentry.calls == []


def test_sentry_permission_error_hides_server_detail(config, tmp_path):
    sentry = FakeSentry(error=SentryForbiddenError("Sentry 403 Forbidden", status_code=403))
    pipeline, slack, _sentry, _repos, codex, _registry = build(config, tmp_path, sentry=sentry)
    pipeline.handle(request())

    message = slack.posts[-1][2]
    assert "403" in message
    assert "event:read" in message
    assert codex.calls == []


def test_revision_mismatch_fails_closed(config, tmp_path):
    repos = FakeRepositories(tmp_path, revision_error=RevisionUnavailable("no sha"))
    pipeline, slack, _sentry, repos, codex, _registry = build(config, tmp_path, repos=repos)
    pipeline.handle(request())

    message = slack.posts[-1][2]
    assert "배포 revision" in message
    assert "추측" in message
    assert codex.calls == []
    assert repos.snapshots == []


def test_codex_timeout_tells_the_user_it_can_be_retried(config, tmp_path):
    codex = FakeCodexRunner(error=CodexTimeout("timed out"))
    pipeline, slack, _sentry, repos, _codex, _registry = build(config, tmp_path, codex=codex)
    pipeline.handle(request())

    assert "제한 시간" in slack.posts[-1][2]
    assert "다시 시도할 수 있습니다" in slack.posts[-1][2]
    assert repos.cleaned == 1, "the snapshot must be removed even on failure"


def test_quota_error_is_surfaced(config, tmp_path):
    codex = FakeCodexRunner(error=CodexQuotaExceeded("limit"))
    pipeline, slack, *_ = build(config, tmp_path, codex=codex)
    pipeline.handle(request())
    assert "한도" in slack.posts[-1][2]


def test_second_request_for_the_same_issue_is_refused(config, tmp_path):
    registry = JobRegistry()
    registry.claim(("C100", PARENT_TS, "4242"))
    pipeline, slack, _sentry, _repos, codex, _registry = build(
        config, tmp_path, registry=registry
    )
    pipeline.handle(request())

    assert "이미 진행 중" in slack.posts[-1][2]
    assert codex.calls == []


def test_unexpected_crash_is_reported_without_leaking_details(config, tmp_path):
    class Exploding(FakeRepositories):
        def resolve_project(self, slug):
            raise ValueError("boom: /secret/path token=abcdef")

    pipeline, slack, *_ = build(config, tmp_path, repos=Exploding(tmp_path))
    pipeline.handle(request())

    message = slack.posts[-1][2]
    assert "알 수 없는 오류" in message
    assert "boom" not in message
    assert "abcdef" not in message


SECRETS = (
    "leak@example.com",
    "hunter2",
    "sk-live-abcdefghijklmnop",
    "4111 1111 1111 1111",
    "203.0.113.9",
)


def test_no_sentry_secret_reaches_codex_or_slack(config, tmp_path):
    pipeline, slack, _sentry, _repos, codex, _registry = build(config, tmp_path)
    pipeline.handle(request())

    prompt = codex.calls[0]["context_text"]
    assert isinstance(prompt, str)
    for secret in SECRETS:
        assert secret not in prompt, f"{secret} leaked into the Codex prompt"
        assert secret not in slack.all_text, f"{secret} leaked into Slack"
    assert "order is null" in prompt  # still useful


def test_context_respects_the_configured_cap(config_path, tmp_path):
    from conftest import make_config

    config = make_config(config_path, ANALYSIS_MAX_CONTEXT_BYTES="1500")
    pipeline, _slack, _sentry, _repos, codex, _registry = build(config, tmp_path)
    pipeline.handle(request())
    prompt = codex.calls[0]["context_text"]
    assert len(prompt.encode("utf-8")) <= 1700


def test_analysis_header_marks_an_unverified_revision(config, tmp_path):
    class Fallback(FakeRepositories):
        def resolve_revision(self, project, hints):
            return ResolvedRevision(sha=SHA, source="fallback:main", verified=False)

    pipeline, slack, *_ = build(config, tmp_path, repos=Fallback(tmp_path))
    pipeline.handle(request())
    assert "미검증" in slack.analyses[0][2]


@pytest.mark.parametrize("failing_step", ["sentry", "codex"])
def test_issue_claim_is_released_after_every_outcome(config, tmp_path, failing_step):
    sentry = FakeSentry(error=SentryForbiddenError("403")) if failing_step == "sentry" else None
    codex = FakeCodexRunner(error=CodexTimeout("t")) if failing_step == "codex" else None
    registry = JobRegistry()
    pipeline, *_ = build(config, tmp_path, sentry=sentry, codex=codex, registry=registry)
    pipeline.handle(request())
    assert registry.active_count == 0
