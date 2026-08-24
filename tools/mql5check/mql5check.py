#!/usr/bin/env python3
"""Static check for the ATLAS MQL5 sources.

MetaEditor is not available in every environment this repository is worked on
in, and "it will probably compile" is not evidence. This script rewrites the
MQL5-only syntax into C++ and hands the result to a C++ compiler, so that the
errors a compiler *can* see -- misspelled API names, undeclared identifiers,
wrong argument counts, wrong argument types, wrong property-enum families --
are found here instead of on the first MetaEditor round trip.

WHAT A CLEAN RUN MEANS
    No error of the kind a C++ compiler can see.

WHAT IT DOES NOT MEAN
    That MetaEditor accepts the file. MQL5 is not C++: it has its own object
    model, its own template rules, `#property` semantics and standard library.
    A clean run here is a necessary condition for compiling, not a sufficient
    one, and this tool must never be cited as a compile.

The rewrites are deliberately few, so that what reaches the compiler is as
close to the original text as possible:

  1. `#property ...`        -> dropped (no C++ equivalent, no semantic weight here)
  2. `input` / `sinput`     -> dropped (an input is an ordinary global for typing)
  3. `T name[];`            -> `MqlArray<T> name;`
  4. `T &name[]` in params  -> `MqlArray<T> &name`
  5. `#include <X>`         -> `#include "X"` so the shim tree resolves
  6. forward declarations   -> synthesised, because MQL5 resolves functions
                              file-wide and C++ does not
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = HERE / "shim"

# Types that appear in `T name[]` declarations in the bridge sources. Listing
# them rather than accepting any identifier keeps the rewrite from mangling
# something that merely looks like an array declaration.
ARRAY_TYPES = (
    "string|double|int|uint|long|ulong|short|ushort|char|uchar|bool|datetime|color|"
    "MqlRates|MqlTick|MqlTradeRequest|MqlTradeResult|MqlTradeTransaction|ENUM_TIMEFRAMES"
)

DECL_ARRAY = re.compile(
    r"^(?P<indent>\s*)(?P<const>const\s+)?(?P<type>" + ARRAY_TYPES + r")\s+"
    r"(?P<name>\w+)\s*\[\s*\]\s*;",
    re.MULTILINE,
)
PARAM_ARRAY = re.compile(
    r"(?P<const>const\s+)?(?P<type>" + ARRAY_TYPES + r")\s*&\s*(?P<name>\w+)\s*\[\s*\]"
)
PROPERTY = re.compile(r"^\s*#property\b.*$", re.MULTILINE)
INPUT_KW = re.compile(r"^(\s*)(?:input|sinput)\s+", re.MULTILINE)
ANGLE_INCLUDE = re.compile(r'^\s*#include\s*<([^>]+)>', re.MULTILINE)

BLOCK_KEYWORDS = {
    "if", "else", "for", "while", "switch", "do", "try", "catch",
    "struct", "class", "enum", "union", "namespace", "extern", "template",
    "return", "case", "default",
}


def blank_comments(src: str) -> str:
    """Return src with comment bodies replaced by spaces.

    Length and line structure are preserved so offsets stay valid, which is
    what lets a prototype be sliced back out of the result by offset.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = out[i + 1] = " "
                i += 2
        elif c in "\"'":
            # Skip the literal without blanking it: prototypes are sliced from
            # this text and a default argument may be a string.
            quote = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    break
                i += 1
            i += 1
        elif c == "#":
            # Preprocessor line: not part of any statement.
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def blank_strings(src: str) -> str:
    """Return src with literal contents replaced by spaces, for brace scanning.

    A brace or semicolon inside a string literal must not move the statement
    boundary. Expects comments to have been blanked already.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        if src[i] in "\"'":
            quote = src[i]
            i += 1
            while i < n:
                if src[i] == "\\":
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if src[i] == quote:
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            i += 1
        else:
            i += 1
    return "".join(out)


def strip_default_args(signature: str) -> str:
    """Remove default argument values so a prototype can coexist with its
    definition -- C++ rejects a default given in both places."""
    open_at = signature.find("(")
    close_at = signature.rfind(")")
    if open_at < 0 or close_at < open_at:
        return signature
    head, params, tail = (
        signature[: open_at + 1],
        signature[open_at + 1 : close_at],
        signature[close_at:],
    )
    pieces, depth, current = [], 0, ""
    for ch in params:
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth -= 1
        if ch == "," and depth == 0:
            pieces.append(current)
            current = ""
            continue
        current += ch
    pieces.append(current)
    cleaned = []
    for piece in pieces:
        depth = 0
        for idx, ch in enumerate(piece):
            if ch in "([<":
                depth += 1
            elif ch in ")]>":
                depth -= 1
            elif ch == "=" and depth == 0:
                piece = piece[:idx]
                break
        cleaned.append(piece.rstrip())
    return head + ",".join(cleaned) + tail


def function_prototypes(src: str) -> list[str]:
    """Synthesise a prototype for every top-level function definition.

    MQL5 resolves function names across the whole file regardless of order;
    C++ requires a declaration first. Without this the check would report
    hundreds of use-before-declaration errors that MetaEditor never raises.
    """
    clean = blank_comments(src)
    scan = blank_strings(clean)
    protos: list[str] = []
    depth = 0
    stmt_start = 0
    i, n = 0, len(scan)
    while i < n:
        ch = scan[i]
        if ch == "{":
            if depth == 0:
                candidate = clean[stmt_start:i].strip()
                flat = " ".join(candidate.split())
                first = flat.split("(")[0].split()
                if (
                    "(" in flat
                    and ")" in flat
                    and first
                    and first[0] not in BLOCK_KEYWORDS
                    and not flat.startswith("=")
                ):
                    protos.append(strip_default_args(flat) + ";")
                stmt_start = i + 1
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                stmt_start = i + 1
        elif ch == ";" and depth == 0:
            stmt_start = i + 1
        i += 1
    return protos


def translate(path: Path) -> str:
    src = path.read_text(encoding="utf-8", errors="replace")
    src = PROPERTY.sub("", src)
    src = INPUT_KW.sub(r"\1", src)
    src = ANGLE_INCLUDE.sub(lambda m: f'#include "{m.group(1)}"', src)
    src = PARAM_ARRAY.sub(
        lambda m: f'{m.group("const") or ""}MqlArray<{m.group("type")}> &{m.group("name")}',
        src,
    )
    src = DECL_ARRAY.sub(
        lambda m: f'{m.group("indent")}{m.group("const") or ""}'
        f'MqlArray<{m.group("type")}> {m.group("name")};',
        src,
    )

    protos = function_prototypes(src)
    # Only headers get an include guard; #pragma once in a main file is a warning.
    header = ['#pragma once'] if path.suffix == ".mqh" else []
    header.append('#include "mql5.hpp"')
    if protos:
        header.append("// --- prototypes synthesised by mql5check (MQL5 resolves file-wide) ---")
        header.extend(protos)
    # Insert after the last #include so the shim types are already visible.
    lines = src.splitlines()
    last_include = 0
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#include"):
            last_include = idx + 1
    lines[last_include:last_include] = header
    return "\n".join(lines) + "\n"


def build(sources: list[Path], workdir: Path) -> list[Path]:
    """Materialise translated sources into a tree the compiler can walk."""
    shutil.copytree(SHIM, workdir, dirs_exist_ok=True)
    written = []
    for src in sources:
        # Preserve the Atlas/ include layout so `#include <Atlas/Json.mqh>` resolves.
        rel = src.name if src.parent.name != "Atlas" else f"Atlas/{src.name}"
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(translate(src), encoding="utf-8")
        written.append(target)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(HERE.parents[1] / "mql5"))
    parser.add_argument("--cxx", default="g++")
    parser.add_argument("--keep", action="store_true", help="keep the translated tree")
    args = parser.parse_args()

    if shutil.which(args.cxx) is None:
        print(f"mql5check: no C++ compiler ({args.cxx}) on PATH", file=sys.stderr)
        return 2

    root = Path(args.root)
    includes = sorted((root / "Include" / "Atlas").glob("*.mqh"))
    experts = sorted(root.glob("*.mq5"))
    if not experts:
        print(f"mql5check: no .mq5 sources under {root}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="mql5check-"))
    try:
        build(includes + experts, workdir)
        failures = 0
        # Each expert is a translation unit; the headers come in through it.
        for expert in experts:
            unit = workdir / expert.name
            cmd = [
                args.cxx, "-std=c++17", "-fsyntax-only",
                # .mq5 is not a suffix the driver knows; name the language explicitly.
                "-x", "c++",
                "-Wall", "-Wextra",
                "-Wno-unused-parameter", "-Wno-unused-variable",
                "-Wno-unused-but-set-variable",
                "-I", str(workdir),
                str(unit),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            label = expert.relative_to(root.parent)
            if proc.returncode == 0 and not proc.stderr.strip():
                print(f"PASS  {label}")
            else:
                failures += 1 if proc.returncode != 0 else 0
                status = "FAIL" if proc.returncode != 0 else "WARN"
                print(f"{status}  {label}")
                sys.stdout.write(
                    proc.stderr.replace(str(workdir) + "/", "").rstrip() + "\n"
                )
        if failures:
            print(f"\nmql5check: {failures} source(s) did not type-check.")
            return 1
        print("\nmql5check: type-checked under the C++ shim. "
              "This is NOT a MetaEditor compile -- see tools/mql5check/README.md.")
        return 0
    finally:
        if args.keep:
            print(f"translated tree kept at {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
