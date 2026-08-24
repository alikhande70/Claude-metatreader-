# mql5check — a static check for the bridge's MQL5 sources

## What problem this solves

`mql5/AtlasBridge.mq5` and its headers have never been through MetaEditor. Until
they are, everything about them is a hypothesis (`docs/STATUS.md`, third column).
The expensive part of resolving that is the round trip: someone opens MetaEditor,
gets a list of errors, sends them back, waits for a fix, and repeats. Each cycle
costs a person's attention.

This tool removes the cycles that are about typos. It rewrites the MQL5-only
syntax into C++ and hands the result to a real C++ compiler, so the errors a
compiler *can* see are found here:

- misspelled or non-existent API names
- undeclared identifiers
- wrong argument counts
- wrong argument types
- a property constant from the wrong enum family (`SymbolInfoDouble(s, SYMBOL_DIGITS)`)
- ordinary syntax errors

```bash
python3 tools/mql5check/mql5check.py     # or: make mql5check
```

## What a PASS does and does not mean

**Does mean:** no error of the kind a C++ compiler can see.

**Does not mean the file compiles in MetaEditor.** MQL5 is not C++. It has its
own object model, its own template and overload rules, its own `#property`
handling and its own standard library. This check is a *necessary* condition for
compiling, never a sufficient one. It must never be described as a compile, and
a PASS never moves anything into the Verified column of `docs/STATUS.md`.

Specific things it cannot see:

| Blind spot | Why |
|---|---|
| Overload sets that differ from the shim | The shim transcribes signatures from the MQL5 reference. Where a real overload is narrower than the shim's, a bad call passes here. |
| `#property` semantics | Dropped entirely. |
| MQL5's implicit number→string conversion rules | Modelled permissively, so a conversion MQL5 rejects may pass. |
| Runtime behaviour of any kind | Nothing is executed. |
| Whether the terminal permits the socket, or the broker exists | Not a compile-time question at all. |

## How it works

`shim/mql5.hpp` declares the MQL5 builtin surface — types, structures,
property enums, and functions — with signatures transcribed from the reference.
`shim/Trade/*.mqh` do the same for the standard library headers the bridge
includes. Nothing has a body; the check is `-fsyntax-only`.

`mql5check.py` performs six rewrites and nothing else, so what reaches the
compiler stays as close to the original text as possible:

1. `#property …` → dropped
2. `input` / `sinput` → dropped (an input is an ordinary global for typing purposes)
3. `T name[];` → `MqlArray<T> name;`
4. `T &name[]` in a parameter list → `MqlArray<T> &name`
5. `#include <X>` → `#include "X"`, so the shim tree resolves
6. forward declarations synthesised for every top-level function, because MQL5
   resolves names file-wide and C++ does not

## The harness must be able to fail

`tests/tools/test_mql5check.py` breaks the real sources seven different ways —
a misspelled builtin, a wrong enum family, a short argument list, an undeclared
variable, a call to a function that does not exist, a missing semicolon, and an
array builtin applied to a non-array — and asserts the checker reports each one.
It also asserts the unmodified sources pass. A checker that cannot fail is worse
than no checker, because it launders a hypothesis into an apparent fact.

## Keeping the shim honest

If MetaEditor reports an error that this tool did not, that is a gap in the shim,
not just a bug in the bridge. Fix the source, then also correct the shim
signature and add a mutation test, so the same class of error is caught next
time.
