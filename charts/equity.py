# charts/equity.py
import io

import matplotlib.pyplot as plt

from charts.theme import locked


@locked
def generate_equity_chart(equity_curve, title="Backtest Equity Curve"):
    if not equity_curve or len(equity_curve) < 2:
        return None
    fig, ax = plt.subplots(figsize=(8, 4.5), facecolor="#1b1b1b")
    ax.set_facecolor("#1b1b1b")
    ax.plot(equity_curve, color="#2962ff", linewidth=1.8)
    ax.axhline(0, color="#666666", linewidth=0.8, linestyle="--")
    ax.set_title(title, color="white", fontsize=11)
    ax.set_xlabel("Trade #", color="white")
    ax.set_ylabel("Cumulative R", color="white")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#444444")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf