from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest
from conftest import PROJECT_SLUG, make_config

from error_analysis_agent.config import ProjectConfig
from error_analysis_agent.repository import (
    GitResult,
    ProjectNotMapped,
    RepositoryManager,
    ResolvedRevision,
    RevisionUnavailable,
    _extract_tar,
    revision_candidates,
    run_git,
)
from error_analysis_agent.sentry_client import RevisionHints

SHA = "1f2e3d4c5b6a7890abcdef1234567890abcdef12"
OTHER_SHA = "aaaabbbbccccddddeeeeffff0000111122223333"


class FakeGit:
    """Records git invocations and resolves only the revisions it is told to."""

    def __init__(self, *, known: dict[str, str] | None = None, known_after_fetch: dict[str, str] | None = None):
        self.known = known or {}
        self.known_after_fetch = known_after_fetch or {}
        self.calls: list[list[str]] = []
        self.fetched = False

    def __call__(self, args, cwd, stdout_path=None, timeout=0.0) -> GitResult:
        self.calls.append(list(args))
        if args[0] == "fetch":
            self.fetched = True
            self.known.update(self.known_after_fetch)
            return GitResult(0, "", "")
        if args[0] == "rev-parse":
            wanted = args[-1].removesuffix("^{commit}")
            sha = self.known.get(wanted)
            return GitResult(0, f"{sha}\n", "") if sha else GitResult(1, "", "")
        return GitResult(0, "", "")


@pytest.fixture
def project(config) -> ProjectConfig:
    return config.projects[0]


# --------------------------------------------------------------------------- #
# project mapping
# --------------------------------------------------------------------------- #


def test_project_is_resolved_by_sentry_slug(config):
    manager = RepositoryManager(config, git=FakeGit())
    assert manager.resolve_project(PROJECT_SLUG).name == PROJECT_SLUG
    assert manager.resolve_project(PROJECT_SLUG.upper()).name == PROJECT_SLUG


def test_unmapped_project_fails_closed(config):
    manager = RepositoryManager(config, git=FakeGit())
    with pytest.raises(ProjectNotMapped):
        manager.resolve_project("some-other-service")


