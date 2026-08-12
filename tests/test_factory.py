"""Forgeo cycle tests: task run, refactor pass, blocker file, git behavior."""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Self

from forgeo.backlog import JSONBacklog
from forgeo.git import GitManager
from forgeo.models import (
    NO_CHANGES_DIRTY_REASON,
    NO_CHANGES_REASON,
    ExecutionResult,
    ExecutionStatus,
    TaskStatus,
)
from tests.conftest import git, make_forgeo, make_task


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


async def test_task_instruction_reaches_agent(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    task = make_task(acceptance_criteria=["tests pass"])
    await backlog.create_task(task)
    await forgeo.run_cycle()
    called_task, _ = agent.calls[0]
    assert called_task.id == "TASK-001"


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
