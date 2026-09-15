import io
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

def generate_chart_snapshot(df, title="XAUUSD Market Snapshot", bars=60, near_zone=None, macro_fvg=None):
    """Generates a real-time candlestick chart buffer for market snapshots."""
    if df is None or len(df) < bars:
        return None

    df_plot = df.tail(bars).copy()
    fig, ax = plt.subplots(figsize=(10, 5.5), facecolor="#121212")
    ax.set_facecolor("#121212")

    times = range(len(df_plot))
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

    # Overlay EMAs if present
    if 'EMA_20' in df_plot.columns and df_plot['EMA_20'].notna().any():
        ax.plot(times, df_plot['EMA_20'].values, color="#00b0ff", linewidth=1.2, label="EMA 20")
    if 'EMA_50' in df_plot.columns and df_plot['EMA_50'].notna().any():
        ax.plot(times, df_plot['EMA_50'].values, color="#ff9100", linewidth=1.2, label="EMA 50")

    # Current Market Price line
    current_close = closes[-1]
    ax.axhline(current_close, color="#ffffff", linestyle="--", linewidth=1.2, label=f"Price: ${current_close:.2f}")

    # Highlight nearest S/R Zone
    if near_zone and near_zone.get("price"):
        ax.axhline(near_zone["price"], color="#aa00ff", linestyle="-.", alpha=0.7, label=f"S/R Zone (${near_zone['price']:.2f})")

    ax.set_title(title, color="white", fontsize=12, fontweight="bold")
    ax.legend(facecolor="#1e1e1e", edgecolor="#333333", labelcolor="white", loc="upper left", fontsize=9)
    ax.tick_params(colors="white")
    ax.set_xticks([])  # Hide bar indices for clean view

    for spine in ax.spines.values():
        spine.set_color("#333333")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf

def generate_signal_chart(df, symbol, signal_type, entry, sl, tp1, tp2, fvg=None, near_zone=None, bars=50):
    """Generates an annotated dark-mode candlestick chart buffer for live Telegram alerts."""
    if df is None or len(df) < bars:
        return None

    df_plot = df.tail(bars).copy()
    fig, ax = plt.subplots(figsize=(10, 5.5), facecolor="#121212")
    ax.set_facecolor("#121212")

    times = range(len(df_plot))
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

    ax.set_title(f"Trading Butler Alert — {symbol} ({signal_type})", color="white", fontsize=12, fontweight="bold")
    ax.legend(facecolor="#1e1e1e", edgecolor="#333333", labelcolor="white", loc="upper left", fontsize=9)
    ax.tick_params(colors="white")
    ax.set_xticks([])

    for spine in ax.spines.values():
        spine.set_color("#333333")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf

def generate_outcome_chart(df: pd.DataFrame, entry_p: float, sl_p: float, tp1_p: float, tp2_p: float, outcome_status: str, symbol: str = "XAUUSD") -> io.BytesIO:
    """Generates a post-trade visual chart with Entry, SL, and TP horizontal levels."""
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    
    # Plot closing price line or candles
    if 'time' in df.columns:
        df['time'] = pd.to_datetime(df['time'])
        ax.plot(df['time'], df['close'], label="Price", color="#1f77b4", linewidth=1.5)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    else:
        ax.plot(df['close'], label="Price", color="#1f77b4", linewidth=1.5)

    # Automatically trail Stop Loss to Break-Even for TP1/TP2/BE states
    if outcome_status in ("HIT_TP1", "HIT_TP2", "CLOSED_BE"):
        effective_sl = entry_p
        sl_label = f"SL (Break-Even): ${entry_p:.2f}"
        sl_color = "#17A2B8"  # Cyan badge color for Break-Even
    else:
        effective_sl = sl_p
        sl_label = f"SL: ${sl_p:.2f}"
        sl_color = "#FF3333"

    # Horizontal Price Lines
    ax.axhline(entry_p, color="#3357FF", linestyle="--", linewidth=1.2, label=f"Entry: ${entry_p:.2f}")
    ax.axhline(effective_sl, color=sl_color, linestyle="--", linewidth=1.2, label=sl_label)
    ax.axhline(tp1_p, color="#28A745", linestyle=":", linewidth=1.2, label=f"TP1: ${tp1_p:.2f}")
    if tp2_p:
        ax.axhline(tp2_p, color="#1E7E34", linestyle="--", linewidth=1.2, label=f"TP2: ${tp2_p:.2f}")

    # Color badge mapping
    status_colors = {
        "HIT_TP1": "#28A745",
        "HIT_TP2": "#1E7E34",
        "CLOSED_BE": "#17A2B8",
        "HIT_SL": "#DC3545"
    }
    title_color = status_colors.get(outcome_status, "#000000")

    ax.set_title(f"[{symbol}] Trade Outcome Post-Mortem: {outcome_status}", color=title_color, fontsize=12, fontweight="bold")
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper left", frameon=True)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf