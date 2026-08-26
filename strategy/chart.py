import io
import pandas as pd
import mplfinance as mpf

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