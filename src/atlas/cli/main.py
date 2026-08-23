"""ATLAS command line.

Every command that produces a number also produces a file: a journal, a report, or both.
That is deliberate -- a result printed to a terminal cannot be diffed against last week's,
and "it was profitable when I ran it" is not a record.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from atlas import __version__
from atlas.core.enums import Timeframe
from atlas.core.errors import AtlasError

app = typer.Typer(
    name="atlas", help="ATLAS -- autonomous trading system for MetaTrader 5",
    no_args_is_help=True, add_completion=False,
)
console = Console()


def _fail(message: str) -> None:
    console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the ATLAS version."""
    console.print(f"ATLAS {__version__}")


@app.command()
def init(
    path: Path = typer.Option(Path("atlas.json"), "--out", "-o", help="where to write it"),
    force: bool = typer.Option(False, "--force", help="overwrite an existing file"),
) -> None:
    """Write an example configuration file."""
    from atlas.config.settings import example_settings

    if path.exists() and not force:
        _fail(f"{path} already exists; pass --force to overwrite")
    example_settings().save(path)
    console.print(f"wrote {path}")
    console.print("Edit the symbol spec to match YOUR broker before trading anything: "
                  "digits, contract size and stops level vary between brokers and a wrong "
                  "value silently changes every position size.")


@app.command()
def doctor(config: Path = typer.Argument(Path("atlas.json"))) -> None:
    """Check a configuration for the mistakes that matter."""
    from atlas.config.settings import AtlasSettings
    from atlas.risk.sizing import size_position
    from atlas.strategy.registry import build

    try:
        cfg = AtlasSettings.load(config)
    except AtlasError as exc:
        _fail(str(exc))
        return

    table = Table(title=f"atlas doctor -- {config}")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail")
    problems = 0

    def row(name: str, ok: bool, detail: str = "") -> None:
        nonlocal problems
        if not ok:
            problems += 1
        table.add_row(name, "[green]ok[/green]" if ok else "[red]PROBLEM[/red]", detail)

    row("symbols configured", bool(cfg.symbols), f"{len(cfg.symbols)} symbol(s)")
    for s in cfg.symbols:
        try:
            strat = build(s.strategy, s.parameters)
            row(f"strategy {s.symbol}", True, strat.describe())
        except AtlasError as exc:
            row(f"strategy {s.symbol}", False, str(exc))
            continue
        if s.spec is None:
            row(f"spec {s.symbol}", cfg.mode.value != "BACKTEST",
                "no spec in config; it will be read from the venue at connect")
            continue
        spec = s.spec
        row(f"spec {s.symbol}", spec.point > 0 and spec.tick_value > 0,
            f"digits={spec.digits} point={spec.point} tick_value={spec.tick_value} "
            f"contract={spec.contract_size} stops_level={spec.stops_level_points}")
        sizing = size_position(spec, cfg.risk.risk_per_trade_pct * 100, 1.0, 300)
        row(f"sizing sanity {s.symbol}", sizing.ideal_volume > 0,
            f"a 300-point stop on a nominal account gives {sizing.ideal_volume:.4f} lots")

    losses = cfg.risk.consecutive_losses_to_daily_breach()
    row("risk limits coherent", True,
        f"{losses:.1f} consecutive full-size losses reach the daily halt threshold")
    row("daily below total", cfg.risk.daily_loss_limit_pct < cfg.risk.total_drawdown_limit_pct,
        f"{cfg.risk.daily_loss_limit_pct}% daily vs {cfg.risk.total_drawdown_limit_pct}% total")
    if losses < 3:
        row("consecutive-loss headroom", False,
            f"only {losses:.1f} losses ends the day; a run of three is normal for a "
            f"40%-win-rate system")
    row("news policy", cfg.news_policy in ("warn", "block"),
        f"{cfg.news_policy}; calendar file: {cfg.news_file or 'none'}")
    if cfg.mode.value == "LIVE" and cfg.news_policy != "block" and not cfg.news_file:
        row("live news protection", False,
            "live mode with no calendar and news_policy='warn' will trade through NFP")
    row("feature window", cfg.feature_window >= cfg.feature_config().state_memory_bars + 50,
        f"{cfg.feature_window} bars vs {cfg.feature_config().state_memory_bars} state memory")
    if cfg.venue.kind == "mt5_bridge":
        row("bridge token", bool(cfg.venue.resolved_token()),
            "set ATLAS_BRIDGE_TOKEN or venue.token; an unauthenticated bridge accepts "
            "orders from anything that can reach the port")

    console.print(table)
    if problems:
        console.print(f"[bold red]{problems} problem(s) found[/bold red]")
        raise typer.Exit(code=1)
    console.print("[green]configuration looks sane[/green]")


