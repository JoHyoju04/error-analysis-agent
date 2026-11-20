# 보안 모델과 한계

[← README](../README.md) · [설치 가이드](SETUP.md) · [운영 가이드](OPERATIONS.md)

이 봇은 **신뢰할 수 없는 입력(Sentry 이벤트, Slack 메시지)을 받아 LLM 에이전트에게
사내 소스코드를 읽히는** 구조다. 그래서 통제는 "에이전트가 착하게 행동하기를 기대"하는
방식이 아니라, **할 수 있는 일 자체를 없애는** 방식으로 설계했다.

- [1. 위협과 통제](#1-위협과-통제)
- [2. 지켜지는 것](#2-지켜지는-것)
- [3. 남는 위험](#3-남는-위험)
- [4. 운영 시 권장 구성](#4-운영-시-권장-구성)

---

## 1. 위협과 통제

| 위협 | 시나리오 | 통제 | 코드 |
|---|---|---|---|
| 코드 변조·배포 | 에이전트가 소스를 수정하거나 push, 배포를 실행 | `Sandbox.read_only` + `ApprovalMode.deny_all`, `.git` 없는 스냅샷, 파일 권한 `0444` | `codex_runner.py`, `repository.py` |
| SSRF | Slack 메시지에 `http://169.254.169.254/...` 같은 링크를 심어 내부망 요청 유도 | 호스트 집합 완전일치 + 조직 slug 일치 + Issue 경로 형태 검증. 요청 URL 은 **설정된 base_url + 숫자 ID** 로만 조립 | `link_parser.py`, `config.py` |
| 명령 주입 | Sentry release 이름에 `--upload-pack=...` 을 넣어 git 옵션 주입 | SHA 정규식 또는 안전 ref 정규식을 통과한 문자열만 git 인자로 전달 | `repository.py:28-32` |
| 프롬프트 인젝션 | 예외 메시지나 로그에 "이전 지시를 무시하고 ~하라"를 심음 | Sentry 데이터를 `ExternalMessage`(도구 출력 권한)로 전달. 과제는 `developer_instructions` 와 `AGENTS.md` 에만 | `codex_runner.py:299-303` |
| 비밀/PII 유출 | 요청 헤더·쿠키·지역변수의 토큰과 개인정보가 외부 모델로 전송 | 애초에 수집하지 않음(요청 엔트리 전체, 프레임 `vars`, `user`, `contexts`) + 태그 화이트리스트 + 정규식 마스킹 | `sentry_client.py`, `redaction.py` |
| 결과를 통한 유출 | 모델 출력에 비밀이 섞여 Slack 에 게시 | 게시 직전 `redact()` 재적용 | `formatters.py:131-132` |
| 무단 실행 | 권한 없는 사용자가 분석을 실행해 사용량 소모 | 채널·사용자 허용목록(사용자는 기본 거부), 스레드 답글만 허용 | `event_handler.py` |
| 중복/폭주 | Slack 이벤트 재전달, 연타, 대기열 폭주 | `event_id`(SQLite) / `(channel, thread_ts)` / `(channel, parent_ts, issue_id)` 3중 방어 + 큐 상한 | `job_manager.py` |
| 자원 고갈 | 에이전트가 응답하지 않고 워커를 점유 | 워치독이 `turn.interrupt()` 후 클라이언트를 닫아 `codex app-server` 자식까지 종료 | `codex_runner.py:320-332` |
| 예기치 않은 과금 | API Key 로 전환되어 토큰 단위 과금 발생 | 계정 종류가 `chatgpt` 가 아니면 거부, Codex 런타임 환경의 `OPENAI_API_KEY`/`CODEX_API_KEY` 를 빈 값으로 덮어쓰기 | `codex_runner.py:129-143, 209-222` |
| 내부 정보 노출 | 서버 오류 원문이 Slack 에 노출 | 도메인 예외의 `user_message` 만 게시, 원문은 로그에만 + `job_id` 로 상관관계 추적 | `pipeline.py` |

---

## 2. 지켜지는 것

- **읽기 전용 분석**: `Sandbox.read_only` + `ApprovalMode.deny_all`.
  스냅샷은 `git archive` 결과라 `.git`·원격·자격증명이 없어 push 경로 자체가 없다.
  추가로 스냅샷 파일 권한을 `0444`(디렉터리 `0555`)로 내린다.
  tar 추출 시 심볼릭·하드링크, 절대 경로, `..` 경로는 건너뛴다.
- **Git 은 봇만 실행**한다. clone/fetch/archive 는 모두 봇의 준비 단계에서 일어난다.
  Sentry 에서 온 revision 문자열은 정규식으로 검증한 뒤에만 git 인자로 넘긴다.
  `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/true` 로 자격증명 프롬프트도 막는다.
- **SSRF 차단**: Slack 링크는 허용된 호스트와 조직, 알려진 Issue 경로 형태만 통과한다.
  메시지의 첫 링크를 무조건 신뢰하지 않고, 거부된 링크는 건너뛰고 다음 후보를 본다.
  허용목록이 비어 있으면 fail-closed 로 거부한다.
- **민감정보 제거**: 요청 헤더/쿠키/바디와 스택 프레임의 지역변수(`vars`)는 **수집하지 않는다**.
  태그는 18개 화이트리스트만 통과한다. 그 위에 이메일·전화번호·주민번호·카드번호·IP·
  JWT·`Bearer`·`xoxb-`/`sk-`/`gh*_`/`AKIA` 토큰·`password=` 류·PEM 블록을 마스킹한다.
  Sentry → Codex, Codex → Slack, 그리고 로그까지 같은 필터를 통과한다.
- **프롬프트 인젝션 완화**: Sentry 데이터는 `ExternalMessage` 로 전달되어 사용자 지시보다
  낮은 신뢰 수준을 가진다. 분석 과제는 `developer_instructions` 와 `AGENTS.md` 에만 있다.
- **타임아웃 강제**: 제한 시간이 지나면 `turn.interrupt()` 후 Codex 클라이언트를 닫아
  하위 `codex app-server` 프로세스까지 종료한다(`Future.cancel()` 로는 멈추지 않는다).
- **중복 방지**: Slack `event_id`(선택적으로 SQLite 영속), `(channel, thread_ts)`,
  `(channel, parent_ts, issue_id)` 3중 방어.
- **fail-closed**: revision 미확인, 매핑 없음, 미인증, 허용 목록 비어 있음 → 모두 중단.
- **API Key 과금 경로 차단**: ChatGPT 계정이 아닌 인증은 거부하고, Codex 런타임 환경의
  `OPENAI_API_KEY`/`CODEX_API_KEY` 를 빈 값으로 덮어쓴다.
- **컨테이너**: non-root(uid 10001), `no-new-privileges:true`, 공개 포트 없음(Socket Mode),
  `tini` 로 자식 프로세스 회수.

---

## 3. 남는 위험

정직하게 적는다. 아래는 현재 구조에서 해결되지 않은 것들이다.

- **봇과 Codex 가 같은 컨테이너에서 돌아간다.** 환경변수에 Slack/Sentry 토큰이 있으므로,
  sandbox 를 우회하는 에이전트는 이론적으로 토큰을 읽을 수 있다. 운영 강화 시
  Codex Worker 를 비밀 없는 별도 컨테이너로 분리해야 한다. **가장 효과가 큰 개선.**
- **SSH 키를 컨테이너에 마운트하면** 같은 위험이 커진다. 권장 구성은 아래 §4 를 따른다.
- **프롬프트 인젝션이 원천 차단되지는 않는다.** `ExternalMessage` 나 문자열 구분자
  (`<SENTRY_CONTEXT>`)는 신뢰 수준을 낮추는 완화책이다. 결과는 사람이 검수해야 한다.
- **정규식 기반 마스킹은 모든 형태의 비밀을 잡지 못한다.** 애초에 Sentry 로 보내는
  이벤트에서 민감정보를 줄이는 것이 근본 대책이다(Sentry SDK 의 `send_default_pii=false`,
  `before_send` 스크러빙).
- **샌드박스를 E2E 로 증명하지는 않았다.** 테스트는 Codex 를 가짜로 두고 전달 인자가
  `Sandbox.read_only`/`ApprovalMode.deny_all` 인지 검증한다. 실제 샌드박스의 격리
  강도는 SDK/런타임에 의존한다.
- **로그 마스킹 필터에 테스트가 없다.** `RedactingFilter` 는 코드로만 존재한다.
- **Slack 429(rate limit) 전용 처리가 없다.** 재시도 로직은 Sentry 클라이언트에만 있다.

---

## 4. 운영 시 권장 구성

컨테이너 안에 Git 자격증명을 두지 않는 구성이다.

1. 호스트에서 저장소를 준비·갱신한다.
   ```bash
   ./scripts/prepare-repo.sh my-service git@github.com:ORG/my-service.git
   ```
2. `.env` 에서 봇의 fetch 를 끈다.
   ```dotenv
   ANALYSIS_ALLOW_FETCH=false
   ```
3. `compose.yaml` 의 저장소 마운트를 읽기 전용으로 바꾼다.
   ```yaml
   - ./repos:/workspace/repos:ro
   ```
4. 저장소 갱신은 호스트의 cron 등으로 수행한다. 이 구성에서는 컨테이너 안에 SSH 키가
   존재하지 않으므로, 에이전트가 샌드박스를 우회해도 원격 저장소에 접근할 수 없다.

추가로:

- `SLACK_ALLOWED_USER_IDS` 를 최소 인원으로 유지한다.
- ChatGPT 전용 계정을 쓰면 개인 사용량과 한도가 섞이지 않는다.
- egress 를 제한한다면 Slack, Sentry, OpenAI 만 열면 된다(fetch 를 끈 경우).
- `./state/codex` 는 비밀번호처럼 취급한다. 여러 호스트에서 동시에 갱신하지 않는다.
