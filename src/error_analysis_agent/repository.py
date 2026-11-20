"""Map a Sentry project to a Git checkout and build a read-only source snapshot.

The bot — never the agent — runs every Git command. The snapshot handed to
Codex is a plain directory extracted with ``git archive``: it carries no
``.git`` directory, no remotes and no credentials, so the agent cannot fetch,
commit or push even if it tried.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import AnalysisSettings, AppConfig, ProjectConfig
from .sentry_client import RevisionHints

log = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SHA_IN_TEXT_RE = re.compile(r"(?<![0-9a-zA-Z])([0-9a-f]{7,40})(?![0-9a-zA-Z])")
# Release names and refs come from Sentry, i.e. from untrusted input. Only this
# character set is ever passed to git, and never something starting with "-".
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@/\-]{0,200}$")


class RepositoryError(RuntimeError):
    user_message = "분석 대상 소스를 준비하지 못했습니다."


class ProjectNotMapped(RepositoryError):
    user_message = (
        "이 Sentry 프로젝트에 대응하는 Git 저장소 매핑이 없습니다. "
        "관리자가 `config.yaml` 의 `projects` 에 추가해야 합니다."
    )


class RevisionUnavailable(RepositoryError):
    user_message = (
        "장애 당시 배포 revision(Commit SHA)을 확인하지 못해 분석을 중단했습니다. "
        "최신 코드로 추측 분석하지 않습니다. 배포 파이프라인이 Sentry Release 에 "
        "Commit SHA 를 기록하도록 설정해야 합니다."
    )


class SnapshotError(RepositoryError):
    user_message = "분석용 소스 스냅샷 생성에 실패했습니다. 관리자가 로그를 확인해야 합니다."


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


GitRunner = Callable[[Sequence[str], Path, "Path | None", float], GitResult]


def run_git(
    args: Sequence[str], cwd: Path, stdout_path: Path | None = None, timeout: float = 120.0
) -> GitResult:
    """Run one git command. Replaced by a fake in tests."""
    command = ["git", *args]
    log.debug("git %s (cwd=%s)", " ".join(args), cwd)
    stdout_file = None
    try:
        if stdout_path is not None:
            stdout_file = stdout_path.open("wb")
        completed = subprocess.run(  # noqa: S603 - fixed binary, validated args
            command,
            cwd=str(cwd),
            stdout=stdout_file or subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RepositoryError(f"git 명령이 timeout 되었습니다: {' '.join(args)}") from exc
    except FileNotFoundError as exc:
        raise RepositoryError("git 실행 파일을 찾을 수 없습니다.") from exc
    finally:
        if stdout_file is not None:
            stdout_file.close()

    stdout = "" if stdout_path is not None else completed.stdout.decode("utf-8", "replace")
    return GitResult(
        returncode=completed.returncode,
        stdout=stdout,
        stderr=completed.stderr.decode("utf-8", "replace"),
    )


@dataclass(frozen=True)
class RevisionCandidate:
    value: str
    source: str


@dataclass(frozen=True)
class ResolvedRevision:
    sha: str
    source: str
    release_version: str | None = None
    verified: bool = True

    @property
    def short(self) -> str:
        return self.sha[:12]


@dataclass
class Snapshot:
    path: Path
    revision: ResolvedRevision
    project: ProjectConfig
    agents_md_source: str
    _cleanup_root: Path | None = None

    def cleanup(self) -> None:
        root = self._cleanup_root or self.path
        if root.exists():
            _make_writable(root)
            shutil.rmtree(root, ignore_errors=True)


def _make_writable(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            target = Path(current) / name
            try:
                target.chmod(target.stat().st_mode | stat.S_IWUSR)
            except OSError:  # pragma: no cover - best effort
                pass
    try:
        root.chmod(root.stat().st_mode | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass


def _make_read_only(root: Path) -> None:
    """Belt and braces on top of ``Sandbox.read_only``."""
    for current, dirs, files in os.walk(root):
        for name in files:
            target = Path(current) / name
            try:
                target.chmod(0o444)
            except OSError:  # pragma: no cover
                pass
        for name in dirs:
            target = Path(current) / name
            try:
                target.chmod(0o555)
            except OSError:  # pragma: no cover
                pass
    try:
        root.chmod(0o555)
    except OSError:  # pragma: no cover
        pass


def revision_candidates(hints: RevisionHints) -> list[RevisionCandidate]:
    """Ordered list of things that might be the deployed commit.

    Explicit commit tags beat a release's recorded commit, which beats a SHA
    embedded in the release name, which beats treating the release name as a
    tag.
    """
    candidates: list[RevisionCandidate] = []
    seen: set[str] = set()

    def add(value: str | None, source: str) -> None:
        if not value:
            return
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        candidates.append(RevisionCandidate(value=cleaned, source=source))

    for key, value in hints.commit_tags:
        add(value, f"tag:{key}")
    add(hints.last_commit_id, "release.lastCommit")

    if hints.release_version:
        for match in _SHA_IN_TEXT_RE.finditer(hints.release_version):
            add(match.group(1), "release.version(sha)")
    add(hints.release_ref, "release.ref")
    add(hints.release_version, "release.version(ref)")
    return candidates


class RepositoryManager:
    """Resolves projects and revisions, and materializes analysis snapshots."""

    def __init__(
        self,
        config: AppConfig,
        *,
        git: GitRunner = run_git,
        settings: AnalysisSettings | None = None,
    ) -> None:
        self._config = config
        self._git = git
        self._settings = settings or config.analysis

    # -- project mapping --------------------------------------------------- #

    def resolve_project(self, project_slug: str | None) -> ProjectConfig:
        project = self._config.project_for_slug(project_slug)
        if project is not None:
            return project
        if project_slug is None and len(self._config.projects) == 1:
            return self._config.projects[0]
        raise ProjectNotMapped(f"매핑되지 않은 Sentry 프로젝트: {project_slug!r}")

    # -- revision ---------------------------------------------------------- #

    def resolve_revision(self, project: ProjectConfig, hints: RevisionHints) -> ResolvedRevision:
        candidates = revision_candidates(hints)
        resolved = self._first_verified(project, candidates)

        if resolved is None and candidates and self._settings.allow_fetch:
            self._fetch(project)
            resolved = self._first_verified(project, candidates)

        if resolved is not None:
            return resolved

        if self._settings.require_release_revision:
            raise RevisionUnavailable(
                "검증된 Commit SHA 를 찾지 못했습니다 "
                f"(후보 {len(candidates)}개, release={hints.release_version!r})"
            )

        sha = self._rev_parse(project, f"refs/heads/{project.default_branch}") or self._rev_parse(
            project, project.default_branch
        )
        if sha is None:
            raise RevisionUnavailable(
                f"기본 브랜치 {project.default_branch} 도 확인할 수 없습니다: {project.repo_dir}"
            )
        log.warning(
            "falling back to default branch project=%s branch=%s", project.name, project.default_branch
        )
        return ResolvedRevision(
            sha=sha,
            source=f"fallback:{project.default_branch}",
            release_version=hints.release_version,
            verified=False,
        )

    def _first_verified(
        self, project: ProjectConfig, candidates: Sequence[RevisionCandidate]
    ) -> ResolvedRevision | None:
        for candidate in candidates:
            sha = self._verify(project, candidate.value)
            if sha:
                return ResolvedRevision(sha=sha, source=candidate.source, release_version=None)
        return None

    def _verify(self, project: ProjectConfig, value: str) -> str | None:
        if _SHA_RE.match(value):
            return self._rev_parse(project, value)
        if not _SAFE_REF_RE.match(value):
            log.info("rejecting unsafe revision candidate: %r", value[:80])
            return None
        for ref in (f"refs/tags/{value}", f"refs/heads/{value}", f"refs/remotes/origin/{value}"):
            sha = self._rev_parse(project, ref)
            if sha:
                return sha
        return None

    def _rev_parse(self, project: ProjectConfig, rev: str) -> str | None:
        result = self._git(
            ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], project.repo_dir, None, 30.0
        )
        if not result.ok:
            return None
        sha = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None

    def _fetch(self, project: ProjectConfig) -> None:
        log.info("fetching project=%s", project.name)
        result = self._git(["fetch", "--quiet", "--tags", "--prune", "origin"], project.repo_dir, None, 300.0)
        if not result.ok:
            log.warning("git fetch failed project=%s stderr=%s", project.name, result.stderr[:500])

    # -- snapshot ---------------------------------------------------------- #

    def create_snapshot(
        self, project: ProjectConfig, revision: ResolvedRevision, job_id: str
    ) -> Snapshot:
        root = self._settings.snapshot_root
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SnapshotError(f"스냅샷 디렉터리를 만들 수 없습니다: {root} ({exc})") from exc

        work_root = Path(tempfile.mkdtemp(prefix=f"{project.name}-{revision.short}-", dir=str(root)))
        source_dir = work_root / "src"
        source_dir.mkdir()
        archive_path = work_root / "source.tar"

        try:
            result = self._git(
                ["archive", "--format=tar", revision.sha], project.repo_dir, archive_path, 300.0
            )
            if not result.ok:
                raise SnapshotError(
                    f"git archive 실패 (revision={revision.short}): {result.stderr[:300]}"
                )
            _extract_tar(archive_path, source_dir)
            archive_path.unlink(missing_ok=True)
            agents_source = self._place_agents_md(source_dir)
            _make_read_only(source_dir)
        except Exception:
            _make_writable(work_root)
            shutil.rmtree(work_root, ignore_errors=True)
            raise

        log.info(
            "snapshot ready project=%s revision=%s path=%s agents_md=%s",
            project.name,
            revision.short,
            source_dir,
            agents_source,
        )
        return Snapshot(
            path=source_dir,
            revision=revision,
            project=project,
            agents_md_source=agents_source,
            _cleanup_root=work_root,
        )

    def _place_agents_md(self, source_dir: Path) -> str:
        """Ensure the snapshot root has the analysis ``AGENTS.md``."""
        target = source_dir / "AGENTS.md"
        policy = self._settings.agents_md_policy
        if policy == "prefer_repo" and target.exists():
            return "repository"

        template = self._settings.agents_md_template
        if not template.exists():
            if target.exists():
                return "repository"
            raise SnapshotError(f"분석용 AGENTS.md 템플릿을 찾을 수 없습니다: {template}")
        shutil.copyfile(template, target)
        return "template"


def _extract_tar(archive_path: Path, destination: Path) -> None:
    """Extract a tar produced by ``git archive``, refusing unsafe members.

    Symlinks, hardlinks, device nodes, absolute paths and ``..`` escapes are
    skipped so the extracted tree stays inside ``destination``.
    """
    destination = destination.resolve()
    with tarfile.open(archive_path, "r:") as tar:
        for member in tar:
            if not (member.isfile() or member.isdir()):
                continue
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                log.warning("skipping unsafe archive member: %r", name[:120])
                continue
            target = (destination / name).resolve()
            if destination != target and destination not in target.parents:
                log.warning("skipping out-of-tree archive member: %r", name[:120])
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:  # pragma: no cover - defensive
                continue
            with extracted, target.open("wb") as handle:
                shutil.copyfileobj(extracted, handle)
