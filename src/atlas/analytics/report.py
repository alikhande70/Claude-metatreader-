"""Run analytics: performance, decision funnel, and execution quality.

Three questions a completed run must be able to answer, and each needs different data:

* **How did it perform?** -- the metrics, in R, net of costs, with the sample-size caveat.
* **Why did it not trade more?** -- the decision funnel. A strategy that stood aside 99% of
  the time because of one gate is a different problem from one that never found a setup.
* **Did execution match the plan?** -- realised slippage against the modelled slippage. This
  is the number that explains a live/backtest divergence, and it is computable from the
  journal without any extra instrumentation.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np

from atlas.analytics.metrics import compute_metrics, summarise
from atlas.bus.events import EventKind
from atlas.bus.journal import Journal
from atlas.core.errors import DataError
from atlas.core.trading import Trade


def load_run(run_dir: Path | str) -> dict:
    """Read a run's journal into the pieces the analytics need."""
    d = Path(run_dir)
    if not (d / "events.jsonl").exists():
        raise DataError(f"{d} does not look like a run directory (no events.jsonl)")
    j = Journal(d, mirror_sqlite=False)
    trades: list[Trade] = []
    decisions: list[dict] = []
    equity: list[tuple[int, float, float]] = []
    fills: list[dict] = []
    halts: list[dict] = []
    started: dict = {}
    for e in j.iter_jsonl():
        if e.kind == EventKind.TRADE_RECORDED:
            trades.append(Trade(**e.payload["trade"]))
        elif e.kind == EventKind.DECISION:
            decisions.append(e.payload["record"])
        elif e.kind == EventKind.EQUITY_POINT:
            equity.append((e.ts, e.payload.get("equity", 0.0), e.payload.get("balance", 0.0)))
        elif e.kind == EventKind.POSITION_OPENED:
            fills.append(e.payload)
        elif e.kind == EventKind.KILL_SWITCH:
            halts.append(e.payload)
        elif e.kind == EventKind.RUN_STARTED:
            started = e.payload
    return {"trades": trades, "decisions": decisions, "equity": equity, "fills": fills,
            "halts": halts, "started": started}


def decision_funnel(decisions: list[dict]) -> list[tuple[str, int, float]]:
    """Reason codes ranked by frequency. The shape of a strategy's selectivity."""
    counts = Counter(d["reason_code"] for d in decisions)
    total = sum(counts.values()) or 1
    return [(code, n, n / total) for code, n in counts.most_common()]


def slippage_report(fills: list[dict]) -> dict[str, float]:
    """Realised entry slippage, in price units.

    ``POSITION_OPENED`` carries the difference between the price the strategy proposed and
    the price actually obtained. Comparing its distribution with the model's assumption is
    the direct test of whether the cost model is honest, and it is the first thing to look at
    when live results diverge from research.
    """
    vals = [f["slippage_price"] for f in fills if "slippage_price" in f]
    if not vals:
        return {}
    a = np.abs(np.array(vals, dtype=float))
    return {
        "n": float(len(a)),
        "mean_abs": float(a.mean()),
        "median_abs": float(np.median(a)),
        "p90_abs": float(np.percentile(a, 90)),
        "max_abs": float(a.max()),
    }


def conviction_vs_outcome(
    decisions: list[dict], trades: list[Trade]
) -> list[tuple[str, int, float]]:
    """Does conviction predict anything?

    The conviction score is a hypothesis, and this is its test. If high-conviction trades do
    not outperform low-conviction ones over a few hundred trades, the scoring layer is not
    earning its parameters and should be deleted rather than re-weighted.
    """
    by_id = {d["decision_id"]: d for d in decisions}
    buckets: dict[str, list[float]] = {"0.0-0.4": [], "0.4-0.6": [], "0.6-0.8": [], "0.8-1.0": []}
    for t in trades:
        d = by_id.get(t.decision_id)
        if d is None:
            continue
        c = float(d.get("conviction", 0.0))
        key = ("0.0-0.4" if c < 0.4 else "0.4-0.6" if c < 0.6 else "0.6-0.8" if c < 0.8
               else "0.8-1.0")
        buckets[key].append(t.r_multiple)
    return [(k, len(v), float(np.mean(v)) if v else 0.0) for k, v in buckets.items()]


def analyse_run(run_dir: Path | str, starting_balance: float = 10_000.0) -> str:
    data = load_run(run_dir)
    trades = data["trades"]
    lines = [f"RUN ANALYSIS -- {run_dir}", ""]
    started = data["started"]
    if started:
        lines.append(f"mode {started.get('mode')}  symbols {started.get('symbols')}")
        lines.append(f"strategies {started.get('strategies')}")
        lines.append("")

    m = compute_metrics(trades, starting_equity=starting_balance,
                        equity_curve=data["equity"] or None)
    lines.append("PERFORMANCE")
    lines.append(summarise(m))
    lines.append("")

    lines.append("DECISION FUNNEL")
    funnel = decision_funnel(data["decisions"])
    if not funnel:
        lines.append("  (no decision records journalled)")
    for code, n, share in funnel:
        lines.append(f"  {code:28s} {n:7d}  {share:6.1%}")
    lines.append("")

    slip = slippage_report(data["fills"])
    if slip:
        lines.append("EXECUTION QUALITY (realised entry slippage, price units)")
        lines.append(f"  n {slip['n']:.0f}  mean {slip['mean_abs']:.5f}  "
                     f"median {slip['median_abs']:.5f}  p90 {slip['p90_abs']:.5f}  "
                     f"max {slip['max_abs']:.5f}")
        lines.append("  Compare with the CostModel's assumption; a persistent gap means the "
                     "model is optimistic and every backtest built on it is too.")
        lines.append("")

    if trades:
        lines.append("CONVICTION vs OUTCOME")
        for bucket, n, mean_r in conviction_vs_outcome(data["decisions"], trades):
            lines.append(f"  conviction {bucket}: {n:4d} trades, mean {mean_r:+.3f}R")
        lines.append("  If this shows no gradient over a few hundred trades, the conviction "
                     "layer is not earning its parameters.")
        lines.append("")

    if data["halts"]:
        lines.append("HALTS")
        for h in data["halts"]:
            lines.append(f"  {h.get('reason')}: {h.get('detail')}")
    return "\n".join(lines)


def export_json(run_dir: Path | str, out: Path | str, starting_balance: float = 10_000.0) -> Path:
    data = load_run(run_dir)
    m = compute_metrics(data["trades"], starting_equity=starting_balance,
                        equity_curve=data["equity"] or None)
    payload = {
        "metrics": m.to_dict(),
        "funnel": decision_funnel(data["decisions"]),
        "slippage": slippage_report(data["fills"]),
        "trades": [t.model_dump(mode="json") for t in data["trades"]],
    }
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return p
