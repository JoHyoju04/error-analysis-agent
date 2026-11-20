"""Shape analysis output for Slack: mrkdwn conversion and safe splitting."""

from __future__ import annotations

import re
from typing import Iterable

from .redaction import redact
from .repository import ResolvedRevision, Snapshot
from .sentry_client import SentryContext

SLACK_HARD_LIMIT = 3900
_FENCE = "```"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)


def start_message(command: str, user: str | None) -> str:
    who = f" <@{user}>" if user else ""
    return f"🔎 AI 분석을 시작합니다.{who} 요청(`{command}`)을 접수했습니다. 완료되면 이 스레드에 결과를 올립니다."


def duplicate_message(issue_id: str | None) -> str:
    target = f"Issue `{issue_id}`" if issue_id else "이 스레드"
    return f"⏳ {target} 분석이 이미 진행 중입니다. 완료될 때까지 기다려 주세요."


def error_message(summary: str, *, job_id: str, retryable: bool = False) -> str:
    retry = "\n다시 시도할 수 있습니다." if retryable else ""
    return f"⚠️ {summary}{retry}\n`job_id: {job_id}`"


def to_slack_mrkdwn(text: str) -> str:
    """Convert common Markdown to Slack mrkdwn, leaving code fences untouched."""
    parts = text.split(_FENCE)
    for index in range(0, len(parts), 2):  # even indexes are outside fences
        block = parts[index]
        lines = []
        for line in block.split("\n"):
            heading = _HEADING_RE.match(line)
            if heading:
                lines.append(f"*{heading.group(2).strip()}*")
                continue
            lines.append(_BOLD_RE.sub(r"*\1*", line))
        parts[index] = "\n".join(lines)
    return _FENCE.join(parts)


def analysis_header(
    context: SentryContext, revision: ResolvedRevision, snapshot: Snapshot, *, job_id: str
) -> str:
    verified = "검증됨" if revision.verified else "미검증(fallback)"
    agents = "저장소 AGENTS.md" if snapshot.agents_md_source == "repository" else "분석 템플릿 AGENTS.md"
    lines = [
        "✅ *AI 장애 분석 결과*",
        f"• Sentry Issue: <{context.issue_url}|{context.short_id or context.issue_id}> — {context.title}",
        f"• 저장소/Revision: `{snapshot.project.name}` @ `{revision.short}` ({revision.source}, {verified})",
        f"• 분석 규칙: {agents}",
    ]
    if context.environment or context.revision.release_version:
        lines.append(
            f"• 환경/릴리스: `{context.environment or '-'}` / `{context.revision.release_version or '-'}`"
        )
    if context.truncated:
        lines.append("• 참고: Sentry 컨텍스트가 크기 제한으로 일부 생략되었습니다.")
    lines.append(f"`job_id: {job_id}`")
    return "\n".join(lines)


def _fence_balance(text: str) -> bool:
    """True when the chunk leaves an unterminated code fence open."""
    return text.count(_FENCE) % 2 == 1


def split_message(text: str, limit: int = SLACK_HARD_LIMIT) -> list[str]:
    """Split ``text`` into Slack-sized chunks at line boundaries.

    A chunk that ends inside a code fence is closed, and the next chunk reopens
    one, so no message renders as broken markup.
    """
    limit = max(200, min(limit, SLACK_HARD_LIMIT))
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal current, length
        if not current:
            return
        body = "\n".join(current)
        chunks.append(body)
        current = []
        length = 0

    for line in text.split("\n"):
        for piece in _hard_wrap(line, limit):
            addition = len(piece) + (1 if current else 0)
            if length + addition > limit:
                flush()
                addition = len(piece)
            current.append(piece)
            length += addition
    flush()

    if not chunks:
        return []

    result: list[str] = []
    carry_fence = False
    for chunk in chunks:
        body = f"{_FENCE}\n{chunk}" if carry_fence else chunk
        carry_fence = _fence_balance(body)
        if carry_fence:
            body = f"{body}\n{_FENCE}"
        result.append(body)
    return result


def _hard_wrap(line: str, limit: int) -> Iterable[str]:
    if len(line) <= limit:
        return [line]
    return [line[index : index + limit] for index in range(0, len(line), limit)]


def render_analysis(
    body: str,
    *,
    header: str,
    limit: int = SLACK_HARD_LIMIT,
) -> list[str]:
    """Header + analysis body, redacted again and split into Slack messages."""
    text = f"{header}\n\n{to_slack_mrkdwn(redact(body))}"
    chunks = split_message(text, limit)
    if len(chunks) <= 1:
        return chunks
    # Re-split with room for the "(1/3)" suffix added below.
    chunks = split_message(text, limit - 24)
    total = len(chunks)
    return [f"{chunk}\n\n_({index}/{total})_" for index, chunk in enumerate(chunks, start=1)]
