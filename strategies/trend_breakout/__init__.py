import numpy as np

from utils.common import signal_spec, target_reasons

from ..base import Strategy, Needs
from . import connectors, rules
from .params import PARAMS
from .sessions import session_of


class TrendBreakout(Strategy):
    label = "H1 Trend/ADX Regime + Asian Range Breakout / Sweep+BOS (ATR-risk sizing)"
    needs = Needs(entry_bars=400, extra=(("H1", 400),))      # reads candles only -> computes no analysis features
    params = PARAMS

    def is_dual(self, score, p):
        return bool(p["dual"])

    def valid_params(self, p):
        return p["max_sl_atr"] > p["min_sl_atr"]

    def summary(self, p):
        return (f"ADX>{p['adx_min']:g} · London {p['london_start']:g}h / NY {p['ny_start']:g}h "
                f"(+{p['window_hours']:g}h) · RR {p['tp1_rr']:g}/{p['tp2_rr']:g} · "
                f"{'dual' if p['dual'] else 'single'}")

    def diagnose(self, a, p):
        tb = a.get("ctx")
        if not tb:
            return "🌅 **Trend Breakout:** context unavailable (not enough entry/H1 history)"
        session = session_of(tb["hour"], p) or "outside windows"
        if np.isfinite(tb["ah"]) and np.isfinite(tb["al"]):
            rng = f"`${tb['al']:.2f} – ${tb['ah']:.2f}`"
        else:
            rng = "`n/a (Asian session incomplete)`"
        return (f"🌅 **Trend Breakout:** H1 regime `{tb['trend'] or 'CHOP'}` "
                f"(ADX `{tb['adx']:.1f}` / min `{p['adx_min']}`)\n"
                f"• **Session:** `{session}` | **Asian range:** {rng}")

    # ---- backtest connectors
    def init_state(self, p):
        return {"fired": set()}

    def prepare_backtest(self, bt, p):
        return connectors.prepare(bt, p)

    def bar_context(self, prep, i, p, state):
        return connectors.bar_context(prep, i, p, state)

    # ---- live connector
    def build_live_context(self, md, p):
        return connectors.live_context(md, p)

    # ---- live
    def evaluate(self, a, p, mem):
        tb = a.get("ctx")
        if not tb:
            return []

        # Dedup keys are per-day; drop yesterday's.
        fired = {k for k in mem.get("fired", set()) if k.startswith(tb["day_key"])}
        mem["fired"] = fired

        found = rules.find_trend_breakout(tb, a["close_price"], a["atr_val"], p["min_rrr"], p, fired)
        if not found:
            return []

        direction, setup, t = found["direction"], found["setup"], found["targets"]
        fired.add(setup["key"])

        is_buy = direction == "BUY"
        icon = "🟢" if is_buy else "🔴"
        side_word = "above" if is_buy else "below"
        mode_txt = "Sweep + BOS" if setup["mode"] == "SWEEP_BOS" else "Range Breakout"
        close_price = a["close_price"]

        msg = (
            f"🌅 **TREND BREAKOUT {direction} ALERT** {icon}\n\n"
            f"📍 **Entry:** `${close_price}` | 📊 **14-ATR:** `${a['atr_val']}`\n"
            f"🛡️ **SL:** `${t['sl_price']}` ({t['sl_source']}, {t['sl_dist']} pts)\n"
            f"🎯 **TP1:** `${t['tp1_price']}` | 🎯 **TP2:** `${t['tp2_price']}` (RRR: `1:{t['rrr']}`)\n\n"
            f"🧭 **H1 Regime:** {'Uptrend' if is_buy else 'Downtrend'} (ADX `{tb['adx']:.1f}`)\n"
            f"🌏 **Asian Range:** `${setup['al']} – ${setup['ah']}` | **Setup:** {mode_txt} | **Session:** {setup['session']}"
        )

        swept = "Asian low" if is_buy else "Asian high"
        brk = "Asian high" if is_buy else "Asian low"
        reasons = [
            f"H1 {'uptrend' if is_buy else 'downtrend'}: EMA50 {'>' if is_buy else '<'} EMA200 "
            f"({tb['ema_f']:.2f} / {tb['ema_s']:.2f}), ADX {tb['adx']:.1f} > {p['adx_min']}",
            f"{setup['session']} open window, Asian range {setup['al']} - {setup['ah']}",
        ]
        if setup["mode"] == "SWEEP_BOS":
            reasons.append(f"{swept} was swept (wick to {setup['sweep_extreme']}) and price closed back inside")
            reasons.append(f"Break of structure: closed {side_word} {setup['level']} in the H1 trend direction")
        else:
            reasons.append(f"Candle closed {side_word} the {brk} ({setup['level']}) in the H1 trend direction")
        reasons += target_reasons(t)

        return [signal_spec(direction, close_price, t["sl_price"], t["tp1_price"], t["tp2_price"], msg,
                            score=rules.SCORE, zone={"fvg_top": setup["ah"], "fvg_bottom": setup["al"]},
                            reasons=reasons, dynamic_sizing=True, dual=self.is_dual(rules.SCORE, p))]

    # ---- backtest
    def decide(self, c):
        p, state = c["p"], c["state"]
        found = rules.find_trend_breakout(c["ctx"], c["close_price"], c["atr_val"], p["min_rrr"], p, state["fired"])
        if not found:
            return None
        state["fired"].add(found["setup"]["key"])
        return {"direction": found["direction"], "entry": c["close_price"], "score": rules.SCORE,
                "breakdown": [], **found["targets"]}


STRATEGY = TrendBreakout()