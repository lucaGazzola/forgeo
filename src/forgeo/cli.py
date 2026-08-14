"""Command-line interface.

Commands:

* ``forgeo`` / ``forgeo init`` — guided first-time setup: asks for the
  forgeo folder, the coding agent command, and the refactor prompt, then
  writes a ``forgeo.yaml``. Running ``forgeo`` or ``forgeo start`` without
  a config triggers it automatically.
* ``forgeo start --config forgeo.yaml`` — run the scheduled forgeo on one
  repository. Every ``interval_minutes`` it picks an ``OPEN`` task from the
  backlog, or runs a refactoring pass when the backlog is empty; everything
  is committed and pushed on the main branch. When the agent needs human
  input, a detailed ``BLOCKER.md`` file is written with what you must do.
  The daemon binds no ports; live state is written to ``daemon.state.json``
  and is served to you by ``forgeo web``.
* ``forgeo once --config forgeo.yaml`` — run exactly one cycle and exit.
   Shares the per-forgeo lock with the daemon, so it never overlaps a
   running ``start``.
* ``forgeo status --config forgeo.yaml`` — print a read-only summary of the
   forgeo (config, backlog, daemon lock, last log outcome) and exit. Never
   starts an agent.
* ``forgeo stop --config forgeo.yaml`` — stop a running daemon gracefully
   (SIGTERM; a cycle in progress finishes first).
* ``forgeo restart --config forgeo.yaml`` — stop the daemon when running,
   then start it again detached in the background, re-reading the config.
* ``forgeo instance add NAME --config PATH`` — register an existing
   ``forgeo.yaml`` under a stable instance name. Optional: ``start`` and
   ``stop`` register Forgeo automatically under its config's ``name``
   when it is not in the registry yet.
* ``forgeo instance rm NAME`` — unregister an instance (never touches its
   config file or repository).
* ``forgeo instance list`` / ``forgeo list`` — a table of every registered
   instance: config path, repository, daemon state, last outcome, and
   backlog counts.
* ``forgeo web [--host HOST] [--port PORT]`` — serve the central
   multi-instance dashboard in the foreground (default ``0.0.0.0:8790``),
   aggregating every registered instance straight from its files. With
   ``-d``/``--detach`` it starts in the background and returns once the
   server reports it bound; ``forgeo web stop`` SIGTERMs a running
   dashboard and ``forgeo web status`` reports whether one is running.
   The dashboard is host-global (one per user): its lock lives at
   ``~/.config/forgeo/web.lock`` (or ``$FORGEO_CONFIG_DIR/web.lock``),
   independent of any per-repo backlog lock.

``start``, ``once``, ``status``, ``stop`` and ``restart`` each accept either
``--config PATH`` (a config file) or ``--name NAME`` (an instance resolved
from the registry); the two options are mutually exclusive.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from forgeo import __version__
from forgeo.agent import DockerSandboxAgent, SandboxUnavailableError, ShellAgent
from forgeo.backlog import (
    BacklogUnavailableError,
    backlog_status_counts,
    oldest_open_task,
    open_backlog,
)
from forgeo.central import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    WEB_START_TIMEOUT_SECONDS,
    WEB_STOP_TIMEOUT_SECONDS,
)
from forgeo.config import load_config
from forgeo.daemon import ForgeoDaemon, acquire_run_lock, is_lock_held
from forgeo.daemon_control import (
    STOP_TIMEOUT_SECONDS,
    DaemonError,
    restart_daemon,
    stop_daemon,
)
from forgeo.forgeo import Forgeo
from forgeo.git import GitManager
from forgeo.instances import (
    InstanceInfo,
    add_instance,
    ensure_registered,
    list_instances,
    remove_instance,
    resolve_instance,
)
from forgeo.models import ForgeoConfig, SandboxMode, Task
from forgeo.paths import lock_path, runs_path, update_state_path
from forgeo.runs import RunRecorder
from forgeo.setup import run_setup
from forgeo.update import check_for_update

DEFAULT_CONFIG = Path("forgeo.yaml")

console = Console()


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="forgeo",
        description="A scheduled software forgeo: executes backlog tasks on main, "
        "refactors when idle, and writes BLOCKER.md when it needs human input.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="action")

    init_parser = sub.add_parser(
        "init", help="Guided first-time setup: interactively write a forgeo.yaml."
    )
    init_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Where to write the config (default: forgeo.yaml).",
    )
    init_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing config file."
    )

    start_parser = sub.add_parser("start", help="Start the scheduled forgeo for a repository.")
    _add_config_or_name(start_parser)
    start_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="Override the schedule interval from the config file.",
    )

    once_parser = sub.add_parser("once", help="Run exactly one forgeo cycle and exit.")
    _add_config_or_name(once_parser)

    status_parser = sub.add_parser(
        "status",
        help="Print a read-only summary of Forgeo (never starts an agent).",
    )
    _add_config_or_name(status_parser)

    stop_parser = sub.add_parser(
        "stop",
        help="Stop a running forgeo daemon gracefully (SIGTERM).",
    )
    _add_config_or_name(stop_parser)
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=STOP_TIMEOUT_SECONDS,
        help="Seconds to wait for the daemon to exit (default: 600); a cycle "
        "in progress always finishes first.",
    )

    restart_parser = sub.add_parser(
        "restart",
        help="Restart Forgeo daemon in the background, re-reading the config.",
    )
    _add_config_or_name(restart_parser)
    restart_parser.add_argument(
        "--timeout",
        type=float,
        default=STOP_TIMEOUT_SECONDS,
        help="Seconds to wait for the old daemon to exit (default: 600); a "
        "cycle in progress always finishes first.",
    )

    instance_parser = sub.add_parser(
        "instance",
        help="Register, list, and unregister named forgeo instances.",
    )
    instance_sub = instance_parser.add_subparsers(dest="instance_action")

    instance_add_parser = instance_sub.add_parser(
        "add", help="Register an existing forgeo.yaml under a stable name."
    )
    instance_add_parser.add_argument(
        "name", help="Unique instance name (must match ^[a-zA-Z0-9._-]+$)."
    )
    instance_add_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to Forgeo.yaml to register.",
    )

    instance_rm_parser = instance_sub.add_parser("rm", help="Unregister an instance.")
    instance_rm_parser.add_argument("name", help="Instance name to unregister.")

    instance_sub.add_parser(
        "list", help="List every registered instance and its state."
    )

    sub.add_parser(
        "list",
        help="List every registered instance (alias for `forgeo instance list`).",
    )

    web_parser = sub.add_parser(
        "web",
        help="Serve the central multi-instance dashboard in the foreground.",
    )
    web_parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Bind address (default: {DEFAULT_HOST}).",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Bind port (default: {DEFAULT_PORT}).",
    )
    web_parser.add_argument(
        "-d",
        "--detach",
        action="store_true",
        help="Start the dashboard in the background and return once it binds.",
    )
    web_parser.add_argument(
        "--timeout",
        type=float,
        default=WEB_START_TIMEOUT_SECONDS,
        help="Seconds to wait for the dashboard to bind when detached "
        f"(default: {WEB_START_TIMEOUT_SECONDS:.0f}).",
    )
    web_sub = web_parser.add_subparsers(dest="web_action")
    web_stop_parser = web_sub.add_parser(
        "stop", help="Stop the running central dashboard (SIGTERM)."
    )
    web_stop_parser.add_argument(
        "--timeout",
        type=float,
        default=WEB_STOP_TIMEOUT_SECONDS,
        help="Seconds to wait for the dashboard to exit "
        f"(default: {WEB_STOP_TIMEOUT_SECONDS:.0f}).",
    )
    web_sub.add_parser(
        "status", help="Print whether the central dashboard is running."
    )
    return parser


def _add_config_or_name(
    parser: argparse.ArgumentParser, *, default_config: Path = DEFAULT_CONFIG
) -> None:
    """Add a mutually-exclusive ``--config``/``--name`` option pair.

    ``--config`` keeps its default so plain ``forgeo start`` (etc.) keeps
    resolving to ``forgeo.yaml``; argparse still rejects explicitly passing
    both options together.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help="Forgeo YAML file (default: forgeo.yaml).",
    )
    group.add_argument(
        "--name",
        default=None,
        help="Registered instance name resolved from the registry "
        "(see `forgeo instance`).",
    )


