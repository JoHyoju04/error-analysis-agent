"""Find and validate Sentry issue links inside a Slack message.

A Slack message can carry the Sentry link in ``text``, in Block Kit ``blocks``
or in legacy ``attachments``, so every string field is swept. The first URL is
never trusted blindly: the host must be an allowed Sentry host, the
organization in the path must match the configured one, and the path shape must
be a known Sentry issue route. Everything else is refused, which is what keeps
the bot from issuing requests to attacker-supplied hosts (SSRF).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, urlsplit

# Slack renders links as <https://host/path|label>; the label is dropped here.
_URL_RE = re.compile(r"https?://[^\s<>|\"'\\\]]+")
_ISSUE_ID_RE = re.compile(r"^\d{1,20}$")
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

_MAX_STRINGS = 500
_MAX_DEPTH = 12


class LinkParseError(ValueError):
    """Raised when no trustworthy Sentry issue link can be extracted."""


@dataclass(frozen=True)
class IssueRef:
    """A validated reference to one Sentry issue."""

    issue_id: str
    url: str
    organization: str | None = None
    project_slug: str | None = None
    project_id: str | None = None
    event_id: str | None = None


def iter_message_strings(message: Any, _depth: int = 0) -> Iterator[str]:
    """Yield every string reachable from a Slack message payload."""
    if _depth > _MAX_DEPTH:
        return
    if isinstance(message, str):
        yield message
    elif isinstance(message, dict):
        for value in message.values():
            yield from iter_message_strings(value, _depth + 1)
    elif isinstance(message, (list, tuple)):
        for value in message:
            yield from iter_message_strings(value, _depth + 1)


def iter_candidate_urls(message: Any) -> Iterator[str]:
    """Yield http(s) URLs found anywhere in a Slack message, in reading order."""
    seen: set[str] = set()
    for index, text in enumerate(iter_message_strings(message)):
        if index >= _MAX_STRINGS:
            return
        for match in _URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;:)]}>")
            if url not in seen:
                seen.add(url)
                yield url


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _parse_issue_path(segments: list[str]) -> tuple[str, str | None, str | None] | None:
    """Return ``(issue_id, organization, project_slug)`` for a known issue route."""
    try:
        issues_at = segments.index("issues")
    except ValueError:
        return None

    if issues_at + 1 >= len(segments):
        return None
    issue_id = segments[issues_at + 1]
    if not _ISSUE_ID_RE.match(issue_id):
        return None

    prefix = segments[:issues_at]
    if not prefix:
        # /issues/{id}/ on an org-scoped host
        return issue_id, None, None
    if len(prefix) == 2 and prefix[0] == "organizations":
        # /organizations/{org}/issues/{id}/
        return issue_id, prefix[1], None
    if len(prefix) == 2:
        # legacy /{org}/{project}/issues/{id}/
        return issue_id, prefix[0], prefix[1]
    return None


def _event_id_from_path(segments: list[str]) -> str | None:
    if "events" not in segments:
        return None
    index = segments.index("events")
    if index + 1 >= len(segments):
        return None
    candidate = segments[index + 1]
    return candidate.lower() if _EVENT_ID_RE.match(candidate) else None


def parse_issue_url(url: str, *, allowed_hosts: Iterable[str], organization: str) -> IssueRef | None:
    """Validate a single URL and return an :class:`IssueRef`, or ``None``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host or host not in {h.lower() for h in allowed_hosts}:
        return None

    segments = _path_segments(parts.path)
    parsed = _parse_issue_path(segments)
    if parsed is None:
        return None
    issue_id, url_org, project_slug = parsed

    if url_org is not None and url_org.lower() != organization.lower():
        return None

    query = parse_qs(parts.query)
    project_id = next((v for v in query.get("project", []) if v.isdigit()), None)

    return IssueRef(
        issue_id=issue_id,
        url=url,
        organization=url_org or organization,
        project_slug=project_slug,
        project_id=project_id,
        event_id=_event_id_from_path(segments),
    )


def extract_issue_ref(message: Any, *, allowed_hosts: Iterable[str], organization: str) -> IssueRef:
    """Return the first trustworthy Sentry issue reference in a Slack message.

    Raises :class:`LinkParseError` when none is found, so the caller can report
    it instead of guessing an issue id.
    """
    allowed = {h.lower() for h in allowed_hosts if h}
    if not allowed:
        raise LinkParseError("허용된 Sentry 호스트가 없습니다.")

    saw_url = False
    for url in iter_candidate_urls(message):
        saw_url = True
        ref = parse_issue_url(url, allowed_hosts=allowed, organization=organization)
        if ref is not None:
            return ref

    if saw_url:
        raise LinkParseError(
            "스레드에서 찾은 링크가 허용된 Sentry 조직/호스트의 Issue 링크가 아닙니다."
        )
    raise LinkParseError("이 스레드에서 Sentry Issue 링크를 찾지 못했습니다.")
