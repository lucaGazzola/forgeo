"""Model tests: task lifecycle statuses and forgeo config validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from forgeo.config import load_config, save_config
from forgeo.models import DEFAULT_REFACTOR_PROMPT, ForgeoConfig, SandboxMode, Task, TaskStatus


def test_task_defaults_to_open():
    task = Task(id="TASK-001", title="t", description="Do it.")
    assert task.status is TaskStatus.OPEN
    assert task.description == "Do it."
    assert task.acceptance_criteria == []
    assert task.agent_command is None
    assert task.agent_timeout_seconds is None


def test_task_requires_description():
    with pytest.raises(ValidationError):
        Task(id="TASK-001", title="t")
    with pytest.raises(ValidationError):
        Task(id="TASK-001", title="t", description="")
    with pytest.raises(ValidationError):
        Task(id="TASK-001", title="t", description="   ")


def test_task_accepts_agent_override_fields():
    task = Task(
        id="TASK-001",
        title="t",
        description="Do it.",
        agent_command="claude -p \"$FORGEO_TASK\" --model cheap",
        agent_timeout_seconds=120,
    )
    assert task.agent_command == "claude -p \"$FORGEO_TASK\" --model cheap"
    assert task.agent_timeout_seconds == 120


def test_task_agent_override_accepts_argv_list():
    task = Task(id="TASK-001", title="t", description="Do it.", agent_command=["sh", "-c", "exit 0"])
    assert task.agent_command == ["sh", "-c", "exit 0"]


def test_task_rejects_blank_agent_command():
    with pytest.raises(ValidationError):
        Task(id="TASK-001", title="t", description="Do it.", agent_command="")


def test_task_rejects_empty_agent_command_list():
    with pytest.raises(ValidationError):
        Task(id="TASK-001", title="t", description="Do it.", agent_command=[])


def test_task_rejects_non_positive_agent_timeout():
    with pytest.raises(ValidationError):
        Task(id="TASK-001", title="t", description="Do it.", agent_timeout_seconds=0)


def test_config_requires_agent_command():
    with pytest.raises(ValidationError):
        ForgeoConfig(name="x", agent_command="")


def test_config_defaults():
    config = ForgeoConfig(agent_command="aider --message hi")
    assert config.interval_minutes == 60
    assert config.branch == "main"
    assert config.blocked_exit_code == 2
    assert config.no_changes_exit_code == 3
    assert config.agent_timeout_seconds is None
    assert config.git_timeout_seconds == 120
    assert config.refactor_prompt == DEFAULT_REFACTOR_PROMPT
    assert config.telegram_bot_token is None
    assert config.telegram_chat_id is None
    assert config.notify_webhook_url is None
    assert config.notify_webhook_events == ["blocked"]


def test_config_rejects_unknown_webhook_events():
    with pytest.raises(ValidationError):
        ForgeoConfig(agent_command="x", notify_webhook_events=["blocked", "bogus"])


def test_config_webhook_events_deduped():
    config = ForgeoConfig(
        agent_command="x", notify_webhook_events=["blocked", "blocked", "failed"]
    )
    assert config.notify_webhook_events == ["blocked", "failed"]


def test_config_rejects_no_changes_exit_code_of_zero():
    with pytest.raises(ValidationError):
        ForgeoConfig(agent_command="x", no_changes_exit_code=0)


def test_config_rejects_no_changes_matching_blocked_exit_code():
    with pytest.raises(ValidationError):
        ForgeoConfig(agent_command="x", blocked_exit_code=2, no_changes_exit_code=2)


def test_config_accepts_distinct_no_changes_exit_code():
    config = ForgeoConfig(agent_command="x", no_changes_exit_code=9)
    assert config.no_changes_exit_code == 9


def test_config_sandbox_defaults_to_none():
    config = ForgeoConfig(agent_command="x")
    assert config.agent_sandbox is SandboxMode.NONE
    assert config.agent_sandbox_image is None
    assert config.agent_sandbox_network == "none"
    assert config.agent_sandbox_mounts == []


def test_config_docker_sandbox_requires_image():
    with pytest.raises(ValidationError):
        ForgeoConfig(agent_command="x", agent_sandbox="docker")


def test_config_docker_sandbox_accepts_image():
    config = ForgeoConfig(
        agent_command="x",
        agent_sandbox="docker",
        agent_sandbox_image="forgeo-agent",
    )
    assert config.agent_sandbox is SandboxMode.DOCKER
    assert config.agent_sandbox_image == "forgeo-agent"


def test_config_rejects_blank_network_and_mounts():
    with pytest.raises(ValidationError):
        ForgeoConfig(agent_command="x", agent_sandbox_network="")
    with pytest.raises(ValidationError):
        ForgeoConfig(agent_command="x", agent_sandbox_mounts=["", "  "])


def test_config_accepts_valid_network_and_mounts():
    config = ForgeoConfig(
        agent_command="x",
        agent_sandbox_network="bridge",
        agent_sandbox_mounts=["/run/secrets/agent"],
    )
    assert config.agent_sandbox_network == "bridge"
    assert config.agent_sandbox_mounts == ["/run/secrets/agent"]


def test_config_rejects_zero_interval():
    with pytest.raises(ValidationError):
        ForgeoConfig(agent_command="x", interval_minutes=0)


def test_load_config_resolves_relative_paths(tmp_path):
    (tmp_path / "forgeo.yaml").write_text(
        "name: demo\nrepo: ../repo\nbacklog: tasks.json\nagent_command: echo\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path / "forgeo.yaml")
    assert config.repo == tmp_path.resolve() / "../repo"
    assert config.backlog == tmp_path.resolve() / "tasks.json"
    assert config.blocker_file == tmp_path.resolve() / "BLOCKER.md"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_save_config_stores_paths_relative_to_file(tmp_path):
    config_path = tmp_path / "forgeo.yaml"
    config = ForgeoConfig(
        name="demo",
        repo=tmp_path / ".." / "repo",
        backlog=tmp_path / "tasks.json",
        blocker_file=tmp_path / "BLOCKER.md",
        agent_command="echo hi",
        log_file=str(tmp_path / "forgeo.log"),
    )
    saved = save_config(config_path, config)

    disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert disk["name"] == "demo"
    assert disk["repo"] == "../repo"
    assert disk["backlog"] == "tasks.json"
    assert disk["blocker_file"] == "BLOCKER.md"
    assert disk["log_file"] == "forgeo.log"

    assert saved.repo.resolve() == (tmp_path / ".." / "repo").resolve()
    assert saved.backlog == (tmp_path / "tasks.json").resolve()
    assert saved.blocker_file == (tmp_path / "BLOCKER.md").resolve()
    assert saved.log_file == str((tmp_path / "forgeo.log").resolve())
    assert load_config(config_path) == saved


def test_save_config_round_trips_with_load_config(tmp_path):
    config_path = tmp_path / "forgeo.yaml"
    config_path.write_text(
        "name: demo\nrepo: ../repo\nbacklog: tasks.json\nagent_command: echo\n",
        encoding="utf-8",
    )
    original = load_config(config_path)
    saved = save_config(config_path, original)
    assert saved == original
    reloaded = load_config(config_path)
    assert reloaded.repo == original.repo
    assert reloaded.backlog == original.backlog


def test_save_config_keeps_explicit_relative_paths(tmp_path):
    config_path = tmp_path / "forgeo.yaml"
    config = ForgeoConfig(
        name="demo",
        repo=Path("."),
        backlog=Path("backlog.json"),
        blocker_file=Path("BLOCKER.md"),
        agent_command="echo hi",
        log_file="forgeo.log",
    )
    save_config(config_path, config)
    disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert disk["repo"] == "."
    assert disk["backlog"] == "backlog.json"
    assert disk["log_file"] == "forgeo.log"
