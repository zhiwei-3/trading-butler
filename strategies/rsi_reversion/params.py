from .._lib import risk_params, rsi_params

# risk params first so the default /optimize axes are min_rrr + sl_mult
PARAMS = risk_params(min_rrr=1.5, sl=1.2, tp1=2.0, tp2=3.5) + rsi_params(buy=30.0, sell=70.0, margin=5.0)