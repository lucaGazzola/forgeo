"""Forgeo cycle tests: task run, refactor pass, blocker file, git behavior."""

from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import UTC, datetime
from typing import Self

import pytest

from forgeo.backlog import JSONBacklog
from forgeo.forgeo import TaskNotRunnableError
from forgeo.git import GitManager
from forgeo.models import (
    NO_CHANGES_DIRTY_REASON,
    NO_CHANGES_REASON,
    ExecutionResult,
    ExecutionStatus,
    TaskStatus,
)
from tests.conftest import git, make_forgeo, make_result, make_task


class FakeResponse:
    """A minimal ``urllib`` response: 200 OK and context-manager support."""

    status = 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeErrorResponse(FakeResponse):
    """An ``urllib`` response that is not an HTTP 200."""

    status = 503


async def test_task_success_is_committed_on_main(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())

    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text("def answer():\n    return 7\n", encoding="utf-8")

    outcome = await forgeo.run_cycle()

    assert outcome == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.COMPLETED
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(git_repo, "log", "-1", "--format=%s") == "Do the thing"
    assert "forgeo" not in git(git_repo, "branch")


async def test_task_success_without_changes_fails_task(git_repo, tmp_path):
    """Exit 0 with an empty tree is not a valid completion: it is FAILED, so
    a silent no-change SUCCESS can never quietly close a task (regression:
    SELF-033 was closed without any work)."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    outcome = await forgeo.run_cycle()

    assert outcome == "task"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.failure_reason == [NO_CHANGES_REASON]
    assert git(git_repo, "rev-list", "--count", "HEAD") == "1"


async def test_no_changes_failure_logs_warning(git_repo, tmp_path, caplog):
    """The no-change SUCCESS case is surfaced as a warning, never silent."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    with caplog.at_level(logging.WARNING):
        await forgeo.run_cycle()

    assert any(NO_CHANGES_REASON in r.message for r in caplog.records)
    assert any("produced no changes" in r.message for r in caplog.records)


async def test_task_explicit_no_changes_completes_task(git_repo, tmp_path):
    """Exit no_changes_exit_code is the explicit no-op contract: the task is
    COMPLETED without a commit, and the tree stays untouched."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(
        status=ExecutionStatus.SUCCESS, exit_code=3, no_changes=True
    )

    outcome = await forgeo.run_cycle()

    assert outcome == "task"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.COMPLETED
    assert git(git_repo, "rev-list", "--count", "HEAD") == "1"
    assert await GitManager(git_repo).a_is_clean()


async def test_task_no_changes_but_dirty_tree_fails(git_repo, tmp_path):
    """An agent that reports no changes while leaving uncommitted work behind
    contradicts itself: the work is discarded and the task is FAILED."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(
        status=ExecutionStatus.SUCCESS, exit_code=3, no_changes=True
    )
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )

    await forgeo.run_cycle()

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.failure_reason == [NO_CHANGES_DIRTY_REASON]
    assert await GitManager(git_repo).a_is_clean()
    assert "def answer()" in (git_repo / "app.py").read_text(encoding="utf-8")


async def test_task_success_pushes_to_remote(git_repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(git_repo, "clone", "--bare", str(git_repo), str(remote))
    git(git_repo, "remote", "add", "origin", str(remote))
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, remote="origin")
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "feature.txt").write_text("done\n", encoding="utf-8")

    await forgeo.run_cycle()

    assert git(remote, "log", "-1", "--format=%s") == "Do the thing"


