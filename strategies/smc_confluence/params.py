from .._lib import risk_params, rsi_params, score_params

# Order matters: the default /optimize axes are the first two sweepable params -> min_score, min_rrr.
PARAMS = (score_params(min_score=35, dual_score=50)
          + risk_params(min_rrr=1.5, sl=1.2, tp1=2.0, tp2=3.5)
          + rsi_params(buy=30.0, sell=70.0, margin=5.0))