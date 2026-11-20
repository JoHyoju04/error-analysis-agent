"""Settings loaded from environment variables and ``config.yaml``.

Secrets (Slack tokens, Sentry token) only ever come from the environment.
Sentry project -> Git repository mapping lives in ``config.yaml`` because it is
not secret and is easier to review as a file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

DEFAULT_COMMAND = "!AI분석"
UNTRUSTED_INPUT_MODES = ("external_message", "inline")
AGENTS_MD_POLICIES = ("prefer_repo", "always_template")
#: ``openai_codex.ReasoningEffort`` members, read from the pinned SDK (0.163.0).
#: Every model advertises its own ``defaultReasoningEffort`` and
#: ``supportedReasoningEfforts``, so a value accepted here can still be refused
#: by the runtime. An unset variable sends nothing and keeps the model default,
#: which is not the same as the explicit ``"none"`` effort.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or inconsistent."""


def _get(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _require(env: Mapping[str, str], name: str) -> str:
    value = _get(env, name)
    if not value:
        raise ConfigError(f"환경변수 {name} 가 설정되지 않았습니다.")
    return value


def _id_set(env: Mapping[str, str], name: str) -> frozenset[str]:
    raw = _get(env, name, "") or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"환경변수 {name} 는 정수여야 합니다: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"환경변수 {name} 는 {minimum} 이상이어야 합니다: {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"환경변수 {name} 는 숫자여야 합니다: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"환경변수 {name} 는 0보다 커야 합니다: {value}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "y", "on"):
        return True
    if lowered in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"환경변수 {name} 는 true/false 여야 합니다: {raw!r}")


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _get(env, name, default) or default
    if value not in allowed:
        raise ConfigError(f"환경변수 {name} 는 {allowed} 중 하나여야 합니다: {value!r}")
    return value


def _optional_choice(env: Mapping[str, str], name: str, allowed: tuple[str, ...]) -> str | None:
    """Like :func:`_choice`, but an unset variable means "leave it to the runtime"."""
    value = _get(env, name)
    if value is None:
        return None
    if value not in allowed:
        raise ConfigError(f"환경변수 {name} 는 {allowed} 중 하나여야 합니다: {value!r}")
    return value


@dataclass(frozen=True)
class SlackSettings:
    bot_token: str
    app_token: str
    allowed_channel_ids: frozenset[str]
    allowed_user_ids: frozenset[str]
    command: str = DEFAULT_COMMAND
    max_message_chars: int = 3500


