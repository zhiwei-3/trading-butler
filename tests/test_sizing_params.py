# tests/test_sizing_params.py
from strategies import Params, _initial_value
from strategies._lib import sizing_params, sizing_spec


def _defaults(**kw):
    return Params({**{prm.key: prm.default for prm in sizing_params(**kw)}})


def test_fixed_lots_by_default_and_risk_spec_when_enabled():
    p = _defaults()
    assert sizing_spec(p) is None
    assert sizing_spec(Params({**p, "dynamic_lots": True, "risk_pct": 1.0, "max_lot": 0.5})) == \
        {"risk_pct": 1.0, "max_lot": 0.5}


def test_legacy_values_seed_only_the_params_that_declare_them():
    legacy = {"risk_percent": 0.8, "max_lot_size": 0.2, "dynamic_lots_enabled": True}
    plain = {p.key: p for p in sizing_params()}
    assert _initial_value(plain["risk_pct"], legacy) == 0.8
    assert _initial_value(plain["max_lot"], legacy) == 0.2
    # a strategy that never sized dynamically must NOT flip on just because the old global was true
    assert _initial_value(plain["dynamic_lots"], legacy) is False

    opted_in = {p.key: p for p in sizing_params(dynamic=True, legacy_dynamic=True)}
    assert _initial_value(opted_in["dynamic_lots"], {"dynamic_lots_enabled": False}) is False
    assert _initial_value(opted_in["dynamic_lots"], {}) is True