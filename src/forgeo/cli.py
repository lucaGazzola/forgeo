"""Command-line interface.

Commands:

* ``forgeo`` / ``forgeo init`` — guided first-time setup: asks for the
  forgeo folder, the coding agent command, and the refactor prompt, then
  writes a ``forgeo.yaml``. Running ``forgeo`` or ``forgeo start`` without
  a config triggers it automatically.
* ``forgeo start --config forgeo.yaml`` — start the scheduled forgeo on one
  repository detached in the background and exit. Every ``interval_minutes``
  it picks an ``OPEN`` task from the configured provider, or runs a refactoring
  pass when the provider has no runnable task; everything is committed and
  pushed on the main branch. When the agent needs human input, a detailed
  ``BLOCKER.md`` file is written with what you must do. The daemon binds no
  ports; live state is written to ``daemon.state.json`` and is served to you
  by ``forgeo web``.
  ``forgeo start -f`` (``--foreground``) runs the daemon in the foreground
  instead, interruptible with Ctrl-C. The daemon watches
  ``forgeo.yaml`` and re-reads it on the next cycle boundary when it changes
  (or on ``SIGHUP``); path changes (``repo``, ``backlog``, ``blocker_file``,
  ``log_file``) still need a ``forgeo restart``.
* ``forgeo once --config forgeo.yaml`` — run exactly one cycle and exit.
   Shares the per-forgeo lock with the daemon, so it never overlaps a
   running ``start``.
* ``forgeo run --task TASK-001 --config forgeo.yaml`` — run exactly one
   specific ``OPEN`` task by id and exit, without waiting for the backlog
   order or a scheduled run. For triage: rerun a ``FAILED`` task (after
   reopening it) or try a risky task right now. With ``--reopen`` a
   ``BLOCKED`` task is reopened (and a ``FAILED`` task retried) first, so
   the ``show -> edit -> reopen -> run`` recovery loop collapses to one
   command. Reuses the same per-forgeo
   lock as ``once`` and the daemon, so it never overlaps them; it refuses
   with a clear error when the task does not exist or is not ``OPEN``.
* ``forgeo task add --title T --description D [--description-file F]`` — create a
   new ``OPEN`` task in the configured backlog without hand-editing JSON
   or opening the dashboard (ids auto-assign as ``TASK-###`` unless
   ``--id`` is given; ``--description -`` / ``--description-file -`` reads
   stdin and ``--description-file PATH`` reads a file, so multiline specs
   never need shell quoting; ``--run-at now`` jumps the queue, an ISO-8601
   time schedules it). ``forgeo task list [--status S]`` shows tasks from
   the terminal; ``forgeo task next`` shows which task the scheduler would
   pick next and why the rest wait (dependencies, future ``run-at``, queue
   order — the answer to "why isn't my task running?");
   ``forgeo task show --task ID`` prints one task's full
   detail (description, acceptance, dependencies, blocker/failure
   reasons); ``forgeo task edit --task ID`` updates a task's title,
   description, acceptance criteria, dependencies, files or ``--run-at``
   schedule in place
   (the terminal equivalent of editing it in the dashboard — fix a
    ``BLOCKED`` task before reopening it, or ``--run-at now`` to run it
    next; ``--description-file`` / ``-`` stdin works here too);
    ``forgeo task reopen --task ID``
   moves a ``BLOCKED`` or ``FAILED`` task back to ``OPEN``;
   ``forgeo task rm --task ID`` deletes a task (typos, duplicates, or
   tasks that will never be done — no JSON editing or dashboard
   needed); ``forgeo task complete-review --task ID`` marks a ``REVIEW``
   task ``COMPLETED`` after merging its branch, and
   ``forgeo task request-changes --task ID`` sends a ``REVIEW`` task back
   to ``OPEN`` for rework (the terminal equivalent of the dashboard's
   Complete / Request-changes buttons). Never starts an agent.
* ``forgeo validate --config forgeo.yaml`` — read-only dry run: validate the
   config, repository, branch and remote resolution, backlog, agent command,
   and lock state. Reports all problems at once, never invokes the agent, and
   exits non-zero when any problem is found.
* ``forgeo status --config forgeo.yaml`` — print a read-only summary of the
   forgeo (config, backlog, daemon lock, last log outcome) and exit. Never
   starts an agent.
* ``forgeo logs --config forgeo.yaml`` — print the tail of the forgeo log
   file (``log_file``) and exit; ``-n`` sets how many lines, ``-f`` follows
   new lines like ``tail -f``. Read-only; never starts an agent.
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
* ``forgeo web [--host HOST] [--port PORT] [--token [TOKEN]]`` — serve the
   central multi-instance dashboard in the foreground (default ``0.0.0.0:8790``),
   aggregating every registered instance straight from its files. With
   ``-d``/``--detach`` it starts in the background and returns once the
   server reports it bound; ``forgeo web stop`` SIGTERMs a running
   dashboard and ``forgeo web status`` reports whether one is running.
   The dashboard is host-global (one per user): its lock lives at
   ``~/.config/forgeo/web.lock`` (or ``$FORGEO_CONFIG_DIR/web.lock``),
   independent of any per-repo backlog lock. ``--token`` (optional) turns on
   bearer auth on every ``/api/*`` route: the token is persisted to
   ``~/.config/forgeo/web.toml`` (generated and printed once when given with
   no value), and a token already present there enables auth even without
   the flag. With no flag and no token file the dashboard stays open.

``start``, ``once``, ``run``, ``task add``, ``task list``, ``task next``,
``task show``, ``task edit``, ``task reopen``, ``task rm``,
``task complete-review``, ``task request-changes``, ``status``, ``logs``,
``validate``, ``stop`` and ``restart`` each accept either ``--config PATH``
(a config file) or ``--name NAME`` (an instance resolved from the
registry); the two options are mutually exclusive.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import time
from collections.abc import Callable, Coroutine
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
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
    unsatisfied_dependencies,
)
from forgeo.central import (
    AUTOGENERATE_TOKEN,
    DEFAULT_HOST,
    DEFAULT_PORT,
    WEB_START_TIMEOUT_SECONDS,
    WEB_STOP_TIMEOUT_SECONDS,
)
from forgeo.config import load_config
from forgeo.daemon import ForgeoDaemon, acquire_run_lock, is_lock_held, read_lock_pid
from forgeo.daemon_control import (
    STOP_TIMEOUT_SECONDS,
    DaemonError,
    restart_daemon,
    start_daemon,
    stop_daemon,
)
from forgeo.forgeo import Forgeo, TaskNotRunnableError
from forgeo.git import GitManager
from forgeo.instances import (
    InstanceInfo,
    add_instance,
    ensure_registered,
    list_instances,
    remove_instance,
    resolve_instance,
)
from forgeo.models import ForgeoConfig, SandboxMode, Task, TaskStatus
from forgeo.paths import lock_path, runs_path, update_state_path
from forgeo.runs import RunRecorder
from forgeo.setup import run_setup
from forgeo.update import check_for_update
from forgeo.validate import render_report, validate_config
from forgeo.web_common import DEFAULT_LOG_LINES, tail_lines

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

    start_parser = sub.add_parser(
        "start", help="Start the scheduled forgeo daemon for a repository."
    )
    _add_config_or_name(start_parser)
    start_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="Override the schedule interval from the config file.",
    )
    start_parser.add_argument(
        "-f",
        "--foreground",
        action="store_true",
        help="Run the daemon in the foreground instead of starting it "
        "detached in the background.",
    )

    once_parser = sub.add_parser("once", help="Run exactly one forgeo cycle and exit.")
    _add_config_or_name(once_parser)

    run_parser = sub.add_parser(
        "run",
        help="Run exactly one specific OPEN task by id and exit.",
    )
    _add_config_or_name(run_parser)
    run_parser.add_argument(
        "--task",
        required=True,
        metavar="TASK_ID",
        help="Id of the OPEN task to run now (triage: rerun a FAILED task "
        "after reopening it, or try a risky task immediately).",
    )
    run_parser.add_argument(
        "--reopen",
        action="store_true",
        help="Reopen a BLOCKED task (or retry a FAILED one) before running, "
        "so `forgeo run --task <id> --reopen` retries in one step.",
    )

    task_parser = sub.add_parser(
        "task",
        help="Manage backlog tasks from the terminal "
        "(no JSON editing or dashboard needed).",
    )
    task_sub = task_parser.add_subparsers(dest="task_action")

    task_add_parser = task_sub.add_parser(
        "add", help="Create a new OPEN task in the backlog."
    )
    _add_config_or_name(task_add_parser)
    task_add_parser.add_argument("--title", required=True, help="Short task title.")
    task_add_parser.add_argument(
        "--description",
        required=False,
        default=None,
        help="What the agent should do (use '-' to read from stdin; "
        "not with --description-file).",
    )
    task_add_parser.add_argument(
        "--description-file",
        type=Path,
        default=None,
        metavar="FILE",
        dest="description_file",
        help="Read the description from FILE ('-' for stdin; "
        "not with --description).",
    )
    task_add_parser.add_argument(
        "--id",
        default=None,
        metavar="TASK_ID",
        help="Task id (default: next TASK-###; must be unique).",
    )
    task_add_parser.add_argument(
        "--acceptance",
        action="append",
        default=None,
        metavar="CRITERION",
        help="Acceptance criterion (repeatable).",
    )
    task_add_parser.add_argument(
        "--depends-on",
        action="append",
        default=None,
        metavar="TASK_ID",
        dest="depends_on",
        help="Id of a task this task waits for (repeatable).",
    )
    task_add_parser.add_argument(
        "--run-at",
        default=None,
        metavar="DATETIME",
        dest="run_at",
        help="Earliest moment the task may be picked (ISO-8601, or 'now' "
        "to run next: due tasks jump ahead of oldest-first order).",
    )

    task_list_parser = task_sub.add_parser(
        "list", help="List backlog tasks and their statuses."
    )
    _add_config_or_name(task_list_parser)
    task_list_parser.add_argument(
        "--status",
        choices=["open", "blocked", "failed", "completed", "review"],
        default=None,
        help="Only show tasks with this status (default: all).",
    )
    task_list_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Show at most N tasks (default: all).",
    )

    task_next_parser = task_sub.add_parser(
        "next", help="Show which task would run next and why (never starts an agent)."
    )
    _add_config_or_name(task_next_parser)

    task_show_parser = task_sub.add_parser(
        "show", help="Show one task's full detail (never starts an agent)."
    )
    _add_config_or_name(task_show_parser)
    task_show_parser.add_argument(
        "--task",
        required=True,
        metavar="TASK_ID",
        help="Id of the task to show in full.",
    )

    task_edit_parser = task_sub.add_parser(
        "edit", help="Update a task's fields in place (never starts an agent)."
    )
    _add_config_or_name(task_edit_parser)
    task_edit_parser.add_argument(
        "--task",
        required=True,
        metavar="TASK_ID",
        help="Id of the task to update.",
    )
    task_edit_parser.add_argument("--title", default=None, help="New task title.")
    task_edit_parser.add_argument(
        "--description",
        default=None,
        help="New task description (use '-' to read from stdin; "
        "not with --description-file).",
    )
    task_edit_parser.add_argument(
        "--description-file",
        type=Path,
        default=None,
        metavar="FILE",
        dest="description_file",
        help="Read the new description from FILE ('-' for stdin; "
        "not with --description).",
    )
    task_edit_parser.add_argument(
        "--acceptance",
        action="append",
        default=None,
        metavar="CRITERION",
        help="Acceptance criterion (repeatable; replaces the whole list).",
    )
    task_edit_parser.add_argument(
        "--depends-on",
        action="append",
        default=None,
        metavar="TASK_ID",
        dest="depends_on",
        help="Dependency task id (repeatable; replaces the whole list).",
    )
    task_edit_parser.add_argument(
        "--files",
        action="append",
        default=None,
        metavar="PATH",
        help="File path the agent may touch (repeatable; replaces the whole list).",
    )
    task_edit_parser.add_argument(
        "--clear-acceptance",
        action="store_true",
        help="Clear all acceptance criteria.",
    )
    task_edit_parser.add_argument(
        "--clear-depends-on",
        action="store_true",
        help="Clear all dependencies.",
    )
    task_edit_parser.add_argument(
        "--run-at",
        default=None,
        metavar="DATETIME",
        dest="run_at",
        help="Earliest moment the task may be picked (ISO-8601, or 'now' "
        "to run next; due tasks jump ahead of oldest-first order).",
    )
    task_edit_parser.add_argument(
        "--clear-run-at",
        action="store_true",
        help="Clear the scheduled run time (back to oldest-first order).",
    )
    task_edit_parser.add_argument(
        "--clear-files",
        action="store_true",
        help="Clear the files-to-modify list.",
    )

    task_reopen_parser = task_sub.add_parser(
        "reopen", help="Move a BLOCKED or FAILED task back to OPEN."
    )
    _add_config_or_name(task_reopen_parser)
    task_reopen_parser.add_argument(
        "--task",
        required=True,
        metavar="TASK_ID",
        help="Id of the BLOCKED or FAILED task to reopen.",
    )

    task_rm_parser = task_sub.add_parser(
        "rm", help="Delete a task from the backlog (never starts an agent)."
    )
    _add_config_or_name(task_rm_parser)
    task_rm_parser.add_argument(
        "--task",
        required=True,
        metavar="TASK_ID",
        help="Id of the task to delete (typos, duplicates, or tasks that "
        "will never be done).",
    )

    task_complete_review_parser = task_sub.add_parser(
        "complete-review",
        help="Mark a REVIEW task COMPLETED after merging its branch "
        "(never starts an agent).",
    )
    _add_config_or_name(task_complete_review_parser)
    task_complete_review_parser.add_argument(
        "--task",
        required=True,
        metavar="TASK_ID",
        help="Id of the REVIEW task to mark COMPLETED (merge its "
        "review branch first).",
    )

    task_request_changes_parser = task_sub.add_parser(
        "request-changes",
        help="Send a REVIEW task back to OPEN for rework "
        "(never starts an agent).",
    )
    _add_config_or_name(task_request_changes_parser)
    task_request_changes_parser.add_argument(
        "--task",
        required=True,
        metavar="TASK_ID",
        help="Id of the REVIEW task to send back to OPEN.",
    )

    status_parser = sub.add_parser(
        "status",
        help="Print a read-only summary of Forgeo (never starts an agent).",
    )
    _add_config_or_name(status_parser)

    logs_parser = sub.add_parser(
        "logs",
        help="Print the tail of the forgeo log file (never starts an agent).",
    )
    _add_config_or_name(logs_parser)
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=_positive_log_lines,
        default=DEFAULT_LOG_LINES,
        metavar="N",
        help=f"How many trailing lines to print (default: {DEFAULT_LOG_LINES}).",
    )
    logs_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Keep printing new lines as they are appended (Ctrl-C to stop).",
    )

    validate_parser = sub.add_parser(
        "validate",
        help="Read-only dry run: check config, repo, branch, remote, backlog, "
        "agent command and lock state without starting anything.",
    )
    _add_config_or_name(validate_parser)

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
        "--token",
        nargs="?",
        const=AUTOGENERATE_TOKEN,
        default=None,
        metavar="TOKEN",
        help="Require a bearer token on every /api/* route. With a value, "
        "that token is persisted to ~/.config/forgeo/web.toml; without a "
        "value a fresh token is generated, printed once, and saved. When "
        "web.toml already holds a token, auth is on even without this flag; "
        "with no flag and no token file the dashboard stays open (no auth).",
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

    auth_parser = sub.add_parser(
        "auth",
        help="Browser/OAuth login for issue backlogs (GitHub, GitLab, Jira).",
    )
    auth_sub = auth_parser.add_subparsers(dest="auth_action")
    auth_login = auth_sub.add_parser("login", help="Log in via browser/OAuth and store a token.")
    auth_login.add_argument(
        "--provider",
        choices=["github", "gitlab", "jira"],
        default="github",
        help="Backlog provider to authenticate (default: github).",
    )
    auth_login.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Forgeo config to read OAuth client_id from (defaults to forgeo.yaml if present).",
    )
    auth_login.add_argument("--client-id", default=None, help="OAuth client ID (overrides config).")
    auth_login.add_argument(
        "--flow",
        choices=["device", "browser"],
        default=None,
        help="OAuth flow to use (overrides config; default: device for github, browser for gitlab/jira; Jira is browser-only).",
    )
    auth_login.add_argument(
        "--scope",
        default=None,
        help="OAuth scope to request (default: repo for github, api for gitlab, offline_access for jira).",
    )
    auth_login.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="Where to store the token (defaults to ~/.config/forgeo/tokens/<provider>.json).",
    )
    auth_login.add_argument(
        "--callback-port",
        type=_callback_port,
        default=None,
        help="Loopback callback port for browser flow (default: ephemeral port).",
    )
    auth_login.add_argument(
        "--cloud-id",
        default=None,
        help="Atlassian Jira Cloud ID (overrides jira.auth.oauth.cloud_id).",
    )
    auth_login.add_argument(
        "--api-base",
        default=None,
        help="Provider API base URL (default: https://api.github.com / https://gitlab.com / jira site, or from config).",
    )
    auth_login.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not open the browser automatically (print the URL instead; browser and device flows).",
    )
    auth_status = auth_sub.add_parser("status", help="Show stored OAuth token status.")
    auth_status.add_argument(
        "--provider",
        choices=["github", "gitlab", "jira"],
        default="github",
        help="Backlog provider (default: github).",
    )
    auth_status.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Forgeo config to resolve token file / api base.",
    )
    auth_status.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="Token file to inspect.",
    )
    auth_status.add_argument("--api-base", default=None, help="Provider API base URL.")
    auth_logout = auth_sub.add_parser("logout", help="Remove the stored OAuth token.")
    auth_logout.add_argument(
        "--provider",
        choices=["github", "gitlab", "jira"],
        default="github",
        help="Backlog provider (default: github).",
    )
    auth_logout.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Forgeo config to resolve token file / api base.",
    )
    auth_logout.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="Token file to remove.",
    )
    auth_logout.add_argument("--api-base", default=None, help="Provider API base URL.")
    return parser


def _add_config_or_name(parser: argparse.ArgumentParser) -> None:
    """Add a mutually-exclusive ``--config``/``--name`` option pair.

    ``--config`` keeps its default so plain ``forgeo start`` (etc.) keeps
    resolving to ``forgeo.yaml``; argparse still rejects explicitly passing
    both options together.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
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

    Shared by ``once`` and the foreground ``start``. With ``register=True``
    the instance is added to the registry *before* the run lock is taken, so
    a visible lock always implies a registered instance. Returns ``None``
    (after printing an error) when any step fails; on success the caller
    owns the lock and must close it.
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
    """Handle ``forgeo start``: the scheduled worker.

    By default the daemon is started detached in the background and this
    command exits; ``--foreground`` runs it in the foreground instead.
    """
    if getattr(args, "foreground", False):
        return _cmd_start_foreground(args)
    return _cmd_start_detached(args)