@dataclass(frozen=True)
class SentrySettings:
    base_url: str
    org: str
    auth_token: str
    timeout_seconds: float = 15.0
    max_retries: int = 3
    extra_allowed_hosts: frozenset[str] = frozenset()

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Hosts a Slack link may point at. Anything else is refused (SSRF)."""
        hosts = {self.host} | {h.lower() for h in self.extra_allowed_hosts}
        return frozenset(h for h in hosts if h)

    def issue_url(self, issue_id: str) -> str:
        return f"{self.base_url}/api/0/organizations/{self.org}/issues/{issue_id}/"

    def latest_event_url(self, issue_id: str) -> str:
        return f"{self.base_url}/api/0/organizations/{self.org}/issues/{issue_id}/events/latest/"


@dataclass(frozen=True)
class CodexSettings:
    codex_home: Path
    model: str | None = None
    untrusted_input_mode: str = "external_message"
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class AnalysisSettings:
    max_parallel_jobs: int = 2
    timeout_seconds: int = 300
    require_release_revision: bool = True
    max_context_bytes: int = 30000
    snapshot_root: Path = Path("/workspace/snapshots")
    allow_fetch: bool = True
    agents_md_policy: str = "prefer_repo"
    agents_md_template: Path = Path("/app/prompts/AGENTS.analysis.md")


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    sentry_project_slug: str
    repo_dir: Path
    repository_url: str | None = None
    default_branch: str = "main"


@dataclass(frozen=True)
class AppConfig:
    slack: SlackSettings
    sentry: SentrySettings
    codex: CodexSettings
    analysis: AnalysisSettings
    projects: tuple[ProjectConfig, ...]
    state_db_path: Path | None = None
    log_level: str = "INFO"

    def project_for_slug(self, slug: str | None) -> ProjectConfig | None:
        if not slug:
            return None
        wanted = slug.strip().lower()
        for project in self.projects:
            if project.sentry_project_slug.lower() == wanted:
                return project
        return None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config 파일을 찾을 수 없습니다: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"config 파일 최상위는 매핑이어야 합니다: {path}")
    return loaded


def _parse_projects(raw: Any, path: Path) -> tuple[ProjectConfig, ...]:
    if not raw:
        raise ConfigError(f"{path} 에 projects 항목이 최소 1개 필요합니다.")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 의 projects 는 매핑이어야 합니다.")

    projects: list[ProjectConfig] = []
    seen_slugs: set[str] = set()
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"{path} 의 projects.{name} 는 매핑이어야 합니다.")
        slug = str(body.get("sentry_project_slug") or name).strip()
        repo_dir = body.get("repo_dir")
        if not repo_dir:
            raise ConfigError(f"{path} 의 projects.{name}.repo_dir 가 필요합니다.")
        if slug.lower() in seen_slugs:
            raise ConfigError(f"{path} 에 sentry_project_slug 가 중복되었습니다: {slug}")
        seen_slugs.add(slug.lower())
        projects.append(
            ProjectConfig(
                name=str(name),
                sentry_project_slug=slug,
                repo_dir=Path(str(repo_dir)),
                repository_url=(str(body["repository_url"]) if body.get("repository_url") else None),
                default_branch=str(body.get("default_branch") or "main"),
            )
        )
    return tuple(projects)


def _analysis_settings(
    env: Mapping[str, str], raw: Mapping[str, Any], config_path: Path
) -> AnalysisSettings:
    """YAML supplies the defaults; environment variables win when both are set."""
    defaults = AnalysisSettings()

    def yaml_int(key: str, fallback: int) -> int:
        value = raw.get(key, fallback)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{config_path} 의 analysis.{key} 는 정수여야 합니다.") from exc

    template = _get(env, "ANALYSIS_AGENTS_MD_TEMPLATE") or str(
        raw.get("agents_md_template") or config_path.parent / "prompts" / "AGENTS.analysis.md"
    )
    snapshot_root = _get(env, "ANALYSIS_SNAPSHOT_ROOT") or str(
        raw.get("snapshot_root") or defaults.snapshot_root
    )
    return AnalysisSettings(
        max_parallel_jobs=_int(
            env, "ANALYSIS_MAX_PARALLEL", yaml_int("max_parallel_jobs", defaults.max_parallel_jobs)
        ),
        timeout_seconds=_int(
            env, "ANALYSIS_TIMEOUT_SECONDS", yaml_int("timeout_seconds", defaults.timeout_seconds)
        ),
        require_release_revision=_bool(
            env,
            "ANALYSIS_REQUIRE_RELEASE_REVISION",
            bool(raw.get("require_release_revision", defaults.require_release_revision)),
        ),
        max_context_bytes=_int(
            env,
            "ANALYSIS_MAX_CONTEXT_BYTES",
            yaml_int("max_context_bytes", defaults.max_context_bytes),
            minimum=1000,
        ),
        snapshot_root=Path(snapshot_root),
        allow_fetch=_bool(env, "ANALYSIS_ALLOW_FETCH", bool(raw.get("allow_fetch", defaults.allow_fetch))),
        agents_md_policy=_choice(
            env,
            "ANALYSIS_AGENTS_MD_POLICY",
            str(raw.get("agents_md_policy") or defaults.agents_md_policy),
            AGENTS_MD_POLICIES,
        ),
        agents_md_template=Path(template),
    )


def load_config(
    env: Mapping[str, str] | None = None, config_path: str | os.PathLike[str] | None = None
) -> AppConfig:
    """Build the full configuration, raising :class:`ConfigError` on any problem."""
    env = os.environ if env is None else env

    path = Path(config_path or _get(env, "CONFIG_PATH", "config.yaml") or "config.yaml")
    raw = _load_yaml(path)
    raw_analysis = raw.get("analysis") or {}
    if not isinstance(raw_analysis, dict):
        raise ConfigError(f"{path} 의 analysis 는 매핑이어야 합니다.")

    base_url = _require(env, "SENTRY_BASE_URL").rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"SENTRY_BASE_URL 이 올바른 URL 이 아닙니다: {base_url!r}")

    codex_home = _get(env, "CODEX_HOME")
    if not codex_home:
        raise ConfigError(
            "환경변수 CODEX_HOME 가 설정되지 않았습니다. "
            "ChatGPT OAuth 세션을 보존할 전용 디렉터리를 지정해야 합니다."
        )

    state_db = _get(env, "STATE_DB_PATH")

    return AppConfig(
        slack=SlackSettings(
            bot_token=_require(env, "SLACK_BOT_TOKEN"),
            app_token=_require(env, "SLACK_APP_TOKEN"),
            allowed_channel_ids=_id_set(env, "SLACK_ALLOWED_CHANNEL_IDS"),
            allowed_user_ids=_id_set(env, "SLACK_ALLOWED_USER_IDS"),
            command=_get(env, "SLACK_COMMAND", DEFAULT_COMMAND) or DEFAULT_COMMAND,
            max_message_chars=_int(env, "SLACK_MAX_MESSAGE_CHARS", 3500, minimum=500),
        ),
        sentry=SentrySettings(
            base_url=base_url,
            org=_require(env, "SENTRY_ORG"),
            auth_token=_require(env, "SENTRY_AUTH_TOKEN"),
            timeout_seconds=_float(env, "SENTRY_TIMEOUT_SECONDS", 15.0),
            max_retries=_int(env, "SENTRY_MAX_RETRIES", 3, minimum=0),
            extra_allowed_hosts=_id_set(env, "SENTRY_EXTRA_ALLOWED_HOSTS"),
        ),
        codex=CodexSettings(
            codex_home=Path(codex_home),
            model=_get(env, "CODEX_MODEL"),
            untrusted_input_mode=_choice(
                env, "CODEX_UNTRUSTED_INPUT_MODE", "external_message", UNTRUSTED_INPUT_MODES
            ),
            reasoning_effort=_optional_choice(env, "CODEX_REASONING_EFFORT", REASONING_EFFORTS),
        ),
        analysis=_analysis_settings(env, raw_analysis, path),
        projects=_parse_projects(raw.get("projects"), path),
        state_db_path=Path(state_db) if state_db else None,
        log_level=(_get(env, "LOG_LEVEL", "INFO") or "INFO").upper(),
    )