async def test_task_error_is_failed_and_work_discarded(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")

    def wreck():
        (git_repo / "app.py").write_text("garbage\n", encoding="utf-8")

    agent.effect = wreck

    await forgeo.run_cycle()

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.failure_reason == ["boom"]
    assert await GitManager(git_repo).a_is_clean()
    assert "def answer()" in (git_repo / "app.py").read_text(encoding="utf-8")


async def test_task_error_falls_back_to_no_detail(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR)

    await forgeo.run_cycle()

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.failure_reason == ["no error detail provided"]


async def test_task_blocked_persists_reason_and_renders_blocker_file(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task(description="Decide the retry policy."))
    agent.result = ExecutionResult(
        status=ExecutionStatus.BLOCKED,
        questions=["Which retry policy should I use?"],
    )
    agent.effect = lambda: (git_repo / "wip.txt").write_text("partial\n", encoding="utf-8")

    await forgeo.run_cycle()

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.BLOCKED
    assert task.blocker_reason == ["Which retry policy should I use?"]
    assert task.blocked_count == 1
    assert not forgeo.config.blocker_file.exists()  # derived view renders next cycle
    assert git(git_repo, "log", "-1", "--format=%s") == "Do the thing [partial]"

    assert await forgeo.run_cycle() == "blocked"
    blocker = forgeo.config.blocker_file.read_text(encoding="utf-8")
    assert "TASK-001" in blocker
    assert "Which retry policy should I use?" in blocker
    assert "set the status of `TASK-001` back to `OPEN`" in blocker
    assert "see the backlog" not in blocker


async def test_task_blocked_falls_back_to_output_logs(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(
        status=ExecutionStatus.BLOCKED,
        output_logs=["context line", "I need a decision"],
    )

    await forgeo.run_cycle()

    task = await backlog.get_task("TASK-001")
    assert task.blocker_reason == ["context line", "I need a decision"]
    await forgeo.run_cycle()
    blocker = forgeo.config.blocker_file.read_text(encoding="utf-8")
    assert "I need a decision" in blocker


async def test_task_blocked_increments_blocked_count(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["again?"])

    await forgeo.run_cycle()
    await backlog.reopen_task("TASK-001")
    await forgeo.run_cycle()

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.BLOCKED
    assert task.blocked_count == 2
    assert task.blocker_reason == ["again?"]


async def test_blocked_task_pauses_until_reopened(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])
    await forgeo.run_cycle()
    assert await forgeo.run_cycle() == "blocked"

    reopened = await backlog.reopen_task("TASK-001")
    assert reopened.status is TaskStatus.OPEN
    assert reopened.blocker_reason == []
    assert reopened.blocked_count == 1

    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "note.txt").write_text("done\n", encoding="utf-8")
    await forgeo.run_cycle()

    assert (await backlog.get_task("TASK-001")).status is TaskStatus.COMPLETED
    assert not forgeo.config.blocker_file.exists()


async def test_derived_blocker_disappears_when_last_blocked_resolved(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["Q?"])
    await forgeo.run_cycle()
    await forgeo.run_cycle()
    assert forgeo.config.blocker_file.exists()

    await backlog.reopen_task("TASK-001")
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "note.txt").write_text("done\n", encoding="utf-8")
    await forgeo.run_cycle()

    assert not forgeo.config.blocker_file.exists()
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.COMPLETED


async def test_stale_blocker_file_is_not_auto_removed(git_repo, tmp_path):
    """A non-derived blocker file (stale or refactor-block) is untouched."""
    forgeo, _agent, _backlog = make_forgeo(git_repo, tmp_path)
    forgeo.config.blocker_file.write_text("stale", encoding="utf-8")
    assert await forgeo.run_cycle() == "paused"
    assert forgeo.config.blocker_file.read_text(encoding="utf-8") == "stale"


async def test_blocked_task_sends_telegram_message(git_repo, tmp_path, monkeypatch):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, telegram_bot_token="TOKEN", telegram_chat_id="CHAT"
    )
    await backlog.create_task(make_task(description="Decide the retry policy."))
    agent.result = ExecutionResult(
        status=ExecutionStatus.BLOCKED,
        questions=["Which retry policy should I use?"],
    )
    agent.effect = lambda: (git_repo / "wip.txt").write_text("partial\n", encoding="utf-8")

    captured = {"calls": 0}

    def fake_urlopen(request, **kwargs):
        captured["calls"] += 1
        captured["url"] = request.full_url
        captured["data"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    assert captured["calls"] == 1
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED
    assert captured["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert captured["data"]["chat_id"] == ["CHAT"]
    text = captured["data"]["text"][0]
    assert "test-forgeo" in text
    assert "TASK-001: Do the thing" in text
    assert "Which retry policy should I use?" in text


async def test_no_telegram_message_when_not_configured(git_repo, tmp_path, monkeypatch):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])

    calls = []

    def fake_urlopen(request, **kwargs):
        calls.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    assert calls == []
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED


