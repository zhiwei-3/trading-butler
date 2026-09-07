import io
import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

def generate_chart_snapshot(df: pd.DataFrame, title: str = "XAUUSD Technical Snapshot") -> io.BytesIO:
    """Renders a candlestick chart with EMA overlays into an in-memory BytesIO buffer."""
    chart_df = df.tail(60).copy()
    if 'time' in chart_df.columns:
        chart_df.set_index(pd.DatetimeIndex(chart_df['time']), inplace=True)

    # Dark / Institutional color palette
    mc = mpf.make_marketcolors(
        up='#00c853', down='#ff1744',
        edge='inherit', wick='inherit', volume='in'
    )
    style = mpf.make_mpf_style(
        base_mpf_style='nightclouds',
        marketcolors=mc,
        gridstyle='--',
        y_on_right=True
    )

def generate_signal_chart(df, symbol, signal_type, entry, sl, tp1, tp2, fvg=None, near_zone=None, bars=50):
    """Generates an annotated dark-mode candlestick chart buffer for live Telegram alerts."""
    if df is None or len(df) < bars:
        return None

    df_plot = df.tail(bars).copy()
    fig, ax = plt.subplots(figsize=(10, 5.5), facecolor="#121212")
    ax.set_facecolor("#121212")

    times = df_plot['time'].values
    opens = df_plot['open'].values
    highs = df_plot['high'].values
    lows = df_plot['low'].values
    closes = df_plot['close'].values

    # Render Candlesticks
    up = closes >= opens
    down = closes < opens
    col_up, col_dn = '#00c853', '#ff3d00'

    ax.vlines(times, lows, highs, color=np.where(up, col_up, col_dn), linewidth=1, alpha=0.8)
    ax.vlines(times, opens, closes, color=np.where(up, col_up, col_dn), linewidth=3.5, alpha=0.9)

    # Plot Signal Level Lines
    ax.axhline(entry, color="#2962ff", linestyle="-", linewidth=1.5, label=f"Entry: ${entry:.2f}")
    ax.axhline(sl, color="#ff1744", linestyle="--", linewidth=1.5, label=f"SL: ${sl:.2f}")
    ax.axhline(tp1, color="#00e676", linestyle="--", linewidth=1.5, label=f"TP1: ${tp1:.2f}")
    ax.axhline(tp2, color="#00b0ff", linestyle=":", linewidth=1.5, label=f"TP2: ${tp2:.2f}")

    # Highlight FVG Zone
    if fvg and fvg.get("fvg_top") and fvg.get("fvg_bottom"):
        ax.axhspan(fvg["fvg_bottom"], fvg["fvg_top"], color="#ffd600", alpha=0.18, label="Active FVG")

    # Highlight S/R Level
    if near_zone and near_zone.get("price"):
        ax.axhline(near_zone["price"], color="#aa00ff", linestyle="-.", alpha=0.7, label=f"S/R Zone (${near_zone['price']:.2f})")

    ax.set_title(f"🤵‍♂️ Trading Butler Alert — {symbol} ({signal_type})", color="white", fontsize=12, fontweight="bold")
    ax.legend(facecolor="#1e1e1e", edgecolor="#333333", labelcolor="white", loc="upper left", fontsize=9)
    ax.tick_params(colors="white")

    for spine in ax.spines.values():
        spine.set_color("#333333")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf

    # Overlay indicator plots (20 EMA & 50 EMA)
    addplots = []
    if 'EMA_20' in chart_df.columns:
        addplots.append(mpf.make_addplot(chart_df['EMA_20'], color='#2962ff', width=1.5))
    if 'EMA_50' in chart_df.columns:
        addplots.append(mpf.make_addplot(chart_df['EMA_50'], color='#ff6d00', width=1.5))

    buf = io.BytesIO()
    mpf.plot(
        chart_df,
        type='candle',
        style=style,
        addplot=addplots,
        title=f"\n{title}",
        ylabel='Price ($)',
        savefig=dict(fname=buf, dpi=180, bbox_inches='tight'),
        volume=False
    )
    buf.seek(0)
    return buf