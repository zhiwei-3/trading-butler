# strategies/_template/params.py
from ..base import Param
from utils.parsers import num, boolean

PARAMS = (
    Param("lookback",      20,   num(5, 200, integer=True),  ("lb",),  "bars in the breakout range",
          sweep=(10, 15, 20, 30, 40, 60)),
    Param("sl_buffer_atr", 0.25, num(0, 5),                  ("buf",), "SL buffer beyond the opposite range edge (ATR)",
          sweep=(0.1, 0.25, 0.5)),
    Param("min_sl_atr",    0.8,  num(0, 10, lo_open=True),   (),       "SL floor (ATR)"),
    Param("max_sl_atr",    3.0,  num(0, 20, lo_open=True),   (),       "skip setups with a wider stop (ATR)"),
    Param("min_rrr",       1.5,  num(0, 20, lo_open=True),   ("rrr",), "minimum TP1:SL reward-to-risk to fire",
          sweep=(1.0, 1.5, 2.0, 2.5), legacy="min_rrr"),
    Param("tp1_rr",        1.5,  num(0, 20, lo_open=True),   (),       "TP1 reward:risk",
          sweep=(1.2, 1.5, 2.0, 2.5)),
    Param("tp2_rr",        3.0,  num(0, 50, lo_open=True),   (),       "TP2 reward:risk"),
    Param("dual",          True, boolean,                    (),       "2 legs: TP1 + TP2/BE runner"),
)