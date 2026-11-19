# charts/alert.py
import numpy as np

from charts.theme import locked, C_ENTRY, C_SL, C_TP1, C_TP2, C_FVG
from charts.timeutil import resolve_offset, series_epoch
from charts.primitives import (new_fig, draw_candles, fit_ylim, label_x_start, place_labels, hline, r_text,
                               set_time_ticks, stamp, entry_tf_label, finish)
from charts.overlays import draw_zone_box, draw_ob_box, overlay_trades, apply_legend


@locked
def generate_signal_chart(df, symbol, signal_type, entry, sl, tp1, tp2,
                          fvg=None, near_zone=None, order_block=None, bars=80, dual=True,
                          trades=None, exclude_id=None, server_offset_s=None, tf_label=None):
    """Alert chart with projected entry/SL/TP lines, shaded risk/reward zones, S/R box, Order Block box, and trade history."""
    if df is None or len(df) < min(bars, 50):
        return None

    offset_s = resolve_offset(server_offset_s)
    dfp = df.tail(bars).reset_index(drop=True)
    n = len(dfp)
    pad = max(12, int(n * 0.45))
    x0 = n - 1
    x1 = label_x_start(n, pad) - 0.5
    sl_dist = abs(entry - sl)
    candle_ts = series_epoch(dfp["time"]) if "time" in dfp.columns else None

    fig, ax = new_fig()
    draw_candles(ax, dfp)

    for col, color in (("EMA_20", "#00b0ff"), ("EMA_50", "#ff9100")):
        if col in dfp.columns and dfp[col].notna().any():
            ax.plot(np.arange(n), dfp[col].to_numpy(float), color=color, linewidth=1.0, alpha=0.8, zorder=5)

    levels = [entry, sl, tp1] + ([tp2] if dual else [])
    fit_ylim(ax, dfp, levels)
    lo, hi = ax.get_ylim()

    ax.fill_between([x0, x1], [entry, entry], [sl, sl], color=C_SL, alpha=0.12, linewidth=0, zorder=1)
    ax.fill_between([x0, x1], [entry, entry], [tp1, tp1], color=C_TP1, alpha=0.12, linewidth=0, zorder=1)
    if dual:
        ax.fill_between([x0, x1], [tp1, tp1], [tp2, tp2], color=C_TP2, alpha=0.07, linewidth=0, zorder=1)

    hline(ax, entry, x0, x1, C_ENTRY, "-", 1.6)
    hline(ax, sl, x0, x1, C_SL, "--")
    hline(ax, tp1, x0, x1, C_TP1, "--")
    if dual:
        hline(ax, tp2, x0, x1, C_TP2, ":")

    ax.plot([x0], [entry], marker="^" if signal_type == "BUY" else "v", color=C_ENTRY,
            markersize=10, markeredgecolor="white", markeredgewidth=0.9, zorder=9)

    if fvg and fvg.get("fvg_top") is not None and fvg.get("fvg_bottom") is not None:
        top, bot = fvg["fvg_top"], fvg["fvg_bottom"]
        if top >= lo and bot <= hi:
            ax.axhspan(max(bot, lo), min(top, hi), xmax=(n + 1) / (n + pad + 1),
                       color=C_FVG, alpha=0.16, zorder=1)

    labels = [
        (entry, f"{signal_type} Entry ${entry:.2f}", C_ENTRY),
        (sl, f"SL ${sl:.2f}{r_text(sl, entry, sl_dist, signal_type)}", C_SL),
        (tp1, f"TP1 ${tp1:.2f}{r_text(tp1, entry, sl_dist, signal_type)}", C_TP1),
    ]
    if dual:
        labels.append((tp2, f"TP2 ${tp2:.2f}{r_text(tp2, entry, sl_dist, signal_type)}", C_TP2))

    draw_zone_box(ax, near_zone, -1, x1, entry, labels)
    draw_ob_box(ax, order_block, -1, x1, entry, labels)

    drawn = overlay_trades(ax, dfp, candle_ts, trades, offset_s, exclude_id=exclude_id)

    ax.set_xlim(-1, n + pad)
    ax.set_ylim(lo, hi)
    set_time_ticks(ax, dfp, offset_s=offset_s)
    place_labels(ax, labels)
    tf = entry_tf_label(tf_label)
    stamp(ax, tf, n)
    ax.set_title(f"Trading Butler Alert — {symbol} ({signal_type}) · {tf}" + ("  •  DUAL ENTRY" if dual else ""),
                 color="white", fontsize=12, fontweight="bold")
    apply_legend(ax, with_trades=drawn)
    return finish(fig)