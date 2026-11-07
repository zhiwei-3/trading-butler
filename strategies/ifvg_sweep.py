# strategies/ifvg_sweep.py
"""Liquidity sweep -> displacement -> inverted FVG (IFVG) retest. Pure rules."""

from config import ALERT_STATE
from utils.common import signal_spec
from utils.smc import find_swing_points, detect_ifvg_setup

LABEL = "Liquidity Sweep + Inverted FVG Retest (IFVG)"


def is_dual(score):
    return False   # always a single TP1-only leg


def calculate_ifvg_targets(direction, entry, setup, atr_val, swing_highs, swing_lows, min_rr):
    """
    Structural SL (beyond the sweep extreme + fractional ATR buffer) and liquidity-based
    TP (nearest BSL/SSL giving >= min_rr, else a fixed-RR fallback). Returns None if the
    resulting risk is unusably tight/wide.
    """
    sign = 1 if direction == "BUY" else -1
    buf = atr_val * ALERT_STATE["ifvg_sl_buffer_atr"]

    sl = setup["sweep_extreme"] - sign * buf
    risk = sign * (entry - sl)
    if risk <= 0 or risk < atr_val * 0.3 or risk > atr_val * ALERT_STATE["ifvg_max_sl_atr"]:
        return None

    fallback_rr = max(ALERT_STATE["ifvg_fallback_rr"], min_rr)
    if direction == "BUY":
        pools = sorted(p for _, p in swing_highs if p > entry)
        liq_label = "BSL"
    else:
        pools = sorted((p for _, p in swing_lows if p < entry), reverse=True)
        liq_label = "SSL"

    qualifying = [p for p in pools if abs(p - entry) >= risk * min_rr]
    if qualifying:
        tp1, tp1_source = qualifying[0], liq_label
        beyond = [p for p in qualifying[1:] if abs(p - tp1) >= atr_val * 0.5]
        tp2 = beyond[0] if beyond else tp1 + sign * max(atr_val * 0.5, risk)
    else:
        tp1, tp1_source = entry + sign * fallback_rr * risk, "Fixed RR"
        tp2 = tp1 + sign * risk

    tp1_dist, tp2_dist = abs(tp1 - entry), abs(tp2 - entry)
    return {
        "sl_price": round(sl, 2), "tp1_price": round(tp1, 2), "tp2_price": round(tp2, 2),
        "sl_dist": round(risk, 2), "tp1_dist": round(tp1_dist, 2), "tp2_dist": round(tp2_dist, 2),
        "sl_source": "Sweep Extreme", "tp1_source": tp1_source,
        "rrr": round(tp1_dist / risk, 2),
    }


def find_ifvg_trade(df, close_price, atr_val, min_rrr, last_key=None):
    """Shared by live and backtest so the two can't drift apart."""
    if df is None:
        return None
    fw = ALERT_STATE["fractal_window"]
    lookback = ALERT_STATE["ifvg_lookback"]
    for direction in ("BUY", "SELL"):
        setup = detect_ifvg_setup(
            df, direction, atr_val, lookback=lookback, left=fw, right=fw,
            eq_tolerance_atr=ALERT_STATE["ifvg_eq_tolerance_atr"],
            max_setup_bars=ALERT_STATE["ifvg_max_setup_bars"],
            min_body_atr=ALERT_STATE["ifvg_min_body_atr"],
        )
        if not setup or setup["key"] == last_key:
            continue
        swing_highs, swing_lows = find_swing_points(df.tail(max(150, lookback)), fw, fw)
        targets = calculate_ifvg_targets(direction, close_price, setup, atr_val,
                                         swing_highs, swing_lows, min_rrr)
        if targets is None or targets["rrr"] < min_rrr:
            continue
        return {"direction": direction, "setup": setup, "targets": targets}
    return None


# ---------------------------------------------------------------- live

def evaluate(a):
    """Own dedup key (independent of last_rsi_signal)."""
    found = find_ifvg_trade(a["df_entry"], a["close_price"], a["atr_val"],
                            ALERT_STATE.get("min_rrr", 1.5), ALERT_STATE.get("last_ifvg_signal"))
    if not found:
        return [], []

    direction, setup, t = found["direction"], found["setup"], found["targets"]
    ALERT_STATE["last_ifvg_signal"] = setup["key"]

    is_buy = direction == "BUY"
    icon = "🟢" if is_buy else "🔴"
    pool = "Equal Lows (SSL)" if is_buy else "Equal Highs (BSL)"
    extreme_lbl = "Sweep Low" if is_buy else "Sweep High"
    close_price = a["close_price"]

    msg = (
        f"🧲 **IFVG SWEEP {direction} ALERT** {icon}\n\n"
        f"📍 **Entry:** `${close_price}` (IFVG retest) | 📊 **14-ATR:** `${a['atr_val']}`\n"
        f"🛡️ **SL:** `${t['sl_price']}` ({t['sl_source']}, {t['sl_dist']} pts)\n"
        f"🎯 **TP1:** `${t['tp1_price']}` ({t['tp1_source']}) | 🎯 **TP2:** `${t['tp2_price']}` (RRR: `1:{t['rrr']}`)\n\n"
        f"💧 **Swept:** {pool} @ `${setup['level']}` → {extreme_lbl} `${setup['sweep_extreme']}`\n"
        f"⚡ **IFVG Zone:** `${setup['zone_bottom']} – ${setup['zone_top']}`"
    )
    reasons = [
        f"{pool} at {setup['level']} was swept (wick to {setup['sweep_extreme']})",
        f"A {'bearish' if is_buy else 'bullish'} FVG formed during the move, then price closed through it with displacement (inversion)",
        f"Price is now retesting the inverted gap {setup['zone_bottom']} - {setup['zone_top']} from the {'above' if is_buy else 'below'}",
        f"SL sits beyond the sweep extreme plus an ATR buffer; TP1 targets {t['tp1_source']}",
        f"RRR 1:{t['rrr']} (minimum {ALERT_STATE.get('min_rrr', 1.5)})",
    ]
    return [signal_spec(direction, close_price, t["sl_price"], t["tp1_price"], t["tp2_price"], msg,
                        score=80, zone=setup, reasons=reasons, dual=is_dual(80))], []


# ---------------------------------------------------------------- backtest

def decide(c):
    state, full = c["state"], c["full"]
    found = find_ifvg_trade(full.get("entry_slice"), c["close_price"], c["atr_val"], c["min_rrr"],
                            state.get("last_ifvg_key"))
    if not found:
        return None
    state["last_ifvg_key"] = found["setup"]["key"]
    return {"direction": found["direction"], "entry": c["close_price"], "score": 80,
            "breakdown": [], **found["targets"]}