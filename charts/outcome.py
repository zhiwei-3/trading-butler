# charts/outcome.py
from charts.theme import (locked, STATUS_STYLE, TERMINAL, C_ENTRY, C_SL, C_TP1, C_TP2, C_BE)
from charts.timeutil import resolve_offset, series_epoch, candle_index
from charts.primitives import (new_fig, draw_candles, fit_ylim, label_x_start, place_labels, hline, r_text,
                               set_time_ticks, stamp, entry_tf_label, first_touch, finish)
from charts.overlays import draw_zone_box, draw_ob_box, overlay_trades, apply_legend


@locked
def generate_outcome_chart(df, entry_p, sl_p, tp1_p, tp2_p, outcome_status, symbol="XAUUSD",
                           direction=None, entry_time=None, exit_time=None, dual=True,
                           pre_bars=25, post_bars=8, trades=None, exclude_id=None,
                           server_offset_s=None, near_zone=None, order_block=None, tf_label=None, min_bars=60):
    """Post-trade chart with entry/exit markers, S/R zone box, and Order Block box."""
    if df is None or len(df) == 0:
        return None
    df = df.copy().reset_index(drop=True)
    if direction is None:
        direction = "BUY" if tp1_p > entry_p else "SELL"

    offset_s = resolve_offset(server_offset_s)
    has_time = "time" in df.columns
    full_ts = series_epoch(df["time"]) if has_time else None
    last = len(df) - 1

    entry_idx = candle_index(full_ts, entry_time, offset_s) if has_time and entry_time is not None else None
    exit_idx = candle_index(full_ts, exit_time, offset_s) if has_time and exit_time is not None else None
    if exit_idx is not None:
        exit_idx = max(exit_idx, entry_idx if entry_idx is not None else 0)

    highs_f, lows_f = df["high"].to_numpy(float), df["low"].to_numpy(float)
    scan_from = min((entry_idx + 1) if entry_idx is not None else max(0, last - 100), last)

    def touch(level, start, stop=None):
        return first_touch(highs_f, lows_f, level, start, stop)

    # ---- exit markers, in FULL-frame candle coordinates: (idx, price, kind)
    markers = []
    if outcome_status in ("HIT_SL", "CLOSED_RUNNER_SL"):
        i = exit_idx if exit_idx is not None else touch(sl_p, scan_from)
        if i is not None:
            markers.append((i, sl_p, "sl"))
    elif outcome_status in ("CLOSED_TP1", "HIT_TP1"):
        i = exit_idx if exit_idx is not None else touch(tp1_p, scan_from)
        if i is not None:
            markers.append((i, tp1_p, "tp"))
    elif outcome_status == "HIT_TP2":
        i2 = exit_idx if exit_idx is not None else touch(tp2_p, scan_from)
        if i2 is not None:
            i1 = touch(tp1_p, scan_from, i2)
            if i1 is not None:
                markers.append((i1, tp1_p, "tp"))
            markers.append((i2, tp2_p, "tp"))
    elif outcome_status == "CLOSED_BE":
        ib = exit_idx
        i1 = touch(tp1_p, scan_from, ib)
        if ib is None and i1 is not None:
            ib = touch(entry_p, i1 + 1)
        if i1 is not None:
            markers.append((i1, tp1_p, "tp"))
        if ib is not None:
            markers.append((ib, entry_p, "be"))

    terminal = outcome_status in TERMINAL

    # ---- trim to the trade's lifespan, but never below min_bars candles
    start = max(0, entry_idx - pre_bars) if entry_idx is not None else max(0, last - 100)
    end = last
    if terminal and markers:
        end = min(last, max(m[0] for m in markers) + post_bars)
    if (end - start + 1) < min_bars:
        start = max(0, end - min_bars + 1)
    dfp = df.iloc[start:end + 1].reset_index(drop=True)
    n = len(dfp)
    candle_ts = full_ts[start:end + 1] if has_time else None

    entry_x = (entry_idx - start) if (entry_idx is not None and entry_idx >= start) else None
    markers = [(i - start, p, k) for i, p, k in markers]
    x0 = entry_x if entry_x is not None else 0
    x_end = max(m[0] for m in markers) if (terminal and markers) else n - 1
    x_end = max(x_end, x0)

    runner_be = dual and outcome_status in ("HIT_TP1", "CLOSED_BE")
    post_tp1 = outcome_status in ("HIT_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_RUNNER_SL")
    show_tp2 = bool(dual and tp2_p)
    sl_dist = abs(entry_p - sl_p)

    pad = max(10, int(n * 0.32))
    fig, ax = new_fig()
    draw_candles(ax, dfp)

    levels = [entry_p, sl_p, tp1_p] + ([tp2_p] if show_tp2 else [])
    fit_ylim(ax, dfp, levels)

    ax.fill_between([x0, x_end], [entry_p, entry_p], [sl_p, sl_p], color=C_SL, alpha=0.10, linewidth=0, zorder=1)
    ax.fill_between([x0, x_end], [entry_p, entry_p], [tp1_p, tp1_p], color=C_TP1, alpha=0.10, linewidth=0, zorder=1)
    if show_tp2:
        ax.fill_between([x0, x_end], [tp1_p, tp1_p], [tp2_p, tp2_p], color=C_TP2, alpha=0.06, linewidth=0, zorder=1)

    ext_to = label_x_start(n, pad) - 0.5
    hline(ax, entry_p, x0, x_end, C_BE if runner_be else C_ENTRY, "-", 1.6)
    hline(ax, sl_p, x0, x_end, C_SL, "--", 1.2, alpha=0.35 if post_tp1 else 1.0)
    hline(ax, tp1_p, x0, x_end, C_TP1, "--", 1.2)
    if show_tp2:
        hline(ax, tp2_p, x0, x_end, C_TP2, ":", 1.2)

    if ext_to > x_end:
        for y, c in ((entry_p, C_ENTRY), (sl_p, C_SL), (tp1_p, C_TP1)):
            ax.hlines(y, x_end, ext_to, colors=c, linestyles=":", linewidth=0.7, alpha=0.3, zorder=2)

    if entry_x is not None:
        ax.plot([entry_x], [entry_p], marker="^" if direction == "BUY" else "v", color=C_ENTRY,
                markersize=11, markeredgecolor="white", markeredgewidth=0.9, zorder=9)

    for idx, price, kind in markers:
        mk, col = {"sl": ("X", C_SL), "tp": ("*", C_TP1), "be": ("D", C_BE)}[kind]
        ax.plot([idx], [price], marker=mk, color=col, markersize=13 if kind == "tp" else 9,
                markeredgecolor="white", markeredgewidth=0.9, zorder=9)

    drawn = overlay_trades(ax, dfp, candle_ts, trades, offset_s, exclude_id=exclude_id)

    entry_label = f"{'Entry / BE' if runner_be else 'Entry'} ${entry_p:.2f}"
    sl_label = f"SL{' (orig)' if post_tp1 else ''} ${sl_p:.2f}{r_text(sl_p, entry_p, sl_dist, direction)}"
    labels = [
        (entry_p, entry_label, C_BE if runner_be else C_ENTRY),
        (sl_p, sl_label, C_SL),
        (tp1_p, f"TP1 ${tp1_p:.2f}{r_text(tp1_p, entry_p, sl_dist, direction)}", C_TP1),
    ]
    if show_tp2:
        labels.append((tp2_p, f"TP2 ${tp2_p:.2f}{r_text(tp2_p, entry_p, sl_dist, direction)}", C_TP2))

    draw_zone_box(ax, near_zone, -1, ext_to, entry_p, labels)
    draw_ob_box(ax, order_block, -1, ext_to, entry_p, labels)

    ax.set_xlim(-1, n + pad)
    set_time_ticks(ax, dfp, offset_s=offset_s)
    place_labels(ax, labels)

    tf = entry_tf_label(tf_label)
    stamp(ax, tf, n)

    if entry_x is None:
        ax.text(0.01, 0.97, "entry bar is outside this chart window — levels shown full-width",
                transform=ax.transAxes, color="#888888", fontsize=7.5, va="top")

    color, text = STATUS_STYLE.get(outcome_status, ("#FFFFFF", outcome_status))
    ax.set_title(f"[{symbol} · {tf}] {direction} — {text}", color=color, fontsize=12, fontweight="bold")
    apply_legend(ax, with_trades=drawn)
    return finish(fig)