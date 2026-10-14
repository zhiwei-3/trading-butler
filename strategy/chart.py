# strategy/chart.py

import io
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D

from mt5_engine import get_broker_utc_offset_seconds

# ------------------------------------------------------------------ theme
BG, GRID, FG = "#121212", "#262626", "#e0e0e0"
UP, DN = "#00c853", "#ff3d00"
C_ENTRY, C_SL = "#2962ff", "#ff1744"
C_TP1, C_TP2 = "#00e676", "#00b0ff"
C_BE, C_SR, C_FVG = "#17a2b8", "#aa00ff", "#ffd600"
C_SUP, C_RES = "#00c853", "#ff3d00"  # Support (Green) / Resistance (Red)
C_PAST = "#8ab4f8"                   # past (historical) trade entries

TERMINAL = ("HIT_SL", "CLOSED_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_RUNNER_SL")
STATUS_STYLE = {
    "HIT_TP1":           ("#28A745", "LEG 1 TP1 HIT — RUNNER ACTIVE"),
    "CLOSED_TP1":        ("#28A745", "TP1 HIT — FULL CLOSE"),
    "HIT_TP2":           ("#1E9E4A", "TP2 HIT — FULL TARGET"),
    "CLOSED_BE":         ("#17A2B8", "RUNNER CLOSED AT BREAK-EVEN"),
    "CLOSED_RUNNER_SL":  ("#FD7E14", "RUNNER CLOSED AT SL"),
    "HIT_SL":            ("#DC3545", "STOP LOSS HIT"),
}

# status -> (price field on the trade dict, marker kind) for historical exit markers
_EXIT_RULES = {
    "HIT_SL":           ("sl", "sl"),
    "CLOSED_RUNNER_SL": ("sl", "sl"),
    "CLOSED_TP1":       ("tp1", "tp"),
    "HIT_TP1":          ("tp1", "tp"),   # leg 1 won, runner still open
    "HIT_TP2":          ("tp2", "tp"),
    "CLOSED_BE":        ("entry", "be"),
}

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
_ONE_SEC = pd.Timedelta(seconds=1)


