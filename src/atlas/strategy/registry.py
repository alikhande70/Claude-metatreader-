"""Strategy registry.

Strategies are constructed from configuration by name so a run can be reproduced from its
journalled config alone, without importing the module that happened to be in scope.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, replace
from typing import Any

from atlas.core.enums import Timeframe
from atlas.core.errors import ConfigError
from atlas.strategy.base import Strategy
from atlas.strategy.baseline import DC1Params, DonchianBreakout
from atlas.strategy.structure_momentum import SM1Params, StructureMomentum

_BUILDERS: dict[str, tuple[Callable[..., Strategy], type]] = {
    "SM1": (StructureMomentum, SM1Params),
    "DC1": (DonchianBreakout, DC1Params),
}


def available() -> tuple[str, ...]:
    return tuple(_BUILDERS)


def build(name: str, params: dict[str, Any] | None = None) -> Strategy:
    """Instantiate a strategy by name with parameter overrides.

    Unknown parameter names are a hard error rather than being ignored: a typo in a config
    that silently leaves a parameter at its default is a bug that shows up as an inexplicable
    difference between two runs.
    """
    key = name.upper()
    if key not in _BUILDERS:
        raise ConfigError(f"unknown strategy {name!r}; available: {', '.join(available())}")
    cls, param_cls = _BUILDERS[key]
    base = param_cls()
    if params:
        valid = {f.name: f.type for f in fields(param_cls)}
        unknown = set(params) - set(valid)
        if unknown:
            raise ConfigError(
                f"{key}: unknown parameters {sorted(unknown)}; valid: {sorted(valid)}"
            )
        coerced = {
            k: (Timeframe(v) if isinstance(getattr(base, k), Timeframe) else v)
            for k, v in params.items()
        }
        base = replace(base, **coerced)
    return cls(base)
