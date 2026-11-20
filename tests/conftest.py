"""Shared fixtures. No test touches a real Slack, Sentry or Codex endpoint."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from error_analysis_agent.config import AppConfig, load_config

SENTRY_BASE = "https://sentry.example.com"
ORG = "my-org"
PROJECT_SLUG = "example-service"
ISSUE_URL = f"{SENTRY_BASE}/organizations/{ORG}/issues/4242/"


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "SLACK_ALLOWED_CHANNEL_IDS": "C100",
        "SLACK_ALLOWED_USER_IDS": "U100,U200",
        "SENTRY_BASE_URL": SENTRY_BASE,
        "SENTRY_ORG": ORG,
        "SENTRY_AUTH_TOKEN": "sentry-token",
        "SENTRY_MAX_RETRIES": "3",
        "CODEX_HOME": "/tmp/codex-home",
        "ANALYSIS_MAX_PARALLEL": "2",
        "ANALYSIS_TIMEOUT_SECONDS": "300",
    }
    env.update(overrides)
    return env


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repos" / PROJECT_SLUG
    repo_dir.mkdir(parents=True)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "AGENTS.analysis.md").write_text("# analysis rules\n", encoding="utf-8")

    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            projects:
              {PROJECT_SLUG}:
                sentry_project_slug: {PROJECT_SLUG}
                repo_dir: {repo_dir}
                repository_url: git@github.com:ORG/{PROJECT_SLUG}.git
                default_branch: main

            analysis:
              max_parallel_jobs: 2
              timeout_seconds: 300
              require_release_revision: true
              max_context_bytes: 30000
              snapshot_root: {tmp_path / "snapshots"}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config(config_path: Path) -> AppConfig:
    return load_config(base_env(), config_path=config_path)


def make_config(config_path: Path, **env_overrides: str) -> AppConfig:
    return load_config(base_env(**env_overrides), config_path=config_path)


# --------------------------------------------------------------------------- #
# Slack payloads
# --------------------------------------------------------------------------- #


def slack_command_event(**overrides: object) -> dict[str, object]:
    event = {
        "type": "message",
        "text": "!AI분석",
        "user": "U100",
        "channel": "C100",
        "ts": "1700000100.000200",
        "thread_ts": "1700000000.000100",
    }
    event.update(overrides)
    return event


def sentry_alert_message(url: str = ISSUE_URL) -> dict[str, object]:
    """A parent message shaped like Sentry's Slack integration output."""
    return {
        "type": "message",
        "ts": "1700000000.000100",
        "bot_id": "B0SENTRY",
        "text": "[example-service] NullPointerException",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{url}|NullPointerException> 가 발생했습니다.",
                },
            }
        ],
        "attachments": [
            {
                "title": "NullPointerException",
                "title_link": url,
                "text": "culprit: com.example.OrderService in place",
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Sentry payloads
# --------------------------------------------------------------------------- #


def sentry_issue() -> dict[str, object]:
    return {
        "id": "4242",
        "shortId": "EXAMPLE-SERVICE-7A",
        "title": "NullPointerException: order is null",
        "culprit": "com.example.order.OrderService in place",
        "level": "error",
        "platform": "java",
        "count": "17",
        "firstSeen": "2026-10-20T09:00:00Z",
        "lastSeen": "2026-10-22T01:02:03Z",
        "permalink": ISSUE_URL,
        "project": {"id": "99", "slug": PROJECT_SLUG, "name": "Example Service"},
    }


def sentry_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "eventID": "ab" * 16,
        "dateCreated": "2026-10-22T01:02:03Z",
        "title": "NullPointerException: order is null",
        "culprit": "com.example.order.OrderService in place",
        "platform": "java",
        "projectID": "99",
        "release": {
            "version": "example-service@1.4.2+1f2e3d4c5b6a7890abcdef1234567890abcdef12",
            "ref": "v1.4.2",
            "lastCommit": {"id": "1f2e3d4c5b6a7890abcdef1234567890abcdef12"},
        },
        "tags": [
            {"key": "environment", "value": "production"},
            {"key": "level", "value": "error"},
            {"key": "transaction", "value": "POST /api/v1/orders"},
            {"key": "server_name", "value": "api-7c9f"},
            {"key": "sentry:release", "value": "example-service@1.4.2"},
            {"key": "commit", "value": "1f2e3d4c5b6a7890abcdef1234567890abcdef12"},
            # must not reach the prompt
            {"key": "user", "value": "id:4711"},
            {"key": "url", "value": "https://app.example.com/orders?email=leak@example.com"},
            {"key": "auth_token", "value": "super-secret-token-value"},
        ],
        "entries": [
            {
                "type": "exception",
                "data": {
                    "values": [
                        {
                            "type": "NullPointerException",
                            "value": (
                                "order is null for user leak@example.com "
                                "(password=hunter2, ip 203.0.113.9)"
                            ),
                            "module": "java.lang",
                            "mechanism": {"type": "generic", "handled": False},
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "org/springframework/web/Dispatcher.java",
                                        "function": "doDispatch",
                                        "lineNo": 1071,
                                        "inApp": False,
                                    },
                                    {
                                        "filename": "com/example/order/OrderService.java",
                                        "module": "com.example.order.OrderService",
                                        "function": "place",
                                        "lineNo": 88,
                                        "inApp": True,
                                        "context_line": "    return order.getId();",
                                        "vars": {
                                            "password": "hunter2",
                                            "token": "eyJhbGciOiJIUzI1NiJ9.payloadpayload.sig",
                                        },
                                    },
                                ]
                            },
                        }
                    ]
                },
            },
            {
                "type": "request",
                "data": {
                    "url": "https://app.example.com/api/v1/orders",
                    "method": "POST",
                    "headers": [["Authorization", "Bearer sk-live-abcdefghijklmnop"]],
                    "cookies": [["JSESSIONID", "9f8e7d6c5b4a"]],
                    "data": {"card_number": "4111 1111 1111 1111"},
                },
            },
            {
                "type": "breadcrumbs",
                "data": {
                    "values": [
                        {
                            "timestamp": "2026-10-22T01:02:02Z",
                            "level": "info",
                            "category": "http",
                            "message": "POST /api/v1/orders for leak@example.com",
                        }
                    ]
                },
            },
        ],
    }
    event.update(overrides)
    return event
