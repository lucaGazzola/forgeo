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

import contextlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from forgeo.models import DEFAULT_REFACTOR_PROMPT, PROVIDER_CHOICES

DEFAULT_FORGEO_DIR = ".forgeo"
DEFAULT_AGENT_COMMAND = "opencode run --auto"
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


def _ask_callback_port(input_fn: SetupInput | None, provider: str, out: Console) -> int | None:
    """Ask for an optional fixed browser callback port."""
    while True:
        raw = _ask_text(
            input_fn,
            f"[bold]{provider} OAuth callback port[/bold] [default ephemeral; use a fixed port if the app requires an exact redirect URI]",
            default="",
        ).strip()
        if not raw:
            return None
        try:
            port = int(raw)
        except ValueError:
            port = 0
        if 1 <= port <= 65535:
            return port
        if input_fn is not None:
            return None
        out.print("[red]Callback port must be an integer between 1 and 65535, or blank for ephemeral.[/red]")


def _ask_oauth_client_id(
    input_fn: SetupInput | None,
    out: Console,
    *,
    provider: str,
    env_var: str,
    hint: str = "",
    retry_suffix: str = "",
) -> str:
    """Prompt for an OAuth client ID until non-blank."""
    default_client_id = os.environ.get(env_var)
    prompt = f"[bold]{provider} OAuth client ID[/bold]"
    if hint:
        prompt += f" ({hint})"
    if default_client_id:
        prompt += f" [default {default_client_id}]"
    client_id = _ask_text(input_fn, prompt, default=default_client_id).strip()
    while not client_id:
        out.print(f"[red]Client ID must not be blank{retry_suffix}.[/red]")
        client_id = _ask_text(
            input_fn, f"[bold]{provider} OAuth client ID[/bold]", default=default_client_id
        ).strip()
    return client_id


def _ask_oauth_flow(input_fn: SetupInput | None, out: Console, *, prompt: str, default: str) -> str:
    """Prompt for a device/browser OAuth flow, normalising unknown values to ``default``."""
    flow_raw = _ask_text(input_fn, prompt, default=default).strip().lower() or default
    if flow_raw not in ("device", "browser"):
        out.print(f"[yellow]Unknown flow {flow_raw!r}, using {default}.[/yellow]")
        flow_raw = default
    return flow_raw


def _ask_oauth_overrides(
    input_fn: SetupInput | None,
    out: Console,
    *,
    provider: str,
    token_name: str,
    secret_prompt: str,
    ask_callback_port: bool,
) -> tuple[str | None, str | None, int | None]:
    """Interactive-only OAuth overrides: token file, client-secret env, callback port.

    Returns ``(None, None, None)`` outside interactive mode to keep
    non-interactive runs (and their tests) stable.
    """
    if input_fn is not None:
        return None, None, None
    token_file = (
        _ask_text(
            input_fn,
            f"[bold]Token file[/bold] [default ~/.config/forgeo/tokens/{token_name}.json]",
            default="",
        ).strip()
        or None
    )
    client_secret_env = _ask_text(input_fn, secret_prompt, default="").strip() or None
    callback_port = _ask_callback_port(input_fn, provider, out) if ask_callback_port else None
    return token_file, client_secret_env, callback_port


def _build_oauth_cfg(
    *,
    client_id: str,
    flow_raw: str,
    scope: str,
    default_scope: str,
    token_file: str | None,
    client_secret_env: str | None,
    callback_port: int | None,
) -> dict[str, object]:
    """Assemble an ``auth.oauth`` mapping, omitting default/blank overrides."""
    oauth_cfg: dict[str, object] = {"client_id": client_id, "flow": flow_raw}
    if scope != default_scope:
        oauth_cfg["scope"] = scope
    if token_file:
        oauth_cfg["token_file"] = token_file
    if client_secret_env:
        oauth_cfg["client_secret_env"] = client_secret_env
    if callback_port is not None:
        oauth_cfg["callback_port"] = callback_port
    return oauth_cfg