def setup_logging(log_file: str | Path) -> None:
    """Configure the ``forgeo`` logger with a rotating file handler."""
    logger = logging.getLogger("forgeo")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_path = Path(log_file)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def _offer_setup(config_path: Path) -> bool:
    """Offer the guided setup; returns True when a config now exists."""
    if not Confirm.ask("No config found. Run the guided first-time setup now?", default=True):
        return False
    return run_setup(base_dir=config_path.parent.resolve(), config_path=config_path) is not None


def _resolved_config_path(args: argparse.Namespace) -> Path | None:
    """Resolve the config path: ``--name`` from the registry, else ``--config``.

    Prints an error and returns ``None`` when the instance name is not
    registered.
    """
    name = getattr(args, "name", None)
    if name is None:
        return Path(args.config)
    config_path = resolve_instance(name)
    if config_path is None:
        console.print(
            f"[red]Unknown instance: {name}. Register it with "
            f"`forgeo instance add {name} --config PATH`.[/red]"
        )
        return None
    return config_path


def _register_if_missing(
    args: argparse.Namespace, config_path: Path, config: ForgeoConfig
) -> None:
    """Auto-register Forgeo under ``config.name`` when not registered.

    Only ``--config`` invocations register: with ``--name`` the instance
    must already exist (``_resolved_config_path`` errors otherwise). The
    instance name is the config's ``name`` field, so ``start``/``stop``
    always leave Forgeo visible in the registry.
    """
    if getattr(args, "name", None) is not None:
        return
    if ensure_registered(config.name, config_path):
        console.print(
            f"[green]Registered instance {config.name!r} -> "
            f"{config_path.resolve()}.[/green]"
        )


