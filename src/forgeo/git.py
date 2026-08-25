"""Git operations for Forgeo.

Everything happens on a single branch (``main`` by default): commit whatever
the agent changed, then push. No branches, no PRs, no merge strategies.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path


def _git_executable() -> str | None:
    """Return the ``git`` executable path, or ``None`` when absent.

    Wraps :func:`shutil.which` so tests can monkeypatch ``forgeo.git.shutil.which``
    and callers avoid repeating the literal ``\"git\"``.
    """
    return shutil.which("git")


class GitError(RuntimeError):
    """Raised when a git command cannot be executed or fails."""


class GitManager:
    """Run git commands against a single repository (via the git CLI)."""

    def __init__(
        self, repo_path: str | Path, *, timeout_seconds: float = 120
    ) -> None:
        self.repo_path = Path(repo_path)
        self.timeout_seconds = timeout_seconds

    def _run(self, *args: str, check: bool = True) -> str:
        """Execute ``git -C <repo> <args>`` and return combined output."""
        if not _git_executable():
            raise GitError("the 'git' executable was not found on PATH")
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo_path), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        if check and proc.returncode != 0:
            raise GitError(
                f"git {args[0]} failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout.strip()

    def is_git_repo(self) -> bool:
        """Return whether ``repo_path`` is inside a git working tree.

        ``False`` also when the ``git`` executable is missing or the path is
        not a directory, so callers never have to probe the environment
        themselves.
        """
        if not self.repo_path.is_dir():
            return False
        try:
            return self._run("rev-parse", "--is-inside-work-tree") == "true"
        except GitError:
            return False

    def ensure_branch(self, branch: str) -> None:
        """Switch to ``branch``, creating it from HEAD when it does not exist."""
        try:
            self._run("rev-parse", "--verify", f"refs/heads/{branch}")
        except GitError:
            self._run("switch", "-c", branch)
            return
        self._run("switch", branch)

    def is_clean(self) -> bool:
        """Return whether the working tree has no changes."""
        return not bool(self._run("status", "--porcelain"))

    def commit_all(self, message: str) -> str | None:
        """Stage all changes and commit; returns the short sha, or ``None`` if nothing to commit."""
        self._run("add", "-A")
        if not bool(self._run("status", "--porcelain")):
            return None
        self._run("commit", "-m", message)
        return self._run("rev-parse", "--short", "HEAD")

    def push(self, remote: str, branch: str) -> None:
        """Push ``branch`` to ``remote``."""
        self._run("push", remote, branch)

    def reset_hard(self) -> None:
        """Discard all uncommitted changes in the working tree.

        Reverts tracked files and removes untracked ones (Forgeo only
        ever discards work after having verified the tree was clean, so
        everything removed here was produced by the agent).
        """
        self._run("reset", "--hard", "HEAD")
        self._run("clean", "-fd")

    # ------------------------------------------------------------------ #
    # Async wrappers (run git in a worker thread)                         #
    # ------------------------------------------------------------------ #

    async def a_ensure_branch(self, branch: str) -> None:
        await asyncio.to_thread(self.ensure_branch, branch)

    async def a_is_clean(self) -> bool:
        return await asyncio.to_thread(self.is_clean)

    async def a_commit_all(self, message: str) -> str | None:
        return await asyncio.to_thread(self.commit_all, message)

    async def a_push(self, remote: str, branch: str) -> None:
        await asyncio.to_thread(self.push, remote, branch)

    async def a_reset_hard(self) -> None:
        await asyncio.to_thread(self.reset_hard)
