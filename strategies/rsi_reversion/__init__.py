from utils.common import signal_spec, target_reasons, simple_breakdown

from ..base import Strategy, Needs
from .._lib import atr_targets, atr_targets_bt, rsi_watch, rsi_valid
from . import connectors, rules
from .params import PARAMS


class RsiReversion(Strategy):
    label = "RSI Overbought/Oversold S/R Zone Reversion"
    needs = Needs(entry_bars=200)       # standard analysis (S/R zones, order block, structure) is required
    params = PARAMS

    def is_dual(self, score, p):
        return False                    # always a single TP1-only leg

    def valid_params(self, p):
        return rsi_valid(p)

    def summary(self, p):
        return f"RSI {p['rsi_buy']:g}/{p['rsi_sell']:g} · RRR 1:{p['min_rrr']:g} · SL {p['sl_mult']:g}x"

    def diagnose(self, a, p):
        d = rules.setup(a["rsi_val"], a["close_price"], a.get("near_zone"), p)
        state = {"BUY": "🟢 BUY setup armed", "SELL": "🔴 SELL setup armed"}.get(d, "⚪ none")
        return (f"🔄 **RSI Reversion:** Buy ≤ `{p['rsi_buy']}` · Sell ≥ `{p['rsi_sell']}`\n"
                f"• **Trigger now:** {state}")

    # ---- heads-up
    def watch(self, a, p, mem):
        return rsi_watch(a, p, mem)

    # ---- backtest connectors
    def init_state(self, p):
        return {"last": None}

    def prepare_backtest(self, bt, p):
        return connectors.prepare(bt, p)

    def bar_context(self, prep, i, p, state):
        return connectors.bar_context(prep, i, p, state)

    # ---- live
    def evaluate(self, a, p, mem):
        direction = rules.setup(a["rsi_val"], a["close_price"], a["near_zone"], p)
        if direction is None:
            mem["last"] = None          # dedup clears once the trigger condition is gone
            return []
        if mem.get("last") == direction:
            return []

        targets = atr_targets(direction, a, p)
        if targets["rrr"] < p["min_rrr"]:
            return []
        mem["last"] = direction

        close_price = a["close_price"]
        icon = "🟢" if direction == "BUY" else "🔴"
        msg = (f"🔄 **RSI REVERSION {direction} ALERT** {icon}\n\n📍 **Entry:** `${close_price}` | "
               f"🛡️ **SL:** `${targets['sl_price']}` | 🎯 **TP1:** `${targets['tp1_price']}`")
        why = rules.reasons(direction, a["rsi_val"], a["near_zone"], p) + target_reasons(targets)
        return [signal_spec(direction, close_price, targets["sl_price"], targets["tp1_price"],
                            targets["tp2_price"], msg, score=rules.SCORE, reasons=why,
                            dual=self.is_dual(rules.SCORE, p))]

    # ---- backtest
    def decide(self, c):
        p, state = c["p"], c["state"]
        direction = rules.setup(c["rsi_val"], c["close_price"], c["near_zone"], p)
        if direction is None:
            state["last"] = None
            return None
        if state.get("last") == direction:
            return None

        targets = atr_targets_bt(direction, c)
        if targets["rrr"] < p["min_rrr"]:
            return None
        state["last"] = direction
        return {
            "direction": direction, "entry": c["close_price"], "score": rules.SCORE,
            "breakdown": simple_breakdown(direction == "BUY", c["close_price"], c["near_zone"],
                                          c["structure"], c["entry_bullish"], c["macro_bullish"]),
            **targets,
        }


STRATEGY = RsiReversion()