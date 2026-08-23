"""CSV import/export for bar data.

Accepts the two layouts users actually have: MetaTrader 5's exported ``.csv`` (tab or
comma separated, ``DATE TIME OPEN HIGH LOW CLOSE TICKVOL VOL SPREAD``) and a generic
``time,open,high,low,close[,volume][,spread]`` form.

Timestamps in an MT5 export are **broker server time**. The importer therefore requires an
explicit ``server_offset_hours`` (or ``tz``) rather than guessing: a silent two-hour shift
moves every session filter and is invisible in the resulting equity curve.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from atlas.core.enums import Timeframe
from atlas.core.errors import DataError
from atlas.core.market import Bar

_ALIASES = {
    "date": "date", "<date>": "date",
    "time": "time", "<time>": "time",
    "datetime": "datetime", "timestamp": "datetime",
    "open": "open", "<open>": "open",
    "high": "high", "<high>": "high",
    "low": "low", "<low>": "low",
    "close": "close", "<close>": "close",
    "volume": "volume", "vol": "volume", "<vol>": "volume",
    "tickvol": "volume", "<tickvol>": "volume",
    "spread": "spread", "<spread>": "spread",
}


def _parse_ts(row: dict[str, str], server_offset_hours: float) -> int:
    if row.get("datetime"):
        raw = row["datetime"]
    elif "date" in row:
        raw = f"{row['date']} {row.get('time', '00:00:00')}".strip()
    else:
        raise DataError(f"row has no recognisable timestamp column: {sorted(row)}")
    raw = raw.replace(".", "-").replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=UTC)
            break
        except ValueError:
            continue
    else:
        raise DataError(f"unparseable timestamp: {raw!r}")
    return int(dt.timestamp() * 1000) - int(server_offset_hours * 3_600_000)


def read_bars(
    path: Path | str,
    symbol: str,
    tf: Timeframe,
    *,
    server_offset_hours: float,
    default_spread_points: float = 0.0,
) -> list[Bar]:
    """Read bars from CSV, converting server time to UTC.

    ``server_offset_hours`` is mandatory and has no default on purpose (see module docstring).
    """
    p = Path(path)
    if not p.exists():
        raise DataError(f"no such file: {p}")
    text = p.read_text(encoding="utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel  # single-column or unusual file; let the reader fail loudly later
    reader = csv.reader(text.splitlines(), dialect)
    rows = list(reader)
    if len(rows) < 2:
        raise DataError(f"{p} has no data rows")
    header = [_ALIASES.get(h.strip().lower(), h.strip().lower()) for h in rows[0]]
    required = {"open", "high", "low", "close"}
    if not required.issubset(header):
        raise DataError(f"{p}: missing columns {sorted(required - set(header))}; got {header}")

    out: list[Bar] = []
    for raw in rows[1:]:
        if not raw or all(not c.strip() for c in raw):
            continue
        row = dict(zip(header, raw, strict=False))
        ts = _parse_ts(row, server_offset_hours)
        out.append(
            Bar(
                symbol=symbol, tf=tf, ts=ts,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
                spread_points=float(row.get("spread") or default_spread_points),
                complete=True,
            )
        )
    out.sort(key=lambda b: b.ts)
    _validate(out, tf)
    return out


def _validate(bars: Sequence[Bar], tf: Timeframe) -> None:
    """Reject data that would produce plausible-looking but wrong indicator values."""
    if not bars:
        raise DataError("no bars parsed")
    bad = [b for b in bars if not (b.low <= b.open <= b.high and b.low <= b.close <= b.high)]
    if bad:
        raise DataError(f"{len(bad)} bars violate low <= open/close <= high, first at {bad[0].ts}")
    dupes = len(bars) - len({b.ts for b in bars})
    if dupes:
        raise DataError(f"{dupes} duplicate timestamps in the series")


def write_bars(path: Path | str, bars: Iterable[Bar]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["datetime", "open", "high", "low", "close", "volume", "spread"])
        for b in bars:
            dt = datetime.fromtimestamp(b.ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([dt, b.open, b.high, b.low, b.close, b.volume, b.spread_points])
            n += 1
    return n


def gap_report(bars: Sequence[Bar], tf: Timeframe, *, max_gap_multiple: int = 3) -> list[str]:
    """Find suspicious holes in a bar series.

    Weekend gaps are expected and filtered out; anything else is either missing data or a
    market halt, and both change what a backtest means.
    """
    from atlas.core.market import ms_to_dt

    step = tf.seconds * 1000
    issues: list[str] = []
    for prev, cur in pairwise(bars):
        gap = cur.ts - prev.ts
        if gap <= step * max_gap_multiple:
            continue
        d = ms_to_dt(prev.ts)
        if d.weekday() == 4 and d.hour >= 20:  # Friday close -> Sunday open
            continue
        issues.append(
            f"gap of {gap / step:.0f} bars between {ms_to_dt(prev.ts):%Y-%m-%d %H:%M} "
            f"and {ms_to_dt(cur.ts):%Y-%m-%d %H:%M}"
        )
    return issues
