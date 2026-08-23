"""Structural guards for ADR-003 (one decision path) and backtest determinism.

These use the AST rather than regexes over source text. A textual scan flags its own
documentation -- the first version of this file failed on a docstring that *described* the
rule -- and misses anything written slightly differently. Parsing means the guard sees code
and only code.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "atlas"

#: Packages that must behave identically in backtest, paper and live.
GUARDED = ("strategy", "risk", "features", "execution", "analytics")

#: Wall-clock reads break replay determinism: every component must take a Clock.
FORBIDDEN_CALLS = {
    ("time", "time"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("time", "monotonic_ns"),
}

#: Names whose value implies run mode. Comparing against one is behaviour branching.
MODE_NAMES = {"mode", "run_mode", "is_backtest", "is_live", "is_paper"}


def _files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _call_name(node: ast.Call) -> tuple[str, str] | None:
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return (f.value.id, f.attr)
    return None


def _mode_reference(node: ast.expr) -> bool:
    if isinstance(node, ast.Name) and node.id in MODE_NAMES:
        return True
    if isinstance(node, ast.Attribute):
        if node.attr in MODE_NAMES:
            return True
        if isinstance(node.value, ast.Name) and node.value.id == "RunMode":
            return True
    return False


@pytest.mark.parametrize("package", GUARDED)
def test_no_mode_branching_in_the_decision_path(package):
    """A single ``if mode == ...`` here is how a backtest stops describing the live system."""
    offenders: list[str] = []
    for path in _files(package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                parts = [node.left, *node.comparators]
                if any(_mode_reference(p) for p in parts):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name == ("builtins", "isinstance"):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "isinstance":
                    for arg in node.args[1:]:
                        if isinstance(arg, ast.Name) and arg.id.endswith("Venue"):
                            offenders.append(
                                f"{path.relative_to(SRC)}:{node.lineno} (isinstance on venue)"
                            )
    assert not offenders, (
        "ADR-003 violation -- the decision path must not branch on run mode or venue type:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("package", [*GUARDED, "runtime", "data", "backtest"])
def test_no_wall_clock_reads_in_the_decision_path(package):
    offenders: list[str] = []
    for path in _files(package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in FORBIDDEN_CALLS:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, (
        "wall-clock access breaks replay determinism; take a Clock instead:\n"
        + "\n".join(offenders)
    )


def test_the_guard_actually_detects_violations(tmp_path):
    """A structural guard that cannot fail is worse than none -- it produces false comfort."""
    bad = tmp_path / "atlas" / "strategy"
    bad.mkdir(parents=True)
    (bad / "x.py").write_text(
        "import time\n"
        "def f(mode):\n"
        "    if mode == 'BACKTEST':\n"
        "        return time.time()\n"
        "    return 0\n"
    )
    tree = ast.parse((bad / "x.py").read_text())
    compares = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)]
    assert any(_mode_reference(c.left) for c in compares)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(_call_name(c) in FORBIDDEN_CALLS for c in calls)


def test_perf_counter_is_allowed_for_measuring_wall_time():
    """Measuring how long a run took is not the same as reading the clock for decisions."""
    assert ("time", "perf_counter") not in FORBIDDEN_CALLS
    engine = (SRC / "runtime" / "engine.py").read_text(encoding="utf-8")
    assert "time.perf_counter()" in engine