@app.command()
def backtest(
    config: Path = typer.Argument(Path("atlas.json")),
    bars: int = typer.Option(60_000, "--bars", help="synthetic bars when no data file is set"),
    seed: int = typer.Option(99, "--seed"),
    out: Path = typer.Option(Path("runs/backtest"), "--out", "-o"),
    strategy: str | None = typer.Option(None, "--strategy", help="override the strategy"),
    baseline: bool = typer.Option(False, "--baseline", help="also run the DC1 control"),
) -> None:
    """Run a backtest and write a journal plus a summary."""
    from atlas.backtest.runner import BacktestConfig, run_backtest
    from atlas.config.settings import AtlasSettings

    cfg = AtlasSettings.load(config)
    data, specs, strategies, synthetic, label = _load_market(cfg, bars, seed, strategy)
    results = []
    for name, strat_map in _strategy_variants(strategies, baseline, cfg):
        res = asyncio.run(run_backtest(
            bars=data, specs=specs, strategies=strat_map, journal_dir=out / name,
            config=BacktestConfig(
                base_tf=cfg.base_timeframe, warmup_bars=cfg.warmup_bars,
                starting_balance=cfg.venue.starting_balance, leverage=cfg.venue.leverage,
                allowed_sessions=tuple(cfg.allowed_sessions), news_policy=cfg.news_policy,
                magic=cfg.magic, synthetic=synthetic, data_source_label=label,
            ),
            risk_config=cfg.risk, feature_config=cfg.feature_config(), label=name,
        ))
        results.append((name, res))
        console.print(f"\n[bold]{name}[/bold]")
        console.print(res.summary())
    _write_summary(out, results)
    console.print(f"\njournals and summary written to {out}")
    if synthetic:
        console.print("[yellow]This ran on synthetic data. It validates the machinery, not "
                      "the strategy. Point --data at real history before drawing any "
                      "conclusion about the edge.[/yellow]")


@app.command()
def validate(
    config: Path = typer.Argument(Path("atlas.json")),
    bars: int = typer.Option(120_000, "--bars"),
    seed: int = typer.Option(99, "--seed"),
    out: Path = typer.Option(Path("runs/validation"), "--out", "-o"),
    oos_fraction: float = typer.Option(0.3, "--oos", help="fraction held out"),
) -> None:
    """Run the validation battery and write a pass/fail report."""
    from atlas.backtest.runner import BacktestConfig, run_backtest
    from atlas.backtest.validation import assemble_report
    from atlas.config.settings import AtlasSettings

    cfg = AtlasSettings.load(config)
    data, specs, strategies, synthetic, label = _load_market(cfg, bars, seed, None)
    symbol = next(iter(data))
    series = data[symbol]
    split = int(len(series) * (1 - oos_fraction))

    def run(subset, tag):
        return asyncio.run(run_backtest(
            bars={symbol: subset}, specs=specs, strategies=strategies,
            journal_dir=out / tag,
            config=BacktestConfig(
                base_tf=cfg.base_timeframe, warmup_bars=cfg.warmup_bars,
                starting_balance=cfg.venue.starting_balance,
                allowed_sessions=tuple(cfg.allowed_sessions), news_policy=cfg.news_policy,
                magic=cfg.magic, synthetic=synthetic, data_source_label=label,
                journal_no_setup=False,
            ),
            risk_config=cfg.risk, feature_config=cfg.feature_config(), label=tag,
        ))

    console.print("running in-sample...")
    is_res = run(series[:split], "in_sample")
    console.print("running out-of-sample...")
    oos_res = run(series[max(0, split - cfg.warmup_bars):], "out_of_sample")

    console.print("running cost sensitivity...")
    sensitivity = {}
    from atlas.venues.sim.costs import CostModel
    for mult in (1.5, 2.0):
        r = asyncio.run(run_backtest(
            bars={symbol: series[max(0, split - cfg.warmup_bars):]}, specs=specs,
            strategies=strategies, journal_dir=out / f"cost_{mult}",
            config=BacktestConfig(
                base_tf=cfg.base_timeframe, warmup_bars=cfg.warmup_bars,
                starting_balance=cfg.venue.starting_balance,
                allowed_sessions=tuple(cfg.allowed_sessions), magic=cfg.magic,
                synthetic=synthetic, data_source_label=label,
            ),
            risk_config=cfg.risk, feature_config=cfg.feature_config(),
            costs=CostModel(commission_per_lot_per_side=3.5 * mult,
                            entry_slippage_points=2.0 * mult,
                            stop_slippage_points=6.0 * mult),
            label=f"cost_{mult}",
        ))
        sensitivity[f"{mult}x costs"] = r.metrics.expectancy_r

    report = assemble_report(
        label=cfg.name, data_source=label, synthetic=synthetic,
        is_trades=is_res.trades, oos_trades=oos_res.trades,
        cost_sensitivity=sensitivity, starting_equity=cfg.venue.starting_balance,
    )
    out.mkdir(parents=True, exist_ok=True)
    report.save(out / "validation_report.txt")
    console.print("\n" + report.render())
    console.print(f"\nreport written to {out / 'validation_report.txt'}")
    if not report.approved:
        raise typer.Exit(code=2)


