from __future__ import annotations

from pathlib import Path

import pytest
from conftest import PROJECT_SLUG, base_env, make_config

from error_analysis_agent.config import ConfigError, load_config


def test_defaults(config):
    assert config.slack.command == "!AI분석"
    assert config.sentry.host == "sentry.example.com"
    assert config.sentry.allowed_hosts == frozenset({"sentry.example.com"})
    assert config.analysis.require_release_revision is True
    assert config.analysis.max_parallel_jobs == 2
    assert config.codex.untrusted_input_mode == "external_message"
    assert config.codex.reasoning_effort is None
    assert config.projects[0].sentry_project_slug == PROJECT_SLUG


def test_api_urls(config):
    assert config.sentry.issue_url("4242").endswith("/api/0/organizations/my-org/issues/4242/")
    assert config.sentry.latest_event_url("4242").endswith("/issues/4242/events/latest/")


def test_extra_allowed_hosts(config_path):
    config = make_config(config_path, SENTRY_EXTRA_ALLOWED_HOSTS="my-org.sentry.io, sentry.io")
    assert config.sentry.allowed_hosts == frozenset(
        {"sentry.example.com", "my-org.sentry.io", "sentry.io"}
    )


@pytest.mark.parametrize(
    "missing",
    ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SENTRY_BASE_URL", "SENTRY_ORG", "SENTRY_AUTH_TOKEN"],
)
def test_missing_secrets_are_reported_by_name(config_path, missing):
    env = base_env()
    env.pop(missing)
    with pytest.raises(ConfigError, match=missing):
        load_config(env, config_path=config_path)


def test_codex_home_is_required(config_path):
    env = base_env()
    env.pop("CODEX_HOME")
    with pytest.raises(ConfigError, match="CODEX_HOME"):
        load_config(env, config_path=config_path)


def test_bad_values_are_rejected(config_path):
    with pytest.raises(ConfigError, match="정수"):
        make_config(config_path, ANALYSIS_MAX_PARALLEL="many")
    with pytest.raises(ConfigError, match="true/false"):
        make_config(config_path, ANALYSIS_REQUIRE_RELEASE_REVISION="maybe")
    with pytest.raises(ConfigError, match="CODEX_UNTRUSTED_INPUT_MODE"):
        make_config(config_path, CODEX_UNTRUSTED_INPUT_MODE="trusted")
    with pytest.raises(ConfigError, match="CODEX_REASONING_EFFORT"):
        make_config(config_path, CODEX_REASONING_EFFORT="very-high")
    with pytest.raises(ConfigError, match="URL"):
        make_config(config_path, SENTRY_BASE_URL="not-a-url")


def test_reasoning_effort_is_parsed_and_matches_the_sdk_enum(config_path):
    from openai_codex.types import ReasoningEffort

    from error_analysis_agent.config import REASONING_EFFORTS

    assert make_config(config_path, CODEX_REASONING_EFFORT="high").codex.reasoning_effort == "high"
    # blank means "leave it to the runtime", not the explicit "none" effort
    assert make_config(config_path, CODEX_REASONING_EFFORT="  ").codex.reasoning_effort is None
    # the allowlist must not drift from the pinned SDK
    assert set(REASONING_EFFORTS) == {member.value for member in ReasoningEffort}


def test_environment_overrides_yaml(config_path):
    config = make_config(
        config_path,
        ANALYSIS_MAX_PARALLEL="7",
        ANALYSIS_REQUIRE_RELEASE_REVISION="false",
        ANALYSIS_MAX_CONTEXT_BYTES="12000",
    )
    assert config.analysis.max_parallel_jobs == 7
    assert config.analysis.require_release_revision is False
    assert config.analysis.max_context_bytes == 12000


def test_trailing_slash_in_base_url_is_normalized(config_path):
    config = make_config(config_path, SENTRY_BASE_URL="https://sentry.example.com/")
    assert config.sentry.base_url == "https://sentry.example.com"


def test_missing_config_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="찾을 수 없습니다"):
        load_config(base_env(), config_path=tmp_path / "nope.yaml")


def test_projects_section_is_required(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("analysis: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="projects"):
        load_config(base_env(), config_path=path)


def test_repo_dir_is_required(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("projects:\n  svc:\n    sentry_project_slug: svc\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="repo_dir"):
        load_config(base_env(), config_path=path)


def test_duplicate_sentry_slug_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "projects:\n"
        "  a:\n    sentry_project_slug: same\n    repo_dir: /tmp/a\n"
        "  b:\n    sentry_project_slug: SAME\n    repo_dir: /tmp/b\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="중복"):
        load_config(base_env(), config_path=path)


def test_allowlists_are_parsed_from_csv(config_path):
    config = make_config(config_path, SLACK_ALLOWED_USER_IDS=" U1 , U2 ,, U3 ")
    assert config.slack.allowed_user_ids == frozenset({"U1", "U2", "U3"})
