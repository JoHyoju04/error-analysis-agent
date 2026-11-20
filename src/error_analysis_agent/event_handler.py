"""Decide whether an incoming Slack message event starts an analysis.

Pure functions only: no network, no state. The Socket Mode listener calls
:func:`decide` and acts on the returned decision, which keeps the listener fast
and this logic directly unit-testable.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .config import SlackSettings


class Verdict(str, Enum):
    accept = "accept"
    ignore = "ignore"
    reject = "reject"


class Reason(str, Enum):
    accepted = "accepted"

    # silently ignored: not for us / would loop
    not_a_message = "not_a_message"
    message_subtype = "message_subtype"
    bot_message = "bot_message"
    own_message = "own_message"
    not_the_command = "not_the_command"
    channel_not_allowed = "channel_not_allowed"

    # answered in Slack so the person knows why nothing happened
    not_in_thread = "not_in_thread"
    user_not_allowed = "user_not_allowed"
    no_allowlist_configured = "no_allowlist_configured"
    malformed_event = "malformed_event"


# Reasons the user gets told about. Everything else stays in the log only, so a
# busy channel does not get a reply for every unrelated message.
_USER_FACING = {
    Reason.not_in_thread: (
        "`{command}` 은 Sentry 알림 *스레드 안에서* 답글로 입력해야 합니다. "
        "분석할 Sentry 알림 메시지의 스레드를 열고 다시 시도해 주세요."
    ),
    Reason.user_not_allowed: "이 명령을 실행할 권한이 없습니다. 관리자에게 사용자 허용 목록 추가를 요청해 주세요.",
    Reason.no_allowlist_configured: (
        "허용된 사용자 목록이 비어 있어 명령을 거부했습니다. "
        "관리자가 `SLACK_ALLOWED_USER_IDS` 를 설정해야 합니다."
    ),
    Reason.malformed_event: "Slack 이벤트를 해석할 수 없어 분석을 시작하지 못했습니다.",
}


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: Reason
    channel: str | None = None
    thread_ts: str | None = None
    user: str | None = None
    event_ts: str | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict is Verdict.accept

    def user_message(self, command: str) -> str | None:
        template = _USER_FACING.get(self.reason)
        return template.format(command=command) if template else None


def normalize_command(text: str | None) -> str:
    """NFC-normalize and trim so macOS/iOS 한글 입력(NFD)도 동일하게 비교된다."""
    if not text:
        return ""
    return unicodedata.normalize("NFC", text).strip()


def decide(
    event: Mapping[str, Any],
    settings: SlackSettings,
    *,
    bot_user_id: str | None = None,
) -> Decision:
    """Classify one Slack ``message`` event."""
    if not isinstance(event, Mapping):
        return Decision(Verdict.ignore, Reason.not_a_message)
    if event.get("type") != "message":
        return Decision(Verdict.ignore, Reason.not_a_message)

    # Edited / joined / file-share / tombstone events and anything a bot posted
    # are dropped before any other check: replying to them can loop forever.
    if event.get("subtype"):
        return Decision(Verdict.ignore, Reason.message_subtype)
    if event.get("bot_id") or event.get("app_id") or event.get("bot_profile"):
        return Decision(Verdict.ignore, Reason.bot_message)

    user = event.get("user")
    if bot_user_id and user == bot_user_id:
        return Decision(Verdict.ignore, Reason.own_message)

    if normalize_command(event.get("text")) != normalize_command(settings.command):
        return Decision(Verdict.ignore, Reason.not_the_command)

    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    event_ts = event.get("ts")

    if not isinstance(channel, str) or not isinstance(event_ts, str) or not isinstance(user, str):
        return Decision(Verdict.reject, Reason.malformed_event, channel=channel if isinstance(channel, str) else None)

    if settings.allowed_channel_ids and channel not in settings.allowed_channel_ids:
        return Decision(Verdict.ignore, Reason.channel_not_allowed, channel=channel, user=user)

    # A root-channel command has no parent Sentry alert to read, so it cannot work.
    if not isinstance(thread_ts, str) or thread_ts == event_ts:
        return Decision(
            Verdict.reject,
            Reason.not_in_thread,
            channel=channel,
            thread_ts=event_ts,
            user=user,
            event_ts=event_ts,
        )

    # Default deny: an empty user allowlist rejects everyone.
    if not settings.allowed_user_ids:
        return Decision(
            Verdict.reject,
            Reason.no_allowlist_configured,
            channel=channel,
            thread_ts=thread_ts,
            user=user,
            event_ts=event_ts,
        )
    if user not in settings.allowed_user_ids:
        return Decision(
            Verdict.reject,
            Reason.user_not_allowed,
            channel=channel,
            thread_ts=thread_ts,
            user=user,
            event_ts=event_ts,
        )

    return Decision(
        Verdict.accept,
        Reason.accepted,
        channel=channel,
        thread_ts=thread_ts,
        user=user,
        event_ts=event_ts,
    )
