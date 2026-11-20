# AGENTS.md — error-analysis-agent 개발 규칙

이 파일은 **이 봇을 개발하는 사람/AI** 를 위한 규칙이다.
장애 분석 대상 저장소에 주입되는 분석용 규칙은 [`prompts/AGENTS.analysis.md`](prompts/AGENTS.analysis.md) 이며 목적이 다르다.

## 프로젝트 요약

Sentry 알림 Slack 스레드에서 `!AI분석` 을 입력하면, 장애 당시 배포 revision 의 소스
스냅샷을 만들어 Codex(ChatGPT OAuth)로 **읽기 전용** 분석을 실행하고 같은 스레드에 결과를 올린다.

## 반드시 지킬 것

1. **분석은 읽기 전용이다.** 코드 수정, commit, push, PR 생성, 배포, 운영 DB 접속을
   추가하지 않는다. Codex 는 항상 `Sandbox.read_only` + `ApprovalMode.deny_all` 로 실행한다.
2. **Git 명령은 봇만 실행한다.** Agent 에게 git, 원격 저장소 자격증명, Docker socket,
   Slack/Sentry 토큰을 넘기지 않는다. 스냅샷은 `git archive` 결과이므로 `.git` 이 없다.
3. **비밀/개인정보는 경계에서 제거한다.** Sentry → Codex, Codex → Slack, 그리고 로그까지
   `redaction.redact()` 를 통과시킨다. 프레임의 `vars`, 요청 헤더/바디/쿠키는 수집하지 않는다.
4. **Sentry 데이터는 신뢰하지 않는다.** URL 은 허용된 호스트/조직만 통과시키고(SSRF),
   Codex 에는 `ExternalMessage` 로 전달해 사용자 지시보다 낮은 신뢰 수준을 유지한다.
   git 에 넘기는 revision 문자열은 정규식으로 검증한다.
5. **Slack 이벤트 리스너는 즉시 반환한다.** 네트워크 I/O 와 분석은 `JobManager` 워커에서만 한다.
6. **fail-closed.** 배포 revision 을 확인하지 못하면 최신 코드로 추측 분석하지 않는다
   (`require_release_revision`).
7. **값을 하드코딩하지 않는다.** 토큰·URL·채널 ID 는 환경변수, 저장소 매핑은 `config.yaml`.
8. **SDK API 를 추측하지 않는다.** `openai-codex` 는 버전을 고정하고, 공개 API 는
   설치된 패키지에서 확인한 것만 사용한다 (현재 검증 버전: 0.163.0).

## 코드 스타일

- Python 3.10+ 호환. `from __future__ import annotations` 를 사용한다.
- 외부 경계(Slack/Sentry/Codex/git)는 주입 가능한 의존성으로 두어 테스트에서 mock 한다.
- 사용자에게 보이는 문자열은 한국어, 코드/로그/주석은 영어를 기본으로 한다.
- 예외는 `user_message` 를 가진 도메인 예외로 변환해서 Slack 에 올린다.
  서버 원문 오류는 로그에만 남긴다.

## 테스트

```bash
.venv/bin/python -m pytest -q
```

- 외부 API 호출은 전부 mock 한다. 실제 토큰 없이 테스트가 통과해야 한다.
- 기능을 추가하면 `tests/` 에 대응 테스트를 추가한다. 보안 관련 동작
  (ACL, 중복 방지, 민감정보 제거, revision fail-closed, read-only 실행 인자)은
  테스트가 반드시 있어야 한다.
