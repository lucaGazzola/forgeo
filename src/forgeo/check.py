"""One-command quality gates: ``forgeo check`` runs pytest, ruff, and mypy.

Contributors (and agents) must keep all three green before opening a PR —
see ``CONTRIBUTING.md``. Running them one by one is friction, so this module
bundles them: each gate runs via ``sys.executable -m <tool>`` (the same
environment Forgeo itself runs in), and :func:`run_all_gates` reports a
per-gate outcome plus an overall summary. Read-only; never starts an agent
and needs no config file.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Gate:
    """One quality gate: a display name plus the argv to execute."""

    name: str
    argv: list[str]


@dataclass(frozen=True)
class GateOutcome:
    """What happened when one gate ran."""

    name: str
    returncode: int
    output: str
    missing: bool = False

    @property
    def passed(self) -> bool:
        """A gate passes when the tool ran and exited 0."""
        return not self.missing and self.returncode == 0


def default_gates() -> list[Gate]:
    """The three contributor gates, run with the current interpreter."""
    python = sys.executable
    return [
        Gate("pytest", [python, "-m", "pytest"]),
        Gate("ruff", [python, "-m", "ruff", "check"]),
        Gate("mypy", [python, "-m", "mypy", "src/forgeo"]),
    ]


def _is_missing_module(output: str) -> bool:
    """Detect ``python -m <tool>`` failing because the tool is not installed."""
    lowered = output.lower()
    return "no module named" in lowered


def run_gate(gate: Gate) -> GateOutcome:
    """Run one gate, capturing its combined stdout/stderr.

    Never raises on tool failure: a non-zero exit becomes a failed outcome,
    and an uninstalled tool (``No module named ...``) becomes a ``missing``
    outcome with the install hint as its output.
    """
    try:
        proc = subprocess.run(gate.argv, capture_output=True, text=True)
    except OSError as exc:
        return GateOutcome(gate.name, 127, f"{gate.name} could not start: {exc}", missing=True)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0 and _is_missing_module(output):
        hint = (
            f"{gate.name} is not installed in this environment "
            f"({output.splitlines()[0] if output else 'unknown error'}). "
            'Install it with: pip install -e ".[dev]"'
        )
        return GateOutcome(gate.name, proc.returncode, hint, missing=True)
    return GateOutcome(gate.name, proc.returncode, output)


@dataclass
class CheckReport:
    """The per-gate outcomes of one ``forgeo check`` run."""

    outcomes: list[GateOutcome] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """True when every gate passed."""
        return bool(self.outcomes) and all(outcome.passed for outcome in self.outcomes)

    def summary(self) -> str:
        """One-line ``name: PASS/FAIL`` summary plus the overall verdict."""
        parts = [
            f"{outcome.name}: {'PASS' if outcome.passed else 'FAIL'}"
            for outcome in self.outcomes
        ]
        verdict = "check: PASS" if self.healthy else "check: FAIL"
        return f"{verdict} ({', '.join(parts)})"


def run_all_gates(gates: list[Gate] | None = None) -> CheckReport:
    """Run every gate in order and return the collected report."""
    return CheckReport(
        outcomes=[run_gate(gate) for gate in (gates if gates is not None else default_gates())]
    )
