from ..base import Param
from .._lib import sizing_params
from utils.parsers import num, boolean

# Order matters: the default /optimize axes are the first two sweepable params -> adx_min, tp1_rr.
PARAMS = (
    Param("adx_min", 22.0, num(5, 60), ("adx",), "H1 ADX(14) must exceed this (trending market)",
          sweep=(18, 20, 22, 25, 28), legacy="tb_adx_min"),
    Param("tp1_rr", 1.5, num(0, 20, lo_open=True), (), "TP1 reward:risk",
          sweep=(1.2, 1.5, 2.0, 2.5), legacy="tb_tp1_rr"),
    Param("min_rrr", 1.5, num(0, 20, lo_open=True), ("rrr",), "floor on the TP1 reward:risk actually used",
          sweep=(1.0, 1.5, 2.0, 2.5), legacy="min_rrr"),
    Param("tp2_rr", 3.0, num(0, 50, lo_open=True), (), "TP2 reward:risk", legacy="tb_tp2_rr"),
    Param("window_hours", 2.0, num(0.25, 6), ("window",), "how long each London/NY window stays open",
          sweep=(1.0, 1.5, 2.0, 3.0), legacy="tb_window_hours"),
    Param("london_start", 8.0, num(0, 23.75), ("london",), "UTC hour the London window opens", legacy="tb_london_start"),
    Param("ny_start", 13.0, num(0, 23.75), ("ny",), "UTC hour the NY window opens", legacy="tb_ny_start"),
    Param("use_breakout", True, boolean, ("breakout",), "trade a candle close beyond the Asian range", legacy="tb_use_breakout"),
    Param("use_sweep_bos", True, boolean, ("sweep",), "trade an Asian sweep followed by a BOS", legacy="tb_use_sweep_bos"),
    Param("sweep_max_bars", 20, num(1, 200, integer=True), (), "max bars between the sweep and the BOS", legacy="tb_sweep_max_bars"),
    Param("bos_pre_bars", 6, num(1, 100, integer=True), (), "bars before the sweep used to find the BOS level", legacy="tb_bos_pre_bars"),
    Param("sl_buffer_atr", 0.25, num(0, 5), ("buf",), "SL buffer beyond the structural level (ATR)",
          sweep=(0.1, 0.25, 0.5), legacy="tb_sl_buffer_atr"),
    Param("min_sl_atr", 0.8, num(0, 10, lo_open=True), ("min_sl",), "SL floor, protects against spread/wicks (ATR)", legacy="tb_min_sl_atr"),
    Param("max_sl_atr", 3.0, num(0, 20, lo_open=True), ("max_sl",), "skip setups whose stop is wider than this (ATR)", legacy="tb_max_sl_atr"),
    Param("dual", True, boolean, (), "2 legs: TP1 full close + TP2/BE runner", legacy="tb_dual_entry"),
) + sizing_params(dynamic=True, legacy_dynamic=True)       # this strategy sizes by risk % (as before)