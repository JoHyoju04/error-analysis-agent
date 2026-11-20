# 설치 및 설정 가이드

[← README](../README.md)

이 문서는 `error-analysis-agent` 를 처음부터 띄우는 전체 절차와 설정 레퍼런스다.
각 단계 끝의 **검증**을 통과한 뒤 다음 단계로 넘어가면 된다.

- [1. 요구 사항](#1-요구-사항)
- [2. 디렉터리 구조](#2-디렉터리-구조)
- [3. 설치 7단계](#3-설치-7단계)
- [4. 설정 레퍼런스](#4-설정-레퍼런스)
- [5. Sentry 에 Commit SHA 기록하기](#5-sentry-에-commit-sha-기록하기)

---

## 1. 요구 사항

| 항목 | 내용 |
|---|---|
| 실행 환경 | Docker + Docker Compose (또는 Python 3.11+ 직접 실행; SDK 최소 3.10) |
| Slack | 워크스페이스에 App 을 설치할 권한, Sentry 알림이 오는 채널 |
| Sentry | Cloud 또는 Self-Hosted. `event:read` 권한 토큰 |
| ChatGPT | Codex 를 사용할 수 있는 ChatGPT 계정 (브라우저 승인 1회 필요) |
| Git | 분석 대상 서비스 저장소에 대한 읽기 권한 |
| 권장 | 배포 파이프라인이 Sentry Release 또는 event tag 에 **Commit SHA** 를 기록 |

> **Commit SHA 기록을 강하게 권장한다.** 봇은 Sentry 에서 얻은 revision 을 Git 에서 검증한 뒤
> 그 시점의 소스만 분석한다. SHA 가 없으면 기본 설정에서는 분석을 거부한다.
> 설정 방법은 [5. Sentry 에 Commit SHA 기록하기](#5-sentry-에-commit-sha-기록하기) 를 참고한다.

### 과금 모델: 구독에 포함된 사용량만 쓴다

이 봇은 **ChatGPT 계정 로그인(OAuth)만** 사용한다. Codex 사용량은 이미 결제하고 있는
ChatGPT 플랜에 포함된 분량에서 차감되므로 **별도 과금이 발생하지 않는다.**
OpenAI API Key 는 쓰지 않으며, API Platform 크레딧 잔액도 필요하지 않다.

추가 비용이 생길 수 있는 경로는 두 가지뿐이고, 둘 다 막혀 있거나 사용자가 통제한다.

| 경로 | 이 봇의 동작 |
|---|---|
| API Key 로 전환되어 토큰 단위 과금 | **차단.** `account()` 의 계정 종류가 `chatgpt` 가 아니면 분석을 거부한다. 추가로 Codex 런타임에 넘기는 환경에서 `OPENAI_API_KEY`/`CODEX_API_KEY` 를 빈 값으로 덮어써, 호스트에 키가 설정돼 있어도 상속되지 않는다 |
| 플랜 한도를 넘겨 ChatGPT 크레딧 구매 | 한도에 도달하면 분석을 중단하고 Slack 에 `ChatGPT 사용 한도에 도달` 을 알린다. 크레딧을 자동으로 구매하거나 소비하지 않는다 |

> **한도는 봇과 계정 소유자의 개인 사용량이 공유한다.** 분석 1건은 저장소를 탐색하는
> 에이전트 실행이므로 토큰 소모가 작지 않다. 소모를 줄이려면
> `ANALYSIS_MAX_CONTEXT_BYTES` 를 낮추고, `ANALYSIS_TIMEOUT_SECONDS` 로 폭주를 제한하고,
> `SLACK_ALLOWED_USER_IDS` 를 최소 인원으로 유지한다. 전용 계정을 쓰면 개인 사용량과
> 한도가 섞이지 않는다.

> OpenAI 문서는 자동화/CI 용도로는 API Key 를 권장한다. 이 봇은 비용이 추가되지 않는
> ChatGPT 로그인을 선택했으므로, 사내 신뢰된 서버에서만 운영하고 공개 저장소 대상으로는
> 쓰지 않는다. `CODEX_HOME` 의 세션 파일은 비밀번호처럼 취급한다.

---

## 2. 디렉터리 구조

```text
error-analysis-agent/
├── README.md                      # 포트폴리오/설계 문서
├── AGENTS.md                      # 이 봇을 "개발"하는 AI/사람용 규칙
├── docs/
│   ├── SETUP.md                   # 이 문서
│   ├── OPERATIONS.md              # 운영·트러블슈팅·로컬 개발
│   └── SECURITY.md                # 보안 모델과 남는 위험
├── pyproject.toml                 # 의존성 / 버전 고정
├── .env.example                   # 비밀 없는 설정 예제
├── config.yaml                    # Sentry 프로젝트 ↔ Git 저장소 매핑 (예제)
├── config.local.yaml              # (git 제외) 실제 매핑. compose 가 이 파일을 쓴다
├── Dockerfile
├── compose.yaml                   # bot / login / selfcheck 서비스
├── scripts/prepare-repo.sh        # 분석 대상 Git mirror 준비
├── prompts/AGENTS.analysis.md     # 분석 대상 스냅샷에 주입되는 분석 규칙
├── src/error_analysis_agent/
│   ├── main.py                    # Slack Socket Mode 진입점
│   ├── event_handler.py           # 명령 파싱 / ACL (순수 함수)
│   ├── slack_client.py            # 부모 메시지 조회, 스레드 답글
│   ├── link_parser.py             # text/blocks/attachments 의 Sentry 링크 검증
│   ├── sentry_client.py           # REST API, 재시도, 민감정보 제거, 컨텍스트 생성
│   ├── repository.py              # 프로젝트 매핑, revision 검증, 읽기 전용 스냅샷
│   ├── codex_runner.py            # Codex SDK 실행, 인증 확인, 타임아웃 강제 종료
│   ├── pipeline.py                # 전체 흐름 + 오류 → 사용자 메시지 매핑
│   ├── job_manager.py             # 큐, 병렬 제한, 중복 방지(메모리/SQLite)
│   ├── formatters.py              # Slack mrkdwn 변환 및 분할
│   ├── redaction.py               # 비밀/개인정보 마스킹
│   ├── logging_setup.py           # 비밀정보 필터가 붙은 로깅
│   ├── login.py                   # ChatGPT Device Code 로그인 커맨드
│   └── selfcheck.py               # 설정/연결 점검 커맨드
├── tests/                         # pytest (외부 API 전부 mock)
├── repos/                         # (git 제외) 분석 대상 Git mirror
└── state/                         # (git 제외) codex OAuth 세션, SQLite, 스냅샷
```

---

## 3. 설치 7단계

### Step 1. Slack App 생성

1. <https://api.slack.com/apps> → **Create New App** → From scratch → 워크스페이스 선택.
2. **Socket Mode** → `Enable Socket Mode` ON → App-Level Token 생성
   (scope `connections:write`) → `xapp-...` 토큰을 복사해 둔다.
3. **OAuth & Permissions** → *Bot Token Scopes* 추가
   - `chat:write` (필수)
   - `channels:history` — Sentry 알림이 **공개 채널**일 때
   - `groups:history` — **비공개 채널**일 때
4. **Event Subscriptions** → `Enable Events` ON → *Subscribe to bot events*
   - 공개 채널: `message.channels`
   - 비공개 채널: `message.groups`
5. **Install to Workspace** → Bot User OAuth Token `xoxb-...` 복사.
   (Scope 를 바꾸면 **재설치**해야 한다.)
6. Slack 에서 Sentry 알림 채널에 봇 초대: `/invite @앱이름`
7. 채널 ID 와 사용할 사용자 ID 를 확인한다.
   - 채널 ID: 채널 이름 클릭 → 맨 아래 `Channel ID` (`C...`)
   - 사용자 ID: 프로필 → ⋯ → `Copy member ID` (`U...`)

**검증:** Step 4 의 `selfcheck` 에서 `slack auth.test` 가 ✅ 로 나오면 토큰과 설치가 정상이다.

### Step 2. Sentry 토큰 발급

1. Sentry → **Settings → Developer Settings → Organization Tokens**
   (또는 User Auth Token) 에서 토큰을 만든다.
2. 최소 권한: **`event:read`**. `selfcheck` 의 조직 조회까지 ✅ 로 보려면 `org:read` 도 준다
   (없으면 ⚠️ 로 표시되며, 분석 자체는 `event:read` 만으로 동작할 수 있다).
3. `SENTRY_BASE_URL` 은 실제 접속 주소다.
   - Cloud: `https://sentry.io` (조직 전용 호스트를 쓰면 그 호스트)
   - Self-Hosted: 설치한 호스트 (예: `https://sentry.mycorp.com`)
4. `SENTRY_ORG` 은 조직 slug (Sentry URL 의 `/organizations/<slug>/`).

> Slack 알림 링크의 호스트가 `SENTRY_BASE_URL` 과 다르면(예: 조직별 서브도메인)
> `SENTRY_EXTRA_ALLOWED_HOSTS` 에 그 호스트를 추가해야 한다.
> 등록되지 않은 호스트의 링크는 SSRF 방지를 위해 거부된다.

### Step 3. 분석 대상 Git 저장소 준비

```bash
cd error-analysis-agent
./scripts/prepare-repo.sh my-service git@github.com:ORG/my-service.git
```

`repos/my-service` 에 **mirror clone**(작업 트리 없는 전체 이력)이 만들어진다.
이후 갱신은 URL 없이 같은 명령을 다시 실행하면 된다.

그리고 `cp config.yaml config.local.yaml` 후 `config.local.yaml` 을 실제 값으로 수정한다.
(`config.yaml` 은 저장소에 commit 되는 예제이고, `config.local.yaml` 은 `.gitignore` 에 있다.)

```yaml
projects:
  my-service:
    sentry_project_slug: my-service # Sentry Issue 의 프로젝트 slug 와 정확히 일치
    repo_dir: /workspace/repos/my-service # 컨테이너 내부 경로
    repository_url: git@github.com:ORG/my-service.git
    default_branch: main
```

**검증:** Step 4 의 `selfcheck` 에서 `git my-service` 가 ✅ 이고 HEAD 가 보이면 정상이다.

### Step 4. 설정 파일 작성과 점검

```bash
cp .env.example .env
$EDITOR .env    # SLACK_*, SENTRY_* 를 실제 값으로 채운다
```

최소로 채워야 하는 값:

```dotenv
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_ALLOWED_CHANNEL_IDS=C0123456789
SLACK_ALLOWED_USER_IDS=U0123456789,U9876543210
SENTRY_BASE_URL=https://sentry.mycorp.com
SENTRY_ORG=my-org
SENTRY_AUTH_TOKEN=...
```

> `SLACK_ALLOWED_USER_IDS` 가 비어 있으면 **모든 명령을 거부한다**(기본 거부).
> 분석을 실행할 사람을 반드시 명시해야 한다.

이미지를 빌드하고 점검한다.

```bash
docker compose build
docker compose run --rm selfcheck
```

**검증 (예시 출력):**

```text
✅ config — projects=['my-service'] sentry_host=sentry.mycorp.com command='!AI분석'
✅ AGENTS.md 템플릿 — /app/prompts/AGENTS.analysis.md
✅ slack auth.test — bot_user_id=U0BOT...
✅ sentry api — organization=my-org
✅ snapshot 디렉터리 — /workspace/snapshots (쓰기 가능)
✅ git my-service — /workspace/repos/my-service HEAD=3f1a9c2
⚠️  CODEX_HOME — /home/bot/.codex 가 아직 없습니다. 최초 로그인이 필요합니다.
❌ codex 인증 — 미인증 (ChatGPT device code 로그인이 필요합니다) → ...
```

이 시점에 Codex 인증만 ❌ 인 것이 정상이다. 다음 단계에서 해결한다.

### Step 5. ChatGPT 최초 로그인 (Device Code, 1회)

```bash
docker compose run --rm login
```

출력 예:

```text
CODEX_HOME = /home/bot/.codex
========================================================================
브라우저에서 아래 URL 을 열고 사용자 코드를 입력해 승인해 주세요.
  인증 URL : https://auth.openai.com/activate
  사용자 코드: ABCD-EFGH
========================================================================
승인을 기다리는 중입니다... (Ctrl+C 로 취소)
✅ 로그인 성공. 인증이 CODEX_HOME 에 저장되었습니다.
```

브라우저에서 URL 을 열고 ChatGPT 로 로그인한 뒤 코드를 입력해 승인한다.
인증은 `./state/codex` 볼륨에 저장되어 **이후 봇이 재사용**한다.

- 사람이 승인해야 하므로 이 단계는 무인 자동화가 불가능하다.
- 봇과 `login` 은 **같은 이미지·같은 컨테이너 사용자(`bot`, uid 10001)·같은 `CODEX_HOME` 볼륨**을 쓴다.
  다른 사용자로 실행하면 세션을 찾지 못한다.
- 상태만 보려면 `docker compose run --rm login python -m error_analysis_agent.login --status`,
  로그아웃은 `--logout`.

**검증:**

```bash
docker compose run --rm selfcheck
# ✅ codex 인증 — chatgpt you@example.com plus
```

### Step 6. 봇 실행

```bash
docker compose up -d
docker compose logs -f bot
```

**검증 (기동 로그):**

```text
... INFO  error_analysis_agent.main: starting error-analysis-agent projects=['my-service'] max_parallel=2 timeout=300s require_revision=True
... INFO  error_analysis_agent.main: authenticated as bot_user_id=U0BOT...
... INFO  slack_bolt.App: ⚡️ Bolt app is running!
```

외부로 열리는 포트는 없다(Socket Mode).

### Step 7. 실제 연동 E2E 시험

1. 봇이 Sentry 알림 채널에 초대되어 있는지 확인한다.
2. 테스트용 에러를 발생시켜(또는 기존 알림을 이용해) Slack 에 Sentry 알림이 오게 한다.
3. 그 알림 **메시지의 스레드**에 정확히 `!AI분석` 만 입력한다.
4. 같은 스레드에 `🔎 AI 분석을 시작합니다` 가 즉시 달리는지 확인한다.
5. 수 분 내 `✅ AI 장애 분석 결과` 가 **같은 스레드**에 오는지 확인한다.
6. 결과 헤더의 `revision` 이 실제 배포된 Commit 과 같은지, 근거로 제시된 `파일:라인` 이
   그 revision 의 실제 코드와 맞는지 수동 검수한다.
7. 재시작 후에도 인증이 유지되는지 확인한다.

   ```bash
   docker compose restart bot
   docker compose run --rm selfcheck python -m error_analysis_agent.selfcheck --only codex
   # ✅ codex 인증 — chatgpt ...
   ```

   > `docker compose run` 에서 서비스 이름 뒤의 인자는 명령 전체를 덮어쓰므로,
   > 옵션을 줄 때는 위처럼 모듈까지 함께 적어야 한다.

8. 읽기 전용 정책 확인: 분석이 끝난 뒤 `repos/`, 서비스 저장소, Git 원격에 변경이
   없어야 한다. 스냅샷은 작업이 끝나면 삭제된다.

   ```bash
   git -C repos/my-service status      # 변화 없음 (mirror 이므로 작업 트리도 없음)
   ls state/snapshots                  # 작업 종료 후 비어 있음
   ```

---

## 4. 설정 레퍼런스

환경변수는 `.env`, Sentry 프로젝트 ↔ Git 매핑은 `config.local.yaml` 이다
(`config.yaml` 은 예제이며 저장소에 commit 된다).
`analysis` 섹션의 값은 같은 이름의 환경변수가 있으면 **환경변수가 우선**한다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `SLACK_BOT_TOKEN` | (필수) | `xoxb-...` |
| `SLACK_APP_TOKEN` | (필수) | `xapp-...` (Socket Mode) |
| `SLACK_ALLOWED_CHANNEL_IDS` | 빈 값 = 모든 채널 | 쉼표 구분 채널 ID |
| `SLACK_ALLOWED_USER_IDS` | 빈 값 = **전부 거부** | 쉼표 구분 사용자 ID |
| `SLACK_COMMAND` | `!AI분석` | 트리거 명령 (정확히 일치, NFC 정규화) |
| `SLACK_MAX_MESSAGE_CHARS` | `3500` | 메시지 1건 최대 길이 (최소 500) |
| `SENTRY_BASE_URL` | (필수) | Sentry 접속 주소 |
| `SENTRY_ORG` | (필수) | 조직 slug |
| `SENTRY_AUTH_TOKEN` | (필수) | `event:read` 토큰 |
| `SENTRY_TIMEOUT_SECONDS` | `15` | HTTP timeout |
| `SENTRY_MAX_RETRIES` | `3` | 429/5xx/timeout 재시도 횟수(지수 백오프 0.5s→1s→2s) |
| `SENTRY_EXTRA_ALLOWED_HOSTS` | 빈 값 | 추가로 신뢰할 Sentry 호스트 |
| `CODEX_HOME` | (필수) | OAuth 세션 저장 디렉터리 |
| `CODEX_MODEL` | 빈 값 | 비우면 계정 기본 모델 |
| `CODEX_UNTRUSTED_INPUT_MODE` | `external_message` | `external_message` \| `inline` |
| `CODEX_REASONING_EFFORT` | 빈 값 | `none`\|`minimal`\|`low`\|`medium`\|`high`\|`xhigh`\|`max`\|`ultra`. 비우면 모델 기본 강도 (빈 값과 `none` 은 다르다). 모델별 지원 범위가 달라 런타임이 거부할 수 있다 |
| `ANALYSIS_MAX_PARALLEL` | `2` | 동시 분석 수 (큐 상한은 이 값의 4배) |
| `ANALYSIS_TIMEOUT_SECONDS` | `300` | 분석 제한 시간(초과 시 강제 종료) |
| `ANALYSIS_REQUIRE_RELEASE_REVISION` | `true` | revision 미확인 시 중단 |
| `ANALYSIS_MAX_CONTEXT_BYTES` | `30000` | Codex 에 넘기는 컨텍스트 상한 (최소 1000) |
| `ANALYSIS_SNAPSHOT_ROOT` | `/workspace/snapshots` | 스냅샷 생성 위치(쓰기 가능) |
| `ANALYSIS_ALLOW_FETCH` | `true` | revision 없을 때 `git fetch` 허용 |
| `ANALYSIS_AGENTS_MD_POLICY` | `prefer_repo` | `prefer_repo` \| `always_template` |
| `ANALYSIS_AGENTS_MD_TEMPLATE` | `/app/prompts/AGENTS.analysis.md` | 분석용 AGENTS.md |
| `CONFIG_PATH` | `config.yaml` | 매핑 파일 경로 |
| `STATE_DB_PATH` | 빈 값 = 메모리만 | `event_id` 중복 방지 SQLite |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |

설정은 모두 **기동 시점에 검증**된다. 필수 환경변수가 없으면 변수 이름을 지목해
`ConfigError` 로 종료하고, 숫자·enum·URL 형식, `projects` 매핑(최소 1개, `repo_dir` 필수,
`sentry_project_slug` 중복 금지)도 이때 확인한다.

분석용 `AGENTS.md` 는 스냅샷 최상위에 놓인다. 서비스 저장소에 `AGENTS.md` 가 있으면
`prefer_repo` 정책에서 그것을 사용하고, 없으면 `prompts/AGENTS.analysis.md` 를 복사한다.
**원격 저장소는 변경하지 않는다.**

---

## 5. Sentry 에 Commit SHA 기록하기

봇의 핵심 기능(장애 당시 revision 고정)은 **Sentry 이벤트가 release 또는 commit 정보를
들고 올 때만** 동작한다. 배포 파이프라인에서 다음 중 하나를 해 두면 된다.

**방법 A — release 이름에 SHA 를 넣는다 (권장)**

```bash
# 배포 스크립트 예시
export APP_RELEASE="my-service@1.4.2+$(git rev-parse --short HEAD)"
```

애플리케이션에서 Sentry SDK 초기화 시 이 값을 `release` 로 넘긴다.
봇은 release 이름 안에 박힌 7~40자 hex 문자열을 SHA 후보로 인식한다.

**방법 B — `commit` 태그를 붙인다**

이벤트 태그에 아래 키 중 하나로 전체 SHA 를 넣으면 된다. 태그가 release 보다 우선한다.

```text
commit, git_commit, git_sha, gitsha, sha, revision,
release.commit, build_commit, vcs_revision
```

**봇이 확인하는 순서**

1. commit 태그 (위 9종)
2. `release.lastCommit.id`
3. release 이름에 포함된 SHA
4. `release.ref`
5. release 이름 자체를 ref(태그/브랜치)로 해석

각 후보는 `git rev-parse --verify --quiet <rev>^{commit}` 로 **로컬 저장소에 실제로
존재하는지** 확인한다. 전부 실패하면 `ANALYSIS_ALLOW_FETCH=true` 일 때 `git fetch` 를
한 번 수행하고 재시도하며, 그래도 없으면 분석을 중단한다.

**설정하지 않으면** `ANALYSIS_REQUIRE_RELEASE_REVISION=true`(기본값)에서 분석이 거부된다.
`false` 로 내리면 기본 브랜치로 폴백하지만, 결과 헤더에 `미검증(fallback)` 으로 표시되고
"장애 당시 코드"라는 보장이 사라진다.
