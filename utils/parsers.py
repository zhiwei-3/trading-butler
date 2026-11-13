# utils/parsers.py
"""Value parsers for strategy Param(...) definitions. Each takes a str and raises ValueError."""
import math


def num(lo=None, hi=None, integer=False, lo_open=False):
    def parse(s):
        try:
            v = float(s)
        except (TypeError, ValueError):
            raise ValueError("not a number")
        if not math.isfinite(v):
            raise ValueError("not a finite number")
        if integer:
            if not v.is_integer():
                raise ValueError("must be a whole number")
            v = int(v)
        if lo is not None and (v < lo or (lo_open and v == lo)):
            raise ValueError(f"must be {'>' if lo_open else '>='} {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"must be <= {hi}")
        return v
    return parse


def boolean(s):
    s = str(s).strip().lower()
    if s in ("true", "1", "on", "yes"):
        return True
    if s in ("false", "0", "off", "no"):
        return False
    raise ValueError("use on/off")


def choice(*options):
    opts = tuple(o.lower() for o in options)
    def parse(s):
        s = str(s).strip().lower()
        if s not in opts:
            raise ValueError(f"must be one of: {', '.join(opts)}")
        return s
    return parse