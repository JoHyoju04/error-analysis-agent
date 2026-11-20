from __future__ import annotations

import httpx
import pytest
from conftest import ISSUE_URL, ORG, PROJECT_SLUG, sentry_event, sentry_issue

from error_analysis_agent.link_parser import IssueRef
from error_analysis_agent.sentry_client import (
    SentryAuthError,
    SentryClient,
    SentryForbiddenError,
    SentryNotFoundError,
    SentryRateLimitError,
    SentryServerError,
    SentryTimeoutError,
    build_context,
    render_context,
)

ISSUE_REF = IssueRef(issue_id="4242", url=ISSUE_URL, organization=ORG)


class Recorder:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def client_for(settings, handler, sleep=None):
    transport = httpx.MockTransport(handler)
    return SentryClient(
        settings,
        client=httpx.Client(transport=transport),
        sleep=sleep or (lambda _seconds: None),
    )


def test_get_issue_sends_bearer_token(config):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=sentry_issue())

    with client_for(config.sentry, handler) as client:
        issue = client.get_issue("4242")

    assert issue["shortId"] == "EXAMPLE-SERVICE-7A"
    assert seen[0].headers["authorization"] == "Bearer sentry-token"
    assert str(seen[0].url) == f"{config.sentry.base_url}/api/0/organizations/{ORG}/issues/4242/"


def test_latest_event_url(config):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/issues/4242/events/latest/")
        return httpx.Response(200, json=sentry_event())

    with client_for(config.sentry, handler) as client:
        assert client.get_latest_event("4242")["eventID"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, SentryAuthError),
        (403, SentryForbiddenError),
        (404, SentryNotFoundError),
    ],
)
def test_client_errors_are_not_retried(config, status, expected):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"detail": "internal server detail"})

    sleep = Recorder()
    with client_for(config.sentry, handler, sleep) as client, pytest.raises(expected) as info:
        client.get_issue("4242")

    assert calls["n"] == 1
    assert sleep.slept == []
    # the raw server body must never surface in the Slack-facing message
    assert "internal server detail" not in info.value.user_message


def test_rate_limit_is_retried_and_honours_retry_after(config):
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json=sentry_issue()),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    sleep = Recorder()
    with client_for(config.sentry, handler, sleep) as client:
        assert client.get_issue("4242")["id"] == "4242"
    assert sleep.slept == [2.0]


def test_server_errors_exhaust_retries(config):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    sleep = Recorder()
    with client_for(config.sentry, handler, sleep) as client, pytest.raises(SentryServerError):
        client.get_issue("4242")

    assert calls["n"] == config.sentry.max_retries + 1
    assert sleep.slept == [0.5, 1.0, 2.0]  # exponential backoff


def test_timeout_is_mapped(config):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with client_for(config.sentry, handler) as client, pytest.raises(SentryTimeoutError):
        client.get_issue("4242")


def test_transport_error_is_retried_then_mapped(config):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    sleep = Recorder()
    with client_for(config.sentry, handler, sleep) as client, pytest.raises(SentryServerError):
        client.get_issue("4242")
    assert len(sleep.slept) == config.sentry.max_retries


# --------------------------------------------------------------------------- #
# sanitizing
# --------------------------------------------------------------------------- #


@pytest.fixture
def context():
    return build_context(sentry_issue(), sentry_event(), issue_ref=ISSUE_REF)


def test_context_keeps_what_an_analyst_needs(context):
    assert context.issue_id == "4242"
    assert context.project_slug == PROJECT_SLUG
    assert context.title.startswith("NullPointerException")
    assert context.culprit == "com.example.order.OrderService in place"
    assert context.environment == "production"
    assert context.transaction == "POST /api/v1/orders"
    assert context.exceptions[0].type == "NullPointerException"

    app_frames = [frame for frame in context.exceptions[0].frames if frame.in_app]
    assert app_frames[0].filename == "com/example/order/OrderService.java"
    assert app_frames[0].lineno == 88
    assert app_frames[0].context_line == "return order.getId();"


def test_revision_hints_are_extracted_verbatim(context):
    revision = context.revision
    assert revision.release_version == "example-service@1.4.2+1f2e3d4c5b6a7890abcdef1234567890abcdef12"
    assert revision.last_commit_id == "1f2e3d4c5b6a7890abcdef1234567890abcdef12"
    assert ("commit", "1f2e3d4c5b6a7890abcdef1234567890abcdef12") in revision.commit_tags


def test_only_whitelisted_tags_survive(context):
    keys = {key for key, _ in context.tags}
    assert {"environment", "level", "transaction", "server_name"} <= keys
    assert "user" not in keys
    assert "url" not in keys
    assert "auth_token" not in keys


SECRETS = (
    "leak@example.com",
    "hunter2",
    "sk-live-abcdefghijklmnop",
    "9f8e7d6c5b4a",
    "4111 1111 1111 1111",
    "203.0.113.9",
    "eyJhbGciOiJIUzI1NiJ9",
)


def test_no_secret_reaches_the_prompt(context):
    text = render_context(context, max_bytes=30000).text
    for secret in SECRETS:
        assert secret not in text, secret
    # the request entry is dropped wholesale, headers and all
    assert "Authorization" not in text
    assert "JSESSIONID" not in text
    # and the exception message is still useful
    assert "order is null" in text
    assert "[REDACTED]" in text


def test_frame_locals_are_never_collected(context):
    frames = context.exceptions[0].frames
    assert all(not hasattr(frame, "vars") for frame in frames)


def test_render_respects_the_byte_cap(context):
    rendered = render_context(context, max_bytes=1200)
    assert rendered.truncated is True
    assert len(rendered.text.encode("utf-8")) <= 1200 + 120  # plus the truncation note
    assert "생략" in rendered.text
    # the header always survives
    assert "issue_id: 4242" in rendered.text


def test_render_is_untruncated_when_it_fits(context):
    rendered = render_context(context, max_bytes=60000)
    assert rendered.truncated is False
    assert "## Exception 1" in rendered.text


def test_missing_fields_do_not_crash():
    context = build_context({}, {}, issue_ref=ISSUE_REF)
    assert context.title == "(제목 없음)"
    assert context.exceptions == ()
    assert render_context(context).text.startswith("## Sentry Issue")
