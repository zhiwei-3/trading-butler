# strategies/_template/__init__.py
from utils.common import signal_spec, target_reasons

from ..base import Strategy, Needs
from . import connectors
from .params import PARAMS
from .rules import find_breakout


class RangeBreakout(Strategy):
    label = "Range (Donchian) Breakout"
    needs = Needs(entry_bars=200, standard_analysis=False)      # no SMC analysis needed -> fast backtests
    params = PARAMS

    def is_dual(self, score, p):
        return bool(p["dual"])

    def valid_params(self, p):
        return p["tp2_rr"] > p["tp1_rr"]

    # ---- connectors
    def build_live_context(self, md, p):
        return connectors.live_context(md, p)

    def init_state(self, p):
        return {"fired": set()}

    def prepare_backtest(self, bt, p):
        return connectors.prepare(bt, p)

    def bar_context(self, prep, i, p, state):
        return connectors.bar_context(prep, i, p, state)

    # ---- live
    def evaluate(self, a, p, mem):
        fired = mem.setdefault("fired", set())
        found = find_breakout(a.get("ctx"), a["close_price"], a["atr_val"], p["min_rrr"], p, fired)
        if not found:
            return []
        fired.add(found["key"])

        d, t, (lo, hi) = found["direction"], found["targets"], found["range"]
        is_buy = d == "BUY"
        entry = a["close_price"]
        msg = (
            f"📦 **RANGE BREAKOUT {d} ALERT** {'🟢' if is_buy else '🔴'}\n\n"
            f"📍 **Entry:** `${entry}` | 📊 **14-ATR:** `${a['atr_val']}`\n"
            f"🛡️ **SL:** `${t['sl_price']}` ({t['sl_source']}, {t['sl_dist']} pts)\n"
            f"🎯 **TP1:** `${t['tp1_price']}` | 🎯 **TP2:** `${t['tp2_price']}` (RRR: `1:{t['rrr']}`)\n\n"
            f"📦 **Range ({p['lookback']} bars):** `${lo} – ${hi}`"
        )
        reasons = [
            f"Closed {'above' if is_buy else 'below'} the {p['lookback']}-bar range "
            f"{'high' if is_buy else 'low'} ({found['level']})",
        ] + target_reasons(t)
        return [signal_spec(d, entry, t["sl_price"], t["tp1_price"], t["tp2_price"], msg, score=60,
                            zone={"fvg_top": hi, "fvg_bottom": lo},
                            reasons=reasons, dual=self.is_dual(60, p))]

    # ---- backtest
    def decide(self, c):
        state, p = c["state"], c["p"]
        found = find_breakout(c["ctx"], c["close_price"], c["atr_val"], p["min_rrr"], p, state["fired"])
        if not found:
            return None
        state["fired"].add(found["key"])
        return {"direction": found["direction"], "entry": c["close_price"], "score": 60,
                "breakdown": [], **found["targets"]}


STRATEGY = RangeBreakout()