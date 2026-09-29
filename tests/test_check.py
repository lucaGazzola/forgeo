"""Tests for the ``forgeo check`` quality-gate command."""

from __future__ import annotations

import argparse
import subprocess
import sys

from forgeo import cli
from forgeo.check import (
    CheckReport,
    Gate,
    GateOutcome,
    default_gates,
    run_all_gates,
    run_gate,
)


def test_default_gates_run_pytest_ruff_and_mypy_with_current_interpreter() -> None:
    """The gate set is exactly the three CONTRIBUTING.md gates."""
    gates = default_gates()
    assert [gate.name for gate in gates] == ["pytest", "ruff", "mypy"]
    assert gates[0].argv == [sys.executable, "-m", "pytest"]
    assert gates[1].argv == [sys.executable, "-m", "ruff", "check"]
    assert gates[2].argv == [sys.executable, "-m", "mypy", "src/forgeo"]


def test_run_gate_passes_on_exit_zero(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Exit 0 becomes a passing outcome carrying the tool output."""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        assert argv[:2] == [sys.executable, "-m"]
        return subprocess.CompletedProcess(argv, 0, stdout="all good\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = run_gate(Gate("pytest", [sys.executable, "-m", "pytest"]))
    assert outcome.passed
    assert outcome.output == "all good"


def test_run_gate_fails_on_nonzero_exit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A failing tool becomes a failed (not missing) outcome."""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="E001 boom\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = run_gate(Gate("ruff", [sys.executable, "-m", "ruff", "check"]))
    assert not outcome.passed
    assert not outcome.missing
    assert "E001 boom" in outcome.output


def test_run_gate_reports_missing_tool(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``No module named ...`` becomes a missing outcome with an install hint."""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="No module named mypy\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = run_gate(Gate("mypy", [sys.executable, "-m", "mypy", "src/forgeo"]))
    assert not outcome.passed
    assert outcome.missing
    assert 'pip install -e ".[dev]"' in outcome.output


def test_run_gate_reports_unstartable_tool(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An OSError starting the tool becomes a missing outcome, never a raise."""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = run_gate(Gate("pytest", ["pytest"]))
    assert not outcome.passed
    assert outcome.missing


def test_report_summary_lists_every_gate() -> None:
    """The summary names each gate verdict plus the overall result."""
    report = CheckReport(
        outcomes=[
            GateOutcome("pytest", 0, "ok"),
            GateOutcome("ruff", 1, "E001"),
        ]
    )
    assert not report.healthy
    assert report.summary() == "check: FAIL (pytest: PASS, ruff: FAIL)"
    passing = CheckReport(outcomes=[GateOutcome("pytest", 0, "ok")])
    assert passing.healthy
    assert passing.summary() == "check: PASS (pytest: PASS)"


def test_run_all_gates_runs_in_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Gates run sequentially and the report preserves their order."""
    seen: list[str] = []

    def fake_run_gate(gate: Gate) -> GateOutcome:
        seen.append(gate.name)
        return GateOutcome(gate.name, 0, "ok")

    monkeypatch.setattr("forgeo.check.run_gate", fake_run_gate)
    report = run_all_gates()
    assert seen == ["pytest", "ruff", "mypy"]
    assert report.healthy


def test_check_is_listed_and_exits_zero_when_gates_pass(
    monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    """``forgeo check`` prints each gate's output and the PASS summary."""
    parser = cli.build_parser()
    args = parser.parse_args(["check"])
    assert args.action == "check"
    monkeypatch.setattr(
        cli,
        "run_all_gates",
        lambda: CheckReport(
            outcomes=[
                GateOutcome("pytest", 0, "1 passed"),
                GateOutcome("ruff", 0, "All checks passed"),
                GateOutcome("mypy", 0, "Success"),
            ]
        ),
    )
    assert cli.cmd_check(args) == 0
    out = capsys.readouterr().out
    assert "=== pytest ===" in out
    assert "check: PASS (pytest: PASS, ruff: PASS, mypy: PASS)" in out


def test_check_exits_nonzero_when_a_gate_fails(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A failing gate flips the summary to FAIL and the exit code to 1."""
    args = argparse.Namespace()
    monkeypatch.setattr(
        cli,
        "run_all_gates",
        lambda: CheckReport(outcomes=[GateOutcome("pytest", 1, "1 failed")]),
    )
    assert cli.cmd_check(args) == 1
    assert "check: FAIL (pytest: FAIL)" in capsys.readouterr().out


def test_check_is_dispatched_by_main(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """``main(["check"])`` routes through ``_COMMANDS`` to the real command."""
    monkeypatch.setattr(
        cli,
        "run_all_gates",
        lambda: CheckReport(outcomes=[GateOutcome("pytest", 0, "ok")]),
    )
    assert cli.main(["check"]) == 0
    assert "check: PASS (pytest: PASS)" in capsys.readouterr().out
