"""Guided setup tests: the interactive wizard and the CLI fallbacks."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import yaml
from rich.console import Console

from forgeo.cli import _offer_setup, cmd_default, cmd_init, cmd_start
from forgeo.models import DEFAULT_REFACTOR_PROMPT
from forgeo.setup import (
    DEFAULT_AGENT_COMMAND,
    DEFAULT_AGENT_PROMPT,
    add_gitignore,
    build_agent_command,
    run_setup,
)


class AnswerQueue:
    """Serves scripted answers to setup prompts."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)

    def __call__(self, prompt: str) -> str:
        if not self.answers:
            raise AssertionError("setup asked for more input than expected")
        return self.answers.pop(0)


def _run_setup(base: Path, *answers: str) -> dict | None:
    return run_setup(
        base,
        base / "forgeo.yaml",
        input_fn=AnswerQueue(*answers),
        console=Console(file=io.StringIO()),
    )


def test_build_agent_command_appends_prompt():
    assert build_agent_command("opencode run --auto") == (
        f'opencode run --auto "{DEFAULT_AGENT_PROMPT}"'
    )
    assert build_agent_command("") == f' "{DEFAULT_AGENT_PROMPT}"'


def test_build_agent_command_keeps_existing_task_reference():
    command = 'claude -p "$FORGEO_TASK"'
    assert build_agent_command(command) == command


# --------------------------------------------------------------------- #
# The wizard                                                             #
# --------------------------------------------------------------------- #


def test_run_setup_defaults(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    payload = _run_setup(project, "", "", "y", "y")

    assert payload is not None
    assert payload["backlog"] == ".forgeo/backlog.json"
    assert payload["blocker_file"] == ".forgeo/BLOCKER.md"
    assert payload["agent_command"] == build_agent_command(DEFAULT_AGENT_COMMAND)
    assert payload["refactor_prompt"] == DEFAULT_REFACTOR_PROMPT
    assert payload["repo"] == "."
    assert payload["log_file"] == ".forgeo/forgeo.log"

    data = yaml.safe_load((project / "forgeo.yaml").read_text(encoding="utf-8"))
    assert data["backlog"] == ".forgeo/backlog.json"
    assert data["agent_command"] == build_agent_command(DEFAULT_AGENT_COMMAND)
    assert data["refactor_prompt"] == DEFAULT_REFACTOR_PROMPT

    assert (project / ".gitignore").read_text(encoding="utf-8") == ".forgeo/\n"
    assert (project / ".forgeo").is_dir()


def test_run_setup_dumps_prompts_as_literal_block_scalars(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _run_setup(project, "", "", "n", "Line one.", "Line two.", "", "y")

    text = (project / "forgeo.yaml").read_text(encoding="utf-8")
    assert "agent_command: |-" in text
    assert "refactor_prompt: |-" in text
    data = yaml.safe_load(text)
    assert data["refactor_prompt"] == "Line one.\nLine two."
    assert data["agent_command"] == build_agent_command(DEFAULT_AGENT_COMMAND)


def test_run_setup_custom_values(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    payload = _run_setup(
        project,
        "forgeo/",
        "opencode run --auto",
        "n",
        "Fix dead code and duplication.",
        "",
        "n",
    )

    assert payload is not None
    assert payload["backlog"] == "forgeo/backlog.json"
    assert payload["blocker_file"] == "forgeo/BLOCKER.md"
    assert payload["agent_command"] == 'opencode run --auto "' + DEFAULT_AGENT_PROMPT + '"'
    assert payload["refactor_prompt"] == "Fix dead code and duplication."
    assert not (project / ".gitignore").exists()


def test_run_setup_keeps_verbatim_command_with_task_reference(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    payload = _run_setup(
        project,
        "",
        'claude -p "$FORGEO_TASK" --model cheap',
        "y",
        "y",
    )

    assert payload is not None
    assert payload["agent_command"] == 'claude -p "$FORGEO_TASK" --model cheap'


def test_run_setup_keeps_existing_gitignore(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").write_text("__pycache__/\n.forgeo/\n", encoding="utf-8")
    _run_setup(project, "", "", "y", "y")

    assert (project / ".gitignore").read_text(encoding="utf-8") == "__pycache__/\n.forgeo/\n"


def test_run_setup_aborts_on_absolute_folder(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert _run_setup(project, "/tmp/outside") is None
    assert not (project / "forgeo.yaml").exists()


# --------------------------------------------------------------------- #
# .gitignore helper                                                      #
# --------------------------------------------------------------------- #


def test_add_gitignore_creates_and_appends(tmp_path):
    assert add_gitignore(tmp_path, ".forgeo/") is True
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".forgeo/\n"

    (tmp_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    assert add_gitignore(tmp_path, ".forgeo/") is True
    assert ".forgeo/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")

    assert add_gitignore(tmp_path, ".forgeo/") is False


# --------------------------------------------------------------------- #
# CLI wiring                                                             #
# --------------------------------------------------------------------- #


def test_cmd_init_refuses_existing_config(tmp_path):
    path = tmp_path / "forgeo.yaml"
    path.write_text("name: x\nagent_command: echo\n", encoding="utf-8")
    args = argparse.Namespace(config=path, force=False)
    assert cmd_init(args) == 2


def test_cmd_init_force_overwrites(tmp_path, monkeypatch):
    path = tmp_path / "forgeo.yaml"
    path.write_text("name: old\nagent_command: echo\n", encoding="utf-8")
    monkeypatch.setattr("forgeo.cli.run_setup", lambda base_dir, config_path: {"name": "new"})
    args = argparse.Namespace(config=path, force=True)
    assert cmd_init(args) == 0


def test_offer_setup_declines(monkeypatch):
    monkeypatch.setattr("forgeo.cli.Confirm.ask", lambda *a, **k: False)
    assert _offer_setup(Path("forgeo.yaml")) is False


def test_offer_setup_accepts(monkeypatch):
    monkeypatch.setattr("forgeo.cli.Confirm.ask", lambda *a, **k: True)
    monkeypatch.setattr("forgeo.cli.run_setup", lambda base_dir, config_path: {"name": "x"})
    assert _offer_setup(Path("forgeo.yaml")) is True


def test_cmd_start_missing_config_offers_setup(monkeypatch, tmp_path):
    monkeypatch.setattr("forgeo.cli.Confirm.ask", lambda *a, **k: False)
    args = argparse.Namespace(
        config=tmp_path / "forgeo.yaml", interval_minutes=None, foreground=False
    )
    assert cmd_start(args) == 1


def test_cmd_default_without_config_runs_setup(monkeypatch, tmp_path):
    monkeypatch.setattr("forgeo.cli.DEFAULT_CONFIG", tmp_path / "forgeo.yaml")
    monkeypatch.setattr("forgeo.cli.run_setup", lambda base_dir, config_path: {"name": "x"})
    assert cmd_default() == 0


def test_cmd_default_with_config_shows_help(monkeypatch, tmp_path, capsys):
    (tmp_path / "forgeo.yaml").write_text("agent_command: echo\n", encoding="utf-8")
    monkeypatch.setattr("forgeo.cli.DEFAULT_CONFIG", tmp_path / "forgeo.yaml")
    assert cmd_default() == 0
    assert "usage:" in capsys.readouterr().out