async def test_telegram_failure_logs_warning_and_keeps_outcome(
    git_repo, tmp_path, monkeypatch, caplog
):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, telegram_bot_token="TOKEN", telegram_chat_id="CHAT"
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])

    def boom(request, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with caplog.at_level(logging.WARNING):
        outcome = await forgeo.run_cycle()

    assert outcome == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED
    assert not forgeo.config.blocker_file.exists()  # derived view renders next cycle
    assert any("Telegram notification failed" in r.message for r in caplog.records)


async def test_telegram_non_200_logs_warning_and_keeps_outcome(
    git_repo, tmp_path, monkeypatch, caplog
):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, telegram_bot_token="TOKEN", telegram_chat_id="CHAT"
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])

    def fake_urlopen(request, **kwargs):
        return FakeErrorResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        outcome = await forgeo.run_cycle()

    assert outcome == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED
    assert not forgeo.config.blocker_file.exists()  # derived view renders next cycle
    assert any("Telegram notification failed" in r.message for r in caplog.records)


async def test_refactor_blocked_sends_telegram_message(git_repo, tmp_path, monkeypatch):
    forgeo, agent, _backlog = make_forgeo(
        git_repo, tmp_path, telegram_bot_token="TOKEN", telegram_chat_id="CHAT"
    )
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["License question?"])

    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["data"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    text = captured["data"]["text"][0]
    assert "REFACTOR: Refactoring pass" in text
    assert "License question?" in text


async def test_blocked_task_sends_webhook_message(git_repo, tmp_path, monkeypatch):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, notify_webhook_url="https://hooks.example.com/forgeo"
    )
    await backlog.create_task(make_task(description="Decide the retry policy."))
    agent.result = ExecutionResult(
        status=ExecutionStatus.BLOCKED,
        questions=["Which retry policy should I use?"],
    )
    agent.effect = lambda: (git_repo / "wip.txt").write_text("partial\n", encoding="utf-8")

    captured = {"calls": 0}

    def fake_urlopen(request, **kwargs):
        captured["calls"] += 1
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["content_type"] = next(
            value for key, value in request.headers.items() if key.lower() == "content-type"
        )
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    assert captured["calls"] == 1
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED
    assert captured["url"] == "https://hooks.example.com/forgeo"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["payload"] == {
        "forgeo": "test-forgeo",
        "outcome": "blocked",
        "task_id": "TASK-001",
        "task_title": "Do the thing",
        "reason": "Which retry policy should I use?",
    }


async def test_no_webhook_message_when_not_configured(git_repo, tmp_path, monkeypatch):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])

    calls = []

    def fake_urlopen(request, **kwargs):
        calls.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    assert calls == []
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED


