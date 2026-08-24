"""Preflight: does this config trade correctly against what the broker actually reported?

Every check here corresponds to a failure that is quiet at the broker. A wrong `digits`
does not throw; it sizes every position by a factor of ten. A minimum lot that cannot fit
inside the risk budget does not throw either; the system simply stands aside forever and
looks healthy while doing it.

These tests use handmade artifacts rather than a live capture, which is the point: the whole
value of the artifact is that it can be reasoned about with no terminal present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.config.settings import AtlasSettings, SymbolSettings, VenueSettings
from atlas.core.instrument import SymbolSpec
from atlas.risk.config import RiskConfig
from atlas.venues.mt5 import preflight as pf


def artifact_for(spec: SymbolSpec, *, equity: float = 25_000.0, **overrides) -> dict:
    base = {
        "artifact": pf.ARTIFACT_KIND,
        "version": pf.ARTIFACT_VERSION,
        "captured_at_ms": 1_700_000_000_000,
        "connection": {
            "connected": True, "trade_allowed": True, "latency_ms": 12.0,
            "server_offset_seconds": 3 * 3600, "impl": "mql5-ea", "build": 4150,
        },
        "account": {
            "login": 5001, "server": "Broker-Demo", "currency": "USD",
            "balance": equity, "equity": equity, "margin": 0.0, "free_margin": equity,
            "leverage": 100, "trade_allowed": True,
        },
        "symbols_requested": [spec.name],
        "specs": {spec.name: json.loads(spec.model_dump_json())},
        "quotes": {spec.name: {"bid": 2399.90, "ask": 2400.10, "ts": 1_700_000_000_000,
                               "spread_points": 20.0}},
        "missing_symbols": [],
        "errors": [],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def config_for(spec: SymbolSpec | None, name: str = "XAUUSD", **risk) -> AtlasSettings:
    return AtlasSettings(
        symbols=[SymbolSettings(symbol=name, spec=spec)],
        venue=VenueSettings(kind="mt5_bridge"),
        risk=RiskConfig(**risk),
    )


def levels(report: pf.PreflightReport, check_prefix: str) -> list[str]:
    return [f.level for f in report.findings if f.check.startswith(check_prefix)]


# --- the artifact itself ---------------------------------------------------------------


def test_a_foreign_file_is_refused_rather_than_half_read(gold):
    report = pf.evaluate(config_for(gold), {"hello": "world"})
    assert not report.ok
    assert "not a bridge-check artifact" in report.failures[0].message


def test_an_artifact_from_a_different_build_is_refused(gold):
    report = pf.evaluate(config_for(gold), artifact_for(gold, version=99))
    assert not report.ok
    assert "version 99" in report.failures[0].message


# --- the happy path --------------------------------------------------------------------


def test_a_matching_config_passes_cleanly(gold):
    report = pf.evaluate(config_for(gold), artifact_for(gold))
    assert report.ok, [str(f) for f in report.failures]
    assert any(f.check.startswith("spec XAUUSD") and f.level == "PASS" for f in report.findings)


def test_no_override_is_fine_because_the_venue_supplies_the_spec(gold):
    report = pf.evaluate(config_for(None), artifact_for(gold))
    assert report.ok
    assert any("no override" in f.message for f in report.findings)


# --- the failures that are quiet at the broker -------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("digits", 3),
        ("point", 0.001),
        ("tick_value", 10.0),
        ("contract_size", 10.0),
        ("volume_step", 0.1),
        ("volume_min", 0.10),
    ],
)
def test_every_sizing_field_disagreement_is_a_failure(gold, field, value):
    """These are the numbers that turn into lot sizes. A config that disagrees with the
    broker on any of them sizes correctly by its own arithmetic and wrongly in fact."""
    broker = gold.model_copy(update={field: value})
    report = pf.evaluate(config_for(gold), artifact_for(broker))
    assert not report.ok
    assert any(field in f.message for f in report.failures), [str(f) for f in report.failures]


def test_a_zero_tick_value_is_a_failure_not_a_default(gold):
    report = pf.evaluate(config_for(None), artifact_for(gold.model_copy(update={
        "tick_value": 0.0,
        # tick_value is validated on the model, so the artifact carries the raw broker value.
    })))
    assert not report.ok


def test_a_missing_symbol_names_the_suffix_as_the_likely_cause(gold):
    art = artifact_for(gold)
    art["specs"] = {}
    art["missing_symbols"] = ["XAUUSD"]
    report = pf.evaluate(config_for(gold), art)
    assert not report.ok
    assert any("suffix" in f.fix for f in report.failures)


def test_algo_trading_disabled_is_a_failure(gold):
    report = pf.evaluate(config_for(gold),
                         artifact_for(gold, connection={"trade_allowed": False}))
    assert not report.ok
    assert any("10027" in f.message for f in report.failures)


def test_a_symbol_the_broker_has_disabled_is_a_failure(gold):
    report = pf.evaluate(config_for(None),
                         artifact_for(gold.model_copy(update={"trade_allowed": False})))
    assert not report.ok


def test_no_filling_mode_is_a_failure(gold):
    art = artifact_for(gold)
    art["specs"]["XAUUSD"]["filling_modes"] = []
    report = pf.evaluate(config_for(None), art)
    assert not report.ok
    assert any("10030" in f.message for f in report.failures)


def test_a_disconnected_capture_stops_the_report_there(gold):
    report = pf.evaluate(config_for(gold),
                         artifact_for(gold, connection={"connected": False}))
    assert not report.ok
    assert "not connected" in report.failures[0].message


def test_a_missing_server_offset_is_a_failure(gold):
    report = pf.evaluate(config_for(gold),
                         artifact_for(gold, connection={"server_offset_seconds": None}))
    assert not report.ok
    assert any("timestamps" in f.message for f in report.failures)


# --- the failure that looks like nothing happening ------------------------------------


def test_a_minimum_lot_that_cannot_fit_the_risk_budget_is_a_failure(gold):
    """The quietest failure of all: every signal sizes below the minimum lot,
    normalize_volume correctly returns zero, and the system stands aside forever while
    every dashboard panel says it is healthy."""
    report = pf.evaluate(
        config_for(gold, risk_per_trade_pct=0.01),
        artifact_for(gold, equity=1_000.0),
    )
    assert not report.ok
    failure = next(f for f in report.failures if f.check.startswith("risk floor"))
    assert "stand aside indefinitely" in failure.message


def test_a_funded_account_clears_the_risk_floor(gold):
    report = pf.evaluate(config_for(gold, risk_per_trade_pct=0.5), artifact_for(gold))
    assert "FAIL" not in levels(report, "risk floor")


# --- warnings are decisions, not noise --------------------------------------------------


def test_a_dynamic_stops_level_warns_rather_than_failing(gold):
    """0 means "tied to the current spread", not "no restriction" -- worth knowing,
    not worth blocking."""
    report = pf.evaluate(config_for(None),
                         artifact_for(gold.model_copy(update={"stops_level_points": 0})))
    assert report.ok
    assert any("DYNAMIC" in f.message for f in report.warnings)


def test_a_symbol_the_config_ignores_only_warns(gold, eurusd):
    art = artifact_for(gold)
    art["specs"]["EURUSD"] = json.loads(eurusd.model_dump_json())
    report = pf.evaluate(config_for(gold), art)
    assert report.ok
    assert any(f.check == "symbol EURUSD" and f.level == "WARN" for f in report.findings)


def test_an_offset_that_is_not_a_quarter_hour_warns(gold):
    report = pf.evaluate(config_for(gold),
                         artifact_for(gold, connection={"server_offset_seconds": 10_123}))
    assert any("stale tick" in f.message for f in report.warnings)


# --- adopting the broker's values ---------------------------------------------------------


def test_adopt_specs_replaces_the_override_and_clears_the_failures(gold, tmp_path: Path):
    """Retyping a specification is how a digit goes missing, so it is not retyped."""
    broker = gold.model_copy(update={"digits": 3, "point": 0.001, "tick_value": 10.0})
    cfg = config_for(gold)
    path = tmp_path / "atlas.json"
    path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")

    art = artifact_for(broker)
    before = pf.evaluate(AtlasSettings.load(path), art)
    assert not before.ok

    changed = pf.adopt_specs(path, art)
    assert changed == ["XAUUSD"]

    after = pf.evaluate(AtlasSettings.load(path), art)
    assert after.ok, [str(f) for f in after.failures]
    reloaded = AtlasSettings.load(path)
    assert reloaded.symbols[0].spec is not None
    assert reloaded.symbols[0].spec.digits == 3


def test_adopt_specs_is_a_no_op_when_nothing_differs(gold, tmp_path: Path):
    path = tmp_path / "atlas.json"
    path.write_text(config_for(gold).model_dump_json(indent=2), encoding="utf-8")
    assert pf.adopt_specs(path, artifact_for(gold)) == []
