"""Verify each piece of the setup before (or after) running the bot.

    python -m error_analysis_agent.selfcheck
    python -m error_analysis_agent.selfcheck --only config,slack,sentry

Every check is read-only. Nothing is posted to Slack and no analysis is run.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from .codex_runner import CodexRunner, CodexRunnerError
from .config import AppConfig, ConfigError, load_config
from .repository import run_git
from .sentry_client import SentryClient, SentryError, SentryForbiddenError
from .slack_client import SlackError, SlackThreadClient

CHECKS = ("config", "slack", "sentry", "git", "codex")

_MARKS = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}


@dataclass
class Report:
    rows: list[tuple[str, str, str]]

    def add(self, state: str, name: str, detail: str) -> None:
        self.rows.append((state, name, detail))

    def ok(self, name: str, detail: str = "") -> None:
        self.add("ok", name, detail)

    def warn(self, name: str, detail: str) -> None:
        self.add("warn", name, detail)

    def fail(self, name: str, detail: str) -> None:
        self.add("fail", name, detail)

    def print(self) -> None:
        for state, name, detail in self.rows:
            suffix = f" — {detail}" if detail else ""
            print(f"{_MARKS[state]} {name}{suffix}")

    @property
    def failed(self) -> bool:
        return any(state == "fail" for state, _, _ in self.rows)


def check_config(report: Report) -> AppConfig | None:
    try:
        config = load_config()
    except ConfigError as exc:
        report.fail("config", str(exc))
        return None

    report.ok(
        "config",
        f"projects={[project.name for project in config.projects]} "
        f"sentry_host={config.sentry.host} command={config.slack.command!r}",
    )
    if not config.slack.allowed_user_ids:
        report.warn(
            "slack allowlist", "SLACK_ALLOWED_USER_IDS 가 비어 있어 모든 명령이 거부됩니다."
        )
    if not config.slack.allowed_channel_ids:
        report.warn(
            "slack channels", "SLACK_ALLOWED_CHANNEL_IDS 가 비어 있어 모든 채널이 허용됩니다."
        )
    template = config.analysis.agents_md_template
    if template.exists():
        report.ok("AGENTS.md 템플릿", str(template))
    else:
        report.fail("AGENTS.md 템플릿", f"파일이 없습니다: {template}")
    return config


def check_slack(report: Report, config: AppConfig) -> None:
    try:
        from slack_sdk import WebClient
    except ImportError as exc:  # pragma: no cover
        report.fail("slack", f"slack_sdk 를 불러올 수 없습니다: {exc}")
        return
    try:
        client = SlackThreadClient(WebClient(token=config.slack.bot_token))
        bot_user_id = client.bot_user_id()
    except SlackError as exc:
        report.fail("slack auth.test", str(exc))
        return
    report.ok("slack auth.test", f"bot_user_id={bot_user_id}")


def check_sentry(report: Report, config: AppConfig) -> None:
    with SentryClient(config.sentry) as client:
        try:
            org = client.ping()
        except SentryForbiddenError:
            # A token scoped to event:read only cannot read the organization,
            # yet it can still read issues — so this is not fatal.
            report.warn(
                "sentry api",
                "토큰이 조직 조회(org:read) 권한은 없습니다. "
                "Issue 조회(event:read)만 있으면 분석은 동작할 수 있습니다.",
            )
            return
        except SentryError as exc:
            report.fail("sentry api", f"{type(exc).__name__}: {exc}")
            return
    report.ok("sentry api", f"organization={org.get('slug') or config.sentry.org}")


def check_git(report: Report, config: AppConfig) -> None:
    snapshot_root = config.analysis.snapshot_root
    try:
        snapshot_root.mkdir(parents=True, exist_ok=True)
        probe = snapshot_root / ".selfcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.ok("snapshot 디렉터리", f"{snapshot_root} (쓰기 가능)")
    except OSError as exc:
        report.fail("snapshot 디렉터리", f"{snapshot_root} 에 쓸 수 없습니다: {exc}")

    for project in config.projects:
        name = f"git {project.name}"
        if not project.repo_dir.exists():
            report.fail(name, f"repo_dir 가 없습니다: {project.repo_dir}")
            continue
        result = run_git(["rev-parse", "--git-dir"], project.repo_dir, None, 30.0)
        if not result.ok:
            report.fail(name, f"{project.repo_dir} 는 git 저장소가 아닙니다.")
            continue
        head = run_git(["rev-parse", "--short", "HEAD"], project.repo_dir, None, 30.0)
        report.ok(name, f"{project.repo_dir} HEAD={head.stdout.strip() or '?'}")


def check_codex(report: Report, config: AppConfig) -> None:
    home = config.codex.codex_home
    if not home.exists():
        report.warn("CODEX_HOME", f"{home} 가 아직 없습니다. 최초 로그인이 필요합니다.")
    elif not os.access(home, os.W_OK):
        report.fail("CODEX_HOME", f"{home} 에 쓸 수 없습니다. 볼륨 권한을 확인해 주세요.")
    else:
        report.ok("CODEX_HOME", str(home))

    runner = CodexRunner(config.codex)
    try:
        state = runner.describe_account()
    except CodexRunnerError as exc:
        report.fail("codex 인증", str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        report.fail("codex 인증", f"{type(exc).__name__}: {exc}")
        return

    if state.startswith("미인증"):
        report.fail("codex 인증", f"{state} → `python -m error_analysis_agent.login` 을 실행해 주세요.")
    elif state.startswith("chatgpt"):
        report.ok("codex 인증", state)
    else:
        report.warn("codex 인증", f"ChatGPT 계정이 아닙니다: {state}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="error-analysis-agent 설정 점검")
    parser.add_argument(
        "--only",
        default=",".join(CHECKS),
        help=f"실행할 점검 목록 (쉼표 구분). 가능한 값: {', '.join(CHECKS)}",
    )
    args = parser.parse_args(argv)
    selected = {name.strip() for name in args.only.split(",") if name.strip()}
    unknown = selected - set(CHECKS)
    if unknown:
        print(f"[오류] 알 수 없는 점검 항목: {sorted(unknown)}", file=sys.stderr)
        return 2

    report = Report(rows=[])
    config = check_config(report)
    if config is None:
        report.print()
        return 1

    if "slack" in selected:
        check_slack(report, config)
    if "sentry" in selected:
        check_sentry(report, config)
    if "git" in selected:
        check_git(report, config)
    if "codex" in selected:
        check_codex(report, config)

    report.print()
    if report.failed:
        print("\n하나 이상의 점검이 실패했습니다. README 의 해당 단계를 확인해 주세요.", file=sys.stderr)
        return 1
    print("\n모든 점검을 통과했습니다.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
