from utils.common import signal_spec, target_reasons

from .._lib import sizing_spec
from ..base import Strategy, Needs
from . import connectors, rules
from .params import PARAMS


class VolCompressionBreakout(Strategy):
    label = "Volatility Compression Breakout (BB-in-Keltner squeeze)"
    needs = Needs(entry_bars=200)       # reads only the entry candles -> computes no analysis features
    params = PARAMS

    def is_dual(self, score, p):
        return bool(p["dual"])

    def valid_params(self, p):
        return (p["tp2_rr"] > p["tp1_rr"] and p["max_sl_atr"] > p["min_sl_atr"]
                and p["session_end"] > p["session_start"])

    def summary(self, p):
        sess = f"{p['session_start']:g}-{p['session_end']:g}h UTC" if p["use_session_filter"] else "24h"
        return (f"Squeeze ≥{p['min_bars']} bars · KC {p['kc_mult']:g}x · RR {p['tp1_rr']:g}/{p['tp2_rr']:g} · "
                f"{sess} · {'dual' if p['dual'] else 'single'}")

    def diagnose(self, a, p):
        ctx = a.get("ctx")
        if not ctx:
            return "📦 **Vol Compression:** context unavailable (not enough entry-timeframe history)"
        run = rules.squeeze_run_len(ctx["sq"])
        state = (f"🟡 coiling — `{run}` bars (need ≥ `{p['min_bars']}`)" if run
                 else "⚪ no squeeze on the last closed bar")
        sess = "inside" if (not p["use_session_filter"]
                            or p["session_start"] <= ctx["hour"] < p["session_end"]) else "outside"
        return (f"📦 **Vol Compression:** BB inside KC ({p['kc_mult']:g}x ATR)\n"
                f"• **Squeeze:** {state}\n"
                f"• **Session window:** `{sess}` (UTC `{ctx['hour']:.2f}`)")

    # ---- heads-up: a qualifying squeeze is forming (debounced per box, independent of the signal dedup)
    def watch(self, a, p, mem):
        ctx = a.get("ctx")
        box = rules.current_box(ctx, p) if ctx else None
        if box is None:
            mem["watch_state"] = None
            return []
        if mem.get("watch_state") == box["start_key"]:
            return []
        mem["watch_state"] = box["start_key"]
        return [
            f"📦 **VOLATILITY COMPRESSION FORMING (EARLY HEADS-UP)** 📦\n\n"
            f"• **Symbol:** `{a.get('symbol', 'XAUUSD')}` | **Price:** `${a['close_price']}`\n"
            f"• **Squeeze:** `{box['bars']}` bars | **Box:** `${box['lo']:.2f} – ${box['hi']:.2f}`\n"
            f"💡 *Watch for a decisive close outside the box.*"
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
        found = rules.find_compression_breakout(a.get("ctx"), a["close_price"], a["atr_val"],
                                                p["min_rrr"], p, fired)
        if not found:
            return []
        fired.add(found["key"])

        d, t, (lo, hi) = found["direction"], found["targets"], found["range"]
        is_buy = d == "BUY"
        entry = a["close_price"]
        msg = (
            f"📦 **VOLATILITY COMPRESSION {d} ALERT** {'🟢' if is_buy else '🔴'}\n\n"
            f"📍 **Entry:** `${entry}` | 📊 **14-ATR:** `${a['atr_val']}`\n"
            f"🛡️ **SL:** `${t['sl_price']}` ({t['sl_source']}, {t['sl_dist']} pts)\n"
            f"🎯 **TP1:** `${t['tp1_price']}` | 🎯 **TP2:** `${t['tp2_price']}` (RRR: `1:{t['rrr']}`)\n\n"
            f"📦 **Compression:** `{found['squeeze_bars']}` bars | **Box:** `${lo} – ${hi}`"
        )
        reasons = [
            f"BB squeezed inside Keltner for {found['squeeze_bars']} bars (min {p['min_bars']})",
            f"Closed {'above' if is_buy else 'below'} the box {'high' if is_buy else 'low'} "
            f"({found['level']}) with a decisive body",
        ] + target_reasons(t)
        return [signal_spec(d, entry, t["sl_price"], t["tp1_price"], t["tp2_price"], msg,
                            score=rules.SCORE, zone={"fvg_top": hi, "fvg_bottom": lo},
                            risk=sizing_spec(p), reasons=reasons, dual=self.is_dual(rules.SCORE, p))]

    # ---- backtest
    def decide(self, c):
        state, p = c["state"], c["p"]
        found = rules.find_compression_breakout(c["ctx"], c["close_price"], c["atr_val"],
                                                p["min_rrr"], p, state["fired"])
        if not found:
            return None
        state["fired"].add(found["key"])
        return {"direction": found["direction"], "entry": c["close_price"], "score": rules.SCORE,
                "breakdown": [], **found["targets"]}


STRATEGY = VolCompressionBreakout()