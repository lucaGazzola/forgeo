"""Guided first-time setup: ``forgeo init``.

Walks the user through the decisions Forgeo needs before it can
work on a repository:

1. Forgeo folder — where the backlog, ``BLOCKER.md`` and the log live
   (inside the project, gitignored by default);
2. Backlog provider — where tasks live: a local JSON file, an HTTP
   endpoint, or an issue tracker (GitHub/GitLab/Jira);
3. the coding agent command — the bare invocation that launches the agent
   (e.g. ``opencode run --auto``); Forgeo appends the standard task prompt
   (ending in ``$FORGEO_TASK``) so the user never types it;
4. the refactoring prompt — the default is offered; a custom one can be
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
PROVIDER_CHOICES = ("file", "github", "gitlab", "jira", "http")
DEFAULT_PROVIDER = "file"
DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_GITLAB_API = "https://gitlab.com"
DEFAULT_TOKEN_ENV_GITHUB = "GITHUB_TOKEN"
DEFAULT_TOKEN_ENV_GITLAB = "GITLAB_TOKEN"


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
    "update readme, docs and landing page. Read AGENTS.md at the start of\n"
    "the session, and CONTEXT.md if present; if your change materially\n"
    "affects the project overview or conventions, update AGENTS.md and\n"
    "CONTEXT.md accordingly.\n"
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
        try:
            answer = input_fn(prompt)
        except AssertionError:
            # Test queue exhausted — treat as default for backward-compat
            return default or ""
        # Empty answer in tests means "accept default", matching Prompt.ask
        if not answer.strip() and default is not None:
            return default
        return answer
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


def _detect_github_repo(project_root: Path) -> str | None:
    """Try to infer owner/repo from git remote origin."""
    try:
        import subprocess

        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        # Handle git@github.com:owner/repo.git and https://github.com/owner/repo.git
        if url.startswith("git@"):
            # git@github.com:owner/repo.git -> owner/repo
            _, _, path = url.partition(":")
            path = path.removesuffix(".git")
            if "/" in path:
                return path
        elif "github.com" in url:
            # https://github.com/owner/repo.git
            part = url.split("github.com")[-1].lstrip("/:").removesuffix(".git")
            if "/" in part:
                # take first two segments
                segs = part.split("/")
                if len(segs) >= 2:
                    return f"{segs[0]}/{segs[1]}"
        return None
    except Exception:  # noqa: BLE001 - git detection is best-effort, any failure means no detection
        return None


def _persist_token(token_env: str, token_value: str, console: Console) -> None:
    """Write token to ~/.config/forgeo/github_token_env.sh (600) and wire bashrc."""
    if not token_value.strip():
        return
    try:
        cfg_dir = Path.home() / ".config" / "forgeo"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        env_file = cfg_dir / "github_token_env.sh"
        # Keep existing file if it already has a token for same env? Overwrite with new
        env_file.write_text(
            f"# Forgeo GitHub token — generated by `forgeo init`\n"
            f"# Keep permissions 600\n"
            f"export {token_env}='{token_value.strip()}'\n",
            encoding="utf-8",
        )
        try:
            import os

            os.chmod(env_file, 0o600)
        except Exception:  # noqa: BLE001, S110 - chmod is best-effort, ignore failures
            pass
        # Wire bashrc
        bashrc = Path.home() / ".bashrc"
        marker = "github_token_env.sh"
        if bashrc.exists():
            content = bashrc.read_text(encoding="utf-8")
            if marker not in content:
                bashrc.write_text(
                    content.rstrip("\n") + "\n\n# Forgeo GitHub token\n[ -f ~/.config/forgeo/github_token_env.sh ] && . ~/.config/forgeo/github_token_env.sh\n",
                    encoding="utf-8",
                )
        else:
            bashrc.write_text(
                "[ -f ~/.config/forgeo/github_token_env.sh ] && . ~/.config/forgeo/github_token_env.sh\n",
                encoding="utf-8",
            )
        console.print(f"[green]Token saved to {env_file} (600) and wired to ~/.bashrc.[/green]")
        console.print(f"[dim]Run: source {env_file}  or  export {token_env}=xxx  before forgeo start/validate[/dim]")
    except Exception as exc:  # noqa: BLE001 - token persistence is best-effort, never fail setup
        console.print(f"[yellow]Could not persist token: {exc}[/yellow]")


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

    # --- Backlog provider (new: file is historical default) ---
    provider_raw = _ask_text(
        input_fn,
        "[bold]Backlog provider[/bold] [file/github/gitlab/jira/http] [default file]",
        default=DEFAULT_PROVIDER,
    ).strip().lower() or DEFAULT_PROVIDER
    provider = provider_raw if provider_raw in PROVIDER_CHOICES else DEFAULT_PROVIDER
    if provider_raw not in PROVIDER_CHOICES:
        out.print(f"[yellow]Unknown provider {provider_raw!r}, using {DEFAULT_PROVIDER}.[/yellow]")

    backlog = f"{forgeo_dir}/backlog.json"
    backlog_provider = provider if provider != "file" else "file"
    github_cfg = None
    gitlab_cfg = None
    jira_cfg = None
    state_dir = None
    github_token_env: str | None = None

    if provider == "github":
        detected = _detect_github_repo(root)
        default_repo = detected or ""
        github_repo = ""
        while not github_repo.strip() or "/" not in github_repo:
            github_repo = _ask_text(
                input_fn,
                "[bold]GitHub repository[/bold] (owner/repo)" + (f" [default {default_repo}]" if default_repo else ""),
                default=default_repo or None,
            ).strip()
            if not github_repo and default_repo:
                github_repo = default_repo
            if not github_repo or "/" not in github_repo:
                out.print("[red]Repository must be owner/repo (e.g. owner/repo).[/red]")
        github_repo = github_repo.strip()
        token_env = _ask_text(
            input_fn,
            "[bold]GitHub token env var[/bold] [default GITHUB_TOKEN]",
            default=DEFAULT_TOKEN_ENV_GITHUB,
        ).strip() or DEFAULT_TOKEN_ENV_GITHUB
        backlog = DEFAULT_GITHUB_API
        backlog_provider = "github"
        github_cfg = {"repo": github_repo, "auth": {"token_env": token_env}}
        github_token_env = token_env
        state_dir = forgeo_dir
        # Offer to persist token value immediately (interactive only)
        if input_fn is None:
            if _ask_yes_no(input_fn, f"[bold]Paste GitHub token now to save to {token_env}?[/bold] (stored in ~/.config/forgeo/github_token_env.sh, 600)", default=False):
                from rich.prompt import Prompt as _Prompt

                token_value = _Prompt.ask(f"[bold]{token_env}[/bold]", password=True, default="").strip()
                if token_value:
                    _persist_token(token_env, token_value, out)
                else:
                    out.print(f"[dim]Set {token_env} before forgeo validate/start: export {token_env}=ghp_...[/dim]")
            else:
                out.print(f"[dim]Create a classic PAT at https://github.com/settings/tokens/new (scope repo), then: export {token_env}=ghp_...[/dim]")
        else:
            # In tests, do not consume an extra answer — the wizard's token persistence
            # is interactive-only to keep the answer queue aligned.
            pass
    elif provider == "gitlab":
        default_repo = _detect_github_repo(root) or ""
        gitlab_repo = _ask_text(
            input_fn,
            "[bold]GitLab project[/bold] (group/project or numeric id)" + (f" [default {default_repo}]" if default_repo else ""),
            default=default_repo or None,
        ).strip()
        while not gitlab_repo.strip():
            out.print("[red]Project must not be blank.[/red]")
            gitlab_repo = _ask_text(input_fn, "[bold]GitLab project[/bold] (group/project or numeric id)", default=None).strip()
        token_env = _ask_text(
            input_fn,
            "[bold]GitLab token env var[/bold] [default GITLAB_TOKEN]",
            default=DEFAULT_TOKEN_ENV_GITLAB,
        ).strip() or DEFAULT_TOKEN_ENV_GITLAB
        gitlab_api = _ask_text(
            input_fn,
            "[bold]GitLab base URL[/bold] [default https://gitlab.com]",
            default=DEFAULT_GITLAB_API,
        ).strip() or DEFAULT_GITLAB_API
        backlog = gitlab_api.rstrip("/")
        backlog_provider = "gitlab"
        gitlab_cfg = {"repo": gitlab_repo, "auth": {"token_env": token_env}}
        state_dir = forgeo_dir
    elif provider == "jira":
        jira_url = _ask_text(
            input_fn,
            "[bold]Jira base URL[/bold] (e.g. https://jira.example.com)",
            default=None,
        ).strip()
        while not jira_url.strip():
            out.print("[red]Jira URL must not be blank.[/red]")
            jira_url = _ask_text(input_fn, "[bold]Jira base URL[/bold]", default=None).strip()
        jql = _ask_text(
            input_fn,
            "[bold]Jira JQL[/bold] [default project = APP AND labels = forgeo]",
            default="project = APP AND labels = forgeo",
        ).strip() or "project = APP AND labels = forgeo"
        token_env = _ask_text(
            input_fn,
            "[bold]Jira token env var[/bold] [default JIRA_TOKEN]",
            default="JIRA_TOKEN",
        ).strip() or "JIRA_TOKEN"
        backlog = jira_url.rstrip("/")
        backlog_provider = "jira"
        jira_cfg = {"jql": jql, "auth": {"token_env": token_env, "scheme": "bearer"}}
        state_dir = forgeo_dir
        out.print("[dim]Complete Jira workflow/fields in forgeo.yaml (see config/forgeo.yaml example).[/dim]")
    elif provider == "http":
        http_url = _ask_text(
            input_fn,
            "[bold]HTTP backlog URL[/bold] (https://...)",
            default=None,
        ).strip()
        while not http_url.strip():
            out.print("[red]URL must not be blank.[/red]")
            http_url = _ask_text(input_fn, "[bold]HTTP backlog URL[/bold]", default=None).strip()
        backlog = http_url.strip()
        backlog_provider = "http"
        state_dir = forgeo_dir
    else:
        backlog = f"{forgeo_dir}/backlog.json"
        backlog_provider = "file"

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

    payload: dict[str, object] = {
        "name": root.name or "my-forgeo",
        "repo": ".",
        "interval_minutes": 60,
        "branch": "main",
        "backlog": backlog,
        "blocker_file": f"{forgeo_dir}/BLOCKER.md",
        "agent_command": _BlockStr(command),
        "refactor_prompt": _BlockStr(refactor_prompt),
        "log_file": f"{forgeo_dir}/forgeo.log",
    }
    if backlog_provider != "file":
        payload["backlog_provider"] = backlog_provider
    if state_dir is not None:
        payload["state_dir"] = state_dir
    if github_cfg is not None:
        payload["github"] = github_cfg
    if gitlab_cfg is not None:
        payload["gitlab"] = gitlab_cfg
    if jira_cfg is not None:
        payload["jira"] = jira_cfg

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

    backlog_display = str(payload["backlog"])
    if provider != "file":
        backlog_display = f"{backlog_display} [{provider}]"
    next_hint = f"forgeo start --config {config_path.name}"
    if provider == "github" and github_token_env is not None:
        next_hint = f"export {github_token_env}=ghp_... && forgeo validate --config {config_path.name} && {next_hint}"
    out.print(
        Panel.fit(
            f"[bold]Forgeo configured[/bold] in {config_path}\n"
            f"[bold]Repo:[/bold] {root}\n"
            f"[bold]Backlog:[/bold] {escape(backlog_display)}\n"
            f"[bold]Agent:[/bold] {escape(command)}\n"
            f"[bold]Next:[/bold] {escape(next_hint)}",
            title="Forgeo",
            border_style="green",
        )
    )
    if provider in ("github", "gitlab", "jira"):
        out.print(f"[dim]Backlog lives in {provider} — triage in the external board; Forgeo mirrors tasks in the dashboard.[/dim]")
    return payload
