# charts/primitives.py
"""Figure, candles, axes and label helpers shared by every chart."""

import io

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from config import ALERT_STATE, TF_LABELS
from charts.theme import BG, GRID, FG, UP, DN


def new_fig(figsize=(10, 5.5)):
    fig, ax = plt.subplots(figsize=figsize, facecolor=BG)
    ax.set_facecolor(BG)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.8)
    ax.tick_params(colors=FG, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333333")
    return fig, ax


def draw_candles(ax, df, width=0.62):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    x = np.arange(len(df))
    colors = np.where(c >= o, UP, DN)
    ax.vlines(x, l, h, colors=colors, linewidth=1, zorder=3)
    min_body = max((h.max() - l.min()) * 0.0008, 1e-9)
    for xi, oi, ci, col in zip(x, o, c, colors):
        ax.add_patch(Rectangle((xi - width / 2, min(oi, ci)), width, max(abs(ci - oi), min_body),
                               facecolor=col, edgecolor=col, linewidth=0.5, zorder=4))


def set_time_ticks(ax, df, n=6, offset_s=None):
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


def fit_ylim(ax, df, levels, pad=0.08):
    vals = [float(df["low"].min()), float(df["high"].max())] + [float(v) for v in levels if v is not None]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    ax.set_ylim(lo - span * pad, hi + span * pad)


def label_x_start(n, pad):
    """Data-x where the right-hand label column ends near the right chart border."""
    return n + pad - 0.5


def place_labels(ax, labels):
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


def hline(ax, y, x0, x1, color, ls="-", lw=1.4, alpha=1.0):
    ax.hlines(y, x0, x1, colors=color, linestyles=ls, linewidth=lw, alpha=alpha, zorder=5)


def r_text(price, entry, sl_dist, direction):
    if not sl_dist:
        return ""
    r = (price - entry) / sl_dist * (1 if direction == "BUY" else -1)
    return f" ({r:+.1f}R)"


def finish(fig):
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


def first_touch(highs, lows, level, start, stop=None):
    """First candle in [start, stop] whose range contains `level`."""
    end = len(highs) if stop is None else min(stop + 1, len(highs))
    for i in range(max(start, 0), end):
        if lows[i] <= level <= highs[i]:
            return i
    return None


def entry_tf_label(tf_label=None):
    return tf_label or TF_LABELS.get(ALERT_STATE.get("entry_tf"), "?")


def stamp(ax, tf, n):
    """Small corner note so every chart states its timeframe and bar count."""
    ax.text(0.008, 0.015, f"{tf} candles • {n} bars", transform=ax.transAxes, ha="left", va="bottom",
            color="#9e9e9e", fontsize=7.5, zorder=8,
            bbox=dict(boxstyle="round,pad=0.2", fc=BG, ec="none", alpha=0.7))