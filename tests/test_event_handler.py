from __future__ import annotations

import unicodedata

import pytest
from conftest import make_config, slack_command_event

from error_analysis_agent.event_handler import Reason, Verdict, decide


@pytest.fixture
def slack(config):
    return config.slack


def test_exact_command_in_thread_is_accepted(slack):
    decision = decide(slack_command_event(), slack, bot_user_id="UBOT")
    assert decision.verdict is Verdict.accept
    assert decision.reason is Reason.accepted
    assert decision.channel == "C100"
    assert decision.thread_ts == "1700000000.000100"
    assert decision.user == "U100"


@pytest.mark.parametrize(
    "text",
    ["!AI분석 please", "AI분석", "!ai분석", "!AI 분석", "", "!AI분석!"],
)
def test_other_text_is_ignored(slack, text):
    decision = decide(slack_command_event(text=text), slack)
    assert decision.verdict is Verdict.ignore
    assert decision.reason is Reason.not_the_command


def test_nfd_hangul_input_is_accepted(slack):
    """macOS/iOS 입력은 NFD 로 올 수 있으므로 정규화 후 비교해야 한다."""
    decomposed = unicodedata.normalize("NFD", "!AI분석")
    assert decomposed != "!AI분석"
    assert decide(slack_command_event(text=decomposed), slack).verdict is Verdict.accept


def test_bot_and_edited_messages_are_ignored(slack):
    assert decide(slack_command_event(bot_id="B999"), slack).reason is Reason.bot_message
    assert (
        decide(slack_command_event(subtype="message_changed"), slack).reason
        is Reason.message_subtype
    )
    assert decide(slack_command_event(app_id="A1"), slack).reason is Reason.bot_message
    assert decide({"type": "reaction_added"}, slack).reason is Reason.not_a_message


def test_own_message_is_ignored(slack):
    decision = decide(slack_command_event(user="UBOT"), slack, bot_user_id="UBOT")
    assert decision.reason is Reason.own_message


def test_other_channel_is_ignored(slack):
    decision = decide(slack_command_event(channel="C999"), slack)
    assert decision.verdict is Verdict.ignore
    assert decision.reason is Reason.channel_not_allowed


def test_command_outside_a_thread_is_rejected_with_guidance(slack):
    event = slack_command_event(thread_ts=None)
    decision = decide(event, slack)
    assert decision.verdict is Verdict.reject
    assert decision.reason is Reason.not_in_thread
    assert "스레드" in (decision.user_message(slack.command) or "")


def test_root_message_whose_thread_ts_equals_ts_is_rejected(slack):
    ts = "1700000100.000200"
    decision = decide(slack_command_event(ts=ts, thread_ts=ts), slack)
    assert decision.reason is Reason.not_in_thread


def test_user_outside_allowlist_is_rejected(slack):
    decision = decide(slack_command_event(user="U999"), slack)
    assert decision.verdict is Verdict.reject
    assert decision.reason is Reason.user_not_allowed
    assert decision.user_message(slack.command)


def test_empty_user_allowlist_denies_by_default(config_path):
    config = make_config(config_path, SLACK_ALLOWED_USER_IDS="")
    decision = decide(slack_command_event(), config.slack)
    assert decision.verdict is Verdict.reject
    assert decision.reason is Reason.no_allowlist_configured


def test_empty_channel_allowlist_allows_every_channel(config_path):
    config = make_config(config_path, SLACK_ALLOWED_CHANNEL_IDS="")
    decision = decide(slack_command_event(channel="C-other"), config.slack)
    assert decision.verdict is Verdict.accept


def test_malformed_event_is_rejected(slack):
    decision = decide(slack_command_event(user=None), slack)
    assert decision.verdict is Verdict.reject
    assert decision.reason is Reason.malformed_event


def test_custom_command_is_honoured(config_path):
    config = make_config(config_path, SLACK_COMMAND="!분석해줘")
    assert decide(slack_command_event(text="!분석해줘"), config.slack).verdict is Verdict.accept
    assert decide(slack_command_event(), config.slack).verdict is Verdict.ignore
