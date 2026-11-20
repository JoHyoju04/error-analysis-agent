"""Codex runner tests.

The Codex *transport* is faked (no `codex app-server` process is started), but
the enums, error types and ``ExternalMessage`` come from the installed
``openai_codex`` package, so the assertions about ``Sandbox.read_only`` and
``ApprovalMode.deny_all`` are checked against the real SDK values.
"""

from __future__ import annotations

import threading
import types
from pathlib import Path

import openai_codex
import pytest
from openai_codex.types import ReasoningEffort

from error_analysis_agent.codex_runner import (
    CodexAuthRequired,
    CodexQuotaExceeded,
    CodexRunner,
    CodexRuntimeUnavailable,
    CodexTimeout,
    _error_info,
)
from error_analysis_agent.config import CodexSettings


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeAccount:
    def __init__(self, kind: str = "chatgpt", email: str = "dev@example.com") -> None:
        self.type = kind
        self.email = email
        self.plan_type = "plus"


class FakeAccountResponse:
    def __init__(self, account: object | None) -> None:
        self.account = account
        self.requires_openai_auth = account is None


class FakeTurnResult:
    def __init__(self, text: str | None, *, status: str = "completed", error: object = None) -> None:
        self.final_response = text
        self.status = status
        self.error = error
        self.usage = None


class FakeTurnHandle:
    def __init__(self, thread: "FakeThread", payload: object) -> None:
        self._thread = thread
        self._payload = payload
        self.interrupted = False

    def run(self) -> FakeTurnResult:
        return self._thread.produce(self._payload)


class FakeThread:
    def __init__(self, client: "FakeCodex") -> None:
        self.id = "thread_1"
        self._client = client

    def turn(self, payload: object) -> FakeTurnHandle:
        self._client.payloads.append(payload)
        handle = FakeTurnHandle(self, payload)
        self._client.handles.append(handle)
        return handle

    def produce(self, payload: object) -> FakeTurnResult:
        return self._client.next_result(payload)


class FakeCodex:
    """Stands in for ``openai_codex.Codex`` without launching a process."""

    instances: list["FakeCodex"] = []

    def __init__(self, config: object | None = None) -> None:
        self.config = config
        self.account_value: object | None = FakeAccount()
        self.thread_kwargs: dict[str, object] = {}
        self.payloads: list[object] = []
        self.handles: list[FakeTurnHandle] = []
        self.results: list[FakeTurnResult] = []
        self.closed = 0
        FakeCodex.instances.append(self)

    # -- SDK surface ---------------------------------------------------- #

    def account(self, *, refresh_token: bool = False) -> FakeAccountResponse:
        return FakeAccountResponse(self.account_value)

    def thread_start(self, **kwargs: object) -> FakeThread:
        self.thread_kwargs = kwargs
        return FakeThread(self)

    def close(self) -> None:
        self.closed += 1

    # -- test helpers --------------------------------------------------- #

    def next_result(self, _payload: object) -> FakeTurnResult:
        if not self.results:
            return FakeTurnResult("## 장애 요약\n기본 응답")
        return self.results.pop(0)


def fake_sdk(codex_class: type = FakeCodex) -> types.ModuleType:
    module = types.ModuleType("fake_openai_codex")
    module.Codex = codex_class
    # real types on purpose: the assertions below check real enum values
    module.CodexConfig = openai_codex.CodexConfig
    module.Sandbox = openai_codex.Sandbox
    module.ApprovalMode = openai_codex.ApprovalMode
    module.ExternalMessage = openai_codex.ExternalMessage
    module.TransportClosedError = openai_codex.TransportClosedError
    module.is_retryable_error = openai_codex.is_retryable_error
    return module


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeCodex.instances.clear()
    yield
    FakeCodex.instances.clear()


def make_runner(
    tmp_path: Path,
    *,
    mode: str = "external_message",
    codex_class=FakeCodex,
    reasoning_effort: str | None = None,
):
    settings = CodexSettings(
        codex_home=tmp_path / "codex",
        untrusted_input_mode=mode,
        reasoning_effort=reasoning_effort,
    )
    return CodexRunner(settings, sdk_loader=lambda: fake_sdk(codex_class))


def analyze(runner: CodexRunner, tmp_path: Path, *, timeout: int = 30):
    return runner.analyze(
        snapshot_dir=tmp_path / "snapshot",
        context_text="## Sentry Issue\n- issue_id: 4242",
        job_id="job123",
        timeout_seconds=timeout,
    )


# --------------------------------------------------------------------------- #
# read-only execution contract
# --------------------------------------------------------------------------- #


