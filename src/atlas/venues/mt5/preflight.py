"""Capture what a broker actually reports, and check a config against it -- offline.

The problem this solves is a round trip. Everything that decides position size at a real
broker -- ``digits``, ``point``, ``tick_value``, ``contract_size``, the lot grid, the stops
level, the supported filling modes, the server clock offset -- can only be read from a live
terminal. Whoever has the terminal is not necessarily whoever is working on the config, and
a value read off a screen and retyped is a value that can be retyped wrong.

So the terminal side captures **one machine-readable artifact** (``atlas bridge-check
--json``) and the config side checks against it (``atlas preflight``), with no terminal
present and no transcription in between. Every finding names the consequence, because
"digits: 2 vs 3" means nothing until it is spelled "every position would be sized ten times
too large".

Nothing here talks to a venue. Capture is a pure function of what the venue already
returned, and evaluation is a pure function of the artifact and the config, which is what
makes both testable without MetaTrader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from atlas.config.settings import AtlasSettings
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Quote
from atlas.core.trading import AccountState
from atlas.execution.venue import VenueHealth

ARTIFACT_KIND = "atlas-bridge-check"
ARTIFACT_VERSION = 1

Level = Literal["PASS", "WARN", "FAIL"]

#: Spec fields that change the size of a position, or whether an order is accepted at all.
#: A disagreement between the config and the broker on any of these is a hard failure --
#: the config is what sized the order, and the broker is what will fill it.
SIZING_FIELDS = (
    "digits", "point", "tick_size", "tick_value", "contract_size",
    "volume_min", "volume_max", "volume_step",
)


@dataclass(frozen=True, slots=True)
class Finding:
    level: Level
    check: str
    message: str
    fix: str = ""

    def __str__(self) -> str:
        tail = f"  -> {self.fix}" if self.fix else ""
        return f"[{self.level}] {self.check}: {self.message}{tail}"


@dataclass
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: Level, check: str, message: str, fix: str = "") -> None:
        self.findings.append(Finding(level, check, message, fix))

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "FAIL"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "WARN"]

    @property
    def ok(self) -> bool:
        return not self.failures


# --- capture -----------------------------------------------------------------------------


def capture(
    *,
    health: VenueHealth,
    account: AccountState,
    specs: dict[str, SymbolSpec],
    quotes: dict[str, Quote],
    requested: list[str],
    captured_at_ms: int,
    impl: str = "",
    build: int = 0,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Build the artifact. Pure: everything here was already returned by the venue."""
    return {
        "artifact": ARTIFACT_KIND,
        "version": ARTIFACT_VERSION,
        "captured_at_ms": captured_at_ms,
        "connection": {
            "connected": bool(health.connected),
            "trade_allowed": bool(health.trade_allowed),
            "latency_ms": round(float(health.latency_ms or 0.0), 1),
            "server_offset_seconds": health.server_offset_seconds,
            "impl": impl,
            "build": build,
        },
        "account": {
            "login": account.login, "server": account.server, "currency": account.currency,
            "balance": account.balance, "equity": account.equity,
            "margin": account.margin, "free_margin": account.free_margin,
            "leverage": account.leverage, "trade_allowed": account.trade_allowed,
        },
        "symbols_requested": list(requested),
        "specs": {name: json.loads(spec.model_dump_json()) for name, spec in specs.items()},
        "quotes": {
            name: {
                "bid": q.bid, "ask": q.ask, "ts": q.ts,
                "spread_points": round(q.spread_points(specs[name].point), 1),
            }
            for name, q in quotes.items()
            if name in specs
        },
        "missing_symbols": sorted(set(requested) - set(specs)),
        "errors": list(errors or []),
    }


