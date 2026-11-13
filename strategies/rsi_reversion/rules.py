"""PURE rules for RSI overbought/oversold reversion at S/R zones. Shared by live and backtest."""

SCORE = 75     # fixed score (this strategy has no confluence breakdown); single TP1-only leg


def setup(rsi_val, close_price, near_zone, p):
    """'BUY' | 'SELL' | None.
    BUY : RSI oversold AND price holding above a support/mixed zone.
    SELL: RSI overbought AND price rejecting below a resistance/mixed zone."""
    if not near_zone:
        return None
    if (rsi_val <= p["rsi_buy"] and near_zone["type"] in ("support", "mixed")
            and close_price >= near_zone["price"]):
        return "BUY"
    if (rsi_val >= p["rsi_sell"] and near_zone["type"] in ("resistance", "mixed")
            and close_price <= near_zone["price"]):
        return "SELL"
    return None


def reasons(direction, rsi_val, near_zone, p):
    is_buy = direction == "BUY"
    return [
        f"RSI {rsi_val} is {'oversold' if is_buy else 'overbought'} "
        f"(trigger {'<=' if is_buy else '>='} {p['rsi_buy'] if is_buy else p['rsi_sell']})",
        f"Price is {'holding above' if is_buy else 'rejecting below'} a {near_zone['type']} zone at "
        f"{near_zone['price']} ({near_zone['touches']} touches)",
    ]