def _resolve_config(
    args: argparse.Namespace, config_path: Path | None = None
) -> ForgeoConfig | None:
    """Load the config, offering the guided setup when missing.

    Resolves ``--name`` through the instance registry. Applies the optional
    ``interval_minutes`` override. Returns ``None`` when no config can be
    produced.
    """
    if config_path is None:
        config_path = _resolved_config_path(args)
    if config_path is None:
        return None
    if not config_path.exists():
        console.print(f"[yellow]Config file not found: {config_path}[/yellow]")
        if not _offer_setup(config_path):
            console.print(
                "[yellow]Create one with `forgeo init`, or pass --config <file>.[/yellow]"
            )
            return None
    config = load_config(config_path)
    interval = getattr(args, "interval_minutes", None)
    if interval is not None:
        config = config.model_copy(update={"interval_minutes": interval})
    return config


def _acquire_run_lock(config: ForgeoConfig) -> Any | None:
    """Take the per-forgeo lock; prints an error and returns None when busy."""
    lock = acquire_run_lock(lock_path(config))
    if lock is None:
        console.print(
            f"[red]Another forgeo process (daemon or `once`) is already "
            f"running for {config.name!r}.[/red]"
        )
    return lock


def _build_agent(config: ForgeoConfig) -> ShellAgent:
    """Build the configured agent, sandboxed when ``agent_sandbox`` demands it."""
    if config.agent_sandbox is SandboxMode.DOCKER:
        return DockerSandboxAgent(
            config.agent_command,
            image=config.agent_sandbox_image or "",
            network=config.agent_sandbox_network,
            mounts=config.agent_sandbox_mounts,
            timeout_seconds=config.agent_timeout_seconds,
            env=config.agent_env,
            blocked_exit_code=config.blocked_exit_code,
            no_changes_exit_code=config.no_changes_exit_code,
        )
    return ShellAgent(
        config.agent_command,
        timeout_seconds=config.agent_timeout_seconds,
        env=config.agent_env,
        blocked_exit_code=config.blocked_exit_code,
        no_changes_exit_code=config.no_changes_exit_code,
    )


def _make_forgeo(config: ForgeoConfig) -> Forgeo:
    """Build a :class:`Forgeo` wired to the config.

    Raises:
        SandboxUnavailableError: When the configured sandbox backend is
            unavailable (e.g. no docker binary) — callers turn that into a
            clear startup error.
    """
    backlog = open_backlog(config)
    agent = _build_agent(config)
    return Forgeo(
        config,
        backlog,
        agent,
        GitManager(config.repo, timeout_seconds=config.git_timeout_seconds),
    )