@app.command()
def serve(
    config: Path = typer.Argument(Path("atlas.json")),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    run_dir: Path = typer.Option(Path("runs/live"), "--run-dir"),
) -> None:
    """Start the dashboard API against a run directory."""
    import uvicorn

    from atlas.api.app import create_app
    from atlas.config.settings import AtlasSettings

    cfg = AtlasSettings.load(config)
    application = create_app(cfg, run_dir)
    uvicorn.run(application, host=host or cfg.api_host, port=port or cfg.api_port,
                log_level="info")


@app.command()
def live(
    config: Path = typer.Argument(Path("atlas.json")),
    paper: bool = typer.Option(False, "--paper", help="route orders to the simulator"),
    confirm: bool = typer.Option(False, "--yes", help="skip the live-trading confirmation"),
) -> None:
    """Run the engine against a MetaTrader 5 bridge."""
    from atlas.config.settings import AtlasSettings
    from atlas.runtime.live import run_live

    cfg = AtlasSettings.load(config)
    if not paper and not confirm:
        console.print("[bold yellow]This will place real orders on the connected account."
                      "[/bold yellow]")
        console.print(f"  venue     : {cfg.venue.kind} @ {cfg.venue.command_endpoint}")
        console.print(f"  symbols   : {[s.symbol for s in cfg.symbols]}")
        console.print(f"  risk/trade: {cfg.risk.risk_per_trade_pct}%  daily halt "
                      f"{cfg.risk.daily_loss_limit_pct}%  total halt "
                      f"{cfg.risk.total_drawdown_limit_pct}%")
        if not typer.confirm("proceed?"):
            raise typer.Exit(code=1)
    asyncio.run(run_live(cfg, paper=paper))


@app.command("bridge-check")
def bridge_check(
    config: Path = typer.Argument(Path("atlas.json")),
    symbol: str | None = typer.Option(None, "--symbol"),
) -> None:
    """Probe a running MT5 bridge: connectivity, specs, clock offset, permissions."""
    from atlas.config.settings import AtlasSettings
    from atlas.venues.mt5.bridge import BridgeVenue

    cfg = AtlasSettings.load(config)
    syms = [symbol] if symbol else [s.symbol for s in cfg.symbols]

    async def probe():
        venue = BridgeVenue(
            command_endpoint=cfg.venue.command_endpoint,
            event_endpoint=cfg.venue.event_endpoint,
            token=cfg.venue.resolved_token(),
            request_timeout_ms=cfg.venue.request_timeout_ms,
        )
        try:
            health = await venue.connect()
            console.print(f"connected: {health.connected}  trade_allowed: "
                          f"{health.trade_allowed}  latency {health.latency_ms:.0f} ms")
            console.print(f"server offset: {health.server_offset_seconds} s "
                          f"({(health.server_offset_seconds or 0) / 3600:+.1f} h from UTC)")
            acct = await venue.account()
            console.print(f"account {acct.login}@{acct.server} {acct.currency} "
                          f"balance {acct.balance:.2f} equity {acct.equity:.2f} "
                          f"leverage 1:{acct.leverage}")
            specs = await venue.symbol_specs(syms)
            table = Table(title="symbol specifications (from the broker)")
            for col in ("symbol", "digits", "point", "tick_value", "contract",
                        "vol min/step/max", "stops_lvl", "filling"):
                table.add_column(col)
            for name, sp in specs.items():
                table.add_row(name, str(sp.digits), str(sp.point), str(sp.tick_value),
                              str(sp.contract_size),
                              f"{sp.volume_min}/{sp.volume_step}/{sp.volume_max}",
                              str(sp.stops_level_points),
                              ",".join(str(f) for f in sp.filling_modes))
            console.print(table)
            missing = set(syms) - set(specs)
            if missing:
                console.print(f"[red]not available at the broker: {sorted(missing)}[/red] "
                              f"-- check for a suffix such as .m or _i")
            for name in specs:
                q = await venue.quote(name)
                console.print(f"{name}: bid {q.bid} ask {q.ask} spread "
                              f"{q.spread_points(specs[name].point):.0f} pts")
        finally:
            await venue.disconnect()

    try:
        asyncio.run(probe())
    except AtlasError as exc:
        _fail(str(exc))


