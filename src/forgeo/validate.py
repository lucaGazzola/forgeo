"""Read-only dry-run validation: is this forgeo ready to run?

``forgeo validate`` loads a config and checks everything a cycle needs before
it starts — the config parses, the repository exists and is a git repo, the
branch and remote resolve, the backlog parses, the agent command is
non-blank — and reports the run lock state. It never invokes the agent and
makes no writes (no lock is taken, no backlog or snapshot is touched), so it
is safe to run at any time, even while a daemon is active.

A URL backlog is fetched once (a plain GET, with credentials when
``backlog_auth`` is configured) rather than read from disk, so an unreachable
endpoint or a rejected token is reported here instead of at the first cycle.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from forgeo.backlog import open_backlog
from forgeo.daemon import is_lock_held, read_lock_pid
from forgeo.git import GitError, GitManager
from forgeo.models import ForgeoConfig, Task
from forgeo.paths import lock_path


@dataclass
class ValidationReport:
    """All findings of a dry-run validation.

    ``problems`` are things that make the forgeo unable to run; ``warnings``
    are conditions that do not block a start but are worth knowing; ``notes``
    are purely informational.
    """

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lock_held: bool = False
    lock_pid: int | None = None

    @property
    def healthy(self) -> bool:
        """True when no problem was found."""
        return not self.problems


def _command_text(command: str | list[str]) -> str:
    """A single-line rendering of an agent command for the report."""
    if isinstance(command, list):
        return " ".join(command)
    return command


def validate_config(config: ForgeoConfig) -> ValidationReport:
    """Run every read-only check against a loaded config.

    Every check is independent, so a broken repo or backlog does not hide a
    bad remote (or vice versa): all problems are reported at once. Never
    writes anything and never invokes the agent command.
    """
    report = ValidationReport()
    _check_agent_command(config, report)
    git = _check_repo(config, report)
    if git is not None:
        _check_branch(config, git, report)
    _check_remote(config, git, report)
    _check_backlog(config, report)
    _check_task_context(config, report)
    _check_lock(lock_path(config), report)
    return report


def _check_agent_command(config: ForgeoConfig, report: ValidationReport) -> None:
    command = config.agent_command
    if isinstance(command, str) and not command.strip():
        report.problems.append("agent_command must not be blank")
    elif isinstance(command, list) and not command:
        report.problems.append("agent_command must not be an empty list")


def _check_repo(config: ForgeoConfig, report: ValidationReport) -> GitManager | None:
    """Verify the repository exists and is a git working tree.

    Returns a :class:`GitManager` for the repo when the checks pass, so the
    branch/remote checks can reuse it; ``None`` otherwise (the report already
    carries the problem).
    """
    if not shutil.which("git"):
        report.problems.append("the 'git' executable was not found on PATH")
        return None
    repo = config.repo
    if not repo.exists():
        report.problems.append(f"repository does not exist: {repo}")
        return None
    if not repo.is_dir():
        report.problems.append(f"repository path is not a directory: {repo}")
        return None
    git = GitManager(repo, timeout_seconds=config.git_timeout_seconds)
    if not git.is_git_repo():
        report.problems.append(f"repository is not a git repository: {repo}")
        return None
    return git


def _check_branch(config: ForgeoConfig, git: GitManager, report: ValidationReport) -> None:
    try:
        git._run("rev-parse", "--verify", f"refs/heads/{config.branch}")
    except GitError:
        # The daemon creates a missing branch from HEAD on its first cycle.
        # With no commits at all the branch can still be created on the
        # unborn HEAD (git switch -c works), and the first cycle's commit
        # anchors it — but only a clean tree can run: a repository with no
        # commits has every file untracked, so any file at all would make
        # every cycle refuse as dirty.
        try:
            git._run("rev-parse", "--verify", "HEAD")
        except GitError:
            if git._run("status", "--porcelain"):
                report.problems.append(
                    "repository has no commits yet and the working tree is "
                    "not clean; make an initial commit first "
                    "(`git add -A && git commit -m \"Initial commit\"`) — "
                    "forgeo never commits uncommitted work"
                )
            else:
                report.warnings.append(
                    "repository has no commits yet; the first cycle will "
                    "create the initial commit"
                )
        else:
            report.warnings.append(
                f"branch {config.branch!r} does not exist yet; it will be "
                "created on the first cycle"
            )


def _check_remote(
    config: ForgeoConfig, git: GitManager | None, report: ValidationReport
) -> None:
    if not config.remote:
        return
    if git is None:
        return
    try:
        url = git._run("remote", "get-url", config.remote)
    except GitError:
        report.problems.append(f"remote {config.remote!r} is not configured")
    else:
        report.notes.append(f"remote {config.remote!r} resolves to {url}")


def _check_backlog(config: ForgeoConfig, report: ValidationReport) -> None:
    """Verify the backlog is readable and its tasks are valid.

    A missing backlog file is fine (the daemon treats it as empty and creates
    it on first use); a corrupt one is a problem. Reads only — never restores
    or writes anything, unlike the backlog's own corrupt-file recovery.
    """
    if config.backlog_is_url:
        _check_backlog_url(config, report)
        return
    path = Path(config.backlog)
    if not path.exists():
        report.notes.append(
            f"backlog not found: {path} (treated as empty on the first cycle)"
        )
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.problems.append(f"backlog is not valid JSON: {exc}")
        return
    except OSError as exc:
        report.problems.append(f"backlog could not be read: {exc}")
        return
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        report.problems.append("backlog must be a JSON object with a 'tasks' array")
        return
    for index, entry in enumerate(data["tasks"]):
        try:
            Task.model_validate(entry)
        except ValidationError as exc:
            report.problems.append(f"backlog task #{index} is invalid: {exc}")
    report.notes.append(f"backlog parses ({len(data['tasks'])} tasks)")


def _check_backlog_url(config: ForgeoConfig, report: ValidationReport) -> None:
    """Fetch a URL backlog once to prove it answers before a cycle needs it.

    This is the one check that leaves the machine, and it is worth it: an
    unreachable endpoint or a rejected client-credentials grant is exactly
    the failure a dry run should surface, and it would otherwise only show up
    as a failed cycle. Still read-only — a single GET, nothing is written
    back.
    """
    try:
        tasks = asyncio.run(open_backlog(config).list_tasks())
    except Exception as exc:  # noqa: BLE001 - any backend failure is reportable
        report.problems.append(f"backlog endpoint could not be read: {exc}")
        return
    report.notes.append(f"backlog endpoint answers ({len(tasks)} tasks)")


def _check_task_context(config: ForgeoConfig, report: ValidationReport) -> None:
    """A configured context file that is missing or unreadable is a warning.

    The cycle still runs without the context (never a hard failure), but the
    user probably wants to know why the agent is not seeing it.
    """
    path = config.task_context
    if path is None:
        return
    if not path.exists():
        report.warnings.append(
            f"task_context not found: {path} (cycles run without it)"
        )
        return
    try:
        path.read_text(encoding="utf-8")
    except OSError as exc:
        report.warnings.append(f"task_context could not be read: {exc}")


def _check_lock(path: Path, report: ValidationReport) -> None:
    report.lock_held = is_lock_held(path)
    report.lock_pid = read_lock_pid(path) if report.lock_held else None
    if report.lock_held:
        detail = f" (pid {report.lock_pid})" if report.lock_pid is not None else ""
        report.warnings.append(
            f"run lock held{detail}; forgeo start/once will refuse to run "
            "until it is released"
        )


def render_report(config: ForgeoConfig, report: ValidationReport) -> str:
    """Render the validation summary as plain text."""
    lines = [
        f"name: {config.name}",
        f"repo: {config.repo}",
        f"branch: {config.branch}",
        f"agent command: {_command_text(config.agent_command)}",
        f"backlog: {config.backlog}",
        f"lock: {'held' if report.lock_held else 'not held'}",
    ]
    if report.lock_pid is not None:
        lines[-1] += f" (pid {report.lock_pid})"
    for heading, entries in (
        ("Notes", report.notes),
        ("Warnings", report.warnings),
        ("Problems", report.problems),
    ):
        if not entries:
            continue
        lines.append("")
        lines.append(f"{heading}:")
        lines.extend(f"- {entry}" for entry in entries)
    lines.append("")
    if report.problems:
        lines.append(
            f"Forgeo is not ready to run ({len(report.problems)} problem(s))."
        )
    else:
        lines.append("Forgeo is ready to run.")
    return "\n".join(lines)
