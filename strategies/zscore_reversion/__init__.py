from utils.common import signal_spec, target_reasons

from .._lib import sizing_spec
from ..base import Strategy, Needs
from . import connectors, rules
from .params import PARAMS


class ZscoreReversion(Strategy):
    label = "Z-Score Mean Reversion (range regime, TP at the mean)"
    needs = Needs(entry_bars=300)       # reads only the entry candles -> computes no analysis features
    params = PARAMS

    def is_dual(self, score, p):
        return bool(p["dual"])

    def valid_params(self, p):
        return p["stop_z"] > p["entry_z"] and p["max_sl_atr"] > p["min_sl_atr"]

    def summary(self, p):
        return (f"Z ±{p['entry_z']:g} (n={p['z_len']}) · stop Z{p['stop_z']:g} · ADX≤{p['adx_max']:g} · "
                f"RRR ≥{p['min_rrr']:g} · {'dual' if p['dual'] else 'single'}")

    def diagnose(self, a, p):
        ctx = a.get("ctx")
        if not ctx or not rules._finite(ctx["z"][-1], ctx["adx"], ctx["slope"]):
            return "📉 **Z-Score Reversion:** context unavailable (not enough entry-timeframe history)"
        atr = a["atr_val"]
        ok = rules.regime_ok(ctx, atr, p)
        return (f"📉 **Z-Score Reversion:** entry at |Z| ≥ `{p['entry_z']}` · stop `Z={p['stop_z']}`\n"
                f"• **Z now:** `{ctx['z'][-1]:.2f}` | **Mean:** `${ctx['sma']:.2f}`\n"
                f"• **Regime:** {'🟢 ranging' if ok else '🔴 trending / blocked'} "
                f"(ADX `{ctx['adx']:.1f}` / max `{p['adx_max']}`, "
                f"slope `{ctx['slope']:+.2f}` / max `±{p['slope_max_atr'] * atr:.2f}`)")

    # ---- heads-up: a qualifying stretch in a ranging market (debounced per excursion, independent of signal dedup)
    def watch(self, a, p, mem):
        ctx = a.get("ctx")
        if not ctx or not rules._finite(ctx["z"][-1]):
            return []
        z1 = float(ctx["z"][-1])
        if abs(z1) < p["entry_z"]:
            mem["watch_state"] = None
            return []
        side = "BUY" if z1 < 0 else "SELL"
        if mem.get("watch_state") == side or not rules.regime_ok(ctx, a["atr_val"], p):
            return []
        mem["watch_state"] = side
        icon = "🟢 BULLISH" if side == "BUY" else "🔴 BEARISH"
        return [
            f"👀 **{icon} STRETCH FORMING (EARLY HEADS-UP)** 👀\n\n"
            f"• **Symbol:** `{a.get('symbol', 'XAUUSD')}` | **Price:** `${a['close_price']}`\n"
            f"• **Z-score:** `{z1:.2f}` (trigger `±{p['entry_z']}`) | **Mean:** `${ctx['sma']:.2f}`\n"
            f"• **Regime:** ranging ✅ (ADX `{ctx['adx']:.1f}`)\n"
            f"💡 *Waiting for the Z-score to hook back toward the mean.*"
        ]

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
        found = rules.find_zscore_reversion(a.get("ctx"), a["close_price"], a["atr_val"],
                                            p["min_rrr"], p, fired)
        if not found:
            return []
        fired.add(found["key"])

        d, t = found["direction"], found["targets"]
        is_buy = d == "BUY"
        entry = a["close_price"]
        msg = (
            f"📉 **Z-SCORE REVERSION {d} ALERT** {'🟢' if is_buy else '🔴'}\n\n"
            f"📍 **Entry:** `${entry}` | 📊 **14-ATR:** `${a['atr_val']}`\n"
            f"🛡️ **SL:** `${t['sl_price']}` ({t['sl_source']}, {t['sl_dist']} pts)\n"
            f"🎯 **TP1 (mean):** `${t['tp1_price']}` | 🎯 **TP2:** `${t['tp2_price']}` (RRR: `1:{t['rrr']}`)\n\n"
            f"📐 **Z-score:** `{found['z_prev']}` ➜ `{found['z']}` | **Mean:** `${found['mean']}`"
        )
        reasons = [
            f"Z-score reached {found['z_prev']} ({'below -' if is_buy else 'above +'}{p['entry_z']}) "
            f"and hooked back toward the mean ({found['z']})",
            f"Ranging regime: ADX {a['ctx']['adx']:.1f} ≤ {p['adx_max']}, flat mean (slope {a['ctx']['slope']:+.2f})",
        ] + target_reasons(t)
        lo, hi = sorted((entry, t["tp1_price"]))
        return [signal_spec(d, entry, t["sl_price"], t["tp1_price"], t["tp2_price"], msg,
                            score=rules.SCORE, zone={"fvg_top": hi, "fvg_bottom": lo},
                            risk=sizing_spec(p), reasons=reasons, dual=self.is_dual(rules.SCORE, p))]

    # ---- backtest
    def decide(self, c):
        state, p = c["state"], c["p"]
        found = rules.find_zscore_reversion(c["ctx"], c["close_price"], c["atr_val"],
                                            p["min_rrr"], p, state["fired"])
        if not found:
            return None
        state["fired"].add(found["key"])
        return {"direction": found["direction"], "entry": c["close_price"], "score": rules.SCORE,
                "breakdown": [], **found["targets"]}


STRATEGY = ZscoreReversion()