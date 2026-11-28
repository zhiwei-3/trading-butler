from ..base import Param
from .._lib import sizing_params, volume_gate_params
from utils.parsers import num, boolean, choice

# Order matters: the default /optimize axes are the first two sweepable params -> entry_z, z_len.
PARAMS = (
    Param("entry_z", 2.0, num(0.5, 5), ("z", "entry"), "|Z| the prior bar must reach before a hook can fire",
          sweep=(1.5, 2.0, 2.5, 3.0)),
    Param("z_len", 50, num(10, 120, integer=True), ("len",), "SMA / std-dev lookback (bars)",
          sweep=(20, 30, 50, 80)),
    Param("stop_z", 3.5, num(0.5, 8), ("sz",), "hard stop sits at Z = +/- this (also the 'extreme blown through' cap)",
          sweep=(3.0, 3.5, 4.0)),
    Param("adx_max", 25.0, num(5, 80), ("adx_cap",), "entry-TF ADX(14) must be at or below this (ranging market)",
          sweep=(18, 22, 25, 30)),
    Param("slope_bars", 5, num(1, 50, integer=True), (), "bars used to measure the SMA slope"),
    Param("slope_max_atr", 0.6, num(0, 20), ("slope",), "max |SMA move| over slope_bars, in ATR (flat-mean filter)",
          sweep=(0.3, 0.6, 1.0)),
    Param("require_candle", True, boolean, ("candle",), "hook bar must also close in the trade direction"),
    Param("sl_mode", "z", choice("z", "extreme"), (), "stop at Z=stop_z, or beyond the recent extreme"),
    Param("extreme_bars", 3, num(1, 10, integer=True), (), "bars used to find the extreme when sl_mode=extreme"),
    Param("sl_buffer_atr", 0.25, num(0, 5), ("buf",), "SL buffer beyond the stop level (ATR)",
          sweep=(0.1, 0.25, 0.5)),
    Param("min_sl_atr", 0.8, num(0, 10, lo_open=True), ("min_sl",), "SL floor (ATR)"),
    Param("max_sl_atr", 4.0, num(0, 20, lo_open=True), ("max_sl",), "skip setups whose stop is wider than this (ATR)"),
    Param("min_rrr", 1.0, num(0, 20, lo_open=True), ("rrr",), "minimum reward-to-risk (TP1 = the mean) to fire",
          sweep=(0.8, 1.0, 1.2, 1.5)),
    Param("tp2_z", 1.0, num(0, 5), (), "dual only: TP2 sits this many std-devs beyond the mean"),
    Param("dual", False, boolean, (), "2 legs: TP1 (mean) full close + TP2/BE runner"),
) + volume_gate_params() + sizing_params()