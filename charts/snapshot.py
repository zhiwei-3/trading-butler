# charts/snapshot.py
import numpy as np

from charts.theme import locked, C_FVG
from charts.timeutil import resolve_offset, series_epoch
from charts.primitives import (new_fig, draw_candles, fit_ylim, label_x_start, place_labels,
                               set_time_ticks, stamp, entry_tf_label, finish)
from charts.overlays import draw_zone_box, draw_ob_box, overlay_trades, apply_legend


@locked
def generate_chart_snapshot(df, title="XAUUSD Market Snapshot", bars=100, near_zone=None,
                            order_block=None, macro_fvg=None, trades=None, server_offset_s=None, tf_label=None):
    """Real-time candlestick snapshot with EMAs, current price, nearest S/R box, Order Block box, HTF FVG,
    and overlays for all past trade entries/exits inside the candle window."""
    if df is None or len(df) < min(bars, 20):
        return None

    offset_s = resolve_offset(server_offset_s)
    dfp = df.tail(bars).reset_index(drop=True)
    n = len(dfp)
    pad = max(8, int(n * 0.14))
    candle_ts = series_epoch(dfp["time"]) if "time" in dfp.columns else None

    fig, ax = new_fig()
    draw_candles(ax, dfp)

    for col, color in (("EMA_20", "#00b0ff"), ("EMA_50", "#ff9100")):
        if col in dfp.columns and dfp[col].notna().any():
            ax.plot(np.arange(n), dfp[col].to_numpy(float), color=color, linewidth=1.2,
                    label=col.replace("_", " "), zorder=5)

    close = float(dfp["close"].iloc[-1])
    fit_ylim(ax, dfp, [close], pad=0.10)
    lo, hi = ax.get_ylim()
    labels = [(close, f"${close:.2f}", "#ffffff")]
    ax.axhline(close, color="#ffffff", linestyle="--", linewidth=0.9, alpha=0.6, zorder=2)

    x_end = label_x_start(n, pad) - 0.5
    draw_zone_box(ax, near_zone, -1, x_end, close, labels)
    draw_ob_box(ax, order_block, -1, x_end, close, labels)

    if macro_fvg and macro_fvg.get("fvg_top") is not None and macro_fvg.get("fvg_bottom") is not None:
        top, bot = macro_fvg["fvg_top"], macro_fvg["fvg_bottom"]
        if top >= lo and bot <= hi:
            ax.axhspan(max(bot, lo), min(top, hi), color=C_FVG, alpha=0.13, label="HTF FVG", zorder=1)

    drawn = overlay_trades(ax, dfp, candle_ts, trades, offset_s)

    ax.set_xlim(-1, n + pad)
    ax.set_ylim(lo, hi)
    set_time_ticks(ax, dfp, offset_s=offset_s)
    place_labels(ax, labels)
    stamp(ax, entry_tf_label(tf_label), n)
    ax.set_title(title, color="white", fontsize=12, fontweight="bold")
    apply_legend(ax, with_trades=drawn)
    return finish(fig)