def _prepare_worker(
    args: argparse.Namespace, *, register: bool = False
) -> tuple[Path, ForgeoConfig, Forgeo, Any] | None:
    """Resolve the config, take the run lock, and build Forgeo.

    Shared by ``start`` and ``once``. With ``register=True`` the instance is
    added to the registry *before* the run lock is taken, so a visible lock
    always implies a registered instance. Returns ``None`` (after printing
    an error) when any step fails; on success the caller owns the lock and
    must close it.
    """
    config_path = _resolved_config_path(args)
    if config_path is None:
        return None
    config = _resolve_config(args, config_path)
    if config is None:
        return None
    if register:
        _register_if_missing(args, config_path, config)
    setup_logging(config.log_file)
    log = logging.getLogger("forgeo.cli")
    log.info("Loading forgeo config from %s", config_path)
    lock = _acquire_run_lock(config)
    if lock is None:
        return None
    try:
        forgeo = _make_forgeo(config)
    except SandboxUnavailableError as exc:
        lock.close()
        console.print(f"[red]{exc}[/red]")
        log.error("Sandbox unavailable: %s", exc)
        return None
    return config_path, config, forgeo, lock


def cmd_start(args: argparse.Namespace) -> int:
    """Handle ``forgeo start``: the persistent scheduled worker."""
    prepared = _prepare_worker(args, register=True)
    if prepared is None:
        return 1
    _config_path, config, forgeo, lock = prepared

    async def _serve() -> None:
        daemon = ForgeoDaemon(config, forgeo)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, daemon.stop)
            except NotImplementedError:
                pass
        console.print(
            Panel.fit(
                f"[bold]Forgeo:[/bold] {config.name}\n"
                f"[bold]Repo:[/bold] {config.repo}\n"
                f"[bold]Interval:[/bold] {config.interval_minutes} min\n"
                f"[bold]Backlog:[/bold] {config.backlog}\n"
                f"[bold]Branch:[/bold] {config.branch}\n"
                f"[bold]Log:[/bold] {config.log_file}",
                title="Forgeo",
                border_style="green",
            )
        )
        check_for_update(update_state_path(config), print_fn=console.print)
        await daemon.run_forever()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        lock.close()
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    """Handle ``forgeo once``: run exactly one cycle and exit."""
    prepared = _prepare_worker(args)
    if prepared is None:
        return 1
    _config_path, _config, forgeo, lock = prepared

    async def _run_once() -> None:
        check_for_update(update_state_path(_config), print_fn=console.print)
        outcome = await forgeo.run_cycle()
        console.print(f"[green]Cycle finished: {outcome}[/green]")

    try:
        asyncio.run(_run_once())
    except KeyboardInterrupt:
        pass
    except BacklogUnavailableError as exc:
        # A backlog served over HTTP can simply be down; that is an
        # operational failure to report, not a crash to trace back.
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        logging.getLogger("forgeo.cli").error("Backlog unavailable: %s", exc)
        return 1
    finally:
        lock.close()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Handle ``forgeo init``: the guided first-time setup."""
    if args.config.exists() and not args.force:
        console.print(f"[red]{args.config} already exists. Pass --force to overwrite.[/red]")
        return 2
    if run_setup(base_dir=args.config.parent.resolve(), config_path=args.config) is None:
        console.print("[yellow]Setup aborted; nothing was written.[/yellow]")
        return 130
    return 0


def last_outcome_from_runs(config: ForgeoConfig) -> str | None:
    """Return the last run's outcome from ``runs.jsonl``, or ``None``.

    Never raises on a missing or corrupt file; corrupt lines are skipped
    with a warning.
    """
    last_run = RunRecorder(runs_path(config)).read_last()
    if last_run is None:
        return None
    return last_run.outcome.value


def render_status(
    config: ForgeoConfig,
    tasks: list[Task],
    *,
    daemon_running: bool,
    last_outcome: str | None,
) -> str:
    """Render the human-readable status summary as plain text."""
    counts = backlog_status_counts(tasks)
    count_text = " ".join(f"{status}={counts[status]}" for status in counts)
    nxt = oldest_open_task(tasks)
    next_text = f"{nxt.id} — {nxt.title}" if nxt is not None else "(none)"
    daemon_text = "running" if daemon_running else "not running"
    outcome_text = last_outcome if last_outcome is not None else "(none)"
    return "\n".join(
        [
            f"name: {config.name}",
            f"repo: {config.repo}",
            f"interval: {config.interval_minutes} min",
            f"branch: {config.branch}",
            f"backlog: {count_text}",
            f"next: {next_text}",
            f"daemon: {daemon_text}",
            f"last outcome: {outcome_text}",
        ]
    )


def _load_config_or_error(config_path: Path) -> ForgeoConfig | None:
    """Load an existing config; prints an error and returns None when missing."""
    if not config_path.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        return None
    return load_config(config_path)


def _resolve_existing_config(
    args: argparse.Namespace,
) -> tuple[Path, ForgeoConfig] | None:
    """Resolve ``--name``/``--config`` and load an existing config file.

    Prints an error and returns ``None`` when the instance name is unknown
    or the config file does not exist; otherwise returns the resolved
    config path and its loaded :class:`ForgeoConfig`.
    """
    config_path = _resolved_config_path(args)
    if config_path is None:
        return None
    config = _load_config_or_error(config_path)
    if config is None:
        return None
    return config_path, config


def cmd_status(args: argparse.Namespace) -> int:
    """Handle ``forgeo status``: read-only summary; never starts an agent."""
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    try:
        tasks = asyncio.run(open_backlog(config).list_tasks())
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    daemon_running = is_lock_held(lock_path(config))
    last_outcome = last_outcome_from_runs(config)
    console.print(
        render_status(
            config,
            tasks,
            daemon_running=daemon_running,
            last_outcome=last_outcome,
        )
    )
    return 0


def _stop_daemon(config: ForgeoConfig, timeout: float) -> bool:
    """SIGTERM the running daemon and wait for it to exit; False on failure."""
    try:
        stop_daemon(config, timeout)
    except DaemonError as exc:
        console.print(f"[red]{exc}[/red]")
        return False
    console.print(f"[green]Forgeo {config.name!r} stopped.[/green]")
    return True


def cmd_stop(args: argparse.Namespace) -> int:
    """Handle ``forgeo stop``: graceful daemon shutdown via SIGTERM."""
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    config_path, config = resolved
    _register_if_missing(args, config_path, config)
    if not is_lock_held(lock_path(config)):
        console.print(f"[yellow]Forgeo {config.name!r} is not running.[/yellow]")
        return 1
    return 0 if _stop_daemon(config, args.timeout) else 1


def cmd_restart(args: argparse.Namespace) -> int:
    """Handle ``forgeo restart``: stop when running, then start detached."""
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    config_path, config = resolved
    try:
        pid = restart_daemon(config_path, config, args.timeout)
    except DaemonError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(
        f"[green]Forgeo {config.name!r} restarted "
        f"(pid {pid}, interval {config.interval_minutes} min).[/green]"
    )
    return 0


def cmd_default() -> int:
    """Bare ``forgeo``: show help when configured, run the wizard otherwise."""
    if DEFAULT_CONFIG.exists():
        build_parser().print_help()
        return 0
    console.print("[yellow]No forgeo.yaml found — starting the guided setup.[/yellow]")
    return cmd_init(argparse.Namespace(config=DEFAULT_CONFIG, force=False))


def cmd_instance_add(args: argparse.Namespace) -> int:
    """Handle ``forgeo instance add``: register an existing forgeo.yaml.

    Normally unnecessary — ``forgeo start`` and ``forgeo stop`` register
    the config under its ``name`` automatically — but handy to pre-register
    an explicit name or one that differs from ``config.name``.
    """
    try:
        add_instance(args.name, args.config)
    except (ValueError, FileNotFoundError, yaml.YAMLError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(
        f"[green]Registered instance {args.name!r} -> {args.config.resolve()}.[/green]"
    )
    return 0


def cmd_instance_rm(args: argparse.Namespace) -> int:
    """Handle ``forgeo instance rm``: unregister without touching the repo."""
    if remove_instance(args.name):
        console.print(f"[green]Unregistered instance {args.name!r}.[/green]")
        return 0
    console.print(f"[red]Unknown instance: {args.name}[/red]")
    return 1


def _instance_row(info: InstanceInfo) -> tuple[str, ...]:
    """Render one instance's table row (name, daemon state, last outcome)."""
    if info.config is None:
        return (
            info.name,
            "stopped",
            "(none)",
        )
    last_outcome = last_outcome_from_runs(info.config) or "(none)"
    return (
        info.name,
        "running" if info.daemon_running else "stopped",
        last_outcome,
    )