def load_artifact(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such artifact: {p}")
    obj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{p} is not a bridge-check artifact (expected a JSON object)")
    return obj


# --- evaluation ---------------------------------------------------------------------------


def evaluate(cfg: AtlasSettings, artifact: dict[str, Any]) -> PreflightReport:
    """Check a config against what the broker reported. No venue, no network."""
    report = PreflightReport()

    kind = artifact.get("artifact")
    version = artifact.get("version")
    if kind != ARTIFACT_KIND:
        report.add("FAIL", "artifact", f"this is not a bridge-check artifact (got {kind!r})",
                   "run: atlas bridge-check atlas.json --json evidence/bridge-check.json")
        return report
    if version != ARTIFACT_VERSION:
        report.add("FAIL", "artifact",
                   f"artifact version {version} was written by a different ATLAS build "
                   f"(this one reads version {ARTIFACT_VERSION})",
                   "re-capture with this build rather than translating by hand")
        return report
    report.add("PASS", "artifact", f"{ARTIFACT_KIND} v{version}")

    _check_connection(artifact, report)
    _check_symbols(cfg, artifact, report)
    _check_risk_floor(cfg, artifact, report)
    return report


def _check_connection(artifact: dict[str, Any], report: PreflightReport) -> None:
    conn = artifact.get("connection") or {}
    account = artifact.get("account") or {}

    if not conn.get("connected"):
        report.add("FAIL", "connection", "the terminal was not connected when this was captured",
                   "start ATLAS first -- it listens; the terminal connects out")
        return
    report.add("PASS", "connection",
               f"{conn.get('impl') or 'terminal'} build {conn.get('build')}, "
               f"{conn.get('latency_ms')} ms")

    if not conn.get("trade_allowed") or not account.get("trade_allowed"):
        report.add("FAIL", "trading permitted",
                   "the terminal reports that algorithmic trading is disabled; every order "
                   "would come back 10027 CLIENT_DISABLES_AT",
                   "enable Algo Trading in the toolbar and in the EA's own settings, and "
                   "confirm the server has not disabled expert trading for the account")
    else:
        report.add("PASS", "trading permitted", "algorithmic trading is enabled")

    offset = conn.get("server_offset_seconds")
    if offset is None:
        report.add("FAIL", "server clock",
                   "no server offset was measured, so broker timestamps cannot be converted",
                   "the offset comes from the hello frame; a terminal that omits it is a "
                   "bridge bug, not a configuration problem")
    else:
        hours = int(offset) / 3600
        report.add("PASS", "server clock", f"broker server time is UTC{hours:+.1f}")
        if int(offset) % 900 != 0:
            report.add("WARN", "server clock",
                       f"the measured offset ({offset} s) is not a whole quarter hour, which "
                       f"suggests it was inferred from a stale tick",
                       "re-capture while the market is open")

    errors = artifact.get("errors") or []
    for message in errors:
        report.add("WARN", "capture", f"the capture reported: {message}")


def _check_symbols(cfg: AtlasSettings, artifact: dict[str, Any], report: PreflightReport) -> None:
    specs = artifact.get("specs") or {}
    quotes = artifact.get("quotes") or {}
    configured = [s.symbol for s in cfg.symbols]

    for missing in artifact.get("missing_symbols") or []:
        report.add("FAIL", f"symbol {missing}",
                   "the broker does not offer this symbol",
                   "check for a suffix -- XAUUSD.m, XAUUSD_i, XAUUSD.pro and GOLD are all "
                   "real names for the same instrument at different brokers")

    for settings in cfg.symbols:
        name = settings.symbol
        payload = specs.get(name)
        if payload is None:
            if name not in (artifact.get("missing_symbols") or []):
                report.add("FAIL", f"symbol {name}",
                           "the config trades this symbol but the capture did not include it",
                           f"re-capture with: atlas bridge-check --symbol {name}")
            continue
        try:
            broker = SymbolSpec.model_validate(payload)
        except Exception as exc:
            report.add("FAIL", f"symbol {name}", f"the broker's specification is unusable: {exc}")
            continue

        _check_spec_sanity(name, broker, report)
        _check_spec_agreement(name, settings.spec, broker, report)

        quote = quotes.get(name)
        if quote is not None:
            report.add("PASS", f"spread {name}",
                       f"{quote.get('spread_points')} points at capture "
                       f"(bid {quote.get('bid')} / ask {quote.get('ask')})")

    for name in specs:
        if name not in configured:
            report.add("WARN", f"symbol {name}",
                       "the broker offers this symbol but the config does not trade it")


def _check_spec_sanity(name: str, broker: SymbolSpec, report: PreflightReport) -> None:
    if broker.tick_value <= 0:
        report.add("FAIL", f"spec {name}",
                   "the broker reports tick_value 0, and every position size is derived "
                   "from it -- sizing would divide by zero or silently produce nothing",
                   "some brokers only populate it once the symbol is in Market Watch; "
                   "select it there and re-capture")
    if not broker.trade_allowed:
        report.add("FAIL", f"spec {name}", "trading is disabled for this symbol at the broker")

    if not broker.filling_modes:
        report.add("FAIL", f"spec {name}",
                   "the broker reported no supported filling mode; every order would be "
                   "10030 INVALID_FILL")
    if broker.stops_level_points == 0:
        report.add("WARN", f"spec {name}",
                   "the broker reports a stops level of 0, which means a DYNAMIC level tied "
                   "to the current spread rather than no restriction",
                   "the bridge already substitutes a spread-based floor; be aware that a "
                   "tight stop may still be rejected at news time")

    implied = round(broker.point, 10)
    expected = round(10.0 ** -broker.digits, 10)
    if implied != expected:
        report.add("WARN", f"spec {name}",
                   f"point {broker.point} does not match digits {broker.digits} "
                   f"(which implies {expected})",
                   "unusual but legal; confirm it is deliberate before trusting any "
                   "point-based distance")


def _check_spec_agreement(
    name: str, configured: SymbolSpec | None, broker: SymbolSpec, report: PreflightReport
) -> None:
    if configured is None:
        report.add("PASS", f"spec {name}",
                   "no override in the config; the broker's values will be used live")
        return
    mismatched = [
        (f, getattr(configured, f), getattr(broker, f))
        for f in SIZING_FIELDS
        if getattr(configured, f) != getattr(broker, f)
    ]
    if not mismatched:
        report.add("PASS", f"spec {name}", "the config matches the broker on every sizing field")
        return
    for field_name, ours, theirs in mismatched:
        report.add(
            "FAIL", f"spec {name}",
            f"{field_name}: the config says {ours}, the broker says {theirs}",
            "adopt the broker's values -- the config is what sizes the order and the broker "
            "is what fills it. Run: atlas preflight <config> <artifact> --adopt-specs",
        )
    if configured.stops_level_points != broker.stops_level_points:
        report.add("WARN", f"spec {name}",
                   f"stops_level: the config says {configured.stops_level_points}, the broker "
                   f"says {broker.stops_level_points}",
                   "a config that under-states it produces orders rejected with 10016")


def _check_risk_floor(
    cfg: AtlasSettings, artifact: dict[str, Any], report: PreflightReport
) -> None:
    """Can the smallest lot the broker allows fit inside the risk budget at all?

    This is the failure that looks like nothing happening: every signal sizes below the
    minimum lot, ``normalize_volume`` correctly returns zero rather than rounding up, and
    the system stands aside forever without anything looking broken.
    """
    equity = float((artifact.get("account") or {}).get("equity") or 0.0)
    if equity <= 0:
        return
    budget = equity * cfg.risk.risk_per_trade_pct / 100.0
    specs = artifact.get("specs") or {}
    for settings in cfg.symbols:
        payload = specs.get(settings.symbol)
        if payload is None:
            continue
        try:
            broker = SymbolSpec.model_validate(payload)
        except Exception:
            continue
        stop_points = broker.min_stop_distance_points()
        if stop_points <= 0 or broker.tick_value <= 0:
            continue
        smallest = broker.money_for_points(stop_points, broker.volume_min)
        if smallest > budget:
            report.add(
                "FAIL", f"risk floor {settings.symbol}",
                f"the smallest lot the broker allows ({broker.volume_min}) risks "
                f"{smallest:.2f} {artifact['account'].get('currency', '')} over the minimum "
                f"stop distance, but {cfg.risk.risk_per_trade_pct}% of {equity:.2f} is only "
                f"{budget:.2f} -- every signal would size to zero and the system would "
                f"stand aside indefinitely without appearing broken",
                "raise risk_per_trade_pct, fund the account further, or trade a symbol "
                "with a smaller contract size",
            )
        else:
            report.add("PASS", f"risk floor {settings.symbol}",
                       f"minimum lot risks {smallest:.2f} against a {budget:.2f} budget")


# --- adopting the broker's values -----------------------------------------------------------


def adopt_specs(config_path: Path | str, artifact: dict[str, Any]) -> list[str]:
    """Write the broker's specifications into the config, in place.

    Retyping a spec is how a digit goes missing. Returns the symbols that changed.
    """
    path = Path(config_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    specs = artifact.get("specs") or {}
    changed: list[str] = []
    for entry in raw.get("symbols", []):
        payload = specs.get(entry.get("symbol"))
        if payload is None:
            continue
        if entry.get("spec") != payload:
            entry["spec"] = payload
            changed.append(entry["symbol"])
    if changed:
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return changed