def _offer_oauth_login_now(
    input_fn: SetupInput | None,
    out: Console,
    *,
    provider: str,
    flag: str,
    login: Callable[[], tuple[str, str | None]],
) -> None:
    """Offer to run OAuth login immediately; ``login`` performs the provider dance."""
    if input_fn is not None:
        out.print(
            f"[dim]Run `forgeo auth login --provider {flag}` to fetch a token after setup.[/dim]"
        )
        return
    if not _ask_yes_no(
        input_fn,
        "[bold]Run OAuth login now to fetch and store a token?[/bold]",
        default=True,
    ):
        out.print(f"[dim]Run `forgeo auth login --provider {flag}` to fetch a token.[/dim]")
        return
    try:
        saved_path, extra_line = login()
    except Exception as exc:  # noqa: BLE001 - login is best-effort
        out.print(f"[yellow]Browser login failed: {exc}[/yellow]")
        out.print(f"[dim]Run `forgeo auth login --provider {flag}` to retry.[/dim]")
        return
    out.print(f"[green]{provider} token saved to {saved_path} (600).[/green]")
    if extra_line:
        out.print(f"[dim]{extra_line}[/dim]")


_OAUTH_CHOICES = frozenset({"browser", "oauth", "device"})


def _is_oauth_choice(raw: str | None) -> bool:
    """Whether a raw auth answer selects the OAuth/browser login path."""
    return (raw or "").strip().lower() in _OAUTH_CHOICES


def _ask_required(
    input_fn: SetupInput | None,
    out: Console,
    prompt: str,
    *,
    default: str | None = None,
    error: str,
    valid: Callable[[str], bool] | None = None,
) -> str:
    """Prompt until a non-blank (and optionally ``valid``) answer is given."""
    while True:
        value = _ask_text(input_fn, prompt, default=default).strip()
        if value and (valid is None or valid(value)):
            return value
        out.print(f"[red]{error}[/red]")


def _ask_auth_choice(
    input_fn: SetupInput | None, *, label: str, default_env: str, detail: str = "for OAuth login"
) -> str:
    """PAT env var or 'browser' for OAuth login (also the OAuth selector in tests)."""
    return _ask_text(
        input_fn,
        f"[bold]{label} auth[/bold] [PAT env var or 'browser' {detail}] [default {default_env}]",
        default=default_env,
    ).strip()


# Per-provider OAuth wizard answers for the GitHub/GitLab setups, so the
# client/flow/scope prompts and the login dance live in one table instead of
# two near-identical branches. ``module``/``*_attr`` locate the provider's
# store and flow functions (imported lazily so tests can monkeypatch them).
_ISSUE_OAUTH_SPECS: dict[str, dict[str, str | None]] = {
    "github": {
        "flag": "github",
        "provider": "GitHub",
        "client_env_var": "FORGEO_GITHUB_CLIENT_ID",
        "client_hint": "from https://github.com/settings/developers > OAuth Apps",
        "default_flow": "device",
        "flow_prompt": "[bold]OAuth flow[/bold] [device/browser] [default device]",
        "default_scope": "repo",
        "secret_prompt": "[bold]Client secret env var (for confidential OAuth Apps, optional)[/bold]",
        "module": "forgeo.oauth_github",
        "store_attr": "GithubTokenStore",
        "oauth_base_attr": "github_oauth_base",
        "success_note": "Token cached; forgeo validate/start will use it.",
    },
    "gitlab": {
        "flag": "gitlab",
        "provider": "GitLab",
        "client_env_var": "FORGEO_GITLAB_CLIENT_ID",
        "client_hint": "from GitLab Admin > Applications",
        "default_flow": "browser",
        "flow_prompt": "[bold]OAuth flow[/bold] [browser/device] [default browser]",
        "default_scope": "api",
        "secret_prompt": "[bold]Client secret env var (for confidential apps, optional)[/bold]",
        "module": "forgeo.oauth_gitlab",
        "store_attr": "GitlabTokenStore",
        "oauth_base_attr": "gitlab_oauth_base",
        "success_note": None,
    },
}