def test_analysis_runs_read_only_with_approvals_denied(tmp_path):
    runner = make_runner(tmp_path)
    result = analyze(runner, tmp_path)

    client = FakeCodex.instances[-1]
    assert client.thread_kwargs["sandbox"] is openai_codex.Sandbox.read_only
    assert client.thread_kwargs["sandbox"].value == "read-only"
    assert client.thread_kwargs["approval_mode"] is openai_codex.ApprovalMode.deny_all
    assert client.thread_kwargs["approval_mode"].value == "deny_all"
    assert client.thread_kwargs["ephemeral"] is True
    assert client.thread_kwargs["cwd"] == str(tmp_path / "snapshot")
    assert result.text.startswith("## 장애 요약")
    assert result.thread_id == "thread_1"


def test_codex_home_is_passed_to_the_runtime(tmp_path):
    runner = make_runner(tmp_path)
    analyze(runner, tmp_path)
    assert FakeCodex.instances[-1].config.env["CODEX_HOME"] == str(tmp_path / "codex")


def test_reasoning_effort_is_sent_as_a_thread_config_override(tmp_path):
    runner = make_runner(tmp_path, reasoning_effort="high")
    analyze(runner, tmp_path)

    config = FakeCodex.instances[-1].thread_kwargs["config"]
    assert config == {"model_reasoning_effort": "high"}
    # the value must be one the real SDK enum accepts
    assert ReasoningEffort("high").value == "high"


def test_unset_reasoning_effort_leaves_the_model_default(tmp_path):
    runner = make_runner(tmp_path)
    analyze(runner, tmp_path)
    assert FakeCodex.instances[-1].thread_kwargs["config"] is None


def test_api_key_env_vars_are_blanked_so_billing_stays_on_the_chatgpt_plan(tmp_path):
    """호스트에 OPENAI_API_KEY 가 있어도 API 과금 경로로 새지 않아야 한다."""
    runner = make_runner(tmp_path)
    analyze(runner, tmp_path)
    env = FakeCodex.instances[-1].config.env
    assert env["OPENAI_API_KEY"] == ""
    assert env["CODEX_API_KEY"] == ""


def test_codex_env_helper(tmp_path):
    from error_analysis_agent.codex_runner import codex_env

    assert codex_env(tmp_path / "home") == {
        "CODEX_HOME": str(tmp_path / "home"),
        "OPENAI_API_KEY": "",
        "CODEX_API_KEY": "",
    }


def test_sentry_data_is_sent_as_an_untrusted_external_message(tmp_path):
    runner = make_runner(tmp_path)
    analyze(runner, tmp_path)
    payload = FakeCodex.instances[-1].payloads[0]
    assert isinstance(payload, openai_codex.ExternalMessage)
    assert payload.tool_name == "sentry_issue"
    assert payload.namespace == "sentry"
    assert "issue_id: 4242" in payload.content


def test_developer_instructions_forbid_writes_and_untrusted_instructions(tmp_path):
    runner = make_runner(tmp_path)
    analyze(runner, tmp_path)
    instructions = FakeCodex.instances[-1].thread_kwargs["developer_instructions"]
    assert "read-only" in instructions
    assert "untrusted" in instructions
    assert "AGENTS.md" in instructions


def test_inline_mode_delimits_the_context(tmp_path):
    runner = make_runner(tmp_path, mode="inline")
    result = analyze(runner, tmp_path)
    payload = FakeCodex.instances[-1].payloads[0]
    assert isinstance(payload, str)
    assert "<SENTRY_CONTEXT>" in payload and "</SENTRY_CONTEXT>" in payload
    assert result.input_mode == "inline"


def test_empty_external_message_answer_falls_back_to_inline(tmp_path):
    runner = make_runner(tmp_path)

    client_results = [FakeTurnResult(""), FakeTurnResult("## 장애 요약\n두 번째 시도")]

    class Retrying(FakeCodex):
        def __init__(self, config=None):
            super().__init__(config)
            self.results = list(client_results)

    runner = make_runner(tmp_path, codex_class=Retrying)
    result = analyze(runner, tmp_path)

    client = Retrying.instances[-1]
    assert isinstance(client.payloads[0], openai_codex.ExternalMessage)
    assert isinstance(client.payloads[1], str)
    assert result.input_mode == "inline"
    assert "두 번째 시도" in result.text


def test_client_is_always_closed(tmp_path):
    runner = make_runner(tmp_path)
    analyze(runner, tmp_path)
    assert FakeCodex.instances[-1].closed == 1


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #


