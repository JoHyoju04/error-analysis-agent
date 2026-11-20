"""Thin wrapper around the Slack Web API for thread reads and replies."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .formatters import render_analysis

log = logging.getLogger(__name__)


class SlackError(RuntimeError):
    user_message = "Slack API 호출에 실패했습니다."


class SlackPermissionError(SlackError):
    user_message = (
        "Slack 권한이 부족합니다. 봇이 채널에 초대되어 있는지, "
        "`channels:history`/`groups:history`/`chat:write` 스코프가 있는지 확인해야 합니다."
    )


class SlackParentNotFound(SlackError):
    user_message = "스레드의 원본(Sentry 알림) 메시지를 찾지 못했습니다."


_PERMISSION_ERRORS = {
    "missing_scope",
    "not_in_channel",
    "channel_not_found",
    "not_allowed_token_type",
    "invalid_auth",
    "account_inactive",
    "token_revoked",
}


class WebApi(Protocol):
    """The subset of ``slack_sdk.WebClient`` this bot uses."""

    def auth_test(self) -> Mapping[str, Any]: ...

    def conversations_replies(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def chat_postMessage(self, **kwargs: Any) -> Mapping[str, Any]: ...  # noqa: N802


@dataclass(frozen=True)
class ParentMessage:
    ts: str
    raw: Mapping[str, Any]


class SlackThreadClient:
    def __init__(self, client: WebApi, *, max_message_chars: int = 3500) -> None:
        self._client = client
        self._max_chars = max_message_chars

    # -- auth -------------------------------------------------------------- #

    def bot_user_id(self) -> str | None:
        try:
            response = self._client.auth_test()
        except Exception as exc:  # noqa: BLE001 - slack_sdk raises SlackApiError
            raise self._translate(exc, "auth.test") from exc
        return response.get("user_id")

    # -- reads ------------------------------------------------------------- #

    def fetch_parent(self, channel: str, thread_ts: str) -> ParentMessage:
        """Read the thread's root message, i.e. the original Sentry alert."""
        try:
            response = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=1, inclusive=True
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, "conversations.replies") from exc

        messages = response.get("messages") or []
        if not messages:
            raise SlackParentNotFound(f"no parent message for channel={channel} ts={thread_ts}")
        parent = messages[0]
        ts = parent.get("ts")
        if not isinstance(ts, str):
            raise SlackParentNotFound("parent message has no ts")
        return ParentMessage(ts=ts, raw=parent)

    # -- writes ------------------------------------------------------------ #

    def post(self, channel: str, thread_ts: str, text: str) -> str | None:
        try:
            response = self._client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, "chat.postMessage") from exc
        ts = response.get("ts")
        return ts if isinstance(ts, str) else None

    def post_analysis(self, channel: str, thread_ts: str, *, header: str, body: str) -> list[str]:
        posted: list[str] = []
        for chunk in render_analysis(body, header=header, limit=self._max_chars):
            ts = self.post(channel, thread_ts, chunk)
            if ts:
                posted.append(ts)
        return posted

    # -- errors ------------------------------------------------------------ #

    @staticmethod
    def _translate(exc: Exception, api: str) -> SlackError:
        code = _error_code(exc)
        log.warning("slack %s failed error=%s", api, code or type(exc).__name__)
        if code in _PERMISSION_ERRORS:
            return SlackPermissionError(f"{api}: {code}")
        if code in ("thread_not_found", "message_not_found"):
            return SlackParentNotFound(f"{api}: {code}")
        return SlackError(f"{api}: {code or type(exc).__name__}")


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        code = response.get("error")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - pragma: no cover
        return None
    return code if isinstance(code, str) else None
