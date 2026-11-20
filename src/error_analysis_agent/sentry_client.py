"""Sentry REST API client plus the sanitizer that builds the analysis context.

Only two endpoints are used:

* ``GET /api/0/organizations/{org}/issues/{issue_id}/``
* ``GET /api/0/organizations/{org}/issues/{issue_id}/events/latest/``

The raw responses are never forwarded. :func:`build_context` picks the fields
an incident analyst needs, redacts them, and caps the result so a huge event
cannot blow up the Codex prompt.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from .config import SentrySettings
from .link_parser import IssueRef
from .redaction import is_secret_key, redact

log = logging.getLogger(__name__)

#: Tags forwarded to the analysis. Everything else (user ids, IPs, URLs with
#: query strings, custom tags of unknown content) is dropped.
ALLOWED_TAG_KEYS: frozenset[str] = frozenset(
    {
        "level",
        "environment",
        "release",
        "sentry:release",
        "dist",
        "sentry:dist",
        "transaction",
        "handled",
        "mechanism",
        "logger",
        "runtime",
        "runtime.name",
        "os",
        "os.name",
        "server_name",
        "http.method",
        "method",
        "http_method",
    }
)

#: Tag keys that may carry a deploy revision.
REVISION_TAG_KEYS: tuple[str, ...] = (
    "commit",
    "git_commit",
    "git_sha",
    "gitsha",
    "sha",
    "revision",
    "release.commit",
    "build_commit",
    "vcs_revision",
)

MAX_FRAMES_WITH_CODE = 12
MAX_PLAIN_FRAMES = 60
MAX_BREADCRUMBS = 15
MAX_VALUE_CHARS = 2000


class SentryError(RuntimeError):
    """Base class for every Sentry access failure."""

    #: Short Korean sentence safe to post in Slack (never the raw server body).
    user_message = "Sentry 데이터를 가져오지 못했습니다."

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SentryAuthError(SentryError):
    user_message = (
        "Sentry 인증에 실패했습니다(401). 관리자가 `SENTRY_AUTH_TOKEN` 을 확인해야 합니다."
    )


class SentryForbiddenError(SentryError):
    user_message = (
        "Sentry 토큰 권한이 부족합니다(403). 관리자가 토큰의 `event:read` 권한과 "
        "조직 접근 범위를 확인해야 합니다."
    )


class SentryNotFoundError(SentryError):
    user_message = "Sentry 에서 해당 Issue 또는 이벤트를 찾지 못했습니다(404)."


class SentryRateLimitError(SentryError):
    user_message = "Sentry API 요청이 제한되었습니다(429). 잠시 후 다시 시도해 주세요."


class SentryServerError(SentryError):
    user_message = "Sentry 서버 오류로 데이터를 가져오지 못했습니다(5xx). 잠시 후 다시 시도해 주세요."


class SentryTimeoutError(SentryError):
    user_message = "Sentry API 응답이 시간 내에 오지 않았습니다. 잠시 후 다시 시도해 주세요."


@dataclass(frozen=True)
class Frame:
    filename: str | None
    module: str | None
    function: str | None
    lineno: int | None
    in_app: bool
    context_line: str | None = None

    def location(self) -> str:
        where = self.filename or self.module or "<unknown>"
        if self.lineno:
            where = f"{where}:{self.lineno}"
        if self.function:
            where = f"{where} in {self.function}"
        return where


@dataclass(frozen=True)
class ExceptionInfo:
    type: str | None
    value: str | None
    module: str | None
    mechanism: str | None
    frames: tuple[Frame, ...] = ()


@dataclass(frozen=True)
class RevisionHints:
    """Everything that might identify the deployed commit, unredacted on purpose.

    Release names and commit SHAs are not secrets and the Git lookup needs them
    verbatim.
    """

    release_version: str | None = None
    release_ref: str | None = None
    last_commit_id: str | None = None
    commit_tags: tuple[tuple[str, str], ...] = ()
    dist: str | None = None


@dataclass(frozen=True)
class SentryContext:
    issue_id: str
    issue_url: str
    title: str
    short_id: str | None = None
    culprit: str | None = None
    level: str | None = None
    platform: str | None = None
    project_slug: str | None = None
    project_id: str | None = None
    times_seen: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    event_id: str | None = None
    event_timestamp: str | None = None
    transaction: str | None = None
    environment: str | None = None
    message: str | None = None
    exceptions: tuple[ExceptionInfo, ...] = ()
    tags: tuple[tuple[str, str], ...] = ()
    breadcrumbs: tuple[str, ...] = ()
    revision: RevisionHints = field(default_factory=RevisionHints)
    truncated: bool = False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class SentryClient:
    """Minimal, retrying Sentry REST client."""

    def __init__(
        self,
        settings: SentrySettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)

    def __enter__(self) -> "SentryClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- public API -------------------------------------------------------- #

    def ping(self) -> dict[str, Any]:
        """Verify base URL, org and token without touching an issue."""
        return self._get(f"{self._settings.base_url}/api/0/organizations/{self._settings.org}/")

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        return self._get(self._settings.issue_url(issue_id))

    def get_latest_event(self, issue_id: str) -> dict[str, Any]:
        return self._get(self._settings.latest_event_url(issue_id))

    # -- internals --------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.auth_token}",
            "Accept": "application/json",
        }

    def _get(self, url: str) -> dict[str, Any]:
        attempts = self._settings.max_retries + 1
        last_error: SentryError | None = None
        retry_after: str | None = None

        for attempt in range(attempts):
            if attempt:
                self._sleep(self._backoff(attempt, last_error, retry_after))
            try:
                response = self._client.get(
                    url, headers=self._headers(), timeout=self._settings.timeout_seconds
                )
            except httpx.TimeoutException as exc:
                last_error = SentryTimeoutError(f"Sentry 요청 timeout: {type(exc).__name__}")
            except httpx.TransportError as exc:
                last_error = SentryServerError(f"Sentry 연결 실패: {type(exc).__name__}")
            else:
                error = self._classify(response)
                if error is None:
                    return self._json(response)
                if not self._retryable(error):
                    raise error
                last_error = error
                retry_after = response.headers.get("Retry-After")
            log.warning(
                "sentry request failed, attempt=%s/%s error=%s",
                attempt + 1,
                attempts,
                type(last_error).__name__,
            )

        assert last_error is not None
        raise last_error

    def _backoff(
        self, attempt: int, last_error: SentryError | None, retry_after: str | None
    ) -> float:
        if isinstance(last_error, SentryRateLimitError) and retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(0.5 * (2 ** (attempt - 1)), 8.0)

    @staticmethod
    def _retryable(error: SentryError) -> bool:
        return isinstance(error, (SentryRateLimitError, SentryServerError, SentryTimeoutError))

    @staticmethod
    def _classify(response: httpx.Response) -> SentryError | None:
        status = response.status_code
        if 200 <= status < 300:
            return None
        if status == 401:
            return SentryAuthError("Sentry 401 Unauthorized", status_code=status)
        if status == 403:
            return SentryForbiddenError("Sentry 403 Forbidden", status_code=status)
        if status == 404:
            return SentryNotFoundError("Sentry 404 Not Found", status_code=status)
        if status == 429:
            return SentryRateLimitError("Sentry 429 Too Many Requests", status_code=status)
        if status >= 500:
            return SentryServerError(f"Sentry {status}", status_code=status)
        return SentryError(f"Sentry 예상치 못한 응답 코드 {status}", status_code=status)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SentryError(f"Sentry 응답이 JSON 이 아닙니다: {exc}") from exc
        if not isinstance(payload, dict):
            raise SentryError("Sentry 응답 형식이 예상과 다릅니다.")
        return payload


# --------------------------------------------------------------------------- #
# Sanitizing / context building
# --------------------------------------------------------------------------- #


def _text(value: Any, *, limit: int = MAX_VALUE_CHARS) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = redact(str(value)).strip()
    if not text:
        return None
    return text[:limit]


def _entry(event: Mapping[str, Any], entry_type: str) -> Mapping[str, Any] | None:
    entries = event.get("entries")
    if isinstance(entries, Sequence):
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get("type") == entry_type:
                data = entry.get("data")
                if isinstance(data, Mapping):
                    return data
    direct = event.get(entry_type)
    return direct if isinstance(direct, Mapping) else None


def _frames(stacktrace: Any) -> tuple[Frame, ...]:
    if not isinstance(stacktrace, Mapping):
        return ()
    raw_frames = stacktrace.get("frames")
    if not isinstance(raw_frames, Sequence):
        return ()

    frames: list[Frame] = []
    for raw in raw_frames[-MAX_PLAIN_FRAMES:]:
        if not isinstance(raw, Mapping):
            continue
        lineno = raw.get("lineNo", raw.get("lineno"))
        frames.append(
            Frame(
                filename=_text(raw.get("filename") or raw.get("absPath"), limit=400),
                module=_text(raw.get("module"), limit=400),
                function=_text(raw.get("function"), limit=300),
                lineno=int(lineno) if isinstance(lineno, int) else None,
                in_app=bool(raw.get("inApp", raw.get("in_app", False))),
                # `vars` is never read: local variables routinely hold secrets.
                context_line=_text(raw.get("context_line") or raw.get("contextLine"), limit=400),
            )
        )
    return tuple(frames)


def _exceptions(event: Mapping[str, Any]) -> tuple[ExceptionInfo, ...]:
    data = _entry(event, "exception")
    values = data.get("values") if isinstance(data, Mapping) else None
    if not isinstance(values, Sequence):
        return ()

    result: list[ExceptionInfo] = []
    for raw in values[:5]:
        if not isinstance(raw, Mapping):
            continue
        mechanism = raw.get("mechanism")
        result.append(
            ExceptionInfo(
                type=_text(raw.get("type"), limit=300),
                value=_text(raw.get("value")),
                module=_text(raw.get("module"), limit=300),
                mechanism=_text(mechanism.get("type"), limit=100) if isinstance(mechanism, Mapping) else None,
                frames=_frames(raw.get("stacktrace") or raw.get("rawStacktrace")),
            )
        )
    return tuple(result)


def _event_tags(event: Mapping[str, Any]) -> dict[str, str]:
    tags: dict[str, str] = {}
    raw_tags = event.get("tags")
    if isinstance(raw_tags, Sequence):
        for raw in raw_tags:
            if not isinstance(raw, Mapping):
                continue
            key = raw.get("key")
            value = raw.get("value")
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                tags[key] = str(value)
    elif isinstance(raw_tags, Mapping):
        for key, value in raw_tags.items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                tags[key] = str(value)
    return tags


def _breadcrumbs(event: Mapping[str, Any]) -> tuple[str, ...]:
    data = _entry(event, "breadcrumbs")
    values = data.get("values") if isinstance(data, Mapping) else None
    if not isinstance(values, Sequence):
        return ()

    crumbs: list[str] = []
    for raw in list(values)[-MAX_BREADCRUMBS:]:
        if not isinstance(raw, Mapping):
            continue
        parts = [
            str(raw.get("timestamp") or "")[:32],
            str(raw.get("level") or ""),
            str(raw.get("category") or ""),
        ]
        label = " ".join(part for part in parts if part)
        message = _text(raw.get("message") or raw.get("type"), limit=300) or ""
        crumb = f"{label} {message}".strip()
        if crumb:
            crumbs.append(crumb)
    return tuple(crumbs)


def _revision_hints(event: Mapping[str, Any], tags: Mapping[str, str]) -> RevisionHints:
    release = event.get("release")
    version = None
    ref = None
    last_commit = None
    if isinstance(release, Mapping):
        version = release.get("version") or release.get("shortVersion")
        ref = release.get("ref")
        commit = release.get("lastCommit")
        if isinstance(commit, Mapping):
            last_commit = commit.get("id")
    version = version or tags.get("sentry:release") or tags.get("release")

    commit_tags = tuple(
        (key, tags[key]) for key in REVISION_TAG_KEYS if tags.get(key)
    )
    return RevisionHints(
        release_version=str(version)[:200] if version else None,
        release_ref=str(ref)[:200] if ref else None,
        last_commit_id=str(last_commit)[:100] if last_commit else None,
        commit_tags=commit_tags,
        dist=str(tags.get("dist") or tags.get("sentry:dist") or "")[:100] or None,
    )


def _project_of(issue: Mapping[str, Any], event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    project = issue.get("project")
    if isinstance(project, Mapping):
        slug = project.get("slug")
        pid = project.get("id")
        return (str(slug) if slug else None, str(pid) if pid else None)
    pid = event.get("projectID") or issue.get("projectID")
    return (None, str(pid) if pid else None)


def build_context(
    issue: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    issue_ref: IssueRef,
) -> SentryContext:
    """Turn raw Sentry payloads into a redacted context object.

    Size capping happens in :func:`render_context`, which is what the Codex
    prompt is built from.
    """
    tags = _event_tags(event)
    project_slug, project_id = _project_of(issue, event)
    message_entry = _entry(event, "message")
    message = None
    if isinstance(message_entry, Mapping):
        message = _text(message_entry.get("formatted") or message_entry.get("message"))

    return SentryContext(
        issue_id=issue_ref.issue_id,
        issue_url=_safe_issue_url(issue, issue_ref),
        title=_text(issue.get("title") or event.get("title"), limit=500) or "(제목 없음)",
        short_id=_text(issue.get("shortId"), limit=100),
        culprit=_text(issue.get("culprit") or event.get("culprit"), limit=500),
        level=_text(issue.get("level") or tags.get("level"), limit=50),
        platform=_text(issue.get("platform") or event.get("platform"), limit=50),
        project_slug=project_slug or issue_ref.project_slug,
        project_id=project_id or issue_ref.project_id,
        times_seen=_text(issue.get("count"), limit=30),
        first_seen=_text(issue.get("firstSeen"), limit=50),
        last_seen=_text(issue.get("lastSeen"), limit=50),
        event_id=_text(event.get("eventID") or event.get("id"), limit=64),
        event_timestamp=_text(event.get("dateCreated") or event.get("dateReceived"), limit=50),
        transaction=_text(tags.get("transaction") or event.get("transaction"), limit=400),
        environment=_text(tags.get("environment"), limit=100),
        message=message,
        exceptions=_exceptions(event),
        tags=tuple(
            (key, redact(value)[:300])
            for key, value in sorted(tags.items())
            if key in ALLOWED_TAG_KEYS and not is_secret_key(key)
        ),
        breadcrumbs=_breadcrumbs(event),
        revision=_revision_hints(event, tags),
        truncated=False,
    )


def _safe_issue_url(issue: Mapping[str, Any], issue_ref: IssueRef) -> str:
    """Prefer the URL Sentry itself reports; fall back to the validated link."""
    permalink = issue.get("permalink")
    if isinstance(permalink, str) and permalink.startswith(("http://", "https://")):
        return permalink
    return issue_ref.url


@dataclass(frozen=True)
class RenderedContext:
    text: str
    truncated: bool


def _frame_lines(frames: Iterable[Frame]) -> tuple[list[str], list[str]]:
    frames = list(frames)
    app_frames = [frame for frame in frames if frame.in_app]
    detailed_source = app_frames[-MAX_FRAMES_WITH_CODE:] if app_frames else frames[-MAX_FRAMES_WITH_CODE:]
    detailed_ids = {id(frame) for frame in detailed_source}

    detailed: list[str] = []
    for frame in detailed_source:
        line = f"  - {frame.location()}"
        if frame.context_line:
            line += f"\n        | {frame.context_line.strip()}"
        detailed.append(line)

    others = [
        f"  . {frame.location()}{'' if frame.in_app else ' (library)'}"
        for frame in frames
        if id(frame) not in detailed_ids
    ]
    return detailed, others


def render_context(context: SentryContext, *, max_bytes: int = 30000) -> RenderedContext:
    """Render the context as plain text, trimming low-priority blocks first."""
    header = [
        "## Sentry Issue",
        f"- issue_id: {context.issue_id}",
        f"- url: {context.issue_url}",
        f"- title: {context.title}",
    ]
    for label, value in (
        ("short_id", context.short_id),
        ("level", context.level),
        ("platform", context.platform),
        ("project", context.project_slug or context.project_id),
        ("culprit", context.culprit),
        ("transaction", context.transaction),
        ("environment", context.environment),
        ("release", context.revision.release_version),
        ("dist", context.revision.dist),
        ("event_id", context.event_id),
        ("event_time", context.event_timestamp),
        ("times_seen", context.times_seen),
        ("first_seen", context.first_seen),
        ("last_seen", context.last_seen),
    ):
        if value:
            header.append(f"- {label}: {value}")

    blocks: list[str] = ["\n".join(header)]

    for index, exc in enumerate(context.exceptions, start=1):
        lines = [f"\n## Exception {index}"]
        if exc.type:
            lines.append(f"- type: {exc.type}")
        if exc.module:
            lines.append(f"- module: {exc.module}")
        if exc.mechanism:
            lines.append(f"- mechanism: {exc.mechanism}")
        if exc.value:
            lines.append(f"- value: {exc.value}")
        detailed, others = _frame_lines(exc.frames)
        if detailed:
            lines.append("- stack frames (가장 최근 호출이 마지막, 애플리케이션 프레임 우선):")
            lines.extend(detailed)
        if others:
            lines.append("- 그 외 프레임:")
            lines.extend(others)
        blocks.append("\n".join(lines))

    if context.message:
        blocks.append(f"\n## Message\n{context.message}")
    if context.tags:
        blocks.append("\n## Tags\n" + "\n".join(f"- {key}: {value}" for key, value in context.tags))
    if context.breadcrumbs:
        blocks.append("\n## Breadcrumbs\n" + "\n".join(f"- {crumb}" for crumb in context.breadcrumbs))

    out: list[str] = []
    used = 0
    truncated = False
    for block in blocks:
        encoded = len(block.encode("utf-8")) + 1
        if used + encoded <= max_bytes:
            out.append(block)
            used += encoded
            continue
        remaining = max_bytes - used
        truncated = True
        if remaining > 200:
            out.append(_truncate_lines(block, remaining))
        break

    text = "\n".join(out)
    if truncated:
        text += "\n\n[NOTE] 컨텍스트가 크기 제한으로 일부 생략되었습니다."
    return RenderedContext(text=text, truncated=truncated)


def _truncate_lines(block: str, budget_bytes: int) -> str:
    kept: list[str] = []
    used = 0
    for line in block.splitlines():
        encoded = len(line.encode("utf-8")) + 1
        if used + encoded > budget_bytes:
            break
        kept.append(line)
        used += encoded
    return "\n".join(kept)
