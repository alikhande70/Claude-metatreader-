"""The MQL5 static check must be able to fail.

A checker that reports PASS unconditionally is worse than no checker, because it
launders a hypothesis into an apparent fact. Every test here breaks the bridge
sources in a way MetaEditor would reject and asserts the harness says so; the
first test asserts the unmodified sources pass, so the suite pins both ends.

These do NOT claim the sources compile in MetaEditor. They claim the harness
detects the classes of error it advertises.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "tools" / "mql5check" / "mql5check.py"
MQL5 = REPO / "mql5"

pytestmark = pytest.mark.skipif(
    shutil.which("g++") is None, reason="no C++ compiler available for the MQL5 shim check"
)


def run_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def mutated(tmp_path: Path, relative: str, before: str, after: str) -> Path:
    """Copy the bridge sources and make one substitution."""
    root = tmp_path / "mql5"
    shutil.copytree(MQL5, root)
    target = root / relative
    text = target.read_text(encoding="utf-8")
    assert before in text, f"fixture drift: {before!r} no longer present in {relative}"
    target.write_text(text.replace(before, after, 1), encoding="utf-8")
    return root


def test_unmodified_sources_type_check(tmp_path: Path) -> None:
    result = run_check(MQL5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    # The disclaimer is part of the contract: this must never read as a compile.
    assert "NOT a MetaEditor compile" in result.stdout


def test_detects_misspelled_builtin(tmp_path: Path) -> None:
    root = mutated(
        tmp_path,
        "Include/Atlas/Spec.mqh",
        "int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);",
        "int digits = (int)SymbolInfoIntger(symbol, SYMBOL_DIGITS);",
    )
    result = run_check(root)
    assert result.returncode == 1
    assert "SymbolInfoIntger" in result.stdout


def test_detects_wrong_property_enum_family(tmp_path: Path) -> None:
    """SYMBOL_DIGITS is an integer property; asking for it as a double is the
    kind of mistake that silently returns 0.0 rather than throwing."""
    root = mutated(
        tmp_path,
        "Include/Atlas/Spec.mqh",
        'double vmin  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);',
        'double vmin  = SymbolInfoDouble(symbol, SYMBOL_DIGITS);',
    )
    result = run_check(root)
    assert result.returncode == 1


def test_detects_wrong_argument_count_on_ctrade(tmp_path: Path) -> None:
    root = mutated(
        tmp_path,
        "Include/Atlas/Orders.mqh",
        "trade.PositionModify((ulong)ticket, sl, tp);",
        "trade.PositionModify((ulong)ticket, sl);",
    )
    result = run_check(root)
    assert result.returncode == 1
    assert "PositionModify" in result.stdout


def test_detects_undeclared_identifier(tmp_path: Path) -> None:
    root = mutated(
        tmp_path,
        "AtlasBridge.mq5",
        "   EnsureConnected();",
        "   EnsureConnected();\n   g_never_declared = true;",
    )
    result = run_check(root)
    assert result.returncode == 1
    assert "g_never_declared" in result.stdout


def test_detects_call_to_undefined_function(tmp_path: Path) -> None:
    root = mutated(
        tmp_path,
        "AtlasBridge.mq5",
        "   PublishClosedBars();",
        "   PublishClosedBarsTypo();",
    )
    result = run_check(root)
    assert result.returncode == 1
    assert "PublishClosedBarsTypo" in result.stdout


def test_detects_syntax_error(tmp_path: Path) -> None:
    root = mutated(
        tmp_path,
        "Include/Atlas/Json.mqh",
        'string out = "";\n   int n = StringLen(value);',
        'string out = ""\n   int n = StringLen(value);',
    )
    result = run_check(root)
    assert result.returncode == 1


def test_detects_wrong_type_passed_to_array_builtin(tmp_path: Path) -> None:
    """`ArrayResize` on something that is not an array is a compile error in
    MQL5 and must be one here too."""
    root = mutated(
        tmp_path,
        "Include/Atlas/Spec.mqh",
        "ArrayResize(parts, 24);",
        "ArrayResize(digits, 24);",
    )
    result = run_check(root)
    assert result.returncode == 1
