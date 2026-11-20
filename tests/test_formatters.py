from __future__ import annotations

from conftest import ISSUE_URL, sentry_event, sentry_issue

from error_analysis_agent import formatters
from error_analysis_agent.config import ProjectConfig
from error_analysis_agent.link_parser import IssueRef
from error_analysis_agent.repository import ResolvedRevision, Snapshot
from error_analysis_agent.sentry_client import build_context

SHA = "1f2e3d4c5b6a7890abcdef1234567890abcdef12"


def test_markdown_is_converted_to_slack_mrkdwn():
    text = "## 장애 요약\n**High** 확신도\n- 항목"
    assert formatters.to_slack_mrkdwn(text) == "*장애 요약*\n*High* 확신도\n- 항목"


def test_code_fences_are_left_alone():
    text = "## 근거\n```java\nif (order == null) { // **not bold**\n}\n```\n**끝**"
    converted = formatters.to_slack_mrkdwn(text)
    assert "// **not bold**" in converted
    assert converted.endswith("*끝*")


def test_short_messages_are_not_split():
    assert formatters.split_message("짧은 결과") == ["짧은 결과"]


def test_long_messages_split_at_line_boundaries():
    body = "\n".join(f"line {index}" for index in range(500))
    chunks = formatters.split_message(body, 500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "\n".join(chunks) == body


def test_very_long_single_line_is_hard_wrapped():
    chunks = formatters.split_message("x" * 1200, 400)
    assert [len(chunk) for chunk in chunks] == [400, 400, 400]


def test_split_closes_and_reopens_code_fences():
    body = "```\n" + "\n".join(f"code {index}" for index in range(200)) + "\n```"
    chunks = formatters.split_message(body, 600)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk


def test_render_analysis_numbers_the_parts():
    body = "\n".join(f"- 근거 {index}" for index in range(400))
    chunks = formatters.render_analysis(body, header="*결과*", limit=1000)
    assert len(chunks) > 1
    assert chunks[0].startswith("*결과*")
    assert chunks[0].endswith(f"_(1/{len(chunks)})_")
    assert all(len(chunk) <= 1000 for chunk in chunks)


def test_render_analysis_redacts_again_before_posting():
    chunks = formatters.render_analysis(
        "토큰 값은 password=hunter2 입니다", header="*결과*", limit=1000
    )
    assert "hunter2" not in chunks[0]
    assert "[REDACTED]" in chunks[0]


def make_snapshot(tmp_path, source: str = "template") -> Snapshot:
    return Snapshot(
        path=tmp_path,
        revision=ResolvedRevision(sha=SHA, source="tag:commit"),
        project=ProjectConfig(
            name="example-service", sentry_project_slug="example-service", repo_dir=tmp_path
        ),
        agents_md_source=source,
    )


def test_analysis_header_reports_the_traceable_facts(tmp_path):
    context = build_context(
        sentry_issue(), sentry_event(), issue_ref=IssueRef(issue_id="4242", url=ISSUE_URL)
    )
    header = formatters.analysis_header(
        context, ResolvedRevision(sha=SHA, source="tag:commit"), make_snapshot(tmp_path), job_id="j1"
    )
    assert "EXAMPLE-SERVICE-7A" in header
    assert "1f2e3d4c5b6a" in header
    assert "tag:commit, 검증됨" in header
    assert "분석 템플릿 AGENTS.md" in header
    assert "production" in header
    assert "job_id: j1" in header


def test_analysis_header_mentions_truncation(tmp_path):
    import dataclasses

    context = build_context(
        sentry_issue(), sentry_event(), issue_ref=IssueRef(issue_id="4242", url=ISSUE_URL)
    )
    header = formatters.analysis_header(
        dataclasses.replace(context, truncated=True),
        ResolvedRevision(sha=SHA, source="tag:commit"),
        make_snapshot(tmp_path, source="repository"),
        job_id="j1",
    )
    assert "생략" in header
    assert "저장소 AGENTS.md" in header


def test_status_messages():
    assert "<@U100>" in formatters.start_message("!AI분석", "U100")
    assert "!AI분석" in formatters.start_message("!AI분석", None)
    assert "4242" in formatters.duplicate_message("4242")
    assert "이 스레드" in formatters.duplicate_message(None)

    retryable = formatters.error_message("문제 발생", job_id="j1", retryable=True)
    assert "다시 시도할 수 있습니다" in retryable
    assert "job_id: j1" in retryable
    assert "다시 시도" not in formatters.error_message("문제 발생", job_id="j1")