def test_missing_login_is_reported_to_the_admin(tmp_path):
    class NotLoggedIn(FakeCodex):
        def __init__(self, config=None):
            super().__init__(config)
            self.account_value = None

    runner = make_runner(tmp_path, codex_class=NotLoggedIn)
    with pytest.raises(CodexAuthRequired) as info:
        analyze(runner, tmp_path)
    assert "재인증" in info.value.user_message
    assert NotLoggedIn.instances[-1].payloads == []


def test_api_key_account_is_refused(tmp_path):
    """MVP 정책: ChatGPT OAuth 만 사용한다."""

    class ApiKeyLogin(FakeCodex):
        def __init__(self, config=None):
            super().__init__(config)
            self.account_value = FakeAccount(kind="apiKey", email="")

    runner = make_runner(tmp_path, codex_class=ApiKeyLogin)
    with pytest.raises(CodexAuthRequired, match="apiKey"):
        analyze(runner, tmp_path)


def test_describe_account(tmp_path):
    runner = make_runner(tmp_path)
    assert "chatgpt" in runner.describe_account()


def test_runtime_that_cannot_start_is_reported(tmp_path):
    class Broken(FakeCodex):
        def __init__(self, config=None):
            raise OSError("codex binary missing")

    runner = make_runner(tmp_path, codex_class=Broken)
    with pytest.raises(CodexRuntimeUnavailable):
        analyze(runner, tmp_path)


# --------------------------------------------------------------------------- #
# failure modes
# --------------------------------------------------------------------------- #


class QuotaError:
    class Info:
        value = "usageLimitExceeded"

    message = "ChatGPT usage limit reached"
    codex_error_info = Info()


def test_quota_errors_are_distinguished_and_retryable(tmp_path):
    class Quota(FakeCodex):
        def __init__(self, config=None):
            super().__init__(config)
            self.results = [FakeTurnResult(None, status="failed", error=QuotaError())]

    runner = make_runner(tmp_path, codex_class=Quota)
    with pytest.raises(CodexQuotaExceeded) as info:
        analyze(runner, tmp_path)
    assert info.value.retryable is True
    assert "한도" in info.value.user_message


def test_unauthorized_turn_error_maps_to_auth_required(tmp_path):
    class Unauthorized(FakeCodex):
        class Error:
            class Info:
                value = "unauthorized"

            message = "unauthorized"
            codex_error_info = Info()

        def __init__(self, config=None):
            super().__init__(config)
            self.results = [FakeTurnResult(None, status="failed", error=Unauthorized.Error())]

    runner = make_runner(tmp_path, codex_class=Unauthorized)
    with pytest.raises(CodexAuthRequired):
        analyze(runner, tmp_path)


def test_runtime_errors_from_the_sdk_are_wrapped(tmp_path):
    class Exploding(FakeCodex):
        def thread_start(self, **kwargs):
            raise openai_codex.TransportClosedError("codex process closed stdout")

    runner = make_runner(tmp_path, codex_class=Exploding)
    with pytest.raises(CodexRuntimeUnavailable):
        analyze(runner, tmp_path)


def test_error_info_is_normalized():
    assert _error_info(QuotaError()) == "usage_limit_exceeded"
    assert _error_info(None) is None


# --------------------------------------------------------------------------- #
# timeout must really stop the work
# --------------------------------------------------------------------------- #


class HangingHandle(FakeTurnHandle):
    """Blocks like a real long-running turn; only the watchdog can free it."""

    def run(self):
        client = self._thread._client
        if not client.released.wait(timeout=10):
            raise AssertionError("turn was never interrupted")
        raise openai_codex.TransportClosedError("codex process closed stdout")

    def interrupt(self):
        client = self._thread._client
        client.interrupt_calls += 1
        client.released.set()


class HangingThread(FakeThread):
    def turn(self, payload: object) -> HangingHandle:
        self._client.payloads.append(payload)
        handle = HangingHandle(self, payload)
        self._client.handles.append(handle)
        return handle


class HangingCodex(FakeCodex):
    def __init__(self, config=None):
        super().__init__(config)
        self.released = threading.Event()
        self.interrupt_calls = 0

    def thread_start(self, **kwargs: object) -> HangingThread:
        self.thread_kwargs = kwargs
        return HangingThread(self)

    def close(self) -> None:
        super().close()
        self.released.set()


def test_timeout_interrupts_the_turn_and_kills_the_runtime(tmp_path):
    runner = make_runner(tmp_path, codex_class=HangingCodex)
    with pytest.raises(CodexTimeout) as info:
        analyze(runner, tmp_path, timeout=1)

    client = HangingCodex.instances[-1]
    assert client.interrupt_calls == 1, "interrupt() must be attempted first"
    assert client.closed >= 1, "the codex runtime must be closed, not just abandoned"
    assert info.value.retryable is True
