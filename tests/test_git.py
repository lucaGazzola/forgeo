"""Git manager tests against a real repository."""

from __future__ import annotations

import subprocess

import pytest

from forgeo.git import GitError, GitManager
from tests.conftest import git


async def test_ensure_branch_switches_and_creates(git_repo):
    manager = GitManager(git_repo)
    await manager.a_ensure_branch("main")
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    await manager.a_ensure_branch("other")
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "other"


async def test_commit_all_returns_sha_and_cleans_tree(git_repo):
    manager = GitManager(git_repo)
    (git_repo / "new.txt").write_text("hello\n", encoding="utf-8")
    sha = await manager.a_commit_all("forgeo: test commit")
    assert sha
    assert await manager.a_is_clean()
    assert git(git_repo, "log", "-1", "--format=%s") == "forgeo: test commit"


async def test_commit_all_with_no_changes_returns_none(git_repo):
    manager = GitManager(git_repo)
    assert await manager.a_commit_all("forgeo: nothing") is None


async def test_is_clean_detects_dirty_tree(git_repo):
    manager = GitManager(git_repo)
    assert await manager.a_is_clean()
    (git_repo / "dirty.txt").write_text("x\n", encoding="utf-8")
    assert not await manager.a_is_clean()


async def test_reset_hard_discards_changes(git_repo):
    manager = GitManager(git_repo)
    (git_repo / "app.py").write_text("broken\n", encoding="utf-8")
    (git_repo / "extra.txt").write_text("x\n", encoding="utf-8")
    await manager.a_reset_hard()
    assert await manager.a_is_clean()
    assert "def answer()" in (git_repo / "app.py").read_text(encoding="utf-8")
    assert not (git_repo / "extra.txt").exists()


async def test_not_a_repository_raises(tmp_path):
    manager = GitManager(tmp_path)
    with pytest.raises(GitError):
        await manager.a_ensure_branch("main")


def test_is_git_repo_true_for_a_repository(git_repo):
    assert GitManager(git_repo).is_git_repo()


def test_is_git_repo_false_for_a_plain_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not GitManager(plain).is_git_repo()


def test_is_git_repo_false_for_missing_path(tmp_path):
    assert not GitManager(tmp_path / "nope").is_git_repo()


def test_is_git_repo_false_without_git_executable(git_repo, monkeypatch):
    manager = GitManager(git_repo)
    monkeypatch.setattr("forgeo.git.shutil.which", lambda _: None)
    assert not manager.is_git_repo()


def test_missing_git_executable_raises(git_repo, monkeypatch):
    manager = GitManager(git_repo)
    monkeypatch.setattr("forgeo.git.shutil.which", lambda _: None)
    with pytest.raises(GitError, match="git"):
        manager.is_clean()


def test_git_timeout_raises(git_repo, monkeypatch):
    manager = GitManager(git_repo, timeout_seconds=0.01)

    def slow_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

    monkeypatch.setattr("forgeo.git.subprocess.run", slow_run)
    with pytest.raises(GitError, match="timed out"):
        manager._run("status", "--porcelain")
