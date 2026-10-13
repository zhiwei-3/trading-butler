# strategy/chart.py

import io
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ------------------------------------------------------------------ theme
BG, GRID, FG = "#121212", "#262626", "#e0e0e0"
UP, DN = "#00c853", "#ff3d00"
C_ENTRY, C_SL = "#2962ff", "#ff1744"
C_TP1, C_TP2 = "#00e676", "#00b0ff"
C_BE, C_SR, C_FVG = "#17a2b8", "#aa00ff", "#ffd600"
C_SUP, C_RES = "#00c853", "#ff3d00"  # Support (Green) / Resistance (Red)

TERMINAL = ("HIT_SL", "CLOSED_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_RUNNER_SL")
STATUS_STYLE = {
    "HIT_TP1":           ("#28A745", "LEG 1 TP1 HIT — RUNNER ACTIVE"),
    "CLOSED_TP1":        ("#28A745", "TP1 HIT — FULL CLOSE"),
    "HIT_TP2":           ("#1E9E4A", "TP2 HIT — FULL TARGET"),
    "CLOSED_BE":         ("#17A2B8", "RUNNER CLOSED AT BREAK-EVEN"),
    "CLOSED_RUNNER_SL":  ("#FD7E14", "RUNNER CLOSED AT SL"),
    "HIT_SL":            ("#DC3545", "STOP LOSS HIT"),
}


# ------------------------------------------------------------------ helpers
def _find_closest_candle_idx(df_times, target_time):
    """
    Maps a trade timestamp to the exact candle bar it occurred in.
    Uses floor-binning (candle_start <= trade_time < candle_end) and auto-detects
    broker server UTC offsets.
    """
    if target_time is None or len(df_times) == 0:
        return None
    try:
        target_dt = pd.to_datetime(target_time, utc=True)
        candle_times = pd.to_datetime(df_times, utc=True)
        
        target_ts = target_dt.timestamp()
        candle_ts = candle_times.astype("int64") // 10**9

        # Auto-detect broker GMT offset (e.g. +2 or +3 hours EET vs UTC DB)
        raw_diff = candle_ts.values[-1] - target_ts
        hour_offset = round(raw_diff / 3600.0)
        if 1 <= abs(hour_offset) <= 12:
            target_ts += hour_offset * 3600.0

        # Floor-binning: Find candle i where candle_ts[i] <= target_ts < candle_ts[i+1]
        idx = int(np.searchsorted(candle_ts.values, target_ts, side="right")) - 1

        if 0 <= idx < len(candle_ts):
            return idx

        # Fallback to nearest if outside visible range
        return int(np.abs(candle_ts.values - target_ts).argmin())
    except Exception:
        return None