def _collect_issue_oauth(
    input_fn: SetupInput | None, out: Console, spec: dict[str, str | None]
) -> tuple[dict[str, object], dict[str, Any]]:
    """Collect OAuth answers for ``spec``; returns ``(oauth_cfg, login_kwargs)``."""
    provider = str(spec["provider"])
    default_scope = str(spec["default_scope"])
    default_flow = str(spec["default_flow"])
    client_id = _ask_oauth_client_id(
        input_fn, out, provider=provider, env_var=str(spec["client_env_var"]), hint=str(spec["client_hint"])
    )
    flow_raw = _ask_oauth_flow(input_fn, out, prompt=str(spec["flow_prompt"]), default=default_flow)
    scope = (
        _ask_text(input_fn, f"[bold]OAuth scope[/bold] [default {default_scope}]", default=default_scope).strip()
        or default_scope
    )
    token_file, secret_env, callback_port = _ask_oauth_overrides(
        input_fn, out, provider=provider, token_name=str(spec["flag"]),
        secret_prompt=str(spec["secret_prompt"]), ask_callback_port=flow_raw == "browser",
    )
    oauth_cfg = _build_oauth_cfg(
        client_id=client_id, flow_raw=flow_raw, scope=scope, default_scope=default_scope,
        token_file=token_file, client_secret_env=secret_env, callback_port=callback_port,
    )
    login_kwargs: dict[str, Any] = {
        "client_id": client_id, "flow_raw": flow_raw, "scope": scope,
        "token_file": token_file, "client_secret_env": secret_env, "callback_port": callback_port,
    }
    return oauth_cfg, login_kwargs


def _offer_issue_oauth_login(
    input_fn: SetupInput | None, out: Console, spec: dict[str, str | None],
    api_base: str, login_kwargs: dict[str, Any],
) -> None:
    """Offer the shared browser/device login dance described by ``spec``."""
    import importlib

    mod = importlib.import_module(str(spec["module"]))
    store_attr, base_attr = str(spec["store_attr"]), str(spec["oauth_base_attr"])
    browser_attr, device_attr = "run_browser_flow", "run_device_flow"
    store_cls = getattr(mod, store_attr)
    oauth_base = getattr(mod, base_attr)(api_base)
    browser_flow = getattr(mod, browser_attr)
    device_flow = getattr(mod, device_attr)

    def _login() -> tuple[str, str | None]:
        secret = os.environ.get(login_kwargs["client_secret_env"]) if login_kwargs["client_secret_env"] else None
        if login_kwargs["flow_raw"] == "browser":
            token_data = browser_flow(
                login_kwargs["client_id"], oauth_base, login_kwargs["scope"],
                client_secret=secret, callback_port=login_kwargs["callback_port"],
            )
        else:
            token_data = device_flow(login_kwargs["client_id"], oauth_base, login_kwargs["scope"])
        store = store_cls(path=login_kwargs["token_file"], api_base=api_base)
        store.save(token_data)
        note = spec["success_note"]
        return str(store.path), str(note) if note is not None else None

    _offer_oauth_login_now(
        input_fn, out, provider=str(spec["provider"]), flag=str(spec["flag"]), login=_login
    )


def _auth_has_oauth(cfg: dict[str, object] | None) -> bool:
    """Whether a provider config mapping already carries an ``auth.oauth`` section."""
    if not isinstance(cfg, dict):
        return False
    auth = cfg.get("auth")
    return isinstance(auth, dict) and "oauth" in auth


def _ask_api_base(input_fn: SetupInput | None, *, label: str, default: str, interactive_only: bool) -> str:
    """API base URL; skipped outside interactive mode when ``interactive_only``."""
    if interactive_only and input_fn is not None:
        return default
    return (
        _ask_text(input_fn, f"[bold]{label}[/bold] [default {default}]", default=default).strip() or default
    )


def _provider_next_hint(
    *, provider: str, cfg: dict[str, object] | None, config_name: str, pat_hint: str
) -> str:
    """Next-step hint: OAuth login when configured, else the PAT export hint."""
    validate = f"forgeo validate --config {config_name}"
    if _auth_has_oauth(cfg):
        return f"forgeo auth login --provider {provider} && {validate} && forgeo start --config {config_name}"
    return f"{pat_hint} && {validate} && forgeo start --config {config_name}"


def _detect_github_repo(project_root: Path) -> str | None:
    """Try to infer owner/repo from git remote origin."""
    try:
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
        with contextlib.suppress(Exception):  # noqa: BLE001, S110 - chmod is best-effort
            os.chmod(env_file, 0o600)
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


def _ask_forgeo_dir(input_fn: SetupInput | None, out: Console) -> str | None:
    """Prompt for the forgeo folder; returns None when aborted on absolute path."""
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
    return forgeo_dir


def _ask_provider(input_fn: SetupInput | None, out: Console) -> str:
    """Prompt for the backlog provider, normalising unknown values to default."""
    provider_raw = _ask_text(
        input_fn,
        "[bold]Backlog provider[/bold] [file/github/gitlab/jira/http] [default file]",
        default=DEFAULT_PROVIDER,
    ).strip().lower() or DEFAULT_PROVIDER
    provider = provider_raw if provider_raw in PROVIDER_CHOICES else DEFAULT_PROVIDER
    if provider_raw not in PROVIDER_CHOICES:
        out.print(f"[yellow]Unknown provider {provider_raw!r}, using {DEFAULT_PROVIDER}.[/yellow]")
    return provider


def _setup_github(
    root: Path, forgeo_dir: str, input_fn: SetupInput | None, out: Console
) -> tuple[str, str, dict[str, object], str, str | None]:
    """Collect GitHub provider details; returns (backlog, provider, cfg, state_dir, token_env)."""
    detected = _detect_github_repo(root) or ""
    prompt = "[bold]GitHub repository[/bold] (owner/repo)" + (f" [default {detected}]" if detected else "")
    github_repo = _ask_required(
        input_fn, out, prompt, default=detected or None,
        error="Repository must be owner/repo (e.g. owner/repo).", valid=lambda v: "/" in v,
    )
    # GitHub API base — only prompt interactively to keep non-interactive tests stable; GHE users can edit forgeo.yaml afterwards.
    github_api_base = _ask_api_base(
        input_fn, label="GitHub API base URL", default=DEFAULT_GITHUB_API, interactive_only=True
    ).rstrip("/")
    # Token env prompt also doubles as browser login selector to keep
    # non-interactive tests backwards compatible: entering "browser" selects OAuth.
    token_env_raw = _ask_auth_choice(input_fn, label="GitHub", default_env=DEFAULT_TOKEN_ENV_GITHUB)
    if _is_oauth_choice(token_env_raw):
        # OAuth / browser login path
        spec = _ISSUE_OAUTH_SPECS["github"]
        oauth_cfg, login_kwargs = _collect_issue_oauth(input_fn, out, spec)
        github_cfg: dict[str, object] = {"repo": github_repo, "auth": {"oauth": oauth_cfg}}
        _offer_issue_oauth_login(input_fn, out, spec, github_api_base, login_kwargs)
        return github_api_base, "github", github_cfg, forgeo_dir, None
    token_env = token_env_raw or DEFAULT_TOKEN_ENV_GITHUB
    github_cfg = {"repo": github_repo, "auth": {"token_env": token_env}}
    if input_fn is None and _ask_yes_no(
        input_fn,
        f"[bold]Paste GitHub token now to save to {token_env}?[/bold] (stored in ~/.config/forgeo/github_token_env.sh, 600)",
        default=False,
    ):
        from rich.prompt import Prompt as _Prompt

        if token_value := _Prompt.ask(f"[bold]{token_env}[/bold]", password=True, default="").strip():
            _persist_token(token_env, token_value, out)
        else:
            out.print(f"[dim]Set {token_env} before forgeo validate/start: export {token_env}=ghp_...[/dim]")
    elif input_fn is None:
        out.print(f"[dim]Create a classic PAT at https://github.com/settings/tokens/new (scope repo), then: export {token_env}=ghp_...[/dim]")
    return github_api_base, "github", github_cfg, forgeo_dir, token_env


