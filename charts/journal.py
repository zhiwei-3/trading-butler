# charts/journal.py
import io

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from charts.theme import locked, BG, GRID, FG, UP, DN, C_ENTRY, C_SR


def _style_ax(ax):
    ax.set_facecolor(BG)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.8)
    ax.tick_params(colors=FG, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333333")


def _date_axis(ax):
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))


@locked
def generate_journal_chart(trades, curr="USD", title="Trade Journal", subtitle=""):
    """
    3-panel journal dashboard: cumulative P&L (+ drawdown), daily P&L, P&L by close hour.
    `trades`: closed trades with tz-aware 'time', float 'net_pnl' and 'source' ('live'|'dry').
    Live and dry-run get separate equity lines so simulated P&L never blends into real P&L.
    """
    closed = sorted((t for t in trades if t.get("time") is not None), key=lambda t: t["time"])
    if len(closed) < 2:
        return None

    fig = plt.figure(figsize=(10, 8.6), facecolor=BG)
    gs = fig.add_gridspec(2, 2, height_ratios=[1.45, 1])
    ax1 = fig.add_subplot(gs[0, :])
    ax2 = fig.add_subplot(gs[1, 0])
    ax3 = fig.add_subplot(gs[1, 1])
    for ax in (ax1, ax2, ax3):
        _style_ax(ax)

    naive = lambda dt: dt.replace(tzinfo=None)      # aware -> naive for matplotlib

    # ---- 1. cumulative P&L
    names = {"live": "Live", "dry": "Dry-run (simulated)"}
    colors = {"live": C_ENTRY, "dry": C_SR}
    sources = [s for s in ("live", "dry") if any(t["source"] == s for t in closed)]
    for s in sources:
        sub = [t for t in closed if t["source"] == s]
        xs = [naive(sub[0]["time"])] + [naive(t["time"]) for t in sub]
        cum = np.concatenate([[0.0], np.cumsum([t["net_pnl"] for t in sub])])
        ax1.step(xs, cum, where="post", color=colors[s], linewidth=1.8, label=names[s], zorder=4)
        dots = [UP if t["net_pnl"] > 0 else DN if t["net_pnl"] < 0 else "#888888" for t in sub]
        ax1.scatter(xs[1:], cum[1:], s=16, c=dots, zorder=5, edgecolors="none")
        if len(sources) == 1:
            peak = np.maximum.accumulate(cum)
            ax1.fill_between(xs, cum, peak, step="post", color=DN, alpha=0.20, label="Drawdown", zorder=2)
            ax1.plot(xs, peak, color="#888888", linewidth=0.8, linestyle=":", drawstyle="steps-post", zorder=3)
    ax1.axhline(0, color="#666666", linewidth=0.9, linestyle="--")
    ax1.set_title(f"Cumulative P&L ({curr})", color=FG, fontsize=10, loc="left")
    _date_axis(ax1)
    ax1.legend(facecolor="#1e1e1e", edgecolor="#333333", labelcolor="white", loc="upper left", fontsize=8)

    # ---- 2. daily P&L
    daily = {}
    for t in closed:
        d = t["time"].date()
        daily[d] = daily.get(d, 0.0) + t["net_pnl"]
    days = sorted(daily)
    vals = [daily[d] for d in days]
    ax2.bar(days, vals, width=0.7,
            color=[UP if v > 0 else DN if v < 0 else "#888888" for v in vals], zorder=3)
    ax2.axhline(0, color="#666666", linewidth=0.9)
    ax2.set_title("Daily P&L (all sources)" if len(sources) > 1 else "Daily P&L",
                  color=FG, fontsize=10, loc="left")
    _date_axis(ax2)

    # ---- 3. P&L by close hour (the trades' own clock: the journal passes local time)
    hourly = np.zeros(24)
    for t in closed:
        hourly[t["time"].hour] += t["net_pnl"]
    ax3.bar(np.arange(24), hourly, width=0.75,
            color=[UP if v > 0 else DN if v < 0 else "#888888" for v in hourly], zorder=3)
    ax3.axhline(0, color="#666666", linewidth=0.9)
    ax3.set_xticks(range(0, 24, 3))
    ax3.set_xlim(-0.8, 23.8)
    ax3.set_title("P&L by close hour (local time)", color=FG, fontsize=10, loc="left")

    fig.suptitle(title, color="white", fontsize=13, fontweight="bold", y=0.985)
    if subtitle:
        fig.text(0.5, 0.943, subtitle, ha="center", color="#9e9e9e", fontsize=9)
    fig.subplots_adjust(top=0.90, bottom=0.07, left=0.08, right=0.97, hspace=0.38, wspace=0.22)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf