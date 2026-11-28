from ..base import Param
from .._lib import sizing_params, volume_gate_params
from utils.parsers import num, boolean, choice

# Order matters: the default /optimize axes are the first two sweepable params -> min_bars, tp1_rr.
PARAMS = (
    Param("min_bars", 6, num(2, 50, integer=True), ("bars", "squeeze_bars"),
          "min consecutive squeeze bars before a breakout counts",
          sweep=(4, 6, 8, 12)),
    Param("tp1_rr", 1.5, num(0, 20, lo_open=True), (), "TP1 reward:risk",
          sweep=(1.2, 1.5, 2.0, 2.5)),
    Param("min_rrr", 1.5, num(0, 20, lo_open=True), ("rrr",), "floor on the TP1 reward:risk actually used",
          sweep=(1.0, 1.5, 2.0, 2.5)),
    Param("tp2_rr", 3.0, num(0, 50, lo_open=True), (), "TP2 reward:risk"),
    Param("bb_len", 20, num(5, 100, integer=True), (), "Bollinger Band length"),
    Param("bb_mult", 2.0, num(0.5, 5), ("bb",), "Bollinger Band std-dev multiplier"),
    Param("kc_len", 20, num(5, 100, integer=True), (), "Keltner Channel EMA length"),
    Param("kc_mult", 1.5, num(0.5, 5), ("kc",), "Keltner Channel ATR multiplier (lower = stricter squeeze)",
          sweep=(1.0, 1.25, 1.5, 2.0)),
    Param("box_max_bars", 30, num(5, 100, integer=True), (), "max bars of the squeeze run used to build the box"),
    Param("max_box_atr", 3.0, num(0, 20, lo_open=True), (), "skip boxes wider than this (ATR)"),
    Param("close_margin_atr", 0.1, num(0, 2), ("margin",), "close must clear the box edge by this (ATR)",
          sweep=(0.0, 0.1, 0.2)),
    Param("min_body_ratio", 0.5, num(0, 1), ("body",), "breakout candle body / range minimum"),
    Param("use_session_filter", True, boolean, ("session",), "only trade inside the UTC session window"),
    Param("session_start", 7.0, num(0, 23.75), ("sess_start",), "UTC hour the trading window opens"),
    Param("session_end", 20.0, num(0.25, 24), ("sess_end",), "UTC hour the trading window closes"),
    Param("ema_filter", False, boolean, ("ema",), "require EMA20/EMA50 alignment with the breakout direction"),
    Param("sl_at", "edge", choice("edge", "mid"), (), "stop at the opposite box edge or the box midpoint"),
    Param("sl_buffer_atr", 0.25, num(0, 5), ("buf",), "SL buffer beyond the structural level (ATR)",
          sweep=(0.1, 0.25, 0.5)),
    Param("min_sl_atr", 0.8, num(0, 10, lo_open=True), ("min_sl",), "SL floor (ATR)"),
    Param("max_sl_atr", 3.0, num(0, 20, lo_open=True), ("max_sl",), "skip setups whose stop is wider than this (ATR)"),
    Param("dual", True, boolean, (), "2 legs: TP1 full close + TP2/BE runner"),
) + volume_gate_params() + sizing_params()