def _cmd_start_detached(args: argparse.Namespace) -> int:
    """Handle ``forgeo start`` without ``--foreground``: detach and exit.

    Resolves and registers the config like the foreground path, refuses when
    the per-forgeo lock is already held, fails fast when ``forgeo validate``
    finds problems (so a broken config never leaves a silently dead daemon),
    then launches the detached daemon and returns its pid.
    """
    config_path = _resolved_config_path(args)
    if config_path is None:
        return 1
    try:
        config = _resolve_config(args, config_path)
    except (yaml.YAMLError, ValidationError) as exc:
        _print_config_load_error(config_path, exc)
        return 1
    if config is None:
        return 1
    _register_if_missing(args, config_path, config)
    daemon_lock = lock_path(config)
    if is_lock_held(daemon_lock):
        pid = read_lock_pid(daemon_lock)
        suffix = f" (pid {pid})" if pid is not None else ""
        console.print(
            f"[red]Forgeo {config.name!r} is already running{suffix}; "
            f"stop it with `forgeo stop`.[/red]"
        )
        return 1
    report = validate_config(config)
    if not report.healthy:
        console.print(render_report(config, report), soft_wrap=True)
        return 1
    extra_args = (
        ["--interval-minutes", str(args.interval_minutes)]
        if args.interval_minutes is not None
        else None
    )
    try:
        pid = start_daemon(config_path, config, extra_args=extra_args)
    except DaemonError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(
        f"[green]Forgeo {config.name!r} started in the background "
        f"(pid {pid}, interval {config.interval_minutes} min).[/green]"
    )
    return 0