async def test_webhook_completed_sent_when_configured(git_repo, tmp_path, monkeypatch):
    forgeo, agent, backlog = make_forgeo(
        git_repo,
        tmp_path,
        notify_webhook_url="https://hooks.example.com/forgeo",
        notify_webhook_events=["blocked", "completed", "failed"],
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text("changed\n", encoding="utf-8")

    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    assert (await backlog.get_task("TASK-001")).status is TaskStatus.COMPLETED
    assert captured == [
        {
            "forgeo": "test-forgeo",
            "outcome": "completed",
            "task_id": "TASK-001",
            "task_title": "Do the thing",
            "reason": "",
        }
    ]


async def test_webhook_completed_not_sent_by_default(git_repo, tmp_path, monkeypatch):
    """With only the URL set (default events), completed runs do not POST."""
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, notify_webhook_url="https://hooks.example.com/forgeo"
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text("changed\n", encoding="utf-8")

    calls = []

    def fake_urlopen(request, **kwargs):
        calls.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    assert (await backlog.get_task("TASK-001")).status is TaskStatus.COMPLETED
    assert calls == []


async def test_webhook_failed_sent_when_configured(git_repo, tmp_path, monkeypatch):
    forgeo, agent, backlog = make_forgeo(
        git_repo,
        tmp_path,
        notify_webhook_url="https://hooks.example.com/forgeo",
        notify_webhook_events=["blocked", "completed", "failed"],
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="agent exploded")

    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    await forgeo.run_cycle()

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert captured == [
        {
            "forgeo": "test-forgeo",
            "outcome": "failed",
            "task_id": "TASK-001",
            "task_title": "Do the thing",
            "reason": "agent exploded",
        }
    ]


async def test_webhook_failure_logs_warning_and_keeps_outcome(
    git_repo, tmp_path, monkeypatch, caplog
):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, notify_webhook_url="https://hooks.example.com/forgeo"
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])

    def boom(request, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with caplog.at_level(logging.WARNING):
        outcome = await forgeo.run_cycle()

    assert outcome == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED
    assert any("Webhook notification failed" in r.message for r in caplog.records)


async def test_webhook_timeout_logs_warning_and_keeps_outcome(
    git_repo, tmp_path, monkeypatch, caplog
):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, notify_webhook_url="https://hooks.example.com/forgeo"
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])

    def timeout(request, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", timeout)

    with caplog.at_level(logging.WARNING):
        outcome = await forgeo.run_cycle()

    assert outcome == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED
    assert any("Webhook notification failed" in r.message for r in caplog.records)


async def test_webhook_non_200_logs_warning_and_keeps_outcome(
    git_repo, tmp_path, monkeypatch, caplog
):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, notify_webhook_url="https://hooks.example.com/forgeo"
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"])

    def fake_urlopen(request, **kwargs):
        return FakeErrorResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        outcome = await forgeo.run_cycle()

    assert outcome == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.BLOCKED
    assert any("Webhook notification failed" in r.message for r in caplog.records)


async def test_refactor_pass_when_backlog_empty(git_repo, tmp_path):
    forgeo, agent, _backlog = make_forgeo(git_repo, tmp_path)
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    def refactor():
        (git_repo / "app.py").write_text("def answer():\n    return 42  # neat\n", encoding="utf-8")

    agent.effect = refactor

    outcome = await forgeo.run_cycle()

    assert outcome == "refactor"
    task, context = agent.calls[0]
    assert task.id == "REFACTOR"
    assert context.repo_path == git_repo
    assert git(git_repo, "log", "-1", "--format=%s") == "refactoring pass"


async def test_refactor_with_nothing_to_do_commits_nothing(git_repo, tmp_path):
    """A refactor that finds nothing to improve is a normal, successful run —
    unlike a task, an empty diff on a refactor pass is not a failure."""
    forgeo, agent, _backlog = make_forgeo(git_repo, tmp_path)
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    outcome = await forgeo.run_cycle()

    assert outcome == "refactor"
    assert git(git_repo, "rev-list", "--count", "HEAD") == "1"
    assert agent.calls[0][0].id == "REFACTOR"


async def test_refactor_blocked_writes_blocker(git_repo, tmp_path):
    forgeo, agent, _backlog = make_forgeo(git_repo, tmp_path)
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["License question?"])

    await forgeo.run_cycle()

    blocker = forgeo.config.blocker_file.read_text(encoding="utf-8")
    assert "License question?" in blocker
    assert "delete this file" in blocker


async def test_paused_while_blocker_file_exists(git_repo, tmp_path):
    forgeo, agent, _backlog = make_forgeo(git_repo, tmp_path)
    forgeo.config.blocker_file.write_text("stale", encoding="utf-8")
    assert await forgeo.run_cycle() == "paused"
    assert agent.calls == []


