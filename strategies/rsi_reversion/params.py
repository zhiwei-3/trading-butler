# strategies/rsi_reversion/params.py
from .._lib import risk_params, rsi_params, sizing_params, volume_gate_params

PARAMS = (risk_params(min_rrr=1.5, sl=1.2, tp1=2.0, tp2=3.5) + rsi_params(buy=30.0, sell=70.0, margin=5.0)
          + volume_gate_params() + sizing_params())