def _cmd_start_foreground(args: argparse.Namespace) -> int:
    """Handle ``forgeo start -f``: the persistent scheduled worker."""
    prepared = _prepare_worker(args, register=True)
    if prepared is None:
        return 1
    _config_path, config, forgeo, lock = prepared

    async def _serve() -> None:
        daemon = ForgeoDaemon(
            config,
            forgeo,
            config_path=_config_path,
            forgeo_factory=_make_forgeo,
            interval_override=args.interval_minutes,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, daemon.stop)
        if hasattr(signal, "SIGHUP"):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal.SIGHUP, daemon.request_reload)
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


def _run_worker_with_lock(
    lock: Any, runner: Callable[[], Coroutine[Any, Any, int]]
) -> int:
    """Run an async worker to completion, closing the run lock on the way out.

    A ``KeyboardInterrupt`` is swallowed (the cycle is aborted, the lock is
    still released) and a :class:`BacklogUnavailableError` — an HTTP backlog
    that is simply down — is reported as an operational failure rather than
    a crash to trace back. ``runner`` runs in its own event loop and returns
    the command's exit code.
    """
    result = 1
    try:
        result = asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        logging.getLogger("forgeo.cli").error("Backlog unavailable: %s", exc)
        result = 1
    finally:
        lock.close()
    return result


def _run_worker_once(
    args: argparse.Namespace,
    runner: Callable[[Forgeo], Coroutine[Any, Any, int]],
) -> int:
    """Prepare the worker and run one cycle through ``runner``.

    Shared plumbing of ``forgeo once`` and ``forgeo run``: resolve the config,
    take the per-forgeo lock, run the optional update check, execute exactly
    one cycle via ``runner``, and release the lock on the way out.
    """
    prepared = _prepare_worker(args)
    if prepared is None:
        return 1
    _config_path, _config, forgeo, lock = prepared

    async def _execute() -> int:
        check_for_update(update_state_path(_config), print_fn=console.print)
        return await runner(forgeo)

    return _run_worker_with_lock(lock, _execute)


def cmd_once(args: argparse.Namespace) -> int:
    """Handle ``forgeo once``: run exactly one cycle and exit."""

    async def _cycle(forgeo: Forgeo) -> int:
        outcome = await forgeo.run_cycle()
        console.print(f"[green]Cycle finished: {outcome}[/green]")
        return 0

    return _run_worker_once(args, _cycle)


def cmd_run(args: argparse.Namespace) -> int:
    """Handle ``forgeo run``: run exactly one specific task and exit.

    Unlike ``forgeo once`` — which picks the oldest ``OPEN`` task — this
    executes the task named by ``--task`` immediately, for triage: rerun a
    ``FAILED`` task (after reopening it) or try a risky task right now. With
    ``--reopen`` a ``BLOCKED`` task is reopened (and a ``FAILED`` task
    retried through the retry path) first, so ``show -> edit -> reopen ->
    run`` collapses to ``edit -> run --reopen``. It
    reuses the same per-forgeo lock as the daemon and ``once``, so it never
    overlaps them; it refuses (exit 1) when the task does not exist or is
    not ``OPEN``.
    """
    task_id = args.task
    reopen = bool(getattr(args, "reopen", False))

    async def _one(forgeo: Forgeo) -> int:
        try:
            outcome = await forgeo.run_task_id(task_id, reopen=reopen)
        except TaskNotRunnableError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(f"[green]Cycle finished: {outcome}[/green]")
        return 0

    return _run_worker_once(args, _one)


_TASK_ID_RE = re.compile(r"^TASK-(\d+)$")


def _resolve_run_at(value: str | None) -> str | None:
    """Normalize a ``--run-at`` value to an ISO-8601 string.

    ``'now'`` (any case) means "due immediately", so a task jumps ahead of
    the oldest-first order on the next cycle — the terminal shortcut for
    "run this next" without delete/recreate. Anything else is passed
    through for :class:`Task` validation (which rejects it clearly when
    it is not ISO-8601).
    """
    if value is None:
        return None
    if value.strip().lower() == "now":
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat()
    return value.strip()


def _read_description_input(
    description: str | None, description_file: Path | None
) -> tuple[str | None, str | None]:
    """Resolve ``--description``/``--description-file`` to text.

    ``'-'`` (as the description or the file) reads stdin, so multiline
    specs pipe straight in (``echo ... | forgeo task add ...``) without
    shell-quoting pain. Returns ``(text, error)`` — exactly one is set.
    """
    if description is not None and description_file is not None:
        return None, "Pass either --description or --description-file, not both."
    if description is not None and description.strip() == "-":
        return sys.stdin.read(), None
    if description_file is not None and str(description_file) == "-":
        return sys.stdin.read(), None
    if description_file is not None:
        try:
            return Path(description_file).read_text(encoding="utf-8"), None
        except OSError as exc:
            return None, f"Could not read --description-file {description_file}: {exc}"
    return description, None