async def test_dirty_tree_skips_task(git_repo, tmp_path):
    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    (git_repo / "manual.txt").write_text("wip\n", encoding="utf-8")
    outcome = await forgeo.run_cycle()
    assert outcome == "dirty"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.OPEN


# --------------------------------------------------------------------------- #
# forgeo run: one specific task                                                #
# --------------------------------------------------------------------------- #


async def test_run_task_id_executes_specific_task_not_oldest(git_repo, tmp_path):
    """`forgeo run --task` picks the named task even when another OPEN task is
    older, and leaves the older one untouched."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    older = make_task(
        id="OLDEST-001",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    specific = make_task(
        id="SELF-012",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    await backlog.create_task(older)
    await backlog.create_task(specific)
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )

    outcome = await forgeo.run_task_id("SELF-012")

    assert outcome == "task"
    assert (await backlog.get_task("OLDEST-001")).status is TaskStatus.OPEN
    assert (await backlog.get_task("SELF-012")).status is TaskStatus.COMPLETED
    assert agent.calls[0][0].id == "SELF-012"
    assert git(git_repo, "log", "-1", "--format=%s") == "Do the thing"


async def test_run_task_id_refuses_unknown_task(git_repo, tmp_path):
    forgeo, _agent, _backlog = make_forgeo(git_repo, tmp_path)
    with pytest.raises(TaskNotRunnableError, match="does not exist"):
        await forgeo.run_task_id("NOPE-001")


async def test_run_task_id_refuses_non_open_task(git_repo, tmp_path):
    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task(id="SELF-012", status=TaskStatus.COMPLETED))
    with pytest.raises(TaskNotRunnableError, match="COMPLETED"):
        await forgeo.run_task_id("SELF-012")


async def test_run_task_id_refusal_writes_no_run_record(git_repo, tmp_path):
    forgeo, _agent, _backlog = make_forgeo(git_repo, tmp_path)
    with pytest.raises(TaskNotRunnableError):
        await forgeo.run_task_id("NOPE-001")
    assert forgeo.recorder.read() == []


async def test_run_task_id_dirty_tree_returns_dirty(git_repo, tmp_path):
    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    (git_repo / "manual.txt").write_text("wip\n", encoding="utf-8")

    assert await forgeo.run_task_id("TASK-001") == "dirty"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.OPEN


async def test_run_task_id_records_a_run(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )

    await forgeo.run_task_id("TASK-001")

    records = forgeo.recorder.read()
    assert len(records) == 1
    assert records[0].task_id == "TASK-001"
    assert records[0].outcome.value == "SUCCESS"


async def test_task_instruction_reaches_agent(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    task = make_task(acceptance_criteria=["tests pass"])
    await backlog.create_task(task)
    await forgeo.run_cycle()
    called_task, _ = agent.calls[0]
    assert called_task.id == "TASK-001"


async def test_task_context_prepended_to_instruction(git_repo, tmp_path):
    context_file = tmp_path / "CONTEXT.md"
    context_file.write_text(
        "# Project overview\n\nThe forgeo does things.\n", encoding="utf-8"
    )
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, task_context=context_file)
    task = make_task(acceptance_criteria=["tests pass"])
    await backlog.create_task(task)
    await forgeo.run_cycle()

    (instruction,) = agent.instructions
    assert instruction.startswith(f"# Project context (from {context_file.resolve()}")
    assert "# Project overview\n\nThe forgeo does things." in instruction
    assert instruction.endswith(task.instruction)


async def test_task_context_prepended_to_refactor_instruction(git_repo, tmp_path):
    context_file = tmp_path / "CONTEXT.md"
    context_file.write_text("Refactoring context here.\n", encoding="utf-8")
    forgeo, agent, _backlog = make_forgeo(git_repo, tmp_path, task_context=context_file)
    await forgeo.run_cycle()

    (instruction,) = agent.instructions
    assert instruction.startswith(f"# Project context (from {context_file.resolve()}")
    assert "Refactoring context here." in instruction
    assert instruction.endswith(forgeo.config.refactor_prompt)


async def test_task_context_missing_file_runs_without_it(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, task_context=tmp_path / "nope.md"
    )
    task = make_task()
    await backlog.create_task(task)
    await forgeo.run_cycle()

    (instruction,) = agent.instructions
    assert instruction == task.instruction


async def test_task_context_refreshed_each_run(git_repo, tmp_path):
    context_file = tmp_path / "CONTEXT.md"
    context_file.write_text("Version one.\n", encoding="utf-8")
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, task_context=context_file)
    await backlog.create_task(make_task())
    await forgeo.run_cycle()
    context_file.write_text("Version two.\n", encoding="utf-8")
    await backlog.create_task(make_task(id="TASK-002", description="Build it again."))
    await forgeo.run_cycle()

    assert "Version one." in agent.instructions[0]
    assert "Version two." in agent.instructions[1]


async def test_task_agent_command_override_reaches_agent(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(
        make_task(agent_command="claude -p \"$FORGEO_TASK\" --model cheap")
    )
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    await forgeo.run_cycle()

    assert agent.overrides == [("claude -p \"$FORGEO_TASK\" --model cheap", None)]


async def test_task_agent_timeout_override_reaches_agent(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task(agent_command="echo hi", agent_timeout_seconds=45))
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    await forgeo.run_cycle()

    assert agent.overrides == [("echo hi", 45)]


async def test_task_without_override_falls_back_to_global(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, agent_command="echo global")
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    await forgeo.run_cycle()

    assert agent.overrides == [(None, None)]


async def test_override_is_persisted_and_survives_reload(git_repo, tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(
        make_task(agent_command="claude -p \"$FORGEO_TASK\" --model cheap")
    )
    task = await backlog.get_task("TASK-001")
    assert task.agent_command == "claude -p \"$FORGEO_TASK\" --model cheap"


async def test_task_run_snapshots_backlog_before_agent(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )

    await forgeo.run_cycle()

    bak = tmp_path / "backlog.json.bak"
    assert bak.is_file()
    store = json.loads(bak.read_text(encoding="utf-8"))
    assert [task["id"] for task in store["tasks"]] == ["TASK-001"]


async def test_refactor_run_snapshots_backlog_before_agent(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task(status=TaskStatus.COMPLETED))
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)

    await forgeo.run_cycle()

    bak = tmp_path / "backlog.json.bak"
    assert bak.is_file()
    store = json.loads(bak.read_text(encoding="utf-8"))
    assert [task["id"] for task in store["tasks"]] == ["TASK-001"]


# --------------------------------------------------------------------------- #
# Failed-task retry policy                                                     #
# --------------------------------------------------------------------------- #


async def test_failed_retry_disabled_by_default(git_repo, tmp_path):
    """With failed_retry_max left at 0 (the default), a FAILED task stays
    FAILED forever and the engine moves on to a refactor pass: unchanged."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")

    assert await forgeo.run_cycle() == "task"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.retry_count == 0

    assert await forgeo.run_cycle() == "refactor"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.retry_count == 0
    assert task.failure_reason == ["boom"]