def test_multi_project_mapping(tmp_path, config_path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    config_path.write_text(
        f"""
projects:
  service-a:
    sentry_project_slug: service-a
    repo_dir: {repo_a}
  service-b:
    sentry_project_slug: sentry-b-slug
    repo_dir: {repo_b}
    default_branch: develop
analysis:
  snapshot_root: {tmp_path / 'snap'}
  agents_md_template: {tmp_path / 'prompts' / 'AGENTS.analysis.md'}
""",
        encoding="utf-8",
    )
    config = make_config(config_path)
    manager = RepositoryManager(config, git=FakeGit())
    assert manager.resolve_project("service-a").repo_dir == repo_a
    assert manager.resolve_project("sentry-b-slug").repo_dir == repo_b
    assert manager.resolve_project("sentry-b-slug").default_branch == "develop"
    with pytest.raises(ProjectNotMapped):
        manager.resolve_project(None)


def test_single_project_is_used_when_sentry_reports_no_slug(config):
    manager = RepositoryManager(config, git=FakeGit())
    assert manager.resolve_project(None).name == PROJECT_SLUG


# --------------------------------------------------------------------------- #
# revision resolution
# --------------------------------------------------------------------------- #


def test_candidate_priority_order():
    hints = RevisionHints(
        release_version=f"svc@1.4.2+{SHA}",
        release_ref="v1.4.2",
        last_commit_id=OTHER_SHA,
        commit_tags=(("commit", "abc1234"),),
    )
    assert [(c.value, c.source) for c in revision_candidates(hints)] == [
        ("abc1234", "tag:commit"),
        (OTHER_SHA, "release.lastCommit"),
        (SHA, "release.version(sha)"),
        ("v1.4.2", "release.ref"),
        (f"svc@1.4.2+{SHA}", "release.version(ref)"),
    ]


def test_commit_tag_wins_when_several_candidates_resolve(config, project):
    git = FakeGit(known={"abc1234": SHA, OTHER_SHA: OTHER_SHA})
    manager = RepositoryManager(config, git=git)
    revision = manager.resolve_revision(
        project, RevisionHints(commit_tags=(("commit", "abc1234"),), last_commit_id=OTHER_SHA)
    )
    assert revision == ResolvedRevision(sha=SHA, source="tag:commit", release_version=None)
    assert revision.verified is True
    assert git.fetched is False


def test_release_name_is_resolved_as_a_tag(config, project):
    git = FakeGit(known={"refs/tags/v1.4.2": SHA})
    manager = RepositoryManager(config, git=git)
    revision = manager.resolve_revision(project, RevisionHints(release_version="v1.4.2"))
    assert revision.sha == SHA
    assert revision.source == "release.version(ref)"


def test_missing_revision_triggers_one_fetch_then_succeeds(config, project):
    git = FakeGit(known={}, known_after_fetch={SHA: SHA})
    manager = RepositoryManager(config, git=git)
    revision = manager.resolve_revision(project, RevisionHints(last_commit_id=SHA))
    assert revision.sha == SHA
    assert git.fetched is True


def test_unverified_revision_fails_closed(config, project):
    """require_release_revision=true 이면 최신 코드로 추측 분석하지 않는다."""
    git = FakeGit(known={"refs/heads/main": OTHER_SHA})
    manager = RepositoryManager(config, git=git)
    with pytest.raises(RevisionUnavailable):
        manager.resolve_revision(project, RevisionHints(release_version="svc@9.9.9"))


def test_no_revision_information_at_all_fails_closed(config, project):
    manager = RepositoryManager(config, git=FakeGit(known={"refs/heads/main": OTHER_SHA}))
    with pytest.raises(RevisionUnavailable):
        manager.resolve_revision(project, RevisionHints())


def test_fallback_to_default_branch_when_explicitly_allowed(config_path):
    config = make_config(config_path, ANALYSIS_REQUIRE_RELEASE_REVISION="false")
    project = config.projects[0]
    git = FakeGit(known={"refs/heads/main": OTHER_SHA})
    revision = RepositoryManager(config, git=git).resolve_revision(
        project, RevisionHints(release_version="svc@9.9.9")
    )
    assert revision.sha == OTHER_SHA
    assert revision.source == "fallback:main"
    assert revision.verified is False


def test_fetch_is_skipped_when_disabled(config_path):
    config = make_config(config_path, ANALYSIS_ALLOW_FETCH="false")
    git = FakeGit(known={}, known_after_fetch={SHA: SHA})
    with pytest.raises(RevisionUnavailable):
        RepositoryManager(config, git=git).resolve_revision(
            config.projects[0], RevisionHints(last_commit_id=SHA)
        )
    assert git.fetched is False


@pytest.mark.parametrize(
    "hostile",
    [
        "--upload-pack=touch /tmp/pwned",
        "-x",
        "main; rm -rf /",
        "../../etc/passwd",
        "$(whoami)",
        "refs/heads/main\nrm -rf /",
    ],
)
def test_hostile_revision_strings_never_reach_git(config, project, hostile):
    """Release names come from Sentry, so they are untrusted input."""
    git = FakeGit()
    manager = RepositoryManager(config, git=git)
    with pytest.raises(RevisionUnavailable):
        manager.resolve_revision(project, RevisionHints(release_version=hostile))
    passed = [arg for call in git.calls for arg in call]
    assert not any(hostile in arg for arg in passed)


# --------------------------------------------------------------------------- #
# snapshots (real git)
# --------------------------------------------------------------------------- #


def git_cmd(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def real_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo with two commits; returns (path, old_sha, new_sha)."""
    repo = tmp_path / "real-repo"
    repo.mkdir()
    git_cmd(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "app.py").write_text("VERSION = 'old'\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git_cmd(repo, "add", ".")
    git_cmd(repo, "commit", "--quiet", "-m", "first")
    old = git_cmd(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("VERSION = 'new'\n", encoding="utf-8")
    git_cmd(repo, "commit", "--quiet", "-am", "second")
    new = git_cmd(repo, "rev-parse", "HEAD")
    return repo, old, new


@pytest.fixture
def real_manager(tmp_path, config_path, real_repo):
    repo, _old, _new = real_repo
    template = tmp_path / "prompts" / "AGENTS.analysis.md"
    config_path.write_text(
        f"""
projects:
  {PROJECT_SLUG}:
    sentry_project_slug: {PROJECT_SLUG}
    repo_dir: {repo}
analysis:
  snapshot_root: {tmp_path / 'snapshots'}
  agents_md_template: {template}
""",
        encoding="utf-8",
    )
    config = make_config(config_path)
    return RepositoryManager(config, git=run_git), config


def test_snapshot_contains_the_source_at_that_revision(real_manager, real_repo):
    manager, config = real_manager
    _repo, old_sha, _new = real_repo
    revision = manager.resolve_revision(config.projects[0], RevisionHints(last_commit_id=old_sha))
    assert revision.sha == old_sha

    snapshot = manager.create_snapshot(config.projects[0], revision, "job123")
    try:
        assert (snapshot.path / "app.py").read_text(encoding="utf-8") == "VERSION = 'old'\n"
        assert (snapshot.path / "pkg" / "mod.py").exists()
        # no git metadata and therefore no remote, no credentials, no push path
        assert not (snapshot.path / ".git").exists()
        # the analysis AGENTS.md is at the snapshot root
        assert snapshot.agents_md_source == "template"
        assert (snapshot.path / "AGENTS.md").read_text(encoding="utf-8") == "# analysis rules\n"
        # read-only for the container user as well as for Codex
        assert not os.access(snapshot.path / "app.py", os.W_OK)
    finally:
        snapshot.cleanup()
    assert not snapshot.path.exists()


def test_repository_agents_md_is_preferred_when_present(real_manager, real_repo):
    manager, config = real_manager
    repo, _old, _new = real_repo
    (repo / "AGENTS.md").write_text("# repo rules\n", encoding="utf-8")
    git_cmd(repo, "add", "AGENTS.md")
    git_cmd(repo, "commit", "--quiet", "-m", "add agents")
    sha = git_cmd(repo, "rev-parse", "HEAD")

    revision = manager.resolve_revision(config.projects[0], RevisionHints(last_commit_id=sha))
    snapshot = manager.create_snapshot(config.projects[0], revision, "job124")
    try:
        assert snapshot.agents_md_source == "repository"
        assert (snapshot.path / "AGENTS.md").read_text(encoding="utf-8") == "# repo rules\n"
    finally:
        snapshot.cleanup()


def test_always_template_policy_overrides_the_repository(tmp_path, config_path, real_repo):
    repo, _old, _new = real_repo
    (repo / "AGENTS.md").write_text("# repo rules\n", encoding="utf-8")
    git_cmd(repo, "add", "AGENTS.md")
    git_cmd(repo, "commit", "--quiet", "-m", "add agents")
    sha = git_cmd(repo, "rev-parse", "HEAD")
    config_path.write_text(
        f"""
projects:
  {PROJECT_SLUG}:
    sentry_project_slug: {PROJECT_SLUG}
    repo_dir: {repo}
analysis:
  snapshot_root: {tmp_path / 'snapshots'}
  agents_md_template: {tmp_path / 'prompts' / 'AGENTS.analysis.md'}
  agents_md_policy: always_template
""",
        encoding="utf-8",
    )
    config = make_config(config_path)
    manager = RepositoryManager(config, git=run_git)
    revision = manager.resolve_revision(config.projects[0], RevisionHints(last_commit_id=sha))
    snapshot = manager.create_snapshot(config.projects[0], revision, "job125")
    try:
        assert snapshot.agents_md_source == "template"
        assert "analysis rules" in (snapshot.path / "AGENTS.md").read_text(encoding="utf-8")
    finally:
        snapshot.cleanup()


def test_concurrent_snapshots_of_different_revisions_are_isolated(real_manager, real_repo):
    manager, config = real_manager
    _repo, old_sha, new_sha = real_repo
    project = config.projects[0]
    first = manager.create_snapshot(project, ResolvedRevision(old_sha, "test"), "jobA")
    second = manager.create_snapshot(project, ResolvedRevision(new_sha, "test"), "jobB")
    try:
        assert first.path != second.path
        assert (first.path / "app.py").read_text(encoding="utf-8") == "VERSION = 'old'\n"
        assert (second.path / "app.py").read_text(encoding="utf-8") == "VERSION = 'new'\n"
    finally:
        first.cleanup()
        second.cleanup()


def test_extract_tar_refuses_escaping_members(tmp_path):
    archive = tmp_path / "evil.tar"
    payload = tmp_path / "payload"
    payload.write_text("x", encoding="utf-8")
    with tarfile.open(archive, "w") as tar:
        tar.add(payload, arcname="../escaped.txt")
        tar.add(payload, arcname="/absolute.txt")
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        tar.add(payload, arcname="ok.txt")

    destination = tmp_path / "out"
    destination.mkdir()
    _extract_tar(archive, destination)

    assert (destination / "ok.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert not (destination / "link").exists()
    assert sorted(p.name for p in destination.iterdir()) == ["absolute.txt", "ok.txt"]
