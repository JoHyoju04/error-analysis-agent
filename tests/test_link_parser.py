from __future__ import annotations

import pytest
from conftest import ISSUE_URL, ORG, sentry_alert_message

from error_analysis_agent.link_parser import (
    LinkParseError,
    extract_issue_ref,
    iter_candidate_urls,
    parse_issue_url,
)

HOSTS = {"sentry.example.com"}


def extract(message):
    return extract_issue_ref(message, allowed_hosts=HOSTS, organization=ORG)


def test_link_in_plain_text():
    message = {"text": f"에러 발생 <{ISSUE_URL}|NullPointerException>"}
    assert extract(message).issue_id == "4242"


def test_link_in_blocks():
    message = {
        "text": "",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<{ISSUE_URL}|보기>"}}
        ],
    }
    assert extract(message).issue_id == "4242"


def test_link_in_attachments():
    message = {"text": "", "attachments": [{"title_link": ISSUE_URL}]}
    assert extract(message).issue_id == "4242"


def test_realistic_sentry_alert_message():
    ref = extract(sentry_alert_message())
    assert ref.issue_id == "4242"
    assert ref.organization == ORG


def test_legacy_org_project_path():
    url = f"https://sentry.example.com/{ORG}/example-service/issues/777/"
    ref = extract({"text": url})
    assert (ref.issue_id, ref.project_slug) == ("777", "example-service")


def test_event_path_and_project_query():
    url = f"https://sentry.example.com/organizations/{ORG}/issues/4242/events/{'ab' * 16}/?project=99"
    ref = extract({"text": url})
    assert ref.event_id == "ab" * 16
    assert ref.project_id == "99"


def test_issue_only_path():
    ref = extract({"text": "https://sentry.example.com/issues/5150/"})
    assert ref.issue_id == "5150"
    assert ref.organization == ORG


def test_first_link_is_not_blindly_trusted():
    """An attacker-controlled link before the real one must be skipped."""
    message = {
        "text": "see https://evil.example.net/organizations/my-org/issues/1/ and "
        f"{ISSUE_URL} for details"
    }
    assert extract(message).issue_id == "4242"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.net/organizations/my-org/issues/1/",  # wrong host
        "https://sentry.example.com.evil.net/organizations/my-org/issues/1/",  # suffix trick
        "https://sentry.example.com/organizations/other-org/issues/1/",  # wrong org
        "https://sentry.example.com/organizations/my-org/issues/abc/",  # non-numeric id
        "https://sentry.example.com/organizations/my-org/issues/",  # no id
        "https://sentry.example.com/settings/my-org/projects/",  # not an issue route
        "https://sentry.example.com/a/b/c/issues/9/",  # unknown prefix shape
        "http://169.254.169.254/latest/meta-data/",  # SSRF probe
    ],
)
def test_untrusted_urls_are_refused(url):
    assert parse_issue_url(url, allowed_hosts=HOSTS, organization=ORG) is None
    with pytest.raises(LinkParseError):
        extract({"text": url})


def test_message_without_any_link():
    with pytest.raises(LinkParseError, match="찾지 못했습니다"):
        extract({"text": "그냥 잡담입니다"})


def test_no_allowed_hosts_fails_closed():
    with pytest.raises(LinkParseError):
        extract_issue_ref({"text": ISSUE_URL}, allowed_hosts=set(), organization=ORG)


def test_candidate_urls_are_deduplicated_and_trimmed():
    urls = list(
        iter_candidate_urls({"text": f"({ISSUE_URL}).", "attachments": [{"text": ISSUE_URL}]})
    )
    assert urls == [ISSUE_URL]


def test_scheme_relative_and_non_http_urls_are_ignored():
    assert not list(iter_candidate_urls({"text": "javascript:alert(1) ftp://x/y"}))
