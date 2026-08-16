"""Guided first-time setup: ``forgeo init``.

Walks the user through the three decisions Forgeo needs before it can
work on a repository:

1. Forgeo folder — where the backlog, ``BLOCKER.md`` and the log live
   (inside the project, gitignored by default);
2. the coding agent command — the bare invocation that launches the agent
   (e.g. ``opencode run --auto``); Forgeo appends the standard task prompt
   (ending in ``$FORGEO_TASK``) so the user never types it;
3. the refactoring prompt — the default is offered; a custom one can be
   pasted instead.

The result is written as ``forgeo.yaml`` next to the project.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from forgeo.models import DEFAULT_REFACTOR_PROMPT

DEFAULT_FORGEO_DIR = ".forgeo"
DEFAULT_AGENT_COMMAND = "opencode run --auto"


class _BlockStr(str):
    """``str`` that YAML emits as a literal block scalar (``|``).

    Multi-line values like the agent command would otherwise be dumped as
    folded single-quoted scalars whose line breaks turn into spaces when
    parsed back.
    """


class _SetupDumper(yaml.SafeDumper):
    """``SafeDumper`` that knows how to emit :class:`_BlockStr`."""


def _represent_block(dumper: _SetupDumper, data: _BlockStr) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


_SetupDumper.add_representer(_BlockStr, _represent_block)

# The standard task prompt appended to the bare agent command (as a quoted
# argument) when the user does not write one themselves.
DEFAULT_AGENT_PROMPT = (
    "Work on the repository at the current working directory.\n"
    "Make the code changes requested below and nothing else. Do NOT run\n"
    "git commit, git push, or git add -A — the forgeo commits your work\n"
    "itself. Verify with the test suite where applicable and, if needed,\n"
    "update readme, docs and landing page. Read AGENTS.md (and CONTEXT.md\n"
    "if present) at the start of the session; if your change materially\n"
    "affects the project overview, keep them updated.\n"
    "$FORGEO_TASK"
)

SetupInput = Callable[[str], str]


def build_agent_command(command: str) -> str:
    """Compose the full ``agent_command`` from a bare agent invocation.

    A bare invocation like ``opencode run --auto`` gets the standard task
    prompt appended as a quoted argument, so the agent receives the task
    (``$FORGEO_TASK``) without the user typing it. Commands that already
    reference ``$FORGEO_TASK`` are kept verbatim.
    """
    if "$FORGEO_TASK" in command:
        return command
    return f'{command} "{DEFAULT_AGENT_PROMPT}"'


def _ask_text(input_fn: SetupInput | None, prompt: str, default: str | None = None) -> str:
    """Free-text question; ``input_fn`` replaces the terminal in tests."""
    if input_fn is not None:
        return input_fn(prompt)
    if default is None:
        return Prompt.ask(prompt)
    return Prompt.ask(prompt, default=default)


def _ask_yes_no(input_fn: SetupInput | None, prompt: str, default: bool = True) -> bool:
    """Yes/no question; ``input_fn`` replaces the terminal in tests."""
    if input_fn is not None:
        return input_fn(prompt).strip().lower() in ("y", "yes")
    return Confirm.ask(prompt, default=default)


def _ask_multiline(input_fn: SetupInput | None, prompt: str, console: Console) -> str:
    """Multi-line answer; an empty line finishes it."""
    if input_fn is None:
        console.print(prompt)
        prompt = "[dim](paste a line; an empty line finishes)[/dim]"
    lines = []
    while True:
        line = input_fn(prompt) if input_fn is not None else Prompt.ask(prompt)
        if not line.strip():
            break
        lines.append(line.strip())
    return "\n".join(lines)


def add_gitignore(project_root: Path, line: str) -> bool:
    """Append ``line`` to ``<project_root>/.gitignore`` when absent."""
    path = project_root / ".gitignore"
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if line in content.splitlines():
            return False
        content = content.rstrip("\n") + "\n" + line + "\n"
    else:
        content = line + "\n"
    path.write_text(content, encoding="utf-8")
    return True


def run_setup(
    base_dir: Path,
    config_path: Path,
    *,
    console: Console | None = None,
    input_fn: SetupInput | None = None,
) -> dict[str, object] | None:
    """Interactively collect the configuration and write it to ``config_path``.

    Args:
        base_dir: Directory the config lives in (the project root); all
            generated paths are relative to it.
        config_path: Where to write the YAML config.
        console: Rich console for output (a new one when omitted).
        input_fn: Replacement for the terminal prompts (tests).

    Returns the written YAML payload, or ``None`` when the setup was aborted.
    """
    out = console or Console()
    root = base_dir.resolve()
    if not (root / ".git").exists():
        out.print(
            "[yellow]Warning: no .git directory here — Forgeo works on a git "
            "repository.[/yellow]"
        )

    forgeo_dir = _ask_text(
        input_fn,
        f"[bold]Forgeo folder[/bold] for backlog, BLOCKER.md and logs "
        f"[default {DEFAULT_FORGEO_DIR}]",
        default=DEFAULT_FORGEO_DIR,
    ).strip()
    forgeo_dir = forgeo_dir.removeprefix("./").rstrip("/") or DEFAULT_FORGEO_DIR
    if Path(forgeo_dir).is_absolute():
        out.print("[red]Forgeo folder must live inside the project. Aborting.[/red]")
        return None
    if ".." in Path(forgeo_dir).parts:
        out.print(
            "[yellow]Note: Forgeo folder escapes the project root — the "
            "gitignore rule will not protect it.[/yellow]"
        )

    command = _ask_text(
        input_fn,
        f"[bold]Coding agent command[/bold] [default {DEFAULT_AGENT_COMMAND}]\n"
        "[dim](bare invocation; the task prompt is appended automatically)[/dim]",
        default=DEFAULT_AGENT_COMMAND,
    ).strip() or DEFAULT_AGENT_COMMAND
    command = build_agent_command(command)

    if _ask_yes_no(input_fn, "[bold]Use the default refactor prompt?[/bold]", default=True):
        refactor_prompt = DEFAULT_REFACTOR_PROMPT
    else:
        out.print("[bold]Your refactor prompt[/bold] (used when the backlog is empty):")
        refactor_prompt = (
            _ask_multiline(input_fn, "[dim](paste a line; empty line finishes)[/dim]", out)
            or DEFAULT_REFACTOR_PROMPT
        )

    if _ask_yes_no(
        input_fn,
        f"[bold]Add '{escape(forgeo_dir)}/' to .gitignore?[/bold]",
        default=True,
    ):
        if add_gitignore(root, forgeo_dir + "/"):
            out.print(f"[green]Added {forgeo_dir}/ to .gitignore.[/green]")
        else:
            out.print(f"[dim]{forgeo_dir}/ already in .gitignore.[/dim]")

    payload = {
        "name": root.name or "my-forgeo",
        "repo": ".",
        "interval_minutes": 60,
        "branch": "main",
        "backlog": f"{forgeo_dir}/backlog.json",
        "blocker_file": f"{forgeo_dir}/BLOCKER.md",
        "agent_command": _BlockStr(command),
        "refactor_prompt": _BlockStr(refactor_prompt),
        "log_file": f"{forgeo_dir}/forgeo.log",
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.dump(payload, Dumper=_SetupDumper, sort_keys=False, allow_unicode=True)
    config_path.write_text(
        "# Forgeo configuration — generated by `forgeo init`.\n"
        "# Relative paths resolve against this file's directory.\n"
        "# Re-run `forgeo init --force` to regenerate. See README.md for all keys.\n\n"
        + body,
        encoding="utf-8",
    )
    (root / forgeo_dir).mkdir(parents=True, exist_ok=True)

    out.print(
        Panel.fit(
            f"[bold]Forgeo configured[/bold] in {config_path}\n"
            f"[bold]Repo:[/bold] {root}\n"
            f"[bold]Backlog:[/bold] {(root / forgeo_dir) / 'backlog.json'}\n"
            f"[bold]Agent:[/bold] {escape(command)}\n"
            f"[bold]Next:[/bold] forgeo start --config {config_path.name}",
            title="Forgeo",
            border_style="green",
        )
    )
    return payload
