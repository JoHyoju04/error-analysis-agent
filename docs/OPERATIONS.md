# 운영 가이드

[← README](../README.md) · [설치 가이드](SETUP.md) · [보안 모델](SECURITY.md)

- [1. 사용법](#1-사용법)
- [2. 상태 및 오류 메시지](#2-상태-및-오류-메시지)
- [3. 운영 명령](#3-운영-명령)
- [4. 트러블슈팅](#4-트러블슈팅)
- [5. 로컬 개발과 테스트](#5-로컬-개발과-테스트)
- [6. Codex SDK API 검증](#6-codex-sdk-api-검증)

---

## 1. 사용법

- **스레드 답글로** 정확히 `!AI분석` 을 입력한다. 앞뒤 공백은 허용되지만 다른 글자가
  섞이면 무시된다(`!AI분석 해줘` ❌). 입력은 NFC 정규화 후 비교하므로 자모 분리된
  한글 입력도 통과한다.
- 채널 루트에 입력하면 분석할 알림을 알 수 없으므로 안내 메시지만 돌아온다.
- 허용된 채널·사용자만 실행할 수 있다.
- 같은 Issue 의 분석이 진행 중이면 중복 실행되지 않는다.
- 명령어는 `SLACK_COMMAND` 로 바꿀 수 있다.

---

## 2. 상태 및 오류 메시지

| 상태 | Slack 에 올라가는 내용 |
|---|---|
| 명령 접수 | `🔎 AI 분석을 시작합니다.` |
| 작업 중 중복 요청 | `⏳ Issue ... 분석이 이미 진행 중입니다.` |
| 대기열 가득 | `⏳ 분석 대기열이 가득 찼습니다.` |
| 스레드 밖에서 입력 | 스레드에서 입력하라는 안내 |
| 권한 없는 사용자 | 권한이 없다는 안내 |
| Issue 링크 없음 | `이 스레드에서 Sentry Issue 링크를 찾지 못했습니다` |
| 허용되지 않은 링크 | `허용된 Sentry 조직/호스트의 Issue 링크가 아닙니다` |
| Slack 권한 부족 | 채널 초대/스코프 확인 안내 |
| Sentry 401/403 | 토큰·권한 확인 안내 (서버 원문은 로그에만) |
| Sentry 404/429/5xx/timeout | 각각 구분된 안내, 재시도 가능 여부 명시 |
| 저장소 매핑 없음 | `config.yaml` 의 `projects` 추가 안내 |
| Revision 매칭 실패 | `배포 revision 을 확인하지 못해 분석을 중단했습니다` (추측 분석 안 함) |
| Codex 미인증/만료 | 관리자 재인증 안내 |
| Codex 한도 초과 | 한도 안내 + 재시도 가능 |
| Codex 타임아웃 | 제한 시간 초과 안내 + 재시도 가능 |
| 분석 성공 | 요약/원인·확신도/근거(파일:라인)/대안/수정 방향/추가 확인 |

결과가 길면 같은 스레드에 `_(1/3)_` 처럼 나누어 올린다.
로그에는 `job_id`, `event_id`, `channel`, `thread_ts`, `issue_id`, `revision` 이 **비밀정보 없이** 남는다.

채널 ACL 에 걸린 요청과 명령이 아닌 메시지는 **아무 응답도 하지 않는다.** 바쁜 채널에서
무관한 메시지마다 답글이 달리는 것을 막기 위한 의도적 설계다. 사용자 ACL 거부와
스레드 밖 입력만 안내를 보낸다.

---

## 3. 운영 명령

```bash
docker compose ps                     # 상태
docker compose logs -f bot            # 로그 (10MB × 5 로테이션)
docker compose restart bot            # 재시작 (인증 유지)
docker compose down                   # 중지
docker compose run --rm selfcheck     # 점검
./scripts/prepare-repo.sh my-service  # 저장소 갱신
```

- **인증 백업/이관**: `./state/codex` 가 유일한 소유 경로다. 이 디렉터리를 여러 호스트나
  프로세스에서 **동시에** 갱신하면 안 된다. 백업 시 접근 권한을 제한한다.
- **재인증**: `docker compose run --rm login --logout` 후 `docker compose run --rm login`.
- **중복 방지 상태**: `./state/data/jobs.sqlite3`. 지워도 동작하지만 재시작 직후
  같은 Slack 이벤트가 재전달되면 중복 분석될 수 있다. 7일이 지난 레코드는 자동 삭제된다.
- **리소스**: 기본 `cpus: 2.0`, `mem_limit: 2g`, 동시 분석 2건. 필요에 맞게 조정한다.
- **네트워크**: 발신 대상은 Slack, Sentry, OpenAI(Codex 인증·모델), 그리고 fetch 를
  허용한 경우 Git 서버다. egress 제한 시 이 범위만 열면 된다. 인바운드는 없다.
- **종료 동작**: `SIGTERM`/`SIGINT` 를 받으면 Socket Mode 연결을 먼저 닫아 새 이벤트
  수신을 멈추고, 진행 중인 분석이 끝날 때까지 기다린다(`stop_grace_period: 60s`).
  진행 중인 분석은 Codex 타임아웃(기본 300초)까지 이어질 수 있다.
- **Linux 권한**: 볼륨 디렉터리는 컨테이너 사용자 uid 10001 소유여야 한다.
  ```bash
  sudo chown -R 10001:10001 state repos
  ```

---

## 4. 트러블슈팅

| 증상 | 원인과 해결 |
|---|---|
| 명령에 아무 반응이 없다 | 봇이 채널에 초대되지 않았거나 Event Subscriptions 미설정. 비공개 채널이면 `message.groups` + `groups:history` 필요. `SLACK_ALLOWED_CHANNEL_IDS` 에 채널 ID 가 있는지 확인 |
| `⚠️ 이 명령을 실행할 권한이 없습니다` | `SLACK_ALLOWED_USER_IDS` 에 사용자 ID 추가 후 `docker compose up -d` |
| `⚠️ Slack 권한이 부족합니다` | Scope 변경 후 **앱 재설치**, 채널에 `/invite` |
| `Sentry Issue 링크를 찾지 못했습니다` | 알림 메시지의 스레드에서 실행했는지 확인. Sentry 알림이 아닌 메시지의 스레드면 링크가 없다 |
| `허용된 Sentry 조직/호스트의 Issue 링크가 아닙니다` | 링크 호스트가 `SENTRY_BASE_URL` 과 다르다 → `SENTRY_EXTRA_ALLOWED_HOSTS` 추가. 또는 `SENTRY_ORG` 불일치 |
| `Sentry 토큰 권한이 부족합니다(403)` | 토큰에 `event:read` 와 해당 조직/프로젝트 접근 권한 부여 |
| `배포 revision 을 확인하지 못해...` | 배포 시 Sentry Release 에 Commit SHA 를 기록하거나 `commit` 태그 추가([SETUP §5](SETUP.md#5-sentry-에-commit-sha-기록하기)). 저장소를 `prepare-repo.sh` 로 갱신. 급할 때만 `ANALYSIS_REQUIRE_RELEASE_REVISION=false` (추측 분석이므로 결과 신뢰도가 낮아진다) |
| `Git 저장소 매핑이 없습니다` | `config.yaml` 의 `sentry_project_slug` 가 Sentry 프로젝트 slug 와 정확히 같은지 확인 |
| `ChatGPT 인증이 없거나 만료되어...` | `docker compose run --rm login` 재실행. `./state/codex` 소유자/권한 확인 |
| `ChatGPT 계정이 아닌 인증(apiKey)...` | API Key 로그인은 정책상 거부한다. `--logout` 후 device code 로그인 |
| `분석이 제한 시간을 초과` | `ANALYSIS_TIMEOUT_SECONDS` 상향 또는 `ANALYSIS_MAX_CONTEXT_BYTES` 축소 |
| `Codex 런타임을 시작할 수 없어...` | 이미지 아키텍처와 `openai-codex-cli-bin` 불일치 가능. `docker compose build --no-cache` |
| 봇이 자기 메시지에 반응한다 | 발생하지 않도록 `bot_id`/`app_id`/`subtype`/자기 `user_id` 를 모두 걸러낸다. 문제가 보이면 로그의 `reason` 확인 |
| `Tini is not running as PID 1` 경고 | compose 의 `init: true` 와 이미지의 `tini` ENTRYPOINT 가 겹쳐 생기는 경고로, 동작에는 영향이 없다 |

---

## 5. 로컬 개발과 테스트

```bash
cd error-analysis-agent
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
```

```text
161 passed
```

테스트는 Slack·Sentry·Codex 호출을 전부 mock 하므로 **실제 토큰 없이** 돌아간다.
`tests/test_codex_runner.py` 는 전송 계층만 가짜로 두고 `Sandbox`/`ApprovalMode`/
`ExternalMessage` 는 설치된 실제 SDK 타입을 사용해, 읽기 전용 실행 인자가
실제 SDK 값과 일치하는지 검증한다.

보안 관련 동작은 테스트가 반드시 있어야 한다(`AGENTS.md` 규칙). 현재 커버리지:

| 동작 | 대표 테스트 |
|---|---|
| 채널/사용자 ACL, 명령 정확 일치 | `test_event_handler.py` (13개) |
| SSRF 허용목록, 첫 링크 맹신 금지 | `test_link_parser.py::test_untrusted_urls_are_refused`, `::test_first_link_is_not_blindly_trusted` |
| revision fail-closed, git 인자 주입 차단 | `test_repository.py::test_unverified_revision_fails_closed`, `::test_hostile_revision_strings_never_reach_git` |
| 민감정보 미도달 | `test_sentry_client.py::test_no_secret_reaches_the_prompt`, `::test_frame_locals_are_never_collected` |
| 읽기 전용 실행 인자 | `test_codex_runner.py::test_analysis_runs_read_only_with_approvals_denied` |
| API Key 과금 경로 차단 | `test_codex_runner.py::test_api_key_env_vars_are_blanked_so_billing_stays_on_the_chatgpt_plan` |
| 중복 방지 3중 | `test_job_manager.py` (7개), `test_pipeline.py::test_second_request_for_the_same_issue_is_refused` |
| 타임아웃 강제 종료 | `test_codex_runner.py::test_timeout_interrupts_the_turn_and_kills_the_runtime` |

아직 테스트가 없는 구간: 로그 `RedactingFilter`, `SqliteEventStore` 보존 기간 삭제,
결과 게시 중 Slack 오류 경로.

Docker 없이 직접 실행할 수도 있다(환경변수를 직접 지정).

```bash
export CONFIG_PATH=./config.local.yaml CODEX_HOME="$PWD/state/codex"
export ANALYSIS_SNAPSHOT_ROOT="$PWD/state/snapshots"
# ... SLACK_*, SENTRY_* 설정 ...
./.venv/bin/python -m error_analysis_agent.selfcheck
./.venv/bin/python -m error_analysis_agent.login
./.venv/bin/python -m error_analysis_agent.main
```

---

## 6. Codex SDK API 검증

`openai-codex==0.163.0` 에 고정되어 있고, 사용하는 공개 API 는 설치된 패키지에서 확인한 것뿐이다.

| 사용 지점 | 확인된 API |
|---|---|
| 클라이언트 | `Codex(CodexConfig(env={"CODEX_HOME": ...}))`, context manager, `close()` |
| 인증 | `login_chatgpt_device_code() → DeviceCodeLoginHandle(verification_url, user_code, wait(), cancel())` |
| 인증 상태 | `account() → GetAccountResponse(account, requires_openai_auth)`, `account.root.type == "chatgpt"` |
| 스레드 | `thread_start(cwd, sandbox, approval_mode, ephemeral, developer_instructions, model, config)` |
| 추론 강도 | `thread_start(config={"model_reasoning_effort": ...})`, 값은 `openai_codex.types.ReasoningEffort` enum |
| 실행 | `thread.turn(input) → TurnHandle(run(), interrupt())`, `TurnResult(final_response, status, error, usage)` |
| 비신뢰 입력 | `ExternalMessage(tool_name, content, namespace)` |
| 오류 | `TransportClosedError`, `is_retryable_error()`, `TurnError.codex_error_info` |

SDK 버전을 올릴 때는 위 API 가 그대로인지 확인하고 테스트를 다시 돌려야 한다.
`tests/test_config.py::test_reasoning_effort_is_parsed_and_matches_the_sdk_enum` 이
허용 목록과 SDK enum 의 일치를 검증하므로, enum 이 바뀌면 테스트가 먼저 깨진다.