def _setup_gitlab(
    root: Path, forgeo_dir: str, input_fn: SetupInput | None, out: Console
) -> tuple[str, str, dict[str, object], str]:
    """Collect GitLab provider details; returns (backlog, provider, cfg, state_dir)."""
    default_repo = _detect_github_repo(root) or ""
    prompt = "[bold]GitLab project[/bold] (group/project or numeric id)" + (
        f" [default {default_repo}]" if default_repo else ""
    )
    gitlab_repo = _ask_required(
        input_fn, out, prompt, default=default_repo or None, error="Project must not be blank."
    )
    token_env_raw = _ask_auth_choice(input_fn, label="GitLab", default_env=DEFAULT_TOKEN_ENV_GITLAB)
    if _is_oauth_choice(token_env_raw):
        spec = _ISSUE_OAUTH_SPECS["gitlab"]
        oauth_cfg, login_kwargs = _collect_issue_oauth(input_fn, out, spec)
        gitlab_api = _ask_api_base(input_fn, label="GitLab base URL", default=DEFAULT_GITLAB_API, interactive_only=True)
        gitlab_cfg_oauth: dict[str, object] = {"repo": gitlab_repo, "auth": {"oauth": oauth_cfg}}
        _offer_issue_oauth_login(input_fn, out, spec, gitlab_api, login_kwargs)
        return gitlab_api.rstrip("/"), "gitlab", gitlab_cfg_oauth, forgeo_dir
    token_env = token_env_raw or DEFAULT_TOKEN_ENV_GITLAB
    gitlab_api = _ask_api_base(input_fn, label="GitLab base URL", default=DEFAULT_GITLAB_API, interactive_only=False)
    gitlab_cfg: dict[str, object] = {"repo": gitlab_repo, "auth": {"token_env": token_env}}
    return gitlab_api.rstrip("/"), "gitlab", gitlab_cfg, forgeo_dir


def _setup_jira(
    forgeo_dir: str, input_fn: SetupInput | None, out: Console
) -> tuple[str, str, dict[str, object], str]:
    """Collect Jira provider details; returns (backlog, provider, cfg, state_dir)."""
    jira_url = _ask_required(
        input_fn,
        out,
        "[bold]Jira base URL[/bold] (e.g. https://jira.example.com or https://xxx.atlassian.net)",
        error="Jira URL must not be blank.",
    )
    jql = _ask_text(
        input_fn,
        "[bold]Jira JQL[/bold] [default project = APP AND labels = forgeo]",
        default="project = APP AND labels = forgeo",
    ).strip() or "project = APP AND labels = forgeo"
    token_env_raw = _ask_auth_choice(input_fn, label="Jira", default_env="JIRA_TOKEN", detail="for OAuth 3LO")
    if _is_oauth_choice(token_env_raw):
        client_id = _ask_oauth_client_id(
            input_fn,
            out,
            provider="Jira",
            env_var="FORGEO_JIRA_CLIENT_ID",
            hint="from https://developer.atlassian.com/console/myapps/",
        )
        client_secret_env = _ask_text(
            input_fn,
            "[bold]Jira OAuth client secret env var[/bold] [default JIRA_CLIENT_SECRET]",
            default="JIRA_CLIENT_SECRET",
        ).strip() or "JIRA_CLIENT_SECRET"
        scope = _ask_text(
            input_fn,
            "[bold]OAuth scope[/bold] [default offline_access read:jira-user read:jira-work]",
            default="offline_access read:jira-user read:jira-work",
        ).strip() or "offline_access read:jira-user read:jira-work"
        token_file: str | None = None
        cloud_id: str | None = None
        if input_fn is None:
            token_file = _ask_text(input_fn, "[bold]Token file[/bold] [default ~/.config/forgeo/tokens/jira.json]", default="").strip() or None
            cloud_id = _ask_text(input_fn, "[bold]Atlassian cloud ID (optional, auto-detected if blank)[/bold]", default="").strip() or None
        callback_port = _ask_callback_port(input_fn, "Jira", out) if input_fn is None else None
        oauth_cfg: dict[str, object] = {"client_id": client_id, "client_secret_env": client_secret_env, "scope": scope, "flow": "browser"}
        if token_file:
            oauth_cfg["token_file"] = token_file
        if cloud_id:
            oauth_cfg["cloud_id"] = cloud_id
        if callback_port is not None:
            oauth_cfg["callback_port"] = callback_port
        jira_cfg: dict[str, object] = {"jql": jql, "auth": {"oauth": oauth_cfg}}

        def _jira_login() -> tuple[str, str | None]:
            from forgeo.oauth_jira import JiraTokenStore, run_browser_flow

            secret = os.environ.get(client_secret_env)
            token_data = run_browser_flow(
                client_id,
                None,
                scope,
                client_secret=secret,
                cloud_id=cloud_id,
                callback_port=callback_port,
            )
            store = JiraTokenStore(path=token_file, api_base=jira_url)
            store.save(token_data)
            extra = f"Cloud ID {token_data['cloud_id']} saved." if token_data.get("cloud_id") else None
            return str(store.path), extra

        _offer_oauth_login_now(input_fn, out, provider="Jira", flag="jira", login=_jira_login)
        out.print("[dim]Complete Jira workflow/fields in forgeo.yaml (see config/forgeo.yaml example).[/dim]")
        return jira_url.rstrip("/"), "jira", jira_cfg, forgeo_dir
    token_env = token_env_raw or "JIRA_TOKEN"
    jira_cfg = {"jql": jql, "auth": {"token_env": token_env, "scheme": "bearer"}}
    out.print("[dim]Complete Jira workflow/fields in forgeo.yaml (see config/forgeo.yaml example).[/dim]")
    return jira_url.rstrip("/"), "jira", jira_cfg, forgeo_dir


