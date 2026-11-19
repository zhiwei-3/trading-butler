# charts/theme.py
import functools
import threading

BG, GRID, FG = "#121212", "#262626", "#e0e0e0"
UP, DN = "#00c853", "#ff3d00"
C_ENTRY, C_SL = "#2962ff", "#ff1744"
C_TP1, C_TP2 = "#00e676", "#00b0ff"
C_BE, C_SR, C_FVG = "#17a2b8", "#aa00ff", "#e0e0e0"
C_SUP, C_RES = "#00c853", "#ff3d00"            # Support (Green) / Resistance (Red)
C_BULL_OB, C_BEAR_OB = "#00e5ff", "#ff4081"    # Bullish OB (Cyan) / Bearish OB (Pink)
C_PAST = "#8ab4f8"                             # past (historical) trade entries

TERMINAL = ("HIT_SL", "CLOSED_TP1", "HIT_TP2", "CLOSED_BE", "CLOSED_RUNNER_SL")
STATUS_STYLE = {
    "HIT_TP1":           ("#28A745", "LEG 1 TP1 HIT — RUNNER ACTIVE"),
    "CLOSED_TP1":        ("#28A745", "TP1 HIT — FULL CLOSE"),
    "HIT_TP2":           ("#1E9E4A", "TP2 HIT — FULL TARGET"),
    "CLOSED_BE":         ("#17A2B8", "RUNNER CLOSED AT BREAK-EVEN"),
    "CLOSED_RUNNER_SL":  ("#FD7E14", "RUNNER CLOSED AT SL"),
    "CLOSED_MANUAL":     ("#9E9E9E", "CLOSED MANUALLY"),
    "HIT_SL":            ("#DC3545", "STOP LOSS HIT"),
}

# status -> (price field on the trade dict, marker kind) for historical exit markers
EXIT_RULES = {
    "HIT_SL":           ("sl", "sl"),
    "CLOSED_RUNNER_SL": ("sl", "sl"),
    "CLOSED_TP1":       ("tp1", "tp"),
    "HIT_TP1":          ("tp1", "tp"),   # leg 1 won, runner still open
    "HIT_TP2":          ("tp2", "tp"),
    "CLOSED_BE":        ("entry", "be"),
}

# matplotlib's pyplot is not thread-safe; every figure is built under this lock.
CHART_LOCK = threading.RLock()


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with CHART_LOCK:
            return fn(*a, **k)
    return wrapper