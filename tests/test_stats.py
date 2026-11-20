# tests/test_stats.py
from core.stats import deal_stats, signal_summary, realized_return_pct
from utils.fmt import money, pf_text


def trades(*pnls):
    return [{"net_pnl": p} for p in pnls]


def test_deal_stats_counts_profit_factor_drawdown_and_streaks():
    st = deal_stats(trades(10, -5, 10, -5, -5))        # cumulative 10, 5, 15, 10, 5
    assert (st["n"], st["wins"], st["losses"]) == (5, 2, 3)
    assert st["win_rate"] == 40.0 and st["pnl"] == 5
    assert st["pf"] == 1.33                            # 20 gross win / 15 gross loss
    assert st["avg_win"] == 10 and st["avg_loss"] == -5 and st["expectancy"] == 1.0
    assert st["best"] == 10 and st["worst"] == -5
    assert st["max_dd"] == 10                          # peak 15 -> trough 5
    assert (st["max_w"], st["max_l"]) == (1, 2)


def test_breakeven_trades_break_streaks_and_are_not_decided():
    st = deal_stats(trades(5, 5, 0, 5))
    assert st["max_w"] == 2 and st["wins"] == 3 and st["losses"] == 0
    assert st["win_rate"] == 100.0                     # the 0.00 trade is neither a win nor a loss


def test_profit_factor_text_edge_cases():
    assert pf_text(deal_stats(trades(3, 2))) == "∞"    # wins, no losses
    assert pf_text(deal_stats(trades())) == "—"        # nothing at all
    assert pf_text(deal_stats(trades(10, -5))) == "2.0"
    assert deal_stats([])["n"] == 0 and deal_stats([])["win_rate"] == 0.0


def test_signal_summary_ignores_skipped_in_totals_and_win_rate():
    s = signal_summary({"PENDING": 1, "HIT_TP1": 1, "CLOSED_TP1": 2, "HIT_TP2": 1, "CLOSED_BE": 1,
                        "HIT_SL": 2, "CLOSED_MANUAL": 1, "SKIPPED": 4})
    assert s["total"] == 9 and s["skipped"] == 4       # SKIPPED is reported but not counted
    assert (s["wins"], s["closed"], s["win_rate"]) == (4, 6, 66.7)
    assert s["running"] == 1 and s["pending"] == 1
    assert signal_summary({})["win_rate"] == 0.0


def test_realized_return_uses_net_deposits():
    assert realized_return_pct(1100, 1000, 0) == 10.0
    assert realized_return_pct(1000, 1000, 200) == 25.0          # net deposits 800
    assert realized_return_pct(500, 0, 0) is None                # nothing net deposited
    assert realized_return_pct(500, 100, 100) is None


def test_money_formatting():
    assert money(5, "USD") == "+5.00 USD"
    assert money(-1234.5, "EUR") == "-1,234.50 EUR"