@app.command("show-decisions")
def show_decisions(
    run_dir: Path = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", "-n"),
    outcome: str | None = typer.Option(None, "--outcome"),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    """Explain recent decisions from a run's journal."""
    from atlas.backtest.runner import load_decisions

    records = load_decisions(run_dir)
    if outcome:
        records = [r for r in records if r["outcome"] == outcome.upper()]
    if reason:
        records = [r for r in records if r["reason_code"] == reason.upper()]
    if not records:
        console.print("no matching decisions")
        return
    from collections import Counter

    counts = Counter(r["reason_code"] for r in records)
    table = Table(title=f"{len(records)} decisions in {run_dir}")
    table.add_column("reason")
    table.add_column("count", justify="right")
    table.add_column("share", justify="right")
    for code, n in counts.most_common():
        table.add_row(code, str(n), f"{n / len(records):.1%}")
    console.print(table)

    console.print(f"\n[bold]last {min(limit, len(records))} decisions[/bold]")
    for rec in records[-limit:]:
        from atlas.core.decision import DecisionRecord

        d = DecisionRecord(**rec)
        console.print(f"  {d.ts}  {d.explain()}")
        for g in d.gates:
            if not g.passed:
                console.print(f"      [red]x[/red] {g.name}: {g.value} {g.comparison} "
                              f"{g.threshold}  {g.detail}")


@app.command("analyse")
def analyse(
    run_dir: Path = typer.Argument(...),
    starting_balance: float = typer.Option(10_000.0, "--balance"),
) -> None:
    """Performance and decision analytics for a completed run."""
    from atlas.analytics.report import analyse_run

    try:
        console.print(analyse_run(run_dir, starting_balance))
    except AtlasError as exc:
        _fail(str(exc))


# --- helpers -----------------------------------------------------------------------


def _load_market(cfg, bars: int, seed: int, strategy_override: str | None):
    from atlas.core.instrument import SymbolSpec
    from atlas.data.csv_io import read_bars
    from atlas.data.synthetic import SyntheticConfig, generate
    from atlas.strategy.registry import build

    data: dict[str, list] = {}
    specs: dict[str, SymbolSpec] = {}
    strategies = {}
    synthetic = False
    labels = []
    for s in cfg.symbols:
        if s.spec is None:
            _fail(f"{s.symbol}: offline runs need a symbol spec in the config "
                  f"(run 'atlas bridge-check' against a live terminal to read the real one)")
        specs[s.symbol] = s.spec
        if s.data_file:
            if s.server_offset_hours is None:
                _fail(f"{s.symbol}: data_file is set but server_offset_hours is not. MT5 "
                      f"exports are in broker server time and guessing the offset shifts "
                      f"every session filter.")
            data[s.symbol] = read_bars(s.data_file, s.symbol, cfg.base_timeframe,
                                       server_offset_hours=s.server_offset_hours)
            labels.append(f"{s.symbol}:{Path(s.data_file).name}")
        else:
            synthetic = True
            data[s.symbol] = generate(SyntheticConfig(
                symbol=s.symbol, bars=bars, seed=seed, tf=cfg.base_timeframe,
                point=s.spec.point,
            ))
            labels.append(f"{s.symbol}:synthetic(seed={seed},bars={bars})")
        strategies[s.symbol] = build(strategy_override or s.strategy, s.parameters)
    return data, specs, strategies, synthetic, "; ".join(labels)


def _strategy_variants(strategies, baseline: bool, cfg):
    out = [("primary", strategies)]
    if baseline:
        from atlas.strategy.registry import build

        tf = cfg.symbols[0].parameters.get("mtf", Timeframe.H1)
        out.append(("baseline_DC1", {s: build("DC1", {"tf": tf}) for s in strategies}))
    return out


def _write_summary(out: Path, results) -> None:
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        name: {
            "summary": res.summary(),
            "metrics": res.metrics.to_dict(),
            "engine": {
                "decisions": res.stats.decisions, "signals": res.stats.signals,
                "orders_accepted": res.stats.orders_accepted,
                "decision_reasons": res.stats.decision_reasons,
                "risk_rejections": res.stats.risk_rejections,
                "halts": res.stats.halts,
            },
            "parameters": res.strategy_params,
            "synthetic": res.synthetic,
        }
        for name, res in results
    }
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str),
                                      encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    app()
