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


def choice_list(*options):
    """Comma/space separated list of allowed values, e.g. 'high, medium'."""
    opts = tuple(o.lower() for o in options)
    def parse(s):
        vals = [x.strip().lower() for x in str(s).replace(",", " ").split() if x.strip()]
        bad = [v for v in vals if v not in opts]
        if not vals or bad:
            raise ValueError(f"must be from: {', '.join(opts)}")
        return vals
    return parse