# ------------------------------------------------------------------ time helpers
def _utc_epoch(value):
    """Any timestamp-ish value -> UTC epoch seconds. Naive values are treated as UTC
    (that's how the DB stores them). Returns None if unparseable."""
    if value is None:
        return None
    try:
        t = pd.Timestamp(value)
        if pd.isna(t):
            return None
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return int((t - _EPOCH) // _ONE_SEC)
    except Exception:
        return None


def _series_epoch(times):
    """Candle 'time' column -> int64 epoch seconds. Resolution-agnostic (ns/us/ms/s)."""
    idx = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
    return np.asarray((idx - _EPOCH) // _ONE_SEC, dtype="int64")


def _resolve_offset(server_offset_s):
    if server_offset_s is not None:
        return int(server_offset_s)
    try:
        return int(get_broker_utc_offset_seconds())
    except Exception:
        return 0


def _candle_index(candle_ts, utc_value, offset_s):
    """
    Maps a true-UTC timestamp (from the DB) to the candle it occurred in, using
    floor-binning against BROKER-SERVER candle times. Returns None when the time
    lies outside the visible window — never clamps to an edge bar.
    """
    if candle_ts is None or len(candle_ts) == 0:
        return None
    t = _utc_epoch(utc_value)
    if t is None:
        return None
    t += int(offset_s)
    bar = int(np.median(np.diff(candle_ts))) if len(candle_ts) > 1 else 60
    if t < candle_ts[0] or t >= candle_ts[-1] + max(bar, 1):
        return None
    return max(int(np.searchsorted(candle_ts, t, side="right")) - 1, 0)


# ------------------------------------------------------------------ helpers
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


def _set_time_ticks(ax, df, n=6, offset_s=None):
    if "time" not in df.columns or len(df) < 2:
        ax.set_xticks([])
        return
    times = pd.to_datetime(df["time"])
    idx = np.unique(np.linspace(0, len(df) - 1, n).astype(int))
    multi_day = times.iloc[0].date() != times.iloc[-1].date()
    fmt = "%d %b %H:%M" if multi_day else "%H:%M"
    ax.set_xticks(idx)
    ax.set_xticklabels([times.iloc[i].strftime(fmt) for i in idx], color=FG, fontsize=8)
    if offset_s is not None:
        ax.set_xlabel(f"Broker server time (UTC{offset_s / 3600:+g})", color="#888888", fontsize=7.5)


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


def _first_touch(highs, lows, level, start, stop=None):
    """First candle in [start, stop] whose range contains `level`."""
    end = len(highs) if stop is None else min(stop + 1, len(highs))
    for i in range(max(start, 0), end):
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


# ------------------------------------------------------------------ past-trade overlay (shared)
def _trade_price(t, name):
    for key in (name, f"{name}_price"):
        v = t.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _overlay_trades(ax, dfp, candle_ts, trades, offset_s, exclude_id=None):
    """
    Draws historical trades that fall inside the visible window: entry triangle at
    the actual entry price, exit marker at the actual exit level (TP star / BE
    diamond / SL cross), joined by a dotted line. Trades outside the window are
    skipped (never clamped to the chart edge). Returns True if anything was drawn.
    """
    if not trades or candle_ts is None or len(candle_ts) == 0:
        return False

    highs, lows = dfp["high"].to_numpy(float), dfp["low"].to_numpy(float)
    drawn = False

    for t in trades:
        if exclude_id is not None and t.get("id") == exclude_id:
            continue

        direction = str(t.get("direction", "BUY")).upper()
        status = str(t.get("outcome") or t.get("status") or "")
        entry_p = _trade_price(t, "entry")

        xi = _candle_index(candle_ts, t.get("entry_time") or t.get("time") or t.get("timestamp"), offset_s)

        xc, exit_p, kind = None, None, None
        rule = _EXIT_RULES.get(status)
        if rule:
            kind = rule[1]
            exit_p = _trade_price(t, rule[0])
            xc = _candle_index(candle_ts, t.get("closed_at") or t.get("updated_at"), offset_s)

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


def _trade_legend_handles():
    def h(marker, color, label, size=8):
        return Line2D([0], [0], marker=marker, color="none", markerfacecolor=color,
                      markeredgecolor="white", markersize=size, label=label)
    return [
        h("^", C_PAST, "Past entry"),
        h("*", C_TP1, "Past TP", 11),
        h("D", C_BE, "Past BE", 6),
        h("X", C_SL, "Past SL", 7),
    ]


def _apply_legend(ax, with_trades=False):
    handles, _ = ax.get_legend_handles_labels()
    if with_trades:
        handles = handles + _trade_legend_handles()
    if handles:
        ax.legend(handles=handles, facecolor="#1e1e1e", edgecolor="#333333", labelcolor="white",
                  loc="upper left", fontsize=7.5)


# ------------------------------------------------------------------ market snapshot
def generate_chart_snapshot(df, title="XAUUSD Market Snapshot", bars=100, near_zone=None,
                            macro_fvg=None, trades=None, server_offset_s=None):
    """
    Real-time candlestick snapshot with EMAs, current price, nearest S/R, HTF FVG,
    and overlays for all past trade entries/exits inside the candle window.
    """
    if df is None or len(df) < min(bars, 20):
        return None

    offset_s = _resolve_offset(server_offset_s)
    dfp = df.tail(bars).reset_index(drop=True)
    n = len(dfp)
    pad = max(8, int(n * 0.14))
    candle_ts = _series_epoch(dfp["time"]) if "time" in dfp.columns else None

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

    drawn = _overlay_trades(ax, dfp, candle_ts, trades, offset_s)

    ax.set_xlim(-1, n + pad)
    ax.set_ylim(lo, hi)
    _set_time_ticks(ax, dfp, offset_s=offset_s)
    _place_labels(ax, labels)
    ax.set_title(title, color="white", fontsize=12, fontweight="bold")
    _apply_legend(ax, with_trades=drawn)
    return _finish(fig)


# ------------------------------------------------------------------ live signal alert
def generate_signal_chart(df, symbol, signal_type, entry, sl, tp1, tp2,
                          fvg=None, near_zone=None, bars=80, dual=True,
                          trades=None, exclude_id=None, server_offset_s=None):
    """Alert chart with projected entry/SL/TP lines, shaded risk/reward zones and past trade history."""
    if df is None or len(df) < min(bars, 50):
        return None

    offset_s = _resolve_offset(server_offset_s)
    dfp = df.tail(bars).reset_index(drop=True)
    n = len(dfp)
    pad = max(12, int(n * 0.45))
    x0 = n - 1
    x1 = _label_x_start(n, pad) - 0.5
    sl_dist = abs(entry - sl)
    candle_ts = _series_epoch(dfp["time"]) if "time" in dfp.columns else None

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
            markersize=10, markeredgecolor="white", markeredgewidth=0.9, zorder=9)

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

    drawn = _overlay_trades(ax, dfp, candle_ts, trades, offset_s, exclude_id=exclude_id)

    ax.set_xlim(-1, n + pad)
    ax.set_ylim(lo, hi)
    _set_time_ticks(ax, dfp, offset_s=offset_s)
    _place_labels(ax, labels)
    ax.set_title(f"Trading Butler Alert — {symbol} ({signal_type})" + ("  •  DUAL ENTRY" if dual else ""),
                 color="white", fontsize=12, fontweight="bold")
    _apply_legend(ax, with_trades=drawn)
    return _finish(fig)


# ------------------------------------------------------------------ post-trade outcome
def generate_outcome_chart(df, entry_p, sl_p, tp1_p, tp2_p, outcome_status, symbol="XAUUSD",
                           direction=None, entry_time=None, exit_time=None, dual=True,
                           pre_bars=25, post_bars=8, trades=None, exclude_id=None,
                           server_offset_s=None):
    """
    Post-trade chart. Entry and exit markers are placed on the real entry/exit
    candles (entry_time / exit_time are true-UTC values straight from the DB),
    the chart is trimmed to [entry - pre_bars, exit + post_bars], and other past
    trades inside that window are overlaid.
    """
    if df is None or len(df) == 0:
        return None
    df = df.copy().reset_index(drop=True)
    if direction is None:
        direction = "BUY" if tp1_p > entry_p else "SELL"

    offset_s = _resolve_offset(server_offset_s)
    has_time = "time" in df.columns
    full_ts = _series_epoch(df["time"]) if has_time else None
    last = len(df) - 1

    entry_idx = _candle_index(full_ts, entry_time, offset_s) if has_time and entry_time is not None else None
    exit_idx = _candle_index(full_ts, exit_time, offset_s) if has_time and exit_time is not None else None
    if exit_idx is not None:
        exit_idx = max(exit_idx, entry_idx if entry_idx is not None else 0)

    highs_f, lows_f = df["high"].to_numpy(float), df["low"].to_numpy(float)
    scan_from = min((entry_idx + 1) if entry_idx is not None else max(0, last - 100), last)

    def touch(level, start, stop=None):
        return _first_touch(highs_f, lows_f, level, start, stop)

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

    # ---- trim to the trade's lifespan
    start = max(0, entry_idx - pre_bars) if entry_idx is not None else max(0, last - 100)
    end = last
    if terminal and markers:
        end = min(last, max(m[0] for m in markers) + post_bars)
    dfp = df.iloc[start:end + 1].reset_index(drop=True)
    n = len(dfp)
    candle_ts = full_ts[start:end + 1] if has_time else None

    entry_x = (entry_idx - start) if entry_idx is not None else None
    markers = [(i - start, p, k) for i, p, k in markers]
    x0 = entry_x if entry_x is not None else 0
    x_end = max(m[0] for m in markers) if (terminal and markers) else n - 1
    x_end = max(x_end, x0)

    runner_be = dual and outcome_status in ("HIT_TP1", "CLOSED_BE")
    post_tp1 = outcome_status in ("HIT_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_RUNNER_SL")
    show_tp2 = bool(dual and tp2_p)
    sl_dist = abs(entry_p - sl_p)

    pad = max(10, int(n * 0.32))
    fig, ax = _new_fig()
    _draw_candles(ax, dfp)

    levels = [entry_p, sl_p, tp1_p] + ([tp2_p] if show_tp2 else [])
    _fit_ylim(ax, dfp, levels)

    ax.fill_between([x0, x_end], [entry_p, entry_p], [sl_p, sl_p], color=C_SL, alpha=0.10, linewidth=0, zorder=1)
    ax.fill_between([x0, x_end], [entry_p, entry_p], [tp1_p, tp1_p], color=C_TP1, alpha=0.10, linewidth=0, zorder=1)
    if show_tp2:
        ax.fill_between([x0, x_end], [tp1_p, tp1_p], [tp2_p, tp2_p], color=C_TP2, alpha=0.06, linewidth=0, zorder=1)

    ext_to = _label_x_start(n, pad) - 0.5
    _hline(ax, entry_p, x0, x_end, C_BE if runner_be else C_ENTRY, "-", 1.6)
    _hline(ax, sl_p, x0, x_end, C_SL, "--", 1.2, alpha=0.35 if post_tp1 else 1.0)
    _hline(ax, tp1_p, x0, x_end, C_TP1, "--", 1.2)
    if show_tp2:
        _hline(ax, tp2_p, x0, x_end, C_TP2, ":", 1.2)

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

    drawn = _overlay_trades(ax, dfp, candle_ts, trades, offset_s, exclude_id=exclude_id)

    entry_label = f"{'Entry / BE' if runner_be else 'Entry'} ${entry_p:.2f}"
    sl_label = f"SL{' (orig)' if post_tp1 else ''} ${sl_p:.2f}{_r_text(sl_p, entry_p, sl_dist, direction)}"
    labels = [
        (entry_p, entry_label, C_BE if runner_be else C_ENTRY),
        (sl_p, sl_label, C_SL),
        (tp1_p, f"TP1 ${tp1_p:.2f}{_r_text(tp1_p, entry_p, sl_dist, direction)}", C_TP1),
    ]
    if show_tp2:
        labels.append((tp2_p, f"TP2 ${tp2_p:.2f}{_r_text(tp2_p, entry_p, sl_dist, direction)}", C_TP2))

    ax.set_xlim(-1, n + pad)
    _set_time_ticks(ax, dfp, offset_s=offset_s)
    _place_labels(ax, labels)

    if entry_x is None:
        ax.text(0.01, 0.97, "entry bar is outside this chart window — levels shown full-width",
                transform=ax.transAxes, color="#888888", fontsize=7.5, va="top")

    color, text = STATUS_STYLE.get(outcome_status, ("#FFFFFF", outcome_status))
    ax.set_title(f"[{symbol}] {direction} — {text}", color=color, fontsize=12, fontweight="bold")
    _apply_legend(ax, with_trades=drawn)
    return _finish(fig)