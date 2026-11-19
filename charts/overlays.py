# charts/overlays.py
"""Things drawn on top of candles: S/R box, order-block box, past-trade markers, legend."""

import numpy as np
from matplotlib.lines import Line2D

from charts.theme import (C_SUP, C_RES, C_SR, C_BULL_OB, C_BEAR_OB, C_PAST, C_TP1, C_BE, C_SL, EXIT_RULES)
from charts.timeutil import candle_index


def zone_bounds(zone):
    """(bottom, top) of a zone or level dict."""
    if not zone:
        return None
    low, high, price = zone.get("low"), zone.get("high"), zone.get("price")
    if low is None or high is None:
        if price is None:
            return None
        half = float(price) * 0.00075
        low, high = float(price) - half, float(price) + half
    low, high = float(low), float(high)
    return min(low, high), max(low, high)


def zone_style(near_zone, ref_price=None):
    """Support = green, Resistance = red. 'mixed' zones take the colour of the side price is on."""
    z_type = str(near_zone.get("type", "")).lower()
    if "support" in z_type:
        return C_SUP, "Sup"
    if "resistance" in z_type:
        return C_RES, "Res"
    if z_type == "mixed" and ref_price is not None and near_zone.get("price") is not None:
        return (C_SUP, "Sup") if ref_price >= near_zone["price"] else (C_RES, "Res")
    return C_SR, "S/R"


def draw_zone_box(ax, near_zone, x0, x1, ref_price, labels=None):
    """Shaded green (support) / red (resistance) box with edge lines (no axis text label)."""
    bounds = zone_bounds(near_zone)
    if bounds is None:
        return False
    bot, top = bounds
    lo, hi = ax.get_ylim()
    if top < lo or bot > hi:
        return False
    color, _tag = zone_style(near_zone, ref_price)
    ax.fill_between([x0, x1], [bot, bot], [top, top], color=color, alpha=0.18, linewidth=0, zorder=1)
    ax.hlines([bot, top], x0, x1, colors=color, linestyles="-", linewidth=0.9, alpha=0.8, zorder=2)
    return True


def _ob_side(ax, order_block, side, color, x0, x1):
    """One side (bullish/bearish) of a standard SMC order_block dict. Returns True if drawn."""
    if not (order_block.get(f"{side}_ob") or order_block.get(f"{side}_ob_level") is not None):
        return False
    lo, hi = ax.get_ylim()
    level = order_block.get(f"{side}_ob_level") or order_block.get("ob_level")
    bot, top = order_block.get(f"{side}_ob_bottom"), order_block.get(f"{side}_ob_top")
    if (bot is None or top is None) and level is not None:
        half = float(level) * 0.00075
        bot, top = float(level) - half, float(level) + half
    if bot is None or top is None:
        return False
    bot, top = min(bot, top), max(bot, top)
    if top < lo or bot > hi:
        return False
    ax.fill_between([x0, x1], [bot, bot], [top, top], color=color, alpha=0.18, linewidth=0, zorder=1)
    ax.hlines([bot, top], x0, x1, colors=color, linestyles="-.", linewidth=0.9, alpha=0.85, zorder=2)
    return True


def draw_ob_box(ax, order_block, x0, x1, ref_price, labels=None):
    """Shaded Cyan (Bullish OB) / Pink (Bearish OB) box with boundary lines (no axis text label)."""
    if not order_block or not isinstance(order_block, dict):
        return False

    # Case A: standard SMC order_block dictionary from detect_order_block
    if "bullish_ob" in order_block or "bearish_ob" in order_block:
        bull = _ob_side(ax, order_block, "bullish", C_BULL_OB, x0, x1)
        bear = _ob_side(ax, order_block, "bearish", C_BEAR_OB, x0, x1)
        return bull or bear

    # Case B: custom zone-style dict
    bounds = zone_bounds(order_block)
    if bounds is None:
        return False
    bot, top = bounds
    lo, hi = ax.get_ylim()
    if top < lo or bot > hi:
        return False
    z_type = str(order_block.get("type", "")).lower()
    is_bull = "bull" in z_type or (ref_price is not None and ref_price >= (bot + top) / 2)
    color = C_BULL_OB if is_bull else C_BEAR_OB
    ax.fill_between([x0, x1], [bot, bot], [top, top], color=color, alpha=0.18, linewidth=0, zorder=1)
    ax.hlines([bot, top], x0, x1, colors=color, linestyles="-.", linewidth=0.9, alpha=0.85, zorder=2)
    return True


def trade_price(t, name):
    for key in (name, f"{name}_price"):
        v = t.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def overlay_trades(ax, dfp, candle_ts, trades, offset_s, exclude_id=None):
    """Draws historical trades that fall inside the visible window."""
    if not trades or candle_ts is None or len(candle_ts) == 0:
        return False

    highs, lows = dfp["high"].to_numpy(float), dfp["low"].to_numpy(float)
    drawn = False

    for t in trades:
        if exclude_id is not None and t.get("id") == exclude_id:
            continue

        direction = str(t.get("direction", "BUY")).upper()
        status = str(t.get("outcome") or t.get("status") or "")
        entry_p = trade_price(t, "entry")

        xi = candle_index(candle_ts, t.get("entry_time") or t.get("time") or t.get("timestamp"), offset_s)

        xc, exit_p, kind = None, None, None
        rule = EXIT_RULES.get(status)
        if rule:
            kind = rule[1]
            exit_p = trade_price(t, rule[0])
            xc = candle_index(candle_ts, t.get("closed_at") or t.get("updated_at"), offset_s)

        if xi is None and xc is None:
            continue
        if xi is not None and xc is not None and xc < xi:
            xc = xi

        if xi is not None:
            y = entry_p if entry_p is not None else (lows[xi] if direction == "BUY" else highs[xi])
            ax.plot([xi], [y], marker="^" if direction == "BUY" else "v", color=C_PAST,
                    markersize=8, markeredgecolor="white", markeredgewidth=0.7, alpha=0.95, zorder=7)
            drawn = True

        if xc is not None and exit_p is not None and kind:
            color = {"tp": C_TP1, "be": C_BE, "sl": C_SL}[kind]
            marker = {"tp": "*", "be": "D", "sl": "X"}[kind]
            ax.plot([xc], [exit_p], marker=marker, color=color, markersize=11 if kind == "tp" else 7.5,
                    markeredgecolor="white", markeredgewidth=0.7, zorder=7)
            if xi is not None and entry_p is not None:
                ax.plot([xi, xc], [entry_p, exit_p], color=color, linestyle=":", linewidth=1.0,
                        alpha=0.75, zorder=6)
            drawn = True

    return drawn


def trade_legend_handles():
    def h(marker, color, label, size=8):
        return Line2D([0], [0], marker=marker, color="none", markerfacecolor=color,
                      markeredgecolor="white", markersize=size, label=label)
    return [
        h("^", C_PAST, "Past entry"),
        h("*", C_TP1, "Past TP", 11),
        h("D", C_BE, "Past BE", 6),
        h("X", C_SL, "Past SL", 7),
    ]


def apply_legend(ax, with_trades=False):
    handles, _ = ax.get_legend_handles_labels()
    if with_trades:
        handles = handles + trade_legend_handles()
    if handles:
        ax.legend(handles=handles, facecolor="#1e1e1e", edgecolor="#333333", labelcolor="white",
                  loc="upper left", fontsize=7.5)