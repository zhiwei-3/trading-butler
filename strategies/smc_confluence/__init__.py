from config import ALERT_STATE
from utils.common import signal_spec

from ..base import Strategy, Needs
from .._lib import rsi_watch, rsi_valid, sizing_spec
from . import connectors, rules
from .params import PARAMS
from .scoring import build_reasons, format_confluence_breakdown


class SmcConfluence(Strategy):
    label = "Smart Money Concepts + Multi-TF Confluence"
    needs = Needs(
        entry_bars=200,
        features=("structure", "sweeps", "fvg", "order_block", "macd_bias",
                  "candle_pattern", "divergence", "vol_filter_ok", "near_zone"),
    )
    params = PARAMS

    def is_dual(self, score, p):
        """Two legs (TP1 + TP2/BE runner) only when the confluence score clears the threshold."""
        return score >= p["dual_score"]

    def valid_params(self, p):
        return rsi_valid(p)

    def summary(self, p):
        return (f"Score ≥{p['min_score']} · RSI {p['rsi_buy']:g}/{p['rsi_sell']:g} · "
                f"RRR 1:{p['min_rrr']:g} · SL {p['sl_mult']:g}x · dual ≥{p['dual_score']}")

    def diagnose(self, a, p):
        return (f"🏆 **SMC Confluence:** min score `{p['min_score']}` · "
                f"Buy ≤ `{p['rsi_buy']}` · Sell ≥ `{p['rsi_sell']}`\n"
                f"• **Structure:** `{a.get('structure')}` | **MACD:** `{a.get('macd_bias')}` | "
                f"**Candle:** `{a.get('candle_pattern')}`")

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
        found, mem["last"] = rules.scan(a, p, p["require_structure"], mem.get("last"))
        if not found:
            return []

        d, score, breakdown, t = found["direction"], found["score"], found["breakdown"], found["targets"]
        is_buy = d == "BUY"
        close_price, atr_val = a["close_price"], a["atr_val"]
        side = "bullish" if is_buy else "bearish"
        ob = a["order_block"]
        ob_txt = f"{'Bullish' if is_buy else 'Bearish'} OB ✅ @ ${ob['ob_level']}" if ob[f"{side}_ob"] else "None"

        msg = (
            f"🏆 **TRADING BUTLER SIGNAL ALERT** 🏆\n\n"
            f"• **Type:** `{'BUY 🟢' if is_buy else 'SELL 🔴'}` | **Timeframe:** {a['tf_label']}\n"
            f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${atr_val}`\n"
            f"🛡️ **SL:** `${t['sl_price']}` ({t['sl_source']}, {t['sl_dist']} pts)\n"
            f"🎯 **TP1:** `${t['tp1_price']}` ({t['tp1_source']}) | 🎯 **TP2:** `${t['tp2_price']}` (RRR: `1:{t['rrr']}`)\n\n"
            f"{format_confluence_breakdown(score, breakdown)}\n\n"
            f"💧 **Sweep:** {side.title() + ' Sweep ✅' if a['sweeps'][side + '_sweep'] else 'None'}\n"
            f"⚡ **FVG:** {side.title() + ' FVG ✅' if a['fvg'][side + '_fvg'] else 'None'}\n"
            f"🧱 **Order Block:** {ob_txt}"
        )
        return [signal_spec(d, close_price, t["sl_price"], t["tp1_price"], t["tp2_price"], msg,
                            score=score, reasons=build_reasons(d, a, score, breakdown, t, p),
                            dual=self.is_dual(score, p), risk=sizing_spec(p))]

    # ---- backtest
    def decide(self, c):
        state = c["state"]
        found, state["last"] = rules.scan(c["a"], c["p"], c["p"]["require_structure"], state.get("last"))
        if not found:
            return None
        return {"direction": found["direction"], "entry": c["close_price"], "score": found["score"],
                "breakdown": found["breakdown"], **found["targets"]}


STRATEGY = SmcConfluence()