def _next_cli_task_id(tasks: list[Task]) -> str:
    """Next ``TASK-###`` id after the highest existing ``TASK-###`` id."""
    highest = 0
    for task in tasks:
        match = _TASK_ID_RE.match(task.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"TASK-{highest + 1:03d}"


def cmd_task_add(args: argparse.Namespace) -> int:
    """Handle ``forgeo task add``: create a new ``OPEN`` task and exit.

    Never starts an agent. The id auto-assigns as the next ``TASK-###``
    unless ``--id`` names one explicitly; duplicates are refused.
    On issue backlogs (GitHub/GitLab/Jira) the provider assigns the real
    id and the created task's id is reported.
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    title = args.title.strip()
    if not title:
        console.print("[red]--title must not be blank.[/red]")
        return 1
    raw_description = getattr(args, "description", None)
    description_text, desc_error = _read_description_input(
        raw_description, getattr(args, "description_file", None)
    )
    if desc_error is not None:
        console.print(f"[red]{desc_error}[/red]")
        return 1
    if description_text is None or not description_text.strip():
        console.print(
            "[red]--description must not be blank "
            "(or pass --description-file FILE / '-' for stdin).[/red]"
        )
        return 1
    description = description_text.strip()
    backlog = open_backlog(config)
    try:
        existing = asyncio.run(backlog.list_tasks())
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    if args.id is not None:
        task_id = args.id.strip()
        if not task_id:
            console.print("[red]--id must not be blank.[/red]")
            return 1
    else:
        task_id = _next_cli_task_id(existing)
    run_at_raw = _resolve_run_at(getattr(args, "run_at", None))
    try:
        task = Task(
            id=task_id,
            title=title,
            description=description,
            acceptance_criteria=list(args.acceptance or []),
            dependencies=list(args.depends_on or []),
            run_at=run_at_raw,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        console.print(f"[red]Invalid task: {exc}[/red]")
        return 1
    try:
        created = asyncio.run(backlog.create_task(task))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    console.print(f"[green]Created task {created.id} — {created.title}[/green]")
    console.print(
        f"[dim]Run it now with `forgeo run --task {created.id}` "
        f"or wait for the next cycle.[/dim]"
    )
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    """Handle ``forgeo task list``: print backlog tasks; never starts an agent."""
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    limit = args.limit
    if limit is not None and limit < 1:
        console.print("[red]--limit must be an integer >= 1.[/red]")
        return 1
    try:
        tasks = asyncio.run(open_backlog(config).list_tasks())
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    wanted = args.status
    if wanted is not None:
        tasks = [task for task in tasks if task.status is TaskStatus[wanted.upper()]]
        if not tasks:
            console.print(f"[yellow]No {wanted} tasks.[/yellow]")
            return 0
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        console.print("[yellow]No tasks yet.[/yellow]")
        console.print(
            "[yellow]Add one with `forgeo task add --title ... --description ...` "
            "or from the dashboard.[/yellow]"
        )
        return 0
    table = Table(title="Backlog tasks")
    table.add_column("Id")
    table.add_column("Status")
    table.add_column("Title", overflow="fold")
    for task in tasks:
        table.add_row(task.id, task.status.value, task.title)
    console.print(table)
    return 0


def render_task_detail(task: Task) -> str:
    """Render one task's full detail as plain text.

    Plain text (no Rich markup) so descriptions containing ``[...]`` print
    literally. Sections with no content are omitted, except ``description``
    which is always shown — ``task list`` only shows id/status/title, so
    this is the terminal equivalent of opening the task in the dashboard.
    """
    lines = [
        f"id: {task.id}",
        f"status: {task.status.value}",
        f"title: {task.title}",
        f"description: {task.description}",
    ]
    if task.acceptance_criteria:
        lines.append("acceptance criteria:")
        lines.extend(f"  - {criterion}" for criterion in task.acceptance_criteria)
    if task.dependencies:
        lines.append(f"dependencies: {', '.join(task.dependencies)}")
    if task.files_to_modify:
        lines.append(f"files: {', '.join(task.files_to_modify)}")
    lines.append(f"created: {task.created_at.isoformat()}")
    lines.append(f"updated: {task.updated_at.isoformat()}")
    if task.run_at is not None:
        lines.append(f"run_at: {task.run_at.isoformat()}")
    if task.blocked_count:
        lines.append(f"blocked_count: {task.blocked_count}")
    if task.retry_count:
        lines.append(f"retry_count: {task.retry_count}")
    if task.retries_left is not None:
        lines.append(f"retries_left: {task.retries_left}")
    if task.blocker_reason:
        lines.append("blocker reason:")
        lines.extend(f"  {line}" for line in task.blocker_reason)
    if task.failure_reason:
        lines.append("failure reason:")
        lines.extend(f"  {line}" for line in task.failure_reason)
    if task.agent_response:
        lines.append("agent response:")
        lines.extend(f"  {line}" for line in task.agent_response.splitlines())
    if task.review_branch:
        lines.append(f"review_branch: {task.review_branch}")
    if task.review_commit_sha:
        lines.append(f"review_commit: {task.review_commit_sha}")
    return "\n".join(lines)


def _task_show_hint(task: Task) -> str | None:
    """The most useful next step after showing ``task``, if any."""
    if task.status is TaskStatus.BLOCKED or task.status is TaskStatus.FAILED:
        return (
            f"hint: fix it with `forgeo task edit --task {task.id} "
            f"--description ...`, then `forgeo task reopen --task {task.id}` to retry"
        )
    if task.status is TaskStatus.REVIEW:
        return (
            f"hint: after merging, `forgeo task complete-review --task {task.id}`; "
            f"for rework, `forgeo task request-changes --task {task.id}`"
        )
    if task.status is TaskStatus.OPEN:
        return f"hint: run it now with `forgeo run --task {task.id}`"
    return None


def cmd_task_show(args: argparse.Namespace) -> int:
    """Handle ``forgeo task show``: print one task's full detail.

    Read-only; never starts an agent. Works with every provider via
    ``get_task``.
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    try:
        task = asyncio.run(open_backlog(config).get_task(args.task))
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    if task is None:
        console.print(f"[red]Unknown task: {args.task}.[/red]")
        return 1
    console.print(
        render_task_detail(task), markup=False, highlight=False, soft_wrap=True
    )
    hint = _task_show_hint(task)
    if hint is not None:
        console.print(f"[dim]{hint}[/dim]")
    return 0


def cmd_task_edit(args: argparse.Namespace) -> int:
    """Handle ``forgeo task edit``: update a task's editable fields in place.

    Never starts an agent. The terminal equivalent of editing the task in
    the dashboard — the missing step between ``task show`` (see why a task
    is ``BLOCKED``) and ``task reopen`` (retry it). ``--acceptance``,
    ``--depends-on`` and ``--files`` replace the whole list; the
    ``--clear-*`` flags empty one instead. ``--run-at now`` (or an
    ISO-8601 time) schedules the task — due tasks jump ahead of
    oldest-first order; ``--clear-run-at`` returns it to oldest-first.
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    updates: dict[str, Any] = {}
    if args.title is not None:
        if not args.title.strip():
            console.print("[red]--title must not be blank.[/red]")
            return 1
        updates["title"] = args.title.strip()
    raw_description = getattr(args, "description", None)
    raw_description_file = getattr(args, "description_file", None)
    if raw_description is not None or raw_description_file is not None:
        description_text, desc_error = _read_description_input(
            raw_description, raw_description_file
        )
        if desc_error is not None:
            console.print(f"[red]{desc_error}[/red]")
            return 1
        if description_text is None or not description_text.strip():
            console.print("[red]--description must not be blank.[/red]")
            return 1
        updates["description"] = description_text.strip()
    if args.acceptance is not None and args.clear_acceptance:
        console.print("[red]Pass either --acceptance or --clear-acceptance, not both.[/red]")
        return 1
    if args.depends_on is not None and args.clear_depends_on:
        console.print("[red]Pass either --depends-on or --clear-depends-on, not both.[/red]")
        return 1
    if args.files is not None and args.clear_files:
        console.print("[red]Pass either --files or --clear-files, not both.[/red]")
        return 1
    run_at = getattr(args, "run_at", None)
    clear_run_at = getattr(args, "clear_run_at", False)
    if run_at is not None and clear_run_at:
        console.print("[red]Pass either --run-at or --clear-run-at, not both.[/red]")
        return 1
    if args.acceptance is not None:
        updates["acceptance_criteria"] = list(args.acceptance)
    elif args.clear_acceptance:
        updates["acceptance_criteria"] = []
    if args.depends_on is not None:
        updates["dependencies"] = list(args.depends_on)
    elif args.clear_depends_on:
        updates["dependencies"] = []
    if args.files is not None:
        updates["files_to_modify"] = list(args.files)
    elif args.clear_files:
        updates["files_to_modify"] = []
    if run_at is not None:
        resolved_run_at = _resolve_run_at(run_at)
        if resolved_run_at is not None and not resolved_run_at.strip():
            console.print("[red]--run-at must not be blank.[/red]")
            return 1
        updates["run_at"] = resolved_run_at
    elif clear_run_at:
        updates["run_at"] = None
    if not updates:
        console.print(
            "[red]Nothing to update: pass --title, --description, "
            "--description-file, --acceptance, "
            "--depends-on, --files, --run-at, or a --clear-* flag.[/red]"
        )
        return 1
    backlog = open_backlog(config)
    try:
        updated = asyncio.run(backlog.update_task(args.task, updates))
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    except (ValueError, TypeError) as exc:
        console.print(f"[red]Invalid update: {exc}[/red]")
        return 1
    if updated is None:
        console.print(f"[red]Unknown task: {args.task}.[/red]")
        return 1
    console.print(f"[green]Updated task {updated.id}.[/green]")
    return 0


def cmd_task_reopen(args: argparse.Namespace) -> int:
    """Handle ``forgeo task reopen``: move a ``BLOCKED``/``FAILED`` task to ``OPEN``.

    Never starts an agent. ``BLOCKED`` tasks reopen directly; ``FAILED``
    tasks re-queue through the retry path. Tasks that are ``OPEN``,
    ``REVIEW`` or ``COMPLETED`` are refused with an explanation.
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    backlog = open_backlog(config)
    try:
        task = asyncio.run(backlog.get_task(args.task))
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    if task is None:
        console.print(f"[red]Unknown task: {args.task}.[/red]")
        return 1
    previous = task.status
    if previous is TaskStatus.OPEN:
        console.print(f"[yellow]Task {task.id} is already OPEN.[/yellow]")
        return 1
    if previous in (TaskStatus.COMPLETED, TaskStatus.REVIEW):
        console.print(
            f"[red]Task {task.id} is {previous.value} and cannot be reopened "
            "from the terminal.[/red]"
        )
        return 1
    try:
        if previous is TaskStatus.BLOCKED:
            updated = asyncio.run(backlog.reopen_task(task.id))
        else:
            updated = asyncio.run(backlog.retry_task(task.id))
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    if updated is None:
        console.print(f"[red]Could not reopen task {task.id}.[/red]")
        return 1
    console.print(
        f"[green]Reopened task {updated.id} (was {previous.value}) — now OPEN.[/green]"
    )
    return 0


def cmd_task_rm(args: argparse.Namespace) -> int:
    """Handle ``forgeo task rm``: delete a task from the backlog.

    Never starts an agent. The terminal equivalent of deleting the task
    in the dashboard — removes typos, duplicates, or tasks that will
    never be done without hand-editing JSON. Any status can be removed;
    on issue backlogs the provider closes the issue when a hard delete
    is not permitted. Warns when remaining tasks still list the removed
    id in their dependencies.
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    backlog = open_backlog(config)
    try:
        tasks = asyncio.run(backlog.list_tasks())
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    existing = next((task for task in tasks if task.id == args.task), None)
    if existing is None:
        console.print(f"[red]Unknown task: {args.task}.[/red]")
        return 1
    try:
        deleted = asyncio.run(backlog.delete_task(args.task))
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    if deleted is None:
        console.print(f"[red]Could not remove task {args.task}.[/red]")
        return 1
    console.print(f"[green]Removed task {deleted.id} — {deleted.title}[/green]")
    dependents = sorted(
        task.id for task in tasks if deleted.id in (task.dependencies or [])
    )
    if dependents:
        console.print(
            f"[yellow]Warning: {', '.join(dependents)} still "
            f"depend{'s' if len(dependents) == 1 else ''} on removed "
            f"{deleted.id}; update them with `forgeo task edit`.[/yellow]"
        )
    return 0


def _cmd_task_review_transition(
    args: argparse.Namespace, *, action: str, method: str, past: str
) -> int:
    """Shared plumbing for the ``REVIEW`` exit commands.

    Fetches the task first so non-``REVIEW`` tasks are refused with an
    explanation (mirroring the dashboard's ``400 only REVIEW ...`` guard)
    instead of silently transitioning. ``action`` names the command for
    messages, ``method`` the backlog method to call, ``past`` the success
    verb phrase.
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    backlog = open_backlog(config)
    try:
        task = asyncio.run(backlog.get_task(args.task))
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    if task is None:
        console.print(f"[red]Unknown task: {args.task}.[/red]")
        return 1
    if task.status is not TaskStatus.REVIEW:
        console.print(
            f"[red]Task {task.id} is {task.status.value}, not REVIEW: "
            f"`forgeo task {action} --task {task.id}` only applies to REVIEW tasks "
            f"(merge the branch first, then complete it).[/red]"
        )
        return 1
    try:
        updated = asyncio.run(getattr(backlog, method)(task.id))
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    if updated is None:
        console.print(f"[red]Could not update task {task.id}.[/red]")
        return 1
    console.print(f"[green]{past} task {updated.id} — now {updated.status.value}.[/green]")
    return 0


def cmd_task_complete_review(args: argparse.Namespace) -> int:
    """Handle ``forgeo task complete-review``: ``REVIEW`` → ``COMPLETED``.

    Never starts an agent. The terminal equivalent of the dashboard's
    Complete button — merge the review branch manually first, then mark
    the task done without opening the dashboard.
    """
    return _cmd_task_review_transition(
        args, action="complete-review", method="complete_review", past="Completed"
    )


def cmd_task_request_changes(args: argparse.Namespace) -> int:
    """Handle ``forgeo task request-changes``: ``REVIEW`` → ``OPEN``.

    Never starts an agent. The terminal equivalent of the dashboard's
    Request-changes button — sends the task back for rework without
    opening the dashboard.
    """
    return _cmd_task_review_transition(
        args, action="request-changes", method="request_changes", past="Sent back"
    )


_TASK_NEXT_SKIPPED_SHOWN = 10


def _next_skip_reason(
    tasks: list[Task], task: Task, *, now: Any, picked_id: str | None
) -> str:
    """One-line reason why ``task`` is not the next pick.

    Assumes ``task`` is ``OPEN`` and not the picked task. Names unmet
    dependencies first (they block the task regardless of schedule), then
    a future ``run_at``, and falls back to queue order (runnable but not
    oldest / not due first).
    """
    unmet = unsatisfied_dependencies(tasks, task)
    if unmet:
        detail = ", ".join(f"{dep['id']} ({dep['status']})" for dep in unmet)
        return f"waiting on {detail}"
    if task.run_at is not None and task.run_at > now:
        return f"scheduled for {task.run_at.isoformat()}"
    if picked_id is not None:
        return f"queued behind {picked_id}"
    return "not runnable"


def render_task_next(tasks: list[Task], *, now: Any = None) -> str:
    """Explain which task the scheduler would pick next, and why not the rest.

    Read-only counterpart of :func:`forgeo.backlog.oldest_open_task` plus the
    cycle's BLOCKED-first rule from :meth:`forgeo.forgeo.Forgeo._run_cycle`:
    while any task is ``BLOCKED`` the next cycle renders the blocker file
    instead of running anything, so ``next`` reports that pause first. Plain
    text (no Rich markup) so ids and reasons print literally.
    """
    from datetime import UTC, datetime

    if now is None:
        now = datetime.now(UTC)
    blocked = sorted(
        (task for task in tasks if task.status is TaskStatus.BLOCKED),
        key=lambda task: task.created_at,
    )
    if blocked:
        ids = ", ".join(task.id for task in blocked)
        lines = [
            f"next: (paused) — {len(blocked)} BLOCKED task(s): {ids}",
            "why: forgeo renders BLOCKER.md while any task is BLOCKED",
            "hint: resolve them, then `forgeo task reopen --task <id>` to retry",
        ]
        return "\n".join(lines)
    picked = oldest_open_task(tasks, now=now)
    open_tasks = [task for task in tasks if task.status is TaskStatus.OPEN]
    if picked is not None:
        if picked.run_at is not None:
            why = (
                f"due since {picked.run_at.isoformat()} "
                "(overdue run_at tasks run before oldest-first order)"
            )
        else:
            why = "oldest runnable OPEN task"
        lines = [f"next: {picked.id} — {picked.title}", f"why: {why}"]
        skipped = [task for task in open_tasks if task.id != picked.id]
        for task in skipped[:_TASK_NEXT_SKIPPED_SHOWN]:
            reason = _next_skip_reason(tasks, task, now=now, picked_id=picked.id)
            lines.append(f"skipped: {task.id} — {task.title} ({reason})")
        hidden = len(skipped) - min(len(skipped), _TASK_NEXT_SKIPPED_SHOWN)
        if hidden > 0:
            lines.append(f"... +{hidden} more (see `forgeo task list`)")
        lines.append(
            f"hint: run it now with `forgeo run --task {picked.id}` "
            f"or see `forgeo task show --task {picked.id}`"
        )
        return "\n".join(lines)
    if not open_tasks:
        return "\n".join(
            [
                "next: (none) — no OPEN tasks",
                "why: nothing runnable, so the next cycle runs a refactoring pass",
                "hint: add one with `forgeo task add --title ... --description ...`",
            ]
        )
    lines = [f"next: (none) — {len(open_tasks)} OPEN task(s), none runnable"]
    for task in sorted(open_tasks, key=lambda task: task.created_at)[
        :_TASK_NEXT_SKIPPED_SHOWN
    ]:
        reason = _next_skip_reason(tasks, task, now=now, picked_id=None)
        lines.append(f"waiting: {task.id} — {task.title} ({reason})")
    hidden = len(open_tasks) - min(len(open_tasks), _TASK_NEXT_SKIPPED_SHOWN)
    if hidden > 0:
        lines.append(f"... +{hidden} more (see `forgeo task list`)")
    future = sorted(
        task.run_at
        for task in open_tasks
        if task.run_at is not None
        and task.run_at > now
        and not unsatisfied_dependencies(tasks, task)
    )
    if future:
        lines.append(f"earliest scheduled: {future[0].isoformat()}")
    lines.append("hint: see `forgeo task show --task <id>` for full detail")
    return "\n".join(lines)


def cmd_task_next(args: argparse.Namespace) -> int:
    """Handle ``forgeo task next``: explain the scheduler's next pick.

    Read-only; never starts an agent. Works with every provider via
    ``list_tasks`` and mirrors the cycle's pick (BLOCKED-first, then the
    oldest runnable ``OPEN`` task with overdue ``run_at`` first).
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    try:
        tasks = asyncio.run(open_backlog(config).list_tasks())
    except BacklogUnavailableError as exc:
        console.print(f"[red]Backlog unavailable: {exc}[/red]")
        return 1
    console.print(render_task_next(tasks), markup=False, highlight=False, soft_wrap=True)
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """Handle ``forgeo task``: the task-management subcommand group."""
    action = args.task_action
    if action == "add":
        return cmd_task_add(args)
    if action == "list":
        return cmd_task_list(args)
    if action == "next":
        return cmd_task_next(args)
    if action == "show":
        return cmd_task_show(args)
    if action == "edit":
        return cmd_task_edit(args)
    if action == "reopen":
        return cmd_task_reopen(args)
    if action == "rm":
        return cmd_task_rm(args)
    if action == "complete-review":
        return cmd_task_complete_review(args)
    if action == "request-changes":
        return cmd_task_request_changes(args)
    build_parser().print_help()
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


_STATUS_REASON_SHOWN = 3
_STATUS_REASON_CHARS = 100


def _reason_first_line(reason: list[str]) -> str:
    """First non-blank line of a blocker/failure reason, truncated for status."""
    for line in reason:
        stripped = " ".join(line.split())
        if stripped:
            if len(stripped) > _STATUS_REASON_CHARS:
                return stripped[: _STATUS_REASON_CHARS - 1] + "…"
            return stripped
    return ""


def _status_reason_lines(tasks: list[Task], status: TaskStatus) -> list[str]:
    """One ``status: ID — title — reason`` line per task, oldest first.

    Shows at most ``_STATUS_REASON_SHOWN`` tasks, then a ``+N more`` line so
    ``forgeo status`` stays readable with a large blocked/failed backlog.
    """
    matching = sorted(
        (task for task in tasks if task.status is status),
        key=lambda task: task.created_at,
    )
    lines: list[str] = []
    for task in matching[:_STATUS_REASON_SHOWN]:
        reason = (
            task.blocker_reason if status is TaskStatus.BLOCKED else task.failure_reason
        )
        first = _reason_first_line(reason)
        suffix = f" — {first}" if first else ""
        lines.append(f"{status.value.lower()}: {task.id} — {task.title}{suffix}")
    hidden = len(matching) - len(lines)
    if hidden > 0:
        lines.append(f"... +{hidden} more {status.value.lower()} (see web console)")
    return lines


def _next_action(
    tasks: list[Task], *, daemon_running: bool, oldest_open: Task | None
) -> str | None:
    """The single most useful next step for ``forgeo status``, if any needs one."""
    counts = backlog_status_counts(tasks)
    if counts.get("BLOCKED", 0) > 0:
        return (
            "action: resolve BLOCKED tasks above (BLOCKER.md / `forgeo web`), "
            "then `forgeo task reopen --task <id>`"
        )
    if counts.get("FAILED", 0) > 0:
        return (
            "action: inspect FAILED tasks above, "
            "then `forgeo task reopen --task <id>` to retry"
        )
    if oldest_open is not None and not daemon_running:
        open_count = counts.get("OPEN", 0)
        plural = "s" if open_count != 1 else ""
        return f"action: run `forgeo start` to process {open_count} OPEN task{plural}"
    if oldest_open is None:
        if daemon_running:
            return "action: backlog empty — add tasks via `forgeo task add` or wait for refactor cycle"
        return "action: backlog empty — add tasks via `forgeo task add`, then run `forgeo start`"
    return None


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
    lines = [
        f"name: {config.name}",
        f"repo: {config.repo}",
        f"interval: {config.interval_minutes} min",
        f"branch: {config.branch}",
        f"backlog: {count_text}",
        f"next: {next_text}",
        f"daemon: {daemon_text}",
        f"last outcome: {outcome_text}",
    ]
    waiting = _waiting_hint(tasks)
    if waiting is not None:
        lines.append(waiting)
    lines.extend(_status_reason_lines(tasks, TaskStatus.BLOCKED))
    lines.extend(_status_reason_lines(tasks, TaskStatus.FAILED))
    action = _next_action(tasks, daemon_running=daemon_running, oldest_open=nxt)
    if action is not None:
        lines.append(action)
    return "\n".join(lines)


def _waiting_hint(tasks: list[Task]) -> str | None:
    """A status line naming the oldest OPEN task that is not yet runnable and
    the dependency ids keeping it waiting, or ``None`` when there is none."""
    open_tasks = [
        task for task in tasks if task.status is TaskStatus.OPEN
    ]
    if not open_tasks:
        return None
    oldest = min(open_tasks, key=lambda task: task.created_at)
    unmet = unsatisfied_dependencies(tasks, oldest)
    if not unmet:
        return None
    detail = ", ".join(f"{dep['id']} ({dep['status']})" for dep in unmet)
    return f"waiting on: {oldest.id} (needs COMPLETED: {detail})"


def _print_config_load_error(
    config_path: Path, exc: yaml.YAMLError | ValidationError
) -> None:
    """Print a user-facing error for a config that failed to load or validate."""
    if isinstance(exc, yaml.YAMLError):
        console.print(
            f"[red]Config file {config_path} is not valid YAML:[/red]",
            soft_wrap=True,
        )
        console.print(str(exc), soft_wrap=True)
        return
    console.print(
        f"[red]Config file {config_path} is invalid:[/red]",
        soft_wrap=True,
    )
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        console.print(f"[red]- {loc}: {error['msg']}[/red]", soft_wrap=True)


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


def _positive_log_lines(value: str) -> int:
    """Parse a positive ``--lines`` count for ``forgeo logs``."""
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("lines must be an integer >= 1") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("lines must be an integer >= 1")
    return count


def _follow_log(path: Path, *, poll_interval: float = 0.25) -> int:
    """Print lines appended to ``path`` until interrupted (``tail -f``).

    Seeks to the end first (the caller already printed the tail), then
    polls for new content. A truncated file (log rotation) restarts from
    the beginning. ``KeyboardInterrupt`` (Ctrl-C) stops cleanly with exit 0.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            while True:
                line = handle.readline()
                if line:
                    console.print(
                        line.rstrip("\r\n"),
                        markup=False,
                        highlight=False,
                        soft_wrap=True,
                    )
                    position = handle.tell()
                    continue
                try:
                    if path.stat().st_size < position:
                        handle.seek(0)
                        position = 0
                        continue
                except OSError:
                    pass
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        console.print(f"[red]Could not read log file {path}: {exc}[/red]")
        return 1
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Handle ``forgeo logs``: print the tail of the log file and exit.

    Read-only; never starts an agent. Prints the last ``--lines`` lines of
    ``log_file`` (``markup=False`` so ``[...]`` in agent output is never
    treated as Rich markup); with ``--follow`` it keeps printing appended
    lines like ``tail -f`` until Ctrl-C.
    """
    resolved = _resolve_existing_config(args)
    if resolved is None:
        return 1
    _config_path, config = resolved
    log_path = Path(config.log_file)
    if not log_path.exists():
        console.print(
            f"[yellow]No log file yet at {log_path} — "
            f"run `forgeo start` or `forgeo once` first.[/yellow]"
        )
        return 0
    count = getattr(args, "lines", DEFAULT_LOG_LINES)
    for line in tail_lines(log_path, count):
        console.print(line, markup=False, highlight=False, soft_wrap=True)
    if getattr(args, "follow", False):
        return _follow_log(log_path)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Handle ``forgeo validate``: read-only dry run; never starts an agent.

    Loads and validates the config, then checks the repository, branch and
    remote resolution, the backlog, the agent command and the run lock state.
    Never invokes the agent and makes no writes; exits non-zero when any
    problem is found.
    """
    config_path = _resolved_config_path(args)
    if config_path is None:
        return 1
    if not config_path.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        return 1
    try:
        config = load_config(config_path)
    except (yaml.YAMLError, ValidationError) as exc:
        _print_config_load_error(config_path, exc)
        return 1
    report = validate_config(config)
    console.print(render_report(config, report), soft_wrap=True)
    return 0 if report.healthy else 1


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
    dashboard through its host-global lock file. ``--token`` (optional)
    turns on bearer auth on every ``/api/*`` route.
    """
    from forgeo.central import run_foreground

    action = getattr(args, "web_action", None)
    if action == "stop":
        return cmd_web_stop(args)
    if action == "status":
        return cmd_web_status(args)
    if args.detach:
        return cmd_web_detach(args)
    return run_foreground(host=args.host, port=args.port, token=args.token)


def cmd_web_detach(args: argparse.Namespace) -> int:
    """Handle ``forgeo web -d``: run the dashboard as a background process.

    Refuses while another live dashboard holds the host-global lock; a stale
    lock (dead recorded PID) is taken over with a warning, matching the
    daemon's lock behavior. The bearer token is resolved up front and
    persisted to ``web.toml`` so the detached child picks it up; a freshly
    generated token is printed to the console exactly once.
    """
    from forgeo.central import WebLock, WebLockError, resolve_web_token, start_web_detached

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
    token, generated = resolve_web_token(args.token)
    try:
        pid = start_web_detached(args.host, args.port, args.timeout)
    except WebLockError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print(
        f"[green]Forgeo central dashboard started in the background "
        f"(pid {pid}, http://{args.host}:{args.port}).[/green]"
    )
    if generated:
        console.print(
            f"[bold]Web token:[/bold] {token}\n"
            f"[dim]Required on every /api/* request; saved to web.toml.[/dim]"
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


# ------------------------------------------------------------------ #
# Auth (GitHub OAuth / browser login)                                 #
# ------------------------------------------------------------------ #


def _callback_port(value: str) -> int:
    """Parse a valid TCP port for the OAuth loopback callback."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("callback port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("callback port must be between 1 and 65535")
    return port


def _auth_config_path(args: argparse.Namespace) -> Path | None:
    """Resolve an explicit auth config or the project-local default."""
    config_path = getattr(args, "config", None)
    if config_path is not None:
        return Path(config_path)
    default = Path("forgeo.yaml")
    return default if default.exists() else None


_OAUTH_PARAM_DEFAULTS: dict[str, dict[str, str]] = {
    "github": {
        "api_base": "https://api.github.com",
        "flow": "device",
        "scope": "repo",
        "missing": (
            "GitHub OAuth client_id is required. Provide --client-id or set github.auth.oauth.client_id "
            "in forgeo.yaml (create an OAuth App at https://github.com/settings/developers)."
        ),
    },
    "gitlab": {
        "api_base": "https://gitlab.com",
        "flow": "browser",
        "scope": "api",
        "missing": (
            "GitLab OAuth client_id is required. Provide --client-id or set gitlab.auth.oauth.client_id "
            "in forgeo.yaml (create an Application at https://gitlab.com/-/profile/applications)."
        ),
    },
    "jira": {
        "api_base": "https://jira.example.com",
        "flow": "browser",
        "scope": "offline_access read:jira-user read:jira-work",
        "missing": (
            "Jira OAuth client_id is required. Provide --client-id or set jira.auth.oauth.client_id "
            "in forgeo.yaml (create an App at https://developer.atlassian.com/console/myapps/)."
        ),
    },
}


def _resolve_oauth_params(provider: str, args: argparse.Namespace) -> dict[str, Any] | None:
    """Resolve OAuth params for ``provider`` from CLI args and optional config.

    Returns a dict with ``api_base``, ``client_id``, ``flow``, ``scope``,
    ``token_file``, ``callback_port`` (plus ``cloud_id`` for Jira), or ``None``
    after printing an error.
    """
    from pathlib import Path as _P

    from forgeo.config import load_config

    defaults = _OAUTH_PARAM_DEFAULTS[provider]
    config_path = _auth_config_path(args)
    config = None
    if config_path is not None and _P(config_path).exists():
        try:
            config = load_config(config_path)
        except Exception as exc:  # noqa: BLE001 - user-facing error
            console.print(f"[red]Could not load config {config_path}: {exc}[/red]")
            return None

    provider_cfg = getattr(config, provider, None) if config is not None else None
    api_base = getattr(args, "api_base", None)
    # config.backlog is the API base for issue providers
    cfg_backlog = config.backlog if config is not None else None
    if api_base is None and provider_cfg is not None and isinstance(cfg_backlog, str):
        api_base = cfg_backlog
    if api_base is None:
        api_base = defaults["api_base"]

    client_id = getattr(args, "client_id", None)
    flow = getattr(args, "flow", None)
    scope = getattr(args, "scope", None)
    token_file_arg = getattr(args, "token_file", None)
    callback_port = getattr(args, "callback_port", None)
    cloud_id = getattr(args, "cloud_id", None) if provider == "jira" else None

    oauth = (
        provider_cfg.auth.oauth
        if provider_cfg is not None and provider_cfg.auth is not None
        else None
    )
    if oauth is not None:
        if client_id is None:
            client_id = oauth.client_id
        if flow is None:
            flow = oauth.flow
        if scope is None:
            scope = oauth.scope or defaults["scope"]
        if token_file_arg is None and oauth.token_file is not None:
            token_file_arg = _P(oauth.token_file)
        if callback_port is None:
            callback_port = oauth.callback_port
        if provider == "jira" and cloud_id is None:
            cloud_id = oauth.cloud_id

    if client_id is None or not str(client_id).strip():
        console.print(f"[red]{defaults['missing']}[/red]")
        return None

    if flow is None:
        flow = defaults["flow"]
    if scope is None:
        scope = defaults["scope"]
    if provider == "github":
        from forgeo.oauth_github import github_default_token_path as _default_token_path
    elif provider == "gitlab":
        from forgeo.oauth_gitlab import gitlab_default_token_path as _default_token_path
    else:
        from forgeo.oauth_jira import jira_default_token_path as _default_token_path
    token_file = _P(token_file_arg).expanduser() if token_file_arg is not None else _default_token_path(api_base)
    resolved: dict[str, Any] = {
        "api_base": api_base,
        "client_id": str(client_id).strip(),
        "flow": str(flow),
        "scope": str(scope),
        "token_file": token_file,
        "callback_port": callback_port,
    }
    if provider == "jira":
        resolved["cloud_id"] = cloud_id
    return resolved


def _resolve_github_auth_params(
    args: argparse.Namespace,
) -> tuple[str, str, str, str, Path, int | None] | None:
    """Resolve GitHub OAuth params from CLI args and optional config.

    Returns (api_base, client_id, flow, scope, token_file, callback_port) or None after
    printing an error.
    """
    resolved = _resolve_oauth_params("github", args)
    if resolved is None:
        return None
    return (
        resolved["api_base"],
        resolved["client_id"],
        resolved["flow"],
        resolved["scope"],
        resolved["token_file"],
        resolved["callback_port"],
    )


def _resolve_gitlab_auth_params(
    args: argparse.Namespace,
) -> tuple[str, str, str, str, Path, int | None] | None:
    """Resolve GitLab OAuth params."""
    resolved = _resolve_oauth_params("gitlab", args)
    if resolved is None:
        return None
    return (
        resolved["api_base"],
        resolved["client_id"],
        resolved["flow"],
        resolved["scope"],
        resolved["token_file"],
        resolved["callback_port"],
    )


def _resolve_jira_auth_params(
    args: argparse.Namespace,
) -> tuple[str, str, str, str, Path, int | None, str | None] | None:
    """Resolve Jira OAuth params."""
    resolved = _resolve_oauth_params("jira", args)
    if resolved is None:
        return None
    return (
        resolved["api_base"],
        resolved["client_id"],
        resolved["flow"],
        resolved["scope"],
        resolved["token_file"],
        resolved["callback_port"],
        resolved["cloud_id"],
    )


def _resolve_client_secret(args: argparse.Namespace, provider: str) -> str | None:
    """Read the OAuth client secret from the env var named in the config, if any."""
    from forgeo.config import load_config

    try:
        cfg_path = _auth_config_path(args)
        if cfg_path and cfg_path.exists():
            cfg = load_config(cfg_path)
            provider_cfg = getattr(cfg, provider, None)
            oauth = (
                provider_cfg.auth.oauth
                if provider_cfg is not None and provider_cfg.auth is not None
                else None
            )
            if oauth is not None and oauth.client_secret_env:
                import os

                return os.environ.get(oauth.client_secret_env)
    except Exception:
        pass
    return None


def cmd_auth(args: argparse.Namespace) -> int:
    action = getattr(args, "auth_action", None)
    if action == "login":
        return cmd_auth_login(args)
    if action == "status":
        return cmd_auth_status(args)
    if action == "logout":
        return cmd_auth_logout(args)
    build_parser().print_help()
    return 0


def cmd_auth_login(args: argparse.Namespace) -> int:
    """Handle ``forgeo auth login``: run OAuth flow and store token."""
    provider = getattr(args, "provider", "github")
    if provider == "gitlab":
        from forgeo.oauth_gitlab import (
            GitlabOAuthError,
            GitlabTokenStore,
            gitlab_oauth_base,
            run_browser_flow,
            run_device_flow,
        )

        gitlab_params = _resolve_gitlab_auth_params(args)
        if gitlab_params is None:
            return 1
        api_base, client_id, flow, scope, token_file, callback_port = gitlab_params
        oauth_base = gitlab_oauth_base(api_base)
        console.print(f"[bold]GitLab auth[/bold]: provider=gitlab api_base={api_base} oauth_base={oauth_base} flow={flow}")
        # Resolve client_secret if configured
        client_secret = _resolve_client_secret(args, "gitlab")
        try:
            if flow == "browser":
                token_data = run_browser_flow(
                    client_id,
                    oauth_base,
                    scope,
                    client_secret=client_secret,
                    open_browser=not getattr(args, "no_open_browser", False),
                    callback_port=callback_port,
                )
            else:
                token_data = run_device_flow(client_id, oauth_base, scope, open_browser=not getattr(args, "no_open_browser", False))
        except GitlabOAuthError as exc:
            console.print(f"[red]GitLab login failed: {exc}[/red]")
            return 1
        except KeyboardInterrupt:
            console.print("[yellow]Login cancelled.[/yellow]")
            return 130
        try:
            store = GitlabTokenStore(path=token_file, api_base=api_base)
            store.save(token_data)
            console.print(f"[green]GitLab token saved to {store.path} (0600).[/green]")
            console.print(f"[dim]Token type {token_data.get('token_type','Bearer')} scope {token_data.get('scope','')} [/dim]")
            console.print("[green]Login succeeded. Validate with `forgeo validate`.[/green]")
        except OSError as exc:
            console.print(f"[red]Could not save token to {token_file}: {exc}[/red]")
            return 1
        return 0
    if provider == "jira":
        from forgeo.oauth_jira import (
            JiraOAuthError,
            JiraTokenStore,
        )
        from forgeo.oauth_jira import (
            run_browser_flow as run_jira_browser_flow,
        )

        jira_params = _resolve_jira_auth_params(args)
        if jira_params is None:
            return 1
        api_base, client_id, flow, scope, token_file, callback_port, cloud_id = jira_params
        console.print(f"[bold]Jira auth[/bold]: provider=jira api_base={api_base} flow={flow}")
        if flow != "browser":
            console.print("[red]Jira OAuth supports browser flow only; use --flow browser.[/red]")
            return 2
        # Resolve client_secret
        client_secret = _resolve_client_secret(args, "jira")
        try:
            token_data = run_jira_browser_flow(
                client_id,
                None,
                scope,
                client_secret=client_secret,
                cloud_id=cloud_id,
                open_browser=not getattr(args, "no_open_browser", False),
                callback_port=callback_port,
            )
        except JiraOAuthError as exc:
            console.print(f"[red]Jira login failed: {exc}[/red]")
            return 1
        except KeyboardInterrupt:
            console.print("[yellow]Login cancelled.[/yellow]")
            return 130
        try:
            store = JiraTokenStore(path=token_file, api_base=api_base)  # type: ignore[assignment]
            store.save(token_data)
            console.print(f"[green]Jira token saved to {store.path} (0600).[/green]")
            if token_data.get("cloud_id"):
                console.print(f"[dim]Cloud ID {token_data['cloud_id']} [/dim]")
            console.print("[green]Login succeeded. Validate with `forgeo validate`.[/green]")
        except OSError as exc:
            console.print(f"[red]Could not save token to {token_file}: {exc}[/red]")
            return 1
        return 0
    # default github
    from forgeo.oauth_github import (
        GithubOAuthError,
        GithubTokenStore,
        github_oauth_base,
        run_browser_flow,
        run_device_flow,
    )

    github_params = _resolve_github_auth_params(args)
    if github_params is None:
        return 1
    api_base, client_id, flow, scope, token_file, callback_port = github_params
    oauth_base = github_oauth_base(api_base)
    console.print(f"[bold]GitHub auth[/bold]: provider=github api_base={api_base} oauth_base={oauth_base} flow={flow}")

    try:
        if flow == "browser":
            # Resolve client_secret for confidential apps
            client_secret = _resolve_client_secret(args, "github")
            token_data = run_browser_flow(
                client_id,
                oauth_base,
                scope,
                client_secret=client_secret,
                open_browser=not getattr(args, "no_open_browser", False),
                callback_port=callback_port,
            )
        else:
            token_data = run_device_flow(client_id, oauth_base, scope, open_browser=not getattr(args, "no_open_browser", False))
    except GithubOAuthError as exc:
        console.print(f"[red]GitHub login failed: {exc}[/red]")
        return 1
    except KeyboardInterrupt:
        console.print("[yellow]Login cancelled.[/yellow]")
        return 130

    # Persist
    try:
        store = GithubTokenStore(path=token_file, api_base=api_base)  # type: ignore[assignment]
        store.save(token_data)
        console.print(f"[green]GitHub token saved to {store.path} (0600).[/green]")
        console.print(f"[dim]Token type {token_data.get('token_type','Bearer')} scope {token_data.get('scope','')} [/dim]")
        console.print("[green]Login succeeded. Validate with `forgeo validate`.[/green]")
    except OSError as exc:
        console.print(f"[red]Could not save token to {token_file}: {exc}[/red]")
        return 1
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    """Handle ``forgeo auth status``: show token presence/expiry."""
    provider = getattr(args, "provider", "github")
    if provider == "gitlab":
        from forgeo.oauth_gitlab import GitlabTokenStore

        api_base = getattr(args, "api_base", None)
        token_file_arg = getattr(args, "token_file", None)
        config_path = _auth_config_path(args)
        if token_file_arg is None and config_path is not None:
            from pathlib import Path as _P

            from forgeo.config import load_config

            try:
                cfg = load_config(_P(config_path))
                if cfg.gitlab is not None and cfg.gitlab.auth.oauth is not None and cfg.gitlab.auth.oauth.token_file is not None:
                    token_file_arg = _P(cfg.gitlab.auth.oauth.token_file)
                if api_base is None and isinstance(cfg.backlog, str):
                    api_base = cfg.backlog
            except Exception:
                pass
        if api_base is None:
            api_base = "https://gitlab.com"
        store = GitlabTokenStore(path=token_file_arg, api_base=api_base) if token_file_arg is not None else GitlabTokenStore(api_base=api_base)
        data = store.load()
        if data is None:
            console.print(f"[yellow]No GitLab token found at {store.path}.[/yellow]", soft_wrap=True)
            console.print("[dim]Run `forgeo auth login --provider gitlab --client-id <id>` .[/dim]")
            return 1
        token = str(data.get("access_token", ""))
        masked = token[:4] + "…" + token[-4:] if len(token) > 8 else "****"
        expires = data.get("expires_in")
        scope = data.get("scope") or ""
        console.print(f"[green]Token found[/green] at {store.path}", soft_wrap=True)
        console.print(f"  token: {masked}")
        if scope:
            console.print(f"  scope: {scope}")
        if isinstance(expires, int | float):
            console.print(f"  expires_in: {expires}s")
        else:
            console.print("  expires: never")
        return 0
    if provider == "jira":
        from forgeo.oauth_jira import JiraTokenStore

        api_base = getattr(args, "api_base", None)
        token_file_arg = getattr(args, "token_file", None)
        config_path = _auth_config_path(args)
        if token_file_arg is None and config_path is not None:
            from pathlib import Path as _P

            from forgeo.config import load_config

            try:
                cfg = load_config(_P(config_path))
                if cfg.jira is not None and cfg.jira.auth.oauth is not None and cfg.jira.auth.oauth.token_file is not None:
                    token_file_arg = _P(cfg.jira.auth.oauth.token_file)
                if api_base is None and isinstance(cfg.backlog, str):
                    api_base = cfg.backlog
            except Exception:
                pass
        if api_base is None:
            api_base = "https://jira.example.com"
        store = JiraTokenStore(path=token_file_arg, api_base=api_base) if token_file_arg is not None else JiraTokenStore(api_base=api_base)  # type: ignore[assignment]
        data = store.load()
        if data is None:
            console.print(f"[yellow]No Jira token found at {store.path}.[/yellow]", soft_wrap=True)
            console.print("[dim]Run `forgeo auth login --provider jira --client-id <id>` .[/dim]")
            return 1
        token = str(data.get("access_token", ""))
        masked = token[:4] + "…" + token[-4:] if len(token) > 8 else "****"
        expires = data.get("expires_in")
        scope = data.get("scope") or ""
        cloud_id = data.get("cloud_id") or ""
        console.print(f"[green]Token found[/green] at {store.path}", soft_wrap=True)
        console.print(f"  token: {masked}")
        if cloud_id:
            console.print(f"  cloud_id: {cloud_id}")
        if scope:
            console.print(f"  scope: {scope}")
        if isinstance(expires, int | float):
            console.print(f"  expires_in: {expires}s")
        else:
            console.print("  expires: never")
        return 0
    from forgeo.oauth_github import GithubTokenStore

    api_base = getattr(args, "api_base", None)
    token_file_arg = getattr(args, "token_file", None)
    config_path = _auth_config_path(args)
    if token_file_arg is None and config_path is not None:
        from pathlib import Path as _P

        from forgeo.config import load_config

        try:
            cfg = load_config(_P(config_path))
            if cfg.github is not None and cfg.github.auth.oauth is not None and cfg.github.auth.oauth.token_file is not None:
                token_file_arg = _P(cfg.github.auth.oauth.token_file)
            if api_base is None and isinstance(cfg.backlog, str):
                api_base = cfg.backlog
        except Exception:
            pass
    if api_base is None:
        api_base = "https://api.github.com"
    store = GithubTokenStore(path=token_file_arg, api_base=api_base) if token_file_arg is not None else GithubTokenStore(api_base=api_base)  # type: ignore[assignment]
    data = store.load()
    if data is None:
        console.print(f"[yellow]No GitHub token found at {store.path}.[/yellow]", soft_wrap=True)
        console.print("[dim]Run `forgeo auth login --provider github --client-id <id>` .[/dim]")
        return 1
    token = str(data.get("access_token", ""))
    masked = token[:4] + "…" + token[-4:] if len(token) > 8 else "****"
    expires = data.get("expires_in")
    scope = data.get("scope") or data.get("scope", "")
    console.print(f"[green]Token found[/green] at {store.path}", soft_wrap=True)
    console.print(f"  token: {masked}")
    if scope:
        console.print(f"  scope: {scope}")
    if isinstance(expires, int | float):
        console.print(f"  expires_in: {expires}s")
    else:
        console.print("  expires: never (GitHub classic token)")
    return 0


def cmd_auth_logout(args: argparse.Namespace) -> int:
    """Handle ``forgeo auth logout``: delete stored token."""
    provider = getattr(args, "provider", "github")
    if provider == "gitlab":
        from forgeo.oauth_gitlab import GitlabTokenStore

        api_base = getattr(args, "api_base", None)
        token_file_arg = getattr(args, "token_file", None)
        config_path = _auth_config_path(args)
        if token_file_arg is None and config_path is not None:
            from pathlib import Path as _P

            from forgeo.config import load_config

            try:
                cfg = load_config(_P(config_path))
                if cfg.gitlab is not None and cfg.gitlab.auth.oauth is not None and cfg.gitlab.auth.oauth.token_file is not None:
                    token_file_arg = _P(cfg.gitlab.auth.oauth.token_file)
                if api_base is None and isinstance(cfg.backlog, str):
                    api_base = cfg.backlog
            except Exception:
                pass
        if api_base is None:
            api_base = "https://gitlab.com"
        store = GitlabTokenStore(path=token_file_arg, api_base=api_base) if token_file_arg is not None else GitlabTokenStore(api_base=api_base)
        if store.clear():
            console.print(f"[green]Removed token at {store.path}.[/green]")
            return 0
        console.print(f"[yellow]No token to remove at {store.path}.[/yellow]")
        return 1
    if provider == "jira":
        from forgeo.oauth_jira import JiraTokenStore

        api_base = getattr(args, "api_base", None)
        token_file_arg = getattr(args, "token_file", None)
        config_path = _auth_config_path(args)
        if token_file_arg is None and config_path is not None:
            from pathlib import Path as _P

            from forgeo.config import load_config

            try:
                cfg = load_config(_P(config_path))
                if cfg.jira is not None and cfg.jira.auth.oauth is not None and cfg.jira.auth.oauth.token_file is not None:
                    token_file_arg = _P(cfg.jira.auth.oauth.token_file)
                if api_base is None and isinstance(cfg.backlog, str):
                    api_base = cfg.backlog
            except Exception:
                pass
        if api_base is None:
            api_base = "https://jira.example.com"
        store = JiraTokenStore(path=token_file_arg, api_base=api_base) if token_file_arg is not None else JiraTokenStore(api_base=api_base)  # type: ignore[assignment]
        if store.clear():
            console.print(f"[green]Removed token at {store.path}.[/green]")
            return 0
        console.print(f"[yellow]No token to remove at {store.path}.[/yellow]")
        return 1
    from forgeo.oauth_github import GithubTokenStore

    api_base = getattr(args, "api_base", None)
    token_file_arg = getattr(args, "token_file", None)
    config_path = _auth_config_path(args)
    if token_file_arg is None and config_path is not None:
        from pathlib import Path as _P

        from forgeo.config import load_config

        try:
            cfg = load_config(_P(config_path))
            if cfg.github is not None and cfg.github.auth.oauth is not None and cfg.github.auth.oauth.token_file is not None:
                token_file_arg = _P(cfg.github.auth.oauth.token_file)
            if api_base is None and isinstance(cfg.backlog, str):
                api_base = cfg.backlog
        except Exception:
            pass
    if api_base is None:
        api_base = "https://api.github.com"
    store = GithubTokenStore(path=token_file_arg, api_base=api_base) if token_file_arg is not None else GithubTokenStore(api_base=api_base)  # type: ignore[assignment]
    if store.clear():
        console.print(f"[green]Removed token at {store.path}.[/green]")
        return 0
    console.print(f"[yellow]No token to remove at {store.path}.[/yellow]")
    return 1


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "start": cmd_start,
    "once": cmd_once,
    "run": cmd_run,
    "task": cmd_task,
    "init": cmd_init,
    "status": cmd_status,
    "logs": cmd_logs,
    "validate": cmd_validate,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "instance": cmd_instance,
    "list": cmd_instance_list,
    "web": cmd_web,
    "auth": cmd_auth,
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
