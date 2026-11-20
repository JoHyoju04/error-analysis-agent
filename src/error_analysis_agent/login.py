"""One-off ChatGPT device-code login, run by an administrator.

    docker compose run --rm login

The resulting session is written to ``CODEX_HOME``, which must be the same
volume and the same container user the bot runs as — otherwise the bot will not
find the session. No OpenAI API key is involved.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", "").strip()
    if not raw:
        print(
            "[오류] CODEX_HOME 이 설정되지 않았습니다. 봇과 동일한 경로를 지정해야 합니다.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return Path(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ChatGPT device code 로그인")
    parser.add_argument(
        "--status",
        action="store_true",
        help="로그인하지 않고 현재 인증 상태만 출력한다.",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="저장된 인증을 삭제한다.",
    )
    args = parser.parse_args(argv)

    home = _codex_home()
    home.mkdir(parents=True, exist_ok=True)
    print(f"CODEX_HOME = {home}")

    try:
        from openai_codex import Codex, CodexConfig
    except ImportError as exc:  # pragma: no cover - dependency is declared
        print(f"[오류] openai-codex SDK 를 불러올 수 없습니다: {exc}", file=sys.stderr)
        return 2

    from .codex_runner import codex_env

    # Same environment as the bot: ChatGPT session only, never an API key.
    config = CodexConfig(env=codex_env(home))

    try:
        with Codex(config) as codex:
            if args.logout:
                codex.logout()
                print("저장된 인증을 삭제했습니다.")
                return 0

            account = codex.account().account
            current = getattr(account, "root", account)
            if current is not None:
                kind = getattr(current, "type", "unknown")
                email = getattr(current, "email", None) or ""
                print(f"현재 인증: {kind} {email}".rstrip())
                if args.status:
                    return 0
                print("이미 인증된 계정이 있습니다. 다시 로그인하려면 --logout 후 실행해 주세요.")
                return 0
            if args.status:
                print("현재 인증: 없음 (로그인이 필요합니다)")
                return 1

            login = codex.login_chatgpt_device_code()
            print()
            print("=" * 72)
            print("브라우저에서 아래 URL 을 열고 사용자 코드를 입력해 승인해 주세요.")
            print(f"  인증 URL : {login.verification_url}")
            print(f"  사용자 코드: {login.user_code}")
            print("=" * 72)
            print("승인을 기다리는 중입니다... (Ctrl+C 로 취소)")

            result = login.wait()
            if getattr(result, "success", False):
                print("✅ 로그인 성공. 인증이 CODEX_HOME 에 저장되었습니다.")
                return 0
            print(f"❌ 로그인 실패: {getattr(result, 'error', None) or '알 수 없는 오류'}", file=sys.stderr)
            return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\n취소했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[오류] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