async def test_failed_task_is_retried_and_can_succeed(git_repo, tmp_path):
    """A transient failure is retried after the wait; a retried task can
    succeed and its retry count is recorded on the task."""
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, failed_retry_max=1, failed_retry_wait_cycles=1
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="network hiccup")
    agent.effect = lambda: (git_repo / "app.py").write_text("garbage\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.failure_reason == ["network hiccup"]
    assert task.retry_count == 0

    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )
    assert await forgeo.run_cycle() == "task"

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.COMPLETED
    assert task.retry_count == 1
    assert task.failed_wait_cycles == 0
    assert task.failure_reason == []


async def test_failed_task_waits_configured_cycles_before_retry(git_repo, tmp_path):
    """failed_retry_wait_cycles is a backoff: the retry is scheduled only
    after that many cycles of waiting."""
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, failed_retry_max=1, failed_retry_wait_cycles=2
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")
    agent.effect = lambda: (git_repo / "app.py").write_text("garbage\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"
    assert (await backlog.get_task("TASK-001")).failed_wait_cycles == 0

    assert await forgeo.run_cycle() == "refactor"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.failed_wait_cycles == 1

    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )
    assert await forgeo.run_cycle() == "task"

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.COMPLETED
    assert task.retry_count == 1


async def test_failed_task_exhausts_retries_stays_failed(git_repo, tmp_path):
    """Once the retry budget is spent the task stays FAILED with its original
    failure reason preserved, and the engine moves on to other work."""
    forgeo, agent, backlog = make_forgeo(
        git_repo, tmp_path, failed_retry_max=1, failed_retry_wait_cycles=1
    )
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")
    agent.effect = lambda: (git_repo / "app.py").write_text("garbage\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"
    assert await forgeo.run_cycle() == "task"

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.retry_count == 1
    assert task.failure_reason == ["boom"]

    assert await forgeo.run_cycle() == "refactor"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.retry_count == 1
    assert task.failure_reason == ["boom"]


async def test_blocked_task_is_never_retried(git_repo, tmp_path):
    """A BLOCKED task is untouched by the retry policy: it still needs a
    human, and Forgeo keeps pausing until it is reopened."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, failed_retry_max=5)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["which policy?"])

    assert await forgeo.run_cycle() == "task"
    assert await forgeo.run_cycle() == "blocked"

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.BLOCKED
    assert task.retry_count == 0
    assert task.failed_wait_cycles == 0


async def test_retried_task_that_blocks_stays_blocked(git_repo, tmp_path):
    """A retried task that then blocks still needs a human: it is never
    auto-retried while BLOCKED, even with retries left."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, failed_retry_max=3)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")
    await forgeo.run_cycle()

    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["decide?"])
    assert await forgeo.run_cycle() == "task"

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.BLOCKED
    assert task.retry_count == 1

    assert await forgeo.run_cycle() == "blocked"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.BLOCKED
    assert task.retry_count == 1


