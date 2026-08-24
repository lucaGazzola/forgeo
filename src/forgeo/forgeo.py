"""Forgeo: one scheduled run of one repository.

Each run does exactly one of:

1. A ``BLOCKED`` task exists -> render the blocker file (the detailed
   explanation of what the human must do) from the backlog and pause.
2. A retry-eligible ``FAILED`` task has waited ``failed_retry_wait_cycles``
   -> move it back to ``OPEN`` (with backoff) before picking the next task.
3. A runnable ``OPEN`` task exists (its dependencies are all ``COMPLETED``) ->
   execute it with the agent, commit and push the result on the main branch.
4. The backlog has nothing runnable -> run the agent in refactoring mode on
   the same branch, committing and pushing whatever it improves.

Whenever the agent signals BLOCKED, its partial work is committed and pushed
(no branches, nothing lost) and the task records the agent's reason in
``blocker_reason``. The ``BLOCKER.md`` file is a derived view of the backlog's
``BLOCKED`` tasks, re-rendered every cycle with the real per-task reasons; it
disappears automatically once the last ``BLOCKED`` task is resolved. A
refactoring block (which has no task to carry the reason) is the exception: it
writes the file once and keeps Forgeo paused until the file is deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from forgeo.agent import BaseAgent
from forgeo.backlog import BacklogStore, oldest_open_task
from forgeo.git import GitError, GitManager
from forgeo.models import (
    NO_BLOCKER_REASON,
    NO_CHANGES_DIRTY_REASON,
    NO_CHANGES_REASON,
    NO_CHANGES_REPORTED_REASON,
    ExecutionResult,
    ExecutionStatus,
    ForgeoConfig,
    RepoContext,
    RunKind,
    RunOutcome,
    RunRecord,
    Task,
    TaskStatus,
)
from forgeo.notify import BlockedNotice, send_blocked_notice, send_webhook_notice
from forgeo.paths import runs_path
from forgeo.runs import RunRecorder

logger = logging.getLogger(__name__)

#: Marker embedded in the task-derived ``BLOCKER.md`` so the daemon can tell
#: the derived view apart from a refactor-block or stale file on later cycles.
_TASK_BLOCKER_MARKER = "<!-- forgeo:task-blocker:derived -->"


class TaskNotRunnableError(RuntimeError):
    """A task requested through ``forgeo run`` cannot be executed.

    Raised when the task id does not exist in the backlog or its status is
    not ``OPEN``; nothing runs and no run record is written.
    """


def _execution_outcome(status: ExecutionStatus) -> RunOutcome:
    """Map an agent execution status onto a run record outcome.

    The two enums share member names for the agent outcomes (SUCCESS, BLOCKED,
    ERROR), so the mapping is a plain name lookup.
    """
    return RunOutcome[status.name]


def _subject_label(task: Task, *, is_refactor: bool) -> str:
    """Log subject naming the actor of a message."""
    return "Refactoring pass" if is_refactor else f"Task {task.id}"


@dataclass
class BlockerEntry:
    """One blocked run to explain in the blocker file."""

    task: Task
    result: ExecutionResult
    instruction: str


class Forgeo:
    """Executes one scheduled run against a single repository."""

    def __init__(
        self,
        config: ForgeoConfig,
        backlog: BacklogStore,
        agent: BaseAgent,
        git: GitManager,
    ) -> None:
        self.config = config
        self.backlog = backlog
        self.agent = agent
        self.git = git
        self.recorder = RunRecorder(runs_path(config), keep=config.run_history_keep)
        self._last_task: Task | None = None
        self._last_agent_result: ExecutionResult | None = None
        self._last_commit_sha: str | None = None
        self._last_run_reason: str | None = None
        self._blocked_tasks: list[Task] = []

    async def run_cycle(self) -> str:
        """Execute one run; returns a short outcome label.

        Outcomes: ``blocked``, ``task``, ``paused``, ``refactor``, ``dirty``,
        and ``skipped``.
        Every completed cycle appends exactly one run record.
        """
        started_at = datetime.now(UTC)
        outcome = await self._run_cycle()
        self._record_run(outcome, started_at)
        return outcome

    async def _run_cycle(self) -> str:
        """Execute one cycle without recording; returns its outcome label."""
        tasks = await self._begin_run()
        blocked = [t for t in tasks if t.status is TaskStatus.BLOCKED]
        if blocked:
            self._blocked_tasks = blocked
            await self._render_blocker(blocked)
            return "blocked"

        if self._is_derived_blocker():
            logger.info("No BLOCKED tasks remain; removing derived blocker file.")
            self.config.blocker_file.unlink(missing_ok=True)

        await self._process_failed_retries(tasks)
        tasks = await self.backlog.list_tasks()
        task = oldest_open_task(tasks)
        if task is not None:
            return await self._run_task_clean(task)

        if self.config.blocker_file.exists():
            logger.info("Blocker file present; forgeo paused until it is resolved.")
            return "paused"

        await self._refactor()
        return "refactor"

    async def run_task_id(self, task_id: str) -> str:
        """Execute exactly the task ``task_id``; returns a short outcome label.

        Like :meth:`run_cycle` but runs ``task_id`` instead of the oldest
        OPEN task, so a human can triage immediately: rerun a FAILED task
        (after reopening it) or try a risky task now. Every finished run
        appends exactly one run record.

        Raises:
            TaskNotRunnableError: When the task does not exist in the backlog
                or is not ``OPEN`` — nothing runs and no record is written.
        """
        started_at = datetime.now(UTC)
        outcome = await self._run_task_id(task_id)
        self._record_run(outcome, started_at)
        return outcome

    async def _run_task_id(self, task_id: str) -> str:
        """Run ``task_id`` without recording; returns its outcome label.

        Outcome is ``task`` on a completed agent run or ``dirty`` when the
        working tree is dirty. A missing or non-``OPEN`` task raises
        :class:`TaskNotRunnableError`.
        """
        tasks = await self._begin_run()
        task = next((candidate for candidate in tasks if candidate.id == task_id), None)
        if task is None:
            raise TaskNotRunnableError(
                f"Task {task_id!r} does not exist in the backlog at "
                f"{self.config.backlog}."
            )
        if task.status is not TaskStatus.OPEN:
            raise TaskNotRunnableError(
                f"Task {task_id!r} is {task.status.value}; only OPEN tasks can "
                f"be run with `forgeo run`."
            )
        return await self._run_task_clean(task)

    async def _begin_run(self) -> list[Task]:
        """Reset the run state and load the tasks for a new cycle."""
        self._last_task = None
        self._last_agent_result = None
        self._last_commit_sha = None
        self._last_run_reason = None
        self._blocked_tasks = []
        await self.git.a_ensure_branch(self.config.branch)
        await self.backlog.recover_claims()
        return await self.backlog.list_tasks()

    async def _tree_clean_for(self, task: Task) -> bool:
        """True when the working tree is clean enough to run ``task``.

        A dirty tree logs the refusal and returns ``False``; Forgeo never
        commits or discards uncommitted work.
        """
        if await self.git.a_is_clean():
            return True
        logger.error(
            "Working tree of %s is dirty; refusing to run task %s.",
            self.config.repo,
            task.id,
        )
        return False

    async def _run_task_clean(self, task: Task) -> str:
        """Run ``task`` when the working tree is clean; returns the outcome label.

        Shared by the scheduled pick (:meth:`_run_cycle`) and the explicit
        ``forgeo run`` (:meth:`_run_task_id`): a dirty tree is refused with a
        ``dirty`` outcome, otherwise the task runs and the outcome is ``task``.
        """
        if not await self._tree_clean_for(task):
            return "dirty"
        claimed = await self.backlog.claim_task(task)
        if claimed is None:
            logger.info(
                "Task %s was claimed or changed by another worker; skipping this cycle.",
                task.id,
            )
            return "skipped"
        await self._run_task(claimed)
        return "task"

    # ------------------------------------------------------------------ #
    # Failed-task retries                                                 #
    # ------------------------------------------------------------------ #

    async def _process_failed_retries(self, tasks: list[Task]) -> None:
        """Move retry-eligible ``FAILED`` tasks back to ``OPEN`` with backoff.

        A task is retry-eligible when its retry budget is not exhausted: the
        per-task ``retries_left`` override when set, else the config's
        ``failed_retry_max``. Each cycle it stays ``FAILED`` and eligible its
        ``failed_wait_cycles`` counter is bumped; once that reaches
        ``failed_retry_wait_cycles`` the task is moved back to ``OPEN`` and
        its ``retry_count`` is incremented. A task that exhausts its budget
        stays ``FAILED`` with its original ``failure_reason`` preserved.

        With ``failed_retry_max: 0`` (the default) and no per-task override
        this is a no-op, so the historical behavior is unchanged. ``BLOCKED``
        tasks are never touched — blocking always needs a human.
        """
        wait = self.config.failed_retry_wait_cycles
        for task in tasks:
            if task.status is not TaskStatus.FAILED:
                continue
            budget = task.retries_left
            if budget is None:
                budget = self.config.failed_retry_max
            if budget <= 0 or task.retry_count >= budget:
                continue
            if task.failed_wait_cycles + 1 < wait:
                await self.backlog.bump_failed_wait(task.id)
                logger.info(
                    "Task %s failed; retry %d/%d scheduled in %d more cycle(s).",
                    task.id,
                    task.retry_count + 1,
                    budget,
                    wait - task.failed_wait_cycles - 1,
                )
            else:
                await self.backlog.retry_task(task.id)
                logger.info(
                    "Task %s moved back to OPEN for retry %d/%d.",
                    task.id,
                    task.retry_count + 1,
                    budget,
                )

    # ------------------------------------------------------------------ #
    # Run history                                                         #
    # ------------------------------------------------------------------ #

    def _record_run(self, outcome: str, started_at: datetime) -> None:
        """Build and append the run record for a completed cycle."""
        finished_at = datetime.now(UTC)
        task = self._cycle_task(outcome)
        self.recorder.append(
            RunRecord(
                started_at=started_at,
                finished_at=finished_at,
                kind=self._run_kind(outcome),
                task_id=task.id if task is not None else None,
                task_title=task.title if task is not None else None,
                outcome=self._run_outcome(outcome),
                agent_exit_code=self._run_exit_code(outcome),
                commit_sha=self._last_commit_sha if outcome in ("task", "refactor") else None,
                reason=self._last_run_reason if outcome in ("task", "refactor") else None,
                output_logs=self._run_output_logs(outcome),
                retry_count=self._run_retry_count(task, outcome),
                duration_seconds=round((finished_at - started_at).total_seconds(), 3),
            )
        )

    def _run_retry_count(self, task: Task | None, outcome: str) -> int | None:
        """The retry count to surface on a run record, if any.

        Kept only for task runs whose task has actually been retried before
        this run (``retry_count > 0``), so the History tab shows at a glance
        that a run was a retry without cluttering never-retried runs. Refactor
        runs and records for untouched tasks store ``None``.
        """
        if outcome not in ("task", "blocked"):
            return None
        if task is None or task.retry_count <= 0:
            return None
        return task.retry_count

    def _run_output_logs(self, outcome: str) -> list[str] | None:
        """The bounded agent-output tail to persist for a finished cycle.

        Kept only for cycles that actually ran the agent (``task``/``refactor``)
        and capped at ``run_output_lines`` lines so a chatty agent can never
        blow up a run record. ``None`` (not ``[]``) when nothing was captured,
        keeping records without output indistinguishable from pre-field ones.
        """
        if outcome not in ("task", "refactor"):
            return None
        result = self._last_agent_result
        logs = result.output_logs if result is not None else None
        if not logs:
            return None
        cap = self.config.run_output_lines
        if cap <= 0:
            return None
        return logs[-cap:]

    def _run_kind(self, outcome: str) -> RunKind | None:
        if outcome in ("task", "blocked"):
            return RunKind.TASK
        if outcome == "refactor":
            return RunKind.REFACTOR
        return None

    def _cycle_task(self, outcome: str) -> Task | None:
        """The task a finished cycle ran, when there is one to record."""
        if outcome in ("task", "refactor"):
            return self._last_task
        if outcome == "blocked":
            return self._blocked_tasks[0] if self._blocked_tasks else None
        return None

    def _run_outcome(self, outcome: str) -> RunOutcome:
        if outcome in ("task", "refactor"):
            result = self._last_agent_result
            if result is None:
                return RunOutcome.ERROR
            return _execution_outcome(result.status)
        return {
            "blocked": RunOutcome.BLOCKED,
            "paused": RunOutcome.PAUSED,
            "dirty": RunOutcome.DIRTY,
            "skipped": RunOutcome.SKIPPED,
        }.get(outcome, RunOutcome.ERROR)

    def _run_exit_code(self, outcome: str) -> int | None:
        if outcome not in ("task", "refactor"):
            return None
        result = self._last_agent_result
        return result.exit_code if result is not None else None

    # ------------------------------------------------------------------ #
    # Task execution                                                      #
    # ------------------------------------------------------------------ #

    async def _run_task(self, task: Task) -> None:
        """Execute one task: agent run(s), then commit/push on the main branch.

        A silent no-change SUCCESS (exit 0 with an empty tree) is re-run up to
        ``no_changes_retry_max`` more times in the same cycle; once the retry
        budget is spent the task is marked ``BLOCKED`` for human review — the
        only acceptable outcome for a task that ends without code changes.
        """
        logger.info("Running task %s (%s)", task.id, task.title)
        max_retries = self.config.no_changes_retry_max
        for attempt in range(max_retries + 1):
            self._last_run_reason = None
            result, ok = await self._run_agent(
                task,
                instruction=task.instruction,
                success_message=task.title,
                blocked_message=f"{task.title} [partial]",
                command=task.agent_command,
                timeout_seconds=task.agent_timeout_seconds,
            )

            if result.status is ExecutionStatus.BLOCKED:
                await self.backlog.set_blocked(task.id, result.reason, result)
                return
            if result.status is ExecutionStatus.ERROR:
                await self._mark_failed(task, self._failure_reason(result), result)
                return
            if ok:
                await self.backlog.update_status(task.id, TaskStatus.COMPLETED, result)
                self.config.blocker_file.unlink(missing_ok=True)
                self._notify_webhook("completed", task, "")
                logger.info("Task %s completed.", task.id)
                return
            # Not ok: either the commit failed (the task is already marked
            # FAILED inside _handle_execution_result) or the agent exited 0
            # without producing changes (only the reason distinguishes them).
            if self._last_run_reason != NO_CHANGES_REASON:
                return
            if attempt < max_retries:
                logger.warning(
                    "Task %s exited 0 but produced no changes "
                    "(attempt %d/%d); retrying.",
                    task.id,
                    attempt + 1,
                    max_retries,
                )
                continue
            await self._block_no_changes(task)
            return

    async def _run_agent(
        self,
        task: Task,
        *,
        instruction: str,
        success_message: str,
        blocked_message: str,
        command: str | list[str] | None = None,
        timeout_seconds: float | None = None,
        is_refactor: bool = False,
    ) -> tuple[ExecutionResult, bool]:
        """Run the agent for one task or refactoring pass and apply the
        shared SUCCESS / BLOCKED / ERROR side effects (see
        :meth:`_handle_execution_result`).

        Returns the execution result and whether the SUCCESS commit path
        succeeded, so callers can apply backlog status transitions.

        The backlog is snapshotted before the agent starts, so a bad write
        during or after the run can always be rolled back to the pre-run
        state.
        """
        self._last_task = task
        await self.backlog.snapshot()
        result = await self.agent.run_task(
            task,
            RepoContext(repo_path=self.config.repo, branch=self.config.branch),
            command=command,
            timeout_seconds=timeout_seconds,
            instruction=self._full_instruction(instruction),
        )
        self._last_agent_result = result
        ok = await self._handle_execution_result(
            result,
            task=task,
            success_message=success_message,
            blocked_message=blocked_message,
            instruction=instruction,
            is_refactor=is_refactor,
        )
        return result, ok

    def _full_instruction(self, instruction: str) -> str:
        """The instruction handed to the agent, with the project context
        prepended when ``task_context`` is configured.

        The context file is re-read on every run, so the agent's own updates
        to it are picked up on the next cycle. An unreadable or empty context
        file is logged and the run proceeds with the bare instruction: a
        cycle must never fail because a documentation file is missing.
        """
        path = self.config.task_context
        if path is None:
            return instruction
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "task_context %s unreadable (%s); running without it.", path, exc
            )
            return instruction
        text = text.strip()
        if not text:
            logger.warning("task_context %s is empty; running without it.", path)
            return instruction
        return (
            f"# Project context (from {path})\n\n"
            f"{text}\n\n"
            f"# Task\n\n{instruction}"
        )

    async def _handle_execution_result(
        self,
        result: ExecutionResult,
        *,
        task: Task,
        success_message: str,
        blocked_message: str,
        instruction: str,
        is_refactor: bool = False,
    ) -> bool:
        """Apply shared SUCCESS / BLOCKED / ERROR side effects for an agent run.

        * SUCCESS — commit and push ``success_message``. An agent that
          explicitly reported no changes are needed (``result.no_changes``)
          completes the task without a commit; an agent that exited 0 but
          produced no changes fails the task instead of silently completing
          it.
        * BLOCKED — commit partial work under ``blocked_message``, then write
          the blocker file explaining what the human must do.
        * ERROR — hard-reset the working tree and log the failure.

        Returns ``True`` only when the result was SUCCESS and the commit (or
        explicit no-change) path succeeded, so callers can apply backlog
        status transitions. This method does not update task status itself
        (except indirectly when ``_commit_and_push`` fails and marks the task
        FAILED).
        """
        if result.status is ExecutionStatus.SUCCESS:
            if result.no_changes:
                return await self._complete_without_changes(task, is_refactor=is_refactor)
            ok = await self._commit_and_push(success_message, task=task)
            if ok and self._last_commit_sha is None and not is_refactor:
                # Exited 0 but produced no changes: not a completion. The
                # engine cannot tell "deliberately did nothing" from "did
                # nothing", so the agent must opt into no-ops explicitly.
                # The caller decides between an immediate re-run
                # (``no_changes_retry_max``) and a BLOCKED outcome.
                self._last_run_reason = NO_CHANGES_REASON
                logger.warning("Task %s: %s.", task.id, NO_CHANGES_REASON)
                return False
            return ok

        if result.status is ExecutionStatus.BLOCKED:
            if await self._commit_and_push(blocked_message, task=task):
                entry = BlockerEntry(
                    task=task,
                    result=result,
                    instruction=instruction,
                )
                if is_refactor:
                    await self._write_blocker([entry])
                self._notify_blocked(entry)
            if is_refactor:
                logger.warning("Refactoring pass is BLOCKED; blocker file written.")
            else:
                logger.warning("Task %s is BLOCKED; see BLOCKER.md on the next cycle.", task.id)
            return False

        await self._discard_failed_work(task, result, is_refactor=is_refactor)
        return False

    async def _discard_failed_work(
        self,
        task: Task,
        result: ExecutionResult,
        *,
        is_refactor: bool = False,
    ) -> None:
        """Hard-reset agent changes after ERROR and log the failure detail."""
        label = _subject_label(task, is_refactor=is_refactor)
        try:
            await self.git.a_reset_hard()
        except GitError as exc:
            logger.error("Could not discard work for %s: %s", label, exc)
        detail = result.error or "no error detail provided"
        logger.error("%s FAILED: %s", label, detail)

    async def _fail(self, task: Task, result: ExecutionResult) -> None:
        """Discard the agent's work, mark the task FAILED, and log the error."""
        await self._discard_failed_work(task, result)
        await self._mark_failed(task, self._failure_reason(result), result)

    async def _mark_failed(self, task: Task, reason: list[str], result: ExecutionResult) -> None:
        """Persist ``reason`` on ``task`` and send the ``failed`` webhook notice.

        Shared by the direct ERROR path (the work was already discarded in
        :meth:`_handle_execution_result`) and the FAILED transitions from
        :meth:`_fail` and :meth:`_complete_without_changes`.
        """
        await self.backlog.set_failed(task.id, reason, result)
        self._notify_webhook("failed", task, "\n".join(reason))

    async def _block_no_changes(self, task: Task) -> None:
        """Mark a task whose agent exited 0 without producing any changes as
        BLOCKED: the only acceptable outcome for a no-change run is a blocked
        task awaiting human review.

        A no-change SUCCESS is indistinguishable from an agent that simply did
        nothing, so it is not a valid completion. After the retry budget is
        spent the task is blocked (never FAILED), so the run record surfaces
        the reason and the human decides whether to reopen, split, or drop the
        task.
        """
        self._last_run_reason = NO_CHANGES_REASON
        logger.warning(
            "Task %s: %s; marking BLOCKED.", task.id, NO_CHANGES_REASON
        )
        blocked = ExecutionResult(
            status=ExecutionStatus.BLOCKED,
            output_logs=(
                self._last_agent_result.output_logs
                if self._last_agent_result is not None
                else []
            ),
            questions=[NO_CHANGES_REASON],
            exit_code=(
                self._last_agent_result.exit_code
                if self._last_agent_result is not None
                else None
            ),
        )
        self._last_agent_result = blocked
        await self.backlog.set_blocked(task.id, [NO_CHANGES_REASON], blocked)
        entry = BlockerEntry(
            task=task,
            result=blocked,
            instruction=task.instruction,
        )
        self._notify_blocked(entry)

    async def _complete_without_changes(
        self, task: Task, *, is_refactor: bool = False
    ) -> bool:
        """Complete a run whose agent explicitly reported no changes needed.

        Only accepted when the working tree is clean: an agent that claims a
        run needs no change but leaves uncommitted work behind contradicts
        itself and fails the run instead.
        """
        if not await self.git.a_is_clean():
            self._last_run_reason = NO_CHANGES_DIRTY_REASON
            label = _subject_label(task, is_refactor=is_refactor)
            logger.warning(
                "%s reported no changes but left uncommitted changes; marking FAILED.",
                label,
            )
            await self._discard_failed_work(
                task,
                ExecutionResult(status=ExecutionStatus.ERROR, error=NO_CHANGES_DIRTY_REASON),
                is_refactor=is_refactor,
            )
            if not is_refactor:
                await self._mark_failed(
                    task,
                    [NO_CHANGES_DIRTY_REASON],
                    self._last_agent_result or ExecutionResult(status=ExecutionStatus.ERROR),
                )
            return False
        self._last_run_reason = NO_CHANGES_REPORTED_REASON
        logger.info(
            "Task %s completed without changes (explicit no-change signal).", task.id
        )
        return True

    @staticmethod
    def _failure_reason(result: ExecutionResult) -> list[str]:
        """The failure reason to persist on a failed task, from an agent result.

        Mirrors the detail the engine already logs today (``result.error``,
        e.g. the timeout message or a non-zero exit code), with a fallback
        when the agent gave none.
        """
        return [result.error] if result.error else ["no error detail provided"]

    async def _commit_and_push(self, message: str, *, task: Task) -> bool:
        """Commit everything on the main branch and push when a remote is set.

        Returns ``False`` (and marks the task FAILED) when git refuses to
        cooperate.
        """
        try:
            sha = await self.git.a_commit_all(message)
        except GitError as exc:
            self._last_commit_sha = None
            await self._fail(
                task,
                ExecutionResult(status=ExecutionStatus.ERROR, error=f"git: {exc}"),
            )
            return False
        if sha is None:
            logger.info("No changes produced; nothing committed.")
            return True
        self._last_commit_sha = sha
        logger.info("Committed %s: %s", sha, message)
        if self.config.remote:
            try:
                await self.git.a_push(self.config.remote, self.config.branch)
                logger.info("Pushed %s to %s/%s", sha, self.config.remote, self.config.branch)
            except GitError as exc:
                logger.error("Push failed (work stays committed locally): %s", exc)
        return True

    # ------------------------------------------------------------------ #
    # Refactoring pass                                                    #
    # ------------------------------------------------------------------ #

    async def _refactor(self) -> None:
        """Run the agent in refactoring mode; commit and push its changes."""
        refactor_task = Task(
            id="REFACTOR",
            title="Refactoring pass",
            description=self.config.refactor_prompt,
        )
        logger.info("Backlog empty; running refactoring pass.")
        await self._run_agent(
            refactor_task,
            instruction=self.config.refactor_prompt,
            success_message="refactoring pass",
            blocked_message="refactoring pass [partial]",
            is_refactor=True,
        )

    # ------------------------------------------------------------------ #
    # Blocker file                                                        #
    # ------------------------------------------------------------------ #

    def _notify_blocked(self, entry: BlockerEntry) -> None:
        """Send notifications for a newly blocked task or refactor pass.

        The message contains Forgeo name, the task id and title, and the
        first lines of the blocker reason. Notifications are optional and never
        change the outcome of the cycle: a failure is only logged.
        """
        reason = entry.result.reason
        notice = BlockedNotice(
            task_id=entry.task.id,
            task_title=entry.task.title,
            reason="\n".join(reason) if reason else NO_BLOCKER_REASON,
        )
        send_blocked_notice(self.config, notice)
        send_webhook_notice(self.config, "blocked", notice)

    def _notify_webhook(self, outcome: str, task: Task, reason: str) -> None:
        """Send a generic webhook notification for one run outcome.

        ``outcome`` is one of ``blocked``, ``completed`` or ``failed``; the
        notification is skipped unless ``outcome`` is enabled in the config.
        Notifications are optional and never change the outcome of the cycle:
        a failure is only logged.
        """
        send_webhook_notice(
            self.config,
            outcome,
            BlockedNotice(task_id=task.id, task_title=task.title, reason=reason),
        )

    async def _render_blocker(self, blocked: list[Task]) -> None:
        """Render ``BLOCKER.md`` as a derived view of the backlog's BLOCKED tasks.

        Re-rendered every cycle while any task is blocked, so it always shows
        the real per-task reasons from ``blocker_reason`` (never generic
        text). Once the last BLOCKED task is resolved, :meth:`_run_cycle`
        removes the file because it carries the derived-view marker.
        """
        sections = self._blocker_header(include_marker=True)
        for task in blocked:
            sections.append(self._render_blocked_task(task))
            sections.append("")
        self._persist_blocker(sections)
        logger.info(
            "Blocker file rendered from %d BLOCKED task(s) to %s",
            len(blocked),
            self.config.blocker_file,
        )

    def _render_blocked_task(self, task: Task) -> str:
        """Render the explanation and required human action for one blocked task."""
        sections = [self._render_reason_sections(task, task.instruction, task.blocker_reason)]
        sections += [
            "",
            "### What you must do",
            "",
            "1. Decide what the agent needs (edit the repository directly if required).",
            "2. Reopen the task from the web console so Forgeo retries it on the next",
            f"   scheduled run{self._reopen_by_hand(task)}",
            "3. Or delete the task from the web console if it should not be done.",
        ]
        return "\n".join(sections)

    def _reopen_by_hand(self, task: Task) -> str:
        """How to reopen a blocked task without the web console.

        Only a backlog file can be edited in place; a backlog served over HTTP
        belongs to another application, so the human is pointed at it instead
        of at a file that does not exist on this machine.
        """
        if self.config.backlog_is_remote:
            return f", or wherever the backlog at `{self.config.backlog}` is edited."
        return (
            f", or set the status of `{task.id}` back to `OPEN` directly in "
            f"`{self.config.backlog}`."
        )

    def _blocker_header(self, *, include_marker: bool) -> list[str]:
        """The shared ``BLOCKER.md`` preamble (intro, optional derived marker)."""
        lines: list[str] = ["# BLOCKER: Forgeo needs your input", ""]
        if include_marker:
            lines.append(_TASK_BLOCKER_MARKER)
            lines.append("")
        lines.extend(
            [
                "The coding agent could not finish without a human decision. The",
                f"forgeo is paused until this is resolved. Backlog: `{self.config.backlog}`.",
                "",
            ]
        )
        return lines

    def _persist_blocker(self, sections: list[str]) -> None:
        """Write the rendered blocker file (parent dir created if needed)."""
        self.config.blocker_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.blocker_file.write_text("\n".join(sections), encoding="utf-8")

    def _is_derived_blocker(self) -> bool:
        """True when the blocker file is a task-derived view (not a stale or
        refactor-block file), i.e. it carries the derived-view marker."""
        path = self.config.blocker_file
        if not path.is_file():
            return False
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return False
        return _TASK_BLOCKER_MARKER in content

    async def _write_blocker(self, entries: list[BlockerEntry]) -> None:
        """Write the blocker file for a refactoring block (no backlog task).

        A refactor block has no task to carry the reason, so it stays outside
        the derived-view model: the file is written once at block time and
        Forgeo stays paused until the human deletes it.
        """
        sections = self._blocker_header(include_marker=False)
        for entry in entries:
            sections.append(self._render_entry(entry))
            sections.append("")
        self._persist_blocker(sections)
        logger.info("Blocker file written to %s", self.config.blocker_file)

    def _render_entry(self, entry: BlockerEntry) -> str:
        """Render the explanation and required human action for one refactor block."""
        sections = [self._render_reason_sections(entry.task, entry.instruction, entry.result.reason)]
        sections += [
            "",
            "### What you must do",
            "",
            "Decide how to handle this refactoring question, then delete this file.",
            "Forgeo will continue on the next scheduled run.",
        ]
        return "\n".join(sections)

    @staticmethod
    def _render_reason_sections(
        task: Task, instruction: str, reason: list[str]
    ) -> str:
        """Render the shared "asked to do" + "says it needs" sections for one block."""
        lines = [f"## {task.id}: {task.title}", "", "### What the agent was asked to do", ""]
        lines += [f"> {line}" for line in instruction.splitlines()]
        lines += ["", "### What the agent says it needs", ""]
        if not reason:
            reason = [NO_BLOCKER_REASON]
        lines += [f"> {line}" for line in reason[-10:]]
        return "\n".join(lines)