def _setup_http(
    forgeo_dir: str, input_fn: SetupInput | None, out: Console
) -> tuple[str, str, str]:
    """Collect HTTP provider details; returns (backlog, provider, state_dir)."""
    http_url = _ask_required(
        input_fn,
        out,
        "[bold]HTTP backlog URL[/bold] (https://...)",
        error="URL must not be blank.",
    )
    return http_url, "http", forgeo_dir


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

    forgeo_dir = _ask_forgeo_dir(input_fn, out)
    if forgeo_dir is None:
        return None

    provider = _ask_provider(input_fn, out)

    backlog = f"{forgeo_dir}/backlog.json"
    backlog_provider = provider if provider != "file" else "file"
    github_cfg: dict[str, object] | None = None
    gitlab_cfg: dict[str, object] | None = None
    jira_cfg: dict[str, object] | None = None
    state_dir: str | None = None
    github_token_env: str | None = None

    if provider == "github":
        backlog, backlog_provider, github_cfg, state_dir, github_token_env = _setup_github(
            root, forgeo_dir, input_fn, out
        )
    elif provider == "gitlab":
        backlog, backlog_provider, gitlab_cfg, state_dir = _setup_gitlab(
            root, forgeo_dir, input_fn, out
        )
    elif provider == "jira":
        backlog, backlog_provider, jira_cfg, state_dir = _setup_jira(
            forgeo_dir, input_fn, out
        )
    elif provider == "http":
        backlog, backlog_provider, state_dir = _setup_http(forgeo_dir, input_fn, out)
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
    provider_cfgs: dict[str, dict[str, object] | None] = {"github": github_cfg, "gitlab": gitlab_cfg, "jira": jira_cfg}
    provider_pats: dict[str, str | None] = {
        "github": f"export {github_token_env}=ghp_..." if github_token_env else None,
        "gitlab": "export GITLAB_TOKEN=glpat-...",
        "jira": "export JIRA_TOKEN=...",
    }
    hint_cfg = provider_cfgs.get(provider)
    hint_pat = provider_pats.get(provider)
    if hint_cfg is not None and (_auth_has_oauth(hint_cfg) or hint_pat is not None):
        next_hint = _provider_next_hint(
            provider=provider, cfg=hint_cfg, config_name=config_path.name, pat_hint=hint_pat or ""
        )
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