async def test_per_task_retries_left_override_enables_retry(git_repo, tmp_path):
    """A per-task retries_left override enables retries even when the config
    has failed_retry_max: 0."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task(retries_left=1))
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")
    agent.effect = lambda: (git_repo / "app.py").write_text("garbage\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.FAILED

    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS)
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )
    assert await forgeo.run_cycle() == "task"

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.COMPLETED
    assert task.retry_count == 1


async def test_per_task_retries_left_zero_disables_retry(git_repo, tmp_path):
    """A per-task retries_left: 0 opts a task out of retries even when the
    config would retry it."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, failed_retry_max=5)
    await backlog.create_task(make_task(retries_left=0))
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")

    assert await forgeo.run_cycle() == "task"
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.FAILED

    assert await forgeo.run_cycle() == "refactor"
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.retry_count == 0
    assert task.failure_reason == ["boom"]


async def test_manual_reopen_of_failed_task_resets_retry_budget(git_repo, tmp_path):
    """A human reopening a FAILED task (setting it back to OPEN) resets the
    retry budget, so the manual retry gets a fresh failed_retry_max."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, failed_retry_max=1)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom")
    agent.effect = lambda: (git_repo / "app.py").write_text("garbage\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"
    await backlog.retry_task("TASK-001")  # simulate a scheduled retry
    assert await forgeo.run_cycle() == "task"  # retried, fails again

    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.FAILED
    assert task.retry_count == 1

    await backlog.update_status("TASK-001", TaskStatus.OPEN, make_result())  # manual reopen
    task = await backlog.get_task("TASK-001")
    assert task.status is TaskStatus.OPEN
    assert task.retry_count == 0
    assert task.failed_wait_cycles == 0