def _new_fig(figsize=(10, 5.5)):
    fig, ax = plt.subplots(figsize=figsize, facecolor=BG)
    ax.set_facecolor(BG)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.8)
    ax.tick_params(colors=FG, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    return fig, ax


def _draw_candles(ax, df, width=0.62):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    x = np.arange(len(df))
    colors = np.where(c >= o, UP, DN)
    ax.vlines(x, l, h, colors=colors, linewidth=1, zorder=3)
    min_body = max((h.max() - l.min()) * 0.0008, 1e-9)
    for xi, oi, ci, col in zip(x, o, c, colors):
        ax.add_patch(Rectangle((xi - width / 2, min(oi, ci)), width, max(abs(ci - oi), min_body),
                               facecolor=col, edgecolor=col, linewidth=0.5, zorder=4))


def _set_time_ticks(ax, df, n=6):
    if "time" not in df.columns or len(df) < 2:
        ax.set_xticks([])
        return
    times = pd.to_datetime(df["time"])
    idx = np.unique(np.linspace(0, len(df) - 1, n).astype(int))
    multi_day = times.iloc[0].date() != times.iloc[-1].date()
    fmt = "%d %b %H:%M" if multi_day else "%H:%M"
    ax.set_xticks(idx)
    ax.set_xticklabels([times.iloc[i].strftime(fmt) for i in idx], color=FG, fontsize=8)


def _fit_ylim(ax, df, levels, pad=0.08):
    vals = [float(df["low"].min()), float(df["high"].max())] + [float(v) for v in levels if v is not None]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    ax.set_ylim(lo - span * pad, hi + span * pad)


def _label_x_start(n, pad):
    """Data-x where the right-hand label column begins (≈ axes fraction 0.79)."""
    return -1 + 0.79 * (n + pad + 1)


def _place_labels(ax, labels):
    """Right-aligned price labels in axes-fraction x, nudged apart to prevent overlaps."""
    if not labels:
        return
    lo, hi = ax.get_ylim()
    gap = (hi - lo) * 0.05
    labels = sorted(labels, key=lambda t: t[0])
    ys = []
    for y, _, _ in labels:
        ys.append(y if not ys else max(y, ys[-1] + gap))
    over = ys[-1] - (hi - gap * 0.6)
    if over > 0:
        ys = [y - over for y in ys]
    for (_, text, color), y in zip(labels, ys):
        ax.text(0.992, y, text, transform=ax.get_yaxis_transform(), ha="right", va="center",
                color=color, fontsize=8, fontweight="bold", zorder=7, clip_on=False,
                bbox=dict(boxstyle="round,pad=0.25", fc=BG, ec=color, lw=0.9))


def _hline(ax, y, x0, x1, color, ls="-", lw=1.4, alpha=1.0):
    ax.hlines(y, x0, x1, colors=color, linestyles=ls, linewidth=lw, alpha=alpha, zorder=5)


def _r_text(price, entry, sl_dist, direction):
    if not sl_dist:
        return ""
    r = (price - entry) / sl_dist * (1 if direction == "BUY" else -1)
    return f" ({r:+.1f}R)"


def _finish(fig):
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


def _first_touch(highs, lows, level, start):
    for i in range(max(start, 0), len(highs)):
        if lows[i] <= level <= highs[i]:
            return i
    return None


def _get_zone_style(near_zone):
    """Returns dynamic color and label text for support/resistance zones."""
    z_type = str(near_zone.get("type", "")).lower()
    if "support" in z_type:
        return C_SUP, "Sup"
    elif "resistance" in z_type:
        return C_RES, "Res"
    return C_SR, "S/R"


# ------------------------------------------------------------------ market snapshot
def generate_chart_snapshot(df, title="XAUUSD Market Snapshot", bars=100, near_zone=None, macro_fvg=None, trades=None):
    """
    Real-time candlestick snapshot with EMAs, current price, nearest S/R, HTF FVG,
    and overlays for all trade entries and exits within the candle window.
    """
    if df is None or len(df) < min(bars, 20):
        return None

    dfp = df.tail(bars).reset_index(drop=True)
    n = len(dfp)
    pad = max(8, int(n * 0.14))
    fig, ax = _new_fig()
    _draw_candles(ax, dfp)

    for col, color in (("EMA_20", "#00b0ff"), ("EMA_50", "#ff9100")):
        if col in dfp.columns and dfp[col].notna().any():
            ax.plot(np.arange(n), dfp[col].to_numpy(float), color=color, linewidth=1.2,
                    label=col.replace("_", " "), zorder=5)

    close = float(dfp["close"].iloc[-1])
    _fit_ylim(ax, dfp, [close], pad=0.10)
    lo, hi = ax.get_ylim()
    labels = [(close, f"${close:.2f}", "#ffffff")]
    ax.axhline(close, color="#ffffff", linestyle="--", linewidth=0.9, alpha=0.6, zorder=2)

    if near_zone and near_zone.get("price") and lo <= near_zone["price"] <= hi:
        c_zone, z_lbl = _get_zone_style(near_zone)
        ax.axhline(near_zone["price"], color=c_zone, linestyle="-.", alpha=0.85, linewidth=1.2, zorder=2)
        labels.append((near_zone["price"], f"{z_lbl} ${near_zone['price']:.2f}", c_zone))

    if macro_fvg and macro_fvg.get("fvg_top") is not None and macro_fvg.get("fvg_bottom") is not None:
        top, bot = macro_fvg["fvg_top"], macro_fvg["fvg_bottom"]
        if top >= lo and bot <= hi:
            ax.axhspan(max(bot, lo), min(top, hi), color=C_FVG, alpha=0.13, label="HTF FVG", zorder=1)

    # Plot trade entries and exits anchored to candle wicks
    if trades and "time" in dfp.columns:
        lows = dfp["low"].to_numpy(float)
        highs = dfp["high"].to_numpy(float)
        price_range = max((highs.max() - lows.min()), 1.0)
        offset = price_range * 0.02

        for t in trades:
            # 1. Entry Marker
            entry_time = t.get("time") or t.get("entry_time") or t.get("timestamp")
            xi = _find_closest_candle_idx(dfp["time"], entry_time)
            if xi is not None:
                direction = t.get("direction", "BUY")
                y_pos = lows[xi] - offset if direction == "BUY" else highs[xi] + offset
                ax.plot([xi], [y_pos], marker="^" if direction == "BUY" else "v", color=C_ENTRY,
                        markersize=11, markeredgecolor="white", markeredgewidth=1.0, zorder=8)

            # 2. Exit Marker
            outcome = t.get("outcome") or t.get("status", "")
            if outcome in TERMINAL:
                c_time = t.get("closed_at") or t.get("updated_at")
                if c_time is not None:
                    xc = _find_closest_candle_idx(dfp["time"], c_time)
                    if xc is not None:
                        color_m = C_TP1 if "TP" in outcome else (C_BE if "BE" in outcome else C_SL)
                        marker_m = "*" if "TP" in outcome else ("D" if "BE" in outcome else "X")
                        y_pos = highs[xc] + offset if "TP" in outcome else lows[xc] - offset
                        ax.plot([xc], [y_pos], marker=marker_m, color=color_m, markersize=12,
                                markeredgecolor="white", markeredgewidth=1.0, zorder=8)

    ax.set_xlim(-1, n + pad)
    ax.set_ylim(lo, hi)
    _set_time_ticks(ax, dfp)
    _place_labels(ax, labels)
    ax.set_title(title, color="white", fontsize=12, fontweight="bold")
    if ax.get_legend_handles_labels()[0]:
        ax.legend(facecolor="#1e1e1e", edgecolor="#333333", labelcolor="white", loc="upper left", fontsize=8)
    return _finish(fig)


# ------------------------------------------------------------------ live signal alert
def generate_signal_chart(df, symbol, signal_type, entry, sl, tp1, tp2,
                          fvg=None, near_zone=None, bars=50, dual=True):
    """Candlestick alert chart with projected entry, SL, TP1, TP2 lines and shaded risk/reward zones."""
    if df is None or len(df) < bars:
        return None

    dfp = df.tail(bars).reset_index(drop=True)
    n = len(dfp)
    pad = max(12, int(n * 0.45))
    x0 = n - 1
    x1 = _label_x_start(n, pad) - 0.5
    sl_dist = abs(entry - sl)

    fig, ax = _new_fig()
    _draw_candles(ax, dfp)

    for col, color in (("EMA_20", "#00b0ff"), ("EMA_50", "#ff9100")):
        if col in dfp.columns and dfp[col].notna().any():
            ax.plot(np.arange(n), dfp[col].to_numpy(float), color=color, linewidth=1.0, alpha=0.8, zorder=5)

    levels = [entry, sl, tp1] + ([tp2] if dual else [])
    _fit_ylim(ax, dfp, levels)
    lo, hi = ax.get_ylim()

    ax.fill_between([x0, x1], [entry, entry], [sl, sl], color=C_SL, alpha=0.12, linewidth=0, zorder=1)
    ax.fill_between([x0, x1], [entry, entry], [tp1, tp1], color=C_TP1, alpha=0.12, linewidth=0, zorder=1)
    if dual:
        ax.fill_between([x0, x1], [tp1, tp1], [tp2, tp2], color=C_TP2, alpha=0.07, linewidth=0, zorder=1)

    _hline(ax, entry, x0, x1, C_ENTRY, "-", 1.6)
    _hline(ax, sl, x0, x1, C_SL, "--")
    _hline(ax, tp1, x0, x1, C_TP1, "--")
    if dual:
        _hline(ax, tp2, x0, x1, C_TP2, ":")

    ax.plot([x0], [entry], marker="^" if signal_type == "BUY" else "v", color=C_ENTRY,
            markersize=9, markeredgecolor="white", markeredgewidth=0.8, zorder=8)

    if fvg and fvg.get("fvg_top") is not None and fvg.get("fvg_bottom") is not None:
        top, bot = fvg["fvg_top"], fvg["fvg_bottom"]
        if top >= lo and bot <= hi:
            ax.axhspan(max(bot, lo), min(top, hi), xmax=(n + 1) / (n + pad + 1),
                       color=C_FVG, alpha=0.16, zorder=1)

    labels = [
        (entry, f"{signal_type} Entry ${entry:.2f}", C_ENTRY),
        (sl, f"SL ${sl:.2f}{_r_text(sl, entry, sl_dist, signal_type)}", C_SL),
        (tp1, f"TP1 ${tp1:.2f}{_r_text(tp1, entry, sl_dist, signal_type)}", C_TP1),
    ]
    if dual:
        labels.append((tp2, f"TP2 ${tp2:.2f}{_r_text(tp2, entry, sl_dist, signal_type)}", C_TP2))

    if near_zone and near_zone.get("price") and lo <= near_zone["price"] <= hi:
        c_zone, z_lbl = _get_zone_style(near_zone)
        ax.hlines(near_zone["price"], -1, x1, colors=c_zone, linestyles="-.", linewidth=1.1, alpha=0.85, zorder=2)
        labels.append((near_zone["price"], f"{z_lbl} ${near_zone['price']:.2f}", c_zone))

    ax.set_xlim(-1, n + pad)
    ax.set_ylim(lo, hi)
    _set_time_ticks(ax, dfp)
    _place_labels(ax, labels)
    ax.set_title(f"Trading Butler Alert — {symbol} ({signal_type})" + ("  •  DUAL ENTRY" if dual else ""),
                 color="white", fontsize=12, fontweight="bold")
    return _finish(fig)


# ------------------------------------------------------------------ post-trade outcome
def generate_outcome_chart(df, entry_p, sl_p, tp1_p, tp2_p, outcome_status, symbol="XAUUSD",
                           direction=None, entry_time=None, dual=True, pre_bars=25):
    """
    Post-trade execution chart. Plots the exact entry point and ALL exit events
    (Leg 1 TP1, Leg 2 TP2, Break-Even, SL, and Runner SL) on their respective candles.
    """
    if df is None or len(df) == 0:
        return None
    df = df.copy().reset_index(drop=True)
    if direction is None:
        direction = "BUY" if tp1_p > entry_p else "SELL"

    entry_idx = _find_closest_candle_idx(df["time"], entry_time) if "time" in df.columns else None

    start = max(0, entry_idx - pre_bars) if entry_idx is not None else 0
    dfp = df.iloc[start:].reset_index(drop=True)
    n = len(dfp)
    entry_x = (entry_idx - start) if entry_idx is not None else None
    x0 = entry_x if entry_x is not None else 0

    highs, lows = dfp["high"].to_numpy(float), dfp["low"].to_numpy(float)
    sl_dist = abs(entry_p - sl_p)

    scan_from = (entry_x + 1) if entry_x is not None else 0
    markers = []  # List of (candle_idx, price, kind, label_str)

    if outcome_status == "HIT_SL":
        i = _first_touch(highs, lows, sl_p, scan_from)
        if i is not None:
            markers.append((i, sl_p, "sl", "SL Hit"))

    elif outcome_status in ("CLOSED_TP1", "HIT_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_RUNNER_SL"):
        i1 = _first_touch(highs, lows, tp1_p, scan_from)
        if i1 is not None:
            markers.append((i1, tp1_p, "tp", "Leg 1 TP1 Hit"))

            if outcome_status == "HIT_TP2":
                i2 = _first_touch(highs, lows, tp2_p, i1)
                if i2 is not None:
                    markers.append((i2, tp2_p, "tp", "Leg 2 TP2 Hit"))

            elif outcome_status == "CLOSED_BE":
                ib = _first_touch(highs, lows, entry_p, i1 + 1)
                if ib is not None:
                    markers.append((ib, entry_p, "be", "Leg 2 BE Exit"))

            elif outcome_status == "CLOSED_RUNNER_SL":
                isl = _first_touch(highs, lows, sl_p, i1 + 1)
                if isl is not None:
                    markers.append((isl, sl_p, "sl", "Leg 2 SL Hit"))

    x_end = n - 1
    if outcome_status in TERMINAL and markers:
        x_end = max(m[0] for m in markers)
    x_end = max(x_end, x0)

    runner_be = dual and outcome_status in ("HIT_TP1", "CLOSED_BE")
    post_tp1 = outcome_status in ("HIT_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_RUNNER_SL")
    show_tp2 = dual

    pad = max(10, int(n * 0.32))
    fig, ax = _new_fig()
    _draw_candles(ax, dfp)

    levels = [entry_p, sl_p, tp1_p] + ([tp2_p] if show_tp2 and tp2_p else [])
    _fit_ylim(ax, dfp, levels)

    ax.fill_between([x0, x_end], [entry_p, entry_p], [sl_p, sl_p], color=C_SL, alpha=0.10, linewidth=0, zorder=1)
    ax.fill_between([x0, x_end], [entry_p, entry_p], [tp1_p, tp1_p], color=C_TP1, alpha=0.10, linewidth=0, zorder=1)
    if show_tp2 and tp2_p:
        ax.fill_between([x0, x_end], [tp1_p, tp1_p], [tp2_p, tp2_p], color=C_TP2, alpha=0.06, linewidth=0, zorder=1)

    ext_to = _label_x_start(n, pad) - 0.5
    _hline(ax, entry_p, x0, x_end, C_BE if runner_be else C_ENTRY, "-", 1.6)
    _hline(ax, sl_p, x0, x_end, C_SL, "--", 1.2, alpha=0.35 if post_tp1 else 1.0)
    _hline(ax, tp1_p, x0, x_end, C_TP1, "--", 1.2)
    if show_tp2 and tp2_p:
        _hline(ax, tp2_p, x0, x_end, C_TP2, ":", 1.2)

    if ext_to > x_end:
        for y, c in ((entry_p, C_ENTRY), (sl_p, C_SL), (tp1_p, C_TP1)):
            ax.hlines(y, x_end, ext_to, colors=c, linestyles=":", linewidth=0.7, alpha=0.3, zorder=2)

    if entry_x is not None:
        ax.plot([entry_x], [entry_p], marker="^" if direction == "BUY" else "v", color=C_ENTRY,
                markersize=10, markeredgecolor="white", markeredgewidth=0.8, zorder=8)

    for idx, price, kind, label_str in markers:
        style = {"sl": ("X", C_SL), "tp": ("*", C_TP1), "be": ("D", C_BE)}[kind]
        ax.plot([idx], [price], marker=style[0], color=style[1], markersize=12 if kind == "tp" else 9,
                markeredgecolor="white", markeredgewidth=0.8, zorder=8)

    entry_label = f"{'Entry / BE' if runner_be else 'Entry'} ${entry_p:.2f}"
    sl_label = f"SL{' (orig)' if post_tp1 else ''} ${sl_p:.2f}{_r_text(sl_p, entry_p, sl_dist, direction)}"
    labels = [
        (entry_p, entry_label, C_BE if runner_be else C_ENTRY),
        (sl_p, sl_label, C_SL),
        (tp1_p, f"TP1 ${tp1_p:.2f}{_r_text(tp1_p, entry_p, sl_dist, direction)}", C_TP1),
    ]
    if show_tp2 and tp2_p:
        labels.append((tp2_p, f"TP2 ${tp2_p:.2f}{_r_text(tp2_p, entry_p, sl_dist, direction)}", C_TP2))

    ax.set_xlim(-1, n + pad)
    _set_time_ticks(ax, dfp)
    _place_labels(ax, labels)

    if entry_x is None:
        ax.text(0.01, 0.97, "entry bar is older than this chart window — lines shown full-width",
                transform=ax.transAxes, color="#888888", fontsize=7.5, va="top")

    color, text = STATUS_STYLE.get(outcome_status, ("#FFFFFF", outcome_status))
    ax.set_title(f"[{symbol}] {direction} — {text}", color=color, fontsize=12, fontweight="bold")
    return _finish(fig)