def cmd_instance_list(args: argparse.Namespace) -> int:
    """Handle ``forgeo instance list`` / ``forgeo list``."""
    infos = list_instances()
    if not infos:
        console.print("[yellow]No registered instances.[/yellow]")
        console.print(
            "[yellow]Register one with `forgeo instance add NAME --config PATH`.[/yellow]"
        )
        return 0
    table = Table(title="Forgeo instances")
    # Fits any terminal width: three short columns, no long paths.
    for column in ("Name", "Daemon", "Last outcome"):
        table.add_column(column, overflow="fold")
    for info in infos:
        table.add_row(*_instance_row(info))
    console.print(table)
    return 0


def cmd_instance(args: argparse.Namespace) -> int:
    """Handle ``forgeo instance``: the registry subcommand group."""
    action = args.instance_action
    if action == "add":
        return cmd_instance_add(args)
    if action == "rm":
        return cmd_instance_rm(args)
    if action == "list":
        return cmd_instance_list(args)
    build_parser().print_help()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Handle ``forgeo web``: the central multi-instance dashboard.

    Runs in the foreground by default, or detached in the background with
    ``-d``; ``forgeo web stop`` and ``forgeo web status`` manage a running
    dashboard through its host-global lock file.
    """
    from forgeo.central import run_foreground

    action = getattr(args, "web_action", None)
    if action == "stop":
        return cmd_web_stop(args)
    if action == "status":
        return cmd_web_status(args)
    if args.detach:
        return cmd_web_detach(args)
    return run_foreground(host=args.host, port=args.port)


def cmd_web_detach(args: argparse.Namespace) -> int:
    """Handle ``forgeo web -d``: run the dashboard as a background process.

    Refuses while another live dashboard holds the host-global lock; a stale
    lock (dead recorded PID) is taken over with a warning, matching the
    daemon's lock behavior.
    """
    from forgeo.central import WebLock, WebLockError, start_web_detached

    lock = WebLock()
    if lock.is_held():
        console.print(
            f"[red]Central dashboard already running (pid {lock.pid}); "
            f"stop it with `forgeo web stop`.[/red]"
        )
        return 1
    if lock.lock_path.exists():
        console.print(
            f"[yellow]Stale dashboard lock {lock.lock_path} "
            f"(pid {lock.pid} is dead); taking over.[/yellow]"
        )
    try:
        pid = start_web_detached(args.host, args.port, args.timeout)
    except WebLockError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(
        f"[green]Forgeo central dashboard started in the background "
        f"(pid {pid}, http://{args.host}:{args.port}).[/green]"
    )
    return 0


def cmd_web_stop(args: argparse.Namespace) -> int:
    """Handle ``forgeo web stop``: SIGTERM the running dashboard."""
    from forgeo.central import WebLock, WebLockError, stop_web

    lock = WebLock()
    if not lock.is_held():
        console.print("[yellow]Forgeo central dashboard is not running.[/yellow]")
        return 1
    try:
        stop_web(args.timeout)
    except WebLockError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print("[green]Forgeo central dashboard stopped.[/green]")
    return 0


def cmd_web_status(args: argparse.Namespace) -> int:
    """Handle ``forgeo web status``: report whether the dashboard is running."""
    from forgeo.central import WebLock

    lock = WebLock()
    if lock.is_held():
        host = lock.host or "unknown"
        port = lock.port if lock.port is not None else "unknown"
        console.print(
            f"central dashboard: running (pid {lock.pid}, http://{host}:{port})"
        )
        return 0
    console.print("central dashboard: not running")
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "start": cmd_start,
    "once": cmd_once,
    "init": cmd_init,
    "status": cmd_status,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "instance": cmd_instance,
    "list": cmd_instance_list,
    "web": cmd_web,
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by both ``forgeo`` and ``python -m forgeo``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action is None:
        return cmd_default()
    command = _COMMANDS.get(args.action)
    if command is None:
        parser.error(f"unknown command: {args.action}")
        return 2
    return command(args)


if __name__ == "__main__":
    sys.exit(main())
