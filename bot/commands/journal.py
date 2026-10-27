# bot/commands/journal.py

import re
import math
import logging
import asyncio
from itertools import accumulate
from collections import defaultdict
from datetime import datetime, timezone, timedelta, time as dtime
import MetaTrader5 as mt5
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import ALERT_STATE
from database import get_dry_run_journal_signals, get_live_signal_links
from mt5_engine import check_mt5_alive, MT5_LOCK, get_broker_utc_offset_seconds
from strategy.chart import generate_journal_chart
from ._common import admin_only, md_safe
from .core import _HISTORY_START, _deal_stats, _money, _pf_text

PAGE_SIZE = 6
MAX_PAGE = 9999          # keeps callback_data well under Telegram's 64-byte limit
_EPS = 0.005
_MSG_BUDGET = 3900

_REASON = {}
for _name, _label in (("DEAL_REASON_SL", "SL"), ("DEAL_REASON_TP", "TP"), ("DEAL_REASON_SO", "Stop-out")):
    if hasattr(mt5, _name):
        _REASON[getattr(mt5, _name)] = _label


# ================================================================ filter state
# The whole view state lives in the button callback_data (stateless, survives restarts):
#   "<j|jc>:<period>:<side>:<result>:<origin>:<sort>:<page>"
#   period : t today | y yesterday | w week | m month | a all | d<N> last N days
#   side   : a both | B buy | S sell
#   result : a any | W win | L loss | E breakeven | O open
#   origin : a any | b bot (live+dry) | m manual | l live (bot+manual) | d dry-run | x bot live
#   sort   : r newest | o oldest | b best | w worst

_FIELDS = ("period", "side", "result", "origin", "sort", "page")
_DEFAULT = {"period": "a", "side": "a", "result": "a", "origin": "a", "sort": "r", "page": 1}
_VALID = {
    "side": {"a", "B", "S"},
    "result": {"a", "W", "L", "E", "O"},
    "origin": {"a", "b", "m", "l", "d", "x"},
    "sort": {"r", "o", "b", "w"},
}
_RESULT_NAME = {"W": "WIN", "L": "LOSS", "E": "BE", "O": "OPEN"}
_SORT_NEXT = {"r": "o", "o": "b", "b": "w", "w": "r"}
_SORT_LABEL = {"r": "🕒 Newest", "o": "🕘 Oldest", "b": "🏆 Best", "w": "💀 Worst"}

_ORIGIN_TEST = {
    "a": lambda t: True,
    "b": lambda t: t["who"] == "bot",
    "m": lambda t: t["who"] == "manual",
    "l": lambda t: t["source"] == "live",
    "d": lambda t: t["source"] == "dry",
    "x": lambda t: t["who"] == "bot" and t["source"] == "live",
}


def _valid_period(p):
    return p in ("t", "y", "w", "m", "a") or re.fullmatch(r"d\d{1,4}", p) is not None


def _encode(prefix, st, **chg):
    s = {**st, **chg}
    return ":".join([prefix] + [str(s[k]) for k in _FIELDS])


def _decode(data):
    parts = data.split(":")
    try:
        st = dict(zip(_FIELDS, parts[1:7]))
        st["page"] = min(MAX_PAGE, max(1, int(st["page"])))
        if not _valid_period(st["period"]):
            raise ValueError
        for k, allowed in _VALID.items():
            if st[k] not in allowed:
                raise ValueError
        return st
    except (KeyError, ValueError):
        return dict(_DEFAULT)


def _local_midnight(day):
    """Local-timezone midnight for a date (DST-correct)."""
    return datetime.combine(day, dtime.min).astimezone()


def _period_bounds(code):
    now = datetime.now().astimezone()
    today = now.date()
    mid = _local_midnight(today)
    if code == "t":
        return mid, None, "Today"
    if code == "y":
        return _local_midnight(today - timedelta(days=1)), mid, "Yesterday"
    if code == "w":
        return now - timedelta(days=7), None, "Last 7 Days"
    if code == "m":
        return now - timedelta(days=30), None, "Last 30 Days"
    if code == "a":
        return None, None, "All Time"
    days = max(1, min(int(code[1:]), 3650))
    return now - timedelta(days=days), None, f"Last {days} Day{'s' if days != 1 else ''}"


# ================================================================ typed-argument shortcuts

_SIDE = {"buy": "B", "long": "B", "sell": "S", "short": "S"}
_RESULT = {"win": "W", "wins": "W", "won": "W", "loss": "L", "losses": "L", "lost": "L", "lose": "L",
           "be": "E", "breakeven": "E", "open": "O", "running": "O"}
_WHO = {"bot": "bot", "manual": "manual", "man": "manual"}
_SOURCE = {"live": "live", "dry": "dry", "dryrun": "dry", "sim": "dry", "paper": "dry"}
_SORT = {"recent": "r", "newest": "r", "oldest": "o", "best": "b", "worst": "w"}

_USAGE = (
    "📓 **TRADE JOURNAL**\n\n"
    "Just send `/journal` and use the buttons — everything is one tap.\n\n"
    "**Typed shortcuts (optional):**\n"
    "• **Period:** `today` · `yesterday` · `week` · `month` · `all` · `14d`\n"
    "• **Side:** `buy` · `sell`\n"
    "• **Result:** `win` · `loss` · `be` · `open`\n"
    "• **Who/where:** `bot` · `manual` · `live` · `dry`\n"
    "• **Sort:** `newest` · `oldest` · `best` · `worst`  •  **Page:** `p2`\n\n"
    "**Examples:** `/journal today bot win` · `/journal month sell loss` · `/journal all manual best`\n\n"
    "🤖 bot · 🖐️ manual · 🧪 dry-run (simulated)"
)


def _origin_code(who, src):
    if len(who) > 1:
        who = set()
    if len(src) > 1:
        src = set()
    w = next(iter(who), None)
    s = next(iter(src), None)
    if w is None and s is None:
        return "a"
    if w == "manual":
        return "m"                      # manual trades are always live
    if w == "bot":
        return {"live": "x", "dry": "d", None: "b"}[s]
    return "l" if s == "live" else "d"


def _parse_args(args):
    """Returns (state, bad_token)."""
    st = dict(_DEFAULT)
    who, src = set(), set()
    for raw in args:
        t = raw.lower().strip()
        m_page = re.fullmatch(r"p(?:age)?(\d+)", t)
        if m_page:
            st["page"] = min(MAX_PAGE, max(1, int(m_page.group(1))))
        elif t in ("today", "day", "1d"):
            st["period"] = "t"
        elif t == "yesterday":
            st["period"] = "y"
        elif t in ("week", "7d"):
            st["period"] = "w"
        elif t in ("month", "30d"):
            st["period"] = "m"
        elif t in ("all", "alltime", "all-time", "max"):
            st["period"] = "a"
        elif re.fullmatch(r"\d{1,4}d?", t):
            st["period"] = f"d{max(1, int(t.rstrip('d')))}"
        elif t in _SIDE:
            st["side"] = _SIDE[t]
        elif t in _RESULT:
            st["result"] = _RESULT[t]
        elif t in _WHO:
            who.add(_WHO[t])
        elif t in _SOURCE:
            src.add(_SOURCE[t])
        elif t in _SORT:
            st["sort"] = _SORT[t]
        else:
            return None, raw
    st["origin"] = _origin_code(who, src)
    return st, None


def _parse_utc(v):
    """DB timestamps are stored as UTC; returns them as tz-aware LOCAL time."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return dt.astimezone()


def _result(net):
    if net > _EPS:
        return "WIN"
    if net < -_EPS:
        return "LOSS"
    return "BE"


# ================================================================ data: live MT5

def _account_currency():
    """BLOCKING. Account currency for dry-only views; falls back to USD if MT5 is unavailable."""
    try:
        with MT5_LOCK:
            info = mt5.account_info()
        if info is not None and info.currency:
            return info.currency
    except Exception:
        pass
    return "USD"


def _fetch_live_trades(since_utc):
    """BLOCKING. Closed MT5 positions (bot + manual) from deal history. Returns (currency, trades, error)."""
    if not check_mt5_alive():
        return "USD", [], "MT5 terminal is not connected or unreachable."

    offset_s = int(get_broker_utc_offset_seconds())
    shift = timedelta(seconds=offset_s)
    now = datetime.now(timezone.utc)
    # Look back an extra 30 days so a position's opening deal is still in the window.
    start = _HISTORY_START if since_utc is None else since_utc - timedelta(days=30)
    magic = int(ALERT_STATE.get("magic_number", 770077))

    with MT5_LOCK:
        info = mt5.account_info()
        deals = mt5.history_deals_get(start + shift, now + shift + timedelta(days=1))
        deals_err = mt5.last_error() if deals is None else None
        positions = mt5.positions_get()
    if info is None:
        return "USD", [], "Failed to retrieve MT5 account details."
    curr = info.currency or "USD"

    # history_deals_get() returns None on failure — don't mistake that for "no trades".
    if deals is None and deals_err and deals_err[0] != getattr(mt5, "RES_S_OK", 1):
        return curr, [], f"Deal history query failed ({deals_err})."

    links = get_live_signal_links()
    open_ids = {int(p.ticket) for p in (positions or [])}   # still running -> /positions
    to_utc = lambda ts: datetime.fromtimestamp(ts - offset_s, tz=timezone.utc).astimezone()

    by_pos = defaultdict(list)
    for d in (deals or []):
        if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL) and d.position_id:
            by_pos[int(d.position_id)].append(d)

    out_entries = (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY)
    trades = []
    for pid, ds in by_pos.items():
        if pid in open_ids:
            continue
        ins = sorted((d for d in ds if d.entry == mt5.DEAL_ENTRY_IN), key=lambda d: d.time)
        outs = sorted((d for d in ds if d.entry in out_entries), key=lambda d: d.time)
        if not outs:
            continue
        first, last = (ins[0] if ins else outs[0]), outs[-1]
        if ins:
            direction = "BUY" if ins[0].type == mt5.DEAL_TYPE_BUY else "SELL"
        else:   # opening deal fell outside the window: infer from the closing deal
            direction = "SELL" if outs[0].type == mt5.DEAL_TYPE_BUY else "BUY"

        # Round to cents so the BE/WIN/LOSS label and _deal_stats (which counts >0 / <0)
        # can never disagree about a trade like +0.003.
        net = round(sum(d.profit + d.swap + d.commission + getattr(d, "fee", 0.0) for d in ds), 2)
        link = links.get(pid)
        ref = f"#{pid}" + (f" S{link['id']}·L{link['leg']}" if link else "")
        trades.append({
            "source": "live",
            "who": "bot" if first.magic == magic else "manual",
            "symbol": last.symbol,
            "direction": direction,
            "volume": round(sum(d.volume for d in outs), 2),
            "entry": float(ins[0].price) if ins else None,
            "exit": float(last.price),
            "opened": to_utc(ins[0].time) if ins else None,
            "time": to_utc(last.time),
            "net_pnl": float(net),
            "result": _result(net),
            "kind": _REASON.get(getattr(last, "reason", None), "Closed"),
            "ref": ref,
        })
    return curr, trades, None


# ================================================================ data: dry-run

def _contract_size(symbol, cache):
    if symbol not in cache:
        size = 100.0
        try:
            with MT5_LOCK:
                info = mt5.symbol_info(symbol)
            if info and info.trade_contract_size:
                size = float(info.trade_contract_size)
        except Exception:
            pass
        cache[symbol] = size
    return cache[symbol]


def _fetch_dry_trades():
    """BLOCKING. One row per simulated leg, derived from the signal's tracked status."""
    fixed_lot = float(ALERT_STATE.get("fixed_lot_size", 0.01))
    sizes, trades = {}, []

    for s in get_dry_run_journal_signals():
        symbol = s["symbol"] or "XAUUSD"
        direction, status = s["direction"], s["status"]
        dual = bool(s["dual_entry"])
        opened = _parse_utc(s["timestamp"])
        closed = _parse_utc(s["updated_at"]) or opened
        sl, tp1, tp2 = s["sl_price"], s["tp1_price"], s["tp2_price"]
        entry1 = float(s["fill_price"] or s["entry_price"])
        entry2 = float(s["fill_price2"] or s["fill_price"] or s["entry_price"])

        # (leg_no, exit_price | None, kind, entry, lots, close_time)
        legs = []
        if s["exec_mode"] == "DRY":
            lots1 = float(s["lots"] or fixed_lot)
            if status == "PENDING":
                legs.append((1, None, "OPEN", entry1, lots1, opened))
            elif status == "HIT_SL":
                legs.append((1, sl, "SL", entry1, lots1, closed))
            elif status == "CLOSED_TP1":
                legs.append((1, tp1, "TP1", entry1, lots1, closed))
            elif status == "HIT_TP1":
                # Runner still open: updated_at was last written the moment leg 1 hit TP1.
                legs.append((1, tp1, "TP1", entry1, lots1, closed))
            elif status in ("HIT_TP2", "CLOSED_BE"):
                # Leg 1 closed at TP1 earlier; its exact close time isn't stored
                # (updated_at now tracks the runner), so fall back to the entry time.
                legs.append((1, tp1, "TP1", entry1, lots1, opened))
        if dual and s["exec_mode2"] == "DRY":
            lots2 = float(s["lots2"] or fixed_lot)
            if status in ("PENDING", "HIT_TP1"):
                legs.append((2, None, "RUNNER" if status == "HIT_TP1" else "OPEN", entry2, lots2, opened))
            elif status == "HIT_SL":
                legs.append((2, sl, "SL", entry2, lots2, closed))
            elif status == "HIT_TP2":
                legs.append((2, tp2, "TP2", entry2, lots2, closed))
            elif status == "CLOSED_BE":
                legs.append((2, entry2, "BE", entry2, lots2, closed))

        for leg_no, exit_p, kind, entry, lots, when in legs:
            if exit_p is None:
                pnl, res = 0.0, "OPEN"
            else:
                diff = (exit_p - entry) if direction == "BUY" else (entry - exit_p)
                pnl = round(diff * lots * _contract_size(symbol, sizes), 2)
                res = _result(pnl)
            trades.append({
                "source": "dry", "who": "bot",
                "symbol": symbol, "direction": direction, "volume": round(lots, 2),
                "entry": entry, "exit": exit_p,
                "opened": opened, "time": when,
                "net_pnl": pnl, "result": res, "kind": kind,
                "ref": f"S{s['id']}·L{leg_no}",
            })
    return trades


# ================================================================ load + filter

def _build_view(st):
    """BLOCKING. Gathers every trade the state asks for and applies the filters."""
    since, until, _ = _period_bounds(st["period"])
    org = st["origin"]
    want_live = org in ("a", "b", "m", "l", "x")
    want_dry = org in ("a", "b", "d")

    curr, trades, err = "USD", [], None
    if want_live:
        curr, live, err = _fetch_live_trades(since)
        trades += live
    else:
        curr = _account_currency()      # dry-only view: still show the real account currency
    if want_dry:
        trades += _fetch_dry_trades()

    test = _ORIGIN_TEST[org]
    want_res = _RESULT_NAME.get(st["result"])
    want_side = {"B": "BUY", "S": "SELL"}.get(st["side"])

    rows = []
    for t in trades:
        ts = t["time"] or t["opened"]
        if ts is None:
            continue
        if since and ts < since:
            continue
        if until and ts >= until:
            continue
        if want_side and t["direction"] != want_side:
            continue
        if want_res and t["result"] != want_res:
            continue
        if not test(t):
            continue
        rows.append(t)
    return {"curr": curr, "rows": rows, "err": err}


# ================================================================ text visuals

_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values, width=24):
    if len(values) < 2:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[min(int((i + 1) * step) - 1, len(values) - 1)] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return _SPARK[3] * len(values)
    return "".join(_SPARK[int((v - lo) / (hi - lo) * (len(_SPARK) - 1))] for v in values)


def _outcome_bar(w, l, be, width=10):
    n = w + l + be
    if not n:
        return ""
    g = min(round(w / n * width), width)
    r = min(round(l / n * width), width - g)
    return "🟩" * g + "🟥" * r + "⬜" * (width - g - r)


def _day_strip(closed, max_days=14):
    daily = defaultdict(float)
    for t in closed:
        daily[t["time"].date()] += t["net_pnl"]
    days = sorted(daily)[-max_days:]
    if len(days) < 2:
        return ""
    return "".join("🟩" if daily[d] > _EPS else "🟥" if daily[d] < -_EPS else "⬜" for d in days)


def _origin(t):
    if t["source"] == "dry":
        return "🧪"
    return "🤖" if t["who"] == "bot" else "🖐️"


def _origin_label(t):
    if t["source"] == "dry":
        return "🧪 Dry-run"
    return "🤖 Bot (live)" if t["who"] == "bot" else "🖐️ Manual (live)"


def _filter_tag(st):
    names = {
        "side": {"B": "BUY", "S": "SELL"},
        "result": _RESULT_NAME,
        "origin": {"b": "BOT", "m": "MANUAL", "l": "LIVE", "d": "DRY-RUN", "x": "BOT LIVE"},
    }
    parts = [names[k][st[k]] for k in ("side", "result", "origin") if st[k] in names[k]]
    return f"  `[{' · '.join(parts)}]`" if parts else ""


def _fmt_row(t, curr, show_date):
    icon = {"WIN": "✅", "LOSS": "❌", "BE": "🔒", "OPEN": "⏳"}[t["result"]]
    side = "BUY 🟢" if t["direction"] == "BUY" else "SELL 🔴"
    is_open = t["result"] == "OPEN"
    entry = f"${t['entry']:.2f}" if t["entry"] is not None else "?"
    exit_ = "running" if is_open else f"${t['exit']:.2f}"
    pnl = "—" if is_open else _money(t["net_pnl"], curr)
    ts = t["time"] or t["opened"]
    fmt = "%d %b %H:%M" if show_date else "%H:%M"
    when = (f"opened {ts.strftime(fmt)}" if is_open else ts.strftime(fmt)) if ts else "?"
    return (
        f"{icon} {_origin(t)} {side} `{t['volume']}` · `{entry}` ➔ `{exit_}` · {md_safe(t['kind'])}\n"
        f"      `{pnl}` · {when} · `{md_safe(t['ref'])}`"
    )


# ================================================================ render

def _btn(label, st, active, **chg):
    return InlineKeyboardButton(f"● {label}" if active else label,
                                callback_data=_encode("j", st, page=1, **chg))


def _keyboard(st, page, pages):
    def row(field, options):
        return [_btn(label, st, st[field] == code, **{field: code}) for label, code in options]

    kb = [
        row("period", [("Today", "t"), ("Yest", "y"), ("Week", "w"), ("Month", "m"), ("All", "a")]),
        row("side", [("Buy+Sell", "a"), ("🟢 Buy", "B"), ("🔴 Sell", "S")]),
        row("result", [("Any result", "a"), ("✅ Win", "W"), ("❌ Loss", "L"), ("🔒 BE", "E"), ("⏳ Open", "O")]),
        row("origin", [("Any", "a"), ("🤖 Bot", "b"), ("🖐️ Man", "m"), ("💰 Live", "l"), ("🧪 Dry", "d")]),
        [
            InlineKeyboardButton(f"{_SORT_LABEL[st['sort']]} ↻",
                                 callback_data=_encode("j", st, sort=_SORT_NEXT[st["sort"]], page=1)),
            InlineKeyboardButton("📈 Chart", callback_data=_encode("jc", st)),
            InlineKeyboardButton("🔄", callback_data=_encode("j", st)),
        ],
    ]
    if pages > 1:
        kb.append([
            InlineKeyboardButton("◀ Prev" if page > 1 else "·",
                                 callback_data=_encode("j", st, page=page - 1) if page > 1 else "jn"),
            InlineKeyboardButton(f"{page}/{pages}", callback_data="jn"),
            InlineKeyboardButton("Next ▶" if page < pages else "·",
                                 callback_data=_encode("j", st, page=page + 1) if page < pages else "jn"),
        ])
    return InlineKeyboardMarkup(kb)


def _render(st, view):
    curr, rows, err = view["curr"], view["rows"], view["err"]
    _, _, label = _period_bounds(st["period"])

    closed = sorted((t for t in rows if t["result"] != "OPEN"), key=lambda t: t["time"] or t["opened"])
    open_n = len(rows) - len(closed)
    s = _deal_stats(closed)
    be_n = s["n"] - s["wins"] - s["losses"]

    lines = [f"📓 **TRADE JOURNAL** — {label}{_filter_tag(st)}"]
    if err:
        lines.append(f"⚠️ _MT5 offline ({md_safe(err)}) — live/manual trades unavailable._")
    lines.append("")

    if not rows:
        lines.append("📭 _No trades match — tap a button to widen the filters._")
    elif not s["n"]:
        lines.append(f"⏳ `{open_n}` open trade(s), none closed yet.")
    else:
        lines.append(f"{_outcome_bar(s['wins'], s['losses'], be_n)}  **{s['win_rate']}%** win rate")
        lines.append(f"`{s['wins']}W · {s['losses']}L · {be_n}BE` | Open: `{open_n}`")
        spark = _sparkline(list(accumulate(t["net_pnl"] for t in closed)))
        lines.append((f"📈 `{spark}`  " if spark else "💰 ") + f"**{_money(s['pnl'], curr)}**")
        strip = _day_strip(closed)
        if strip:
            lines.append(f"🗓 {strip}")
        lines += [
            "",
            f"• **PF:** `{_pf_text(s)}` | **Expectancy:** `{_money(s['expectancy'], curr)}`",
            f"• **Avg Win:** `{_money(s['avg_win'], curr)}` | **Avg Loss:** `{_money(s['avg_loss'], curr)}`",
            f"• **Best:** `{_money(s['best'], curr)}` | **Worst:** `{_money(s['worst'], curr)}`",
            f"• **Max DD:** `{s['max_dd']:,.2f} {curr}` | **Streaks:** `{s['max_w']}W / {s['max_l']}L`",
        ]
        by_origin = defaultdict(list)
        for t in closed:
            by_origin[_origin_label(t)].append(t["net_pnl"])
        if len(by_origin) > 1:
            lines.append("")
            for lab, pnls in sorted(by_origin.items()):
                w = sum(1 for p in pnls if p > _EPS)
                lines.append(f"{lab}: `{len(pnls)}` trades · `{w}W` · `{_money(sum(pnls), curr)}`")
            if "🧪 Dry-run" in by_origin:
                lines.append("_Net P&L mixes simulated and real results — use 💰 Live / 🧪 Dry to separate._")

    # ---- ordering + paging
    key_fn, rev = {
        "r": (lambda t: t["time"] or t["opened"], True),
        "o": (lambda t: t["time"] or t["opened"], False),
        "b": (lambda t: t["net_pnl"], True),
        "w": (lambda t: t["net_pnl"], False),
    }[st["sort"]]
    ordered = sorted(rows, key=key_fn, reverse=rev)
    pages = max(1, math.ceil(len(ordered) / PAGE_SIZE))
    page = min(st["page"], pages)
    st = {**st, "page": page}   # clamp: Refresh/Chart buttons must never carry an out-of-range page
    page_rows = ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    # ---- rows (grouped by day, with daily subtotals, when sorted by time)
    day_tot = defaultdict(lambda: [0.0, 0])
    for t in closed:
        d = t["time"].date()
        day_tot[d][0] += t["net_pnl"]
        day_tot[d][1] += 1

    grouped = st["sort"] in ("r", "o")
    body, last_day = [], None
    for t in page_rows:
        ts = t["time"] or t["opened"]
        if grouped and ts.date() != last_day:
            last_day = ts.date()
            head = f"📅 **{ts:%a %d %b}**"
            if last_day in day_tot:
                tot = day_tot[last_day]
                head += f" — `{_money(tot[0], curr)}` ({tot[1]})"
            body.append(head)
        body.append(_fmt_row(t, curr, show_date=not grouped))

    footer = []
    if rows:
        tz_name = datetime.now().astimezone().strftime("%Z") or "local"
        footer.append(f"📄 `{page}/{pages}` · `{len(rows)}` trade(s) · times {tz_name}")
        if any(t["source"] == "dry" for t in page_rows):
            footer.append("🧪 _Dry-run P&L is simulated (no spread/slippage)._")

    head_txt = "\n".join(lines)
    while body and len(head_txt) + sum(len(b) + 1 for b in body) + sum(len(f) + 1 for f in footer) + 4 > _MSG_BUDGET:
        body.pop()   # safety net for Telegram's 4096-char limit
        while body and body[-1].startswith("📅"):
            body.pop()   # never leave a day header with no rows under it
    text = head_txt + ("\n\n" + "\n".join(body) if body else "") + ("\n\n" + "\n".join(footer) if footer else "")
    return text, _keyboard(st, page, pages)


# ================================================================ handlers

def _strip_md(text):
    return text.replace("*", "").replace("`", "").replace("_", " ")


async def _safe_edit(query, text, kb):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        logging.warning(f"Journal markdown rejected ({e}); resending as plain text")
        try:
            await query.edit_message_text(_strip_md(text), reply_markup=kb)
        except BadRequest as e2:
            if "not modified" not in str(e2).lower():
                raise


@admin_only
async def journal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/journal [filters] — opens the interactive dashboard."""
    args = context.args or []
    if args and args[0].lower() in ("help", "?"):
        await update.effective_message.reply_text(_USAGE, parse_mode="Markdown")
        return
    st, bad = _parse_args(args)
    if st is None:
        await update.effective_message.reply_text(
            f"❌ Unknown filter `{md_safe(bad)}`.\n\n{_USAGE}", parse_mode="Markdown")
        return

    view = await asyncio.to_thread(_build_view, st)
    text, kb = _render(st, view)
    try:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    except BadRequest:
        await update.effective_message.reply_text(_strip_md(text), reply_markup=kb)


async def journal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles every journal button. Routed from callbacks.py BEFORE its own query.answer(),
    because this function answers the query itself."""
    query = update.callback_query
    data = query.data or ""

    if data == "jn":                      # inert button (page indicator / disabled edge)
        await query.answer()
        return

    st = _decode(data)

    if data.startswith("jc:"):            # ---- chart
        await query.answer("📈 Rendering chart…")
        view = await asyncio.to_thread(_build_view, st)
        closed = sorted((t for t in view["rows"] if t["result"] != "OPEN"),
                        key=lambda t: t["time"] or t["opened"])
        if len(closed) < 2:
            await query.message.reply_text("📭 Need at least 2 closed trades in this view to draw a chart.")
            return
        _, _, label = _period_bounds(st["period"])
        s = _deal_stats(closed)
        curr = view["curr"]
        subtitle = (f"{s['n']} trades · {s['win_rate']}% win rate · PF {_pf_text(s)} · "
                    f"net {s['pnl']:+,.2f} {curr}")
        buf = await asyncio.to_thread(
            generate_journal_chart, closed, curr, f"Trade Journal — {label}", subtitle)
        if buf is None:
            await query.message.reply_text("❌ Could not render the chart.")
            return
        caption = (f"📈 **Journal Chart — {label}**{_filter_tag(st)}\n"
                   f"`{s['n']}` trades · **{s['win_rate']}%** WR · PF `{_pf_text(s)}` · "
                   f"Net `{_money(s['pnl'], curr)}`")
        try:
            await query.message.reply_photo(photo=buf, caption=caption, parse_mode="Markdown")
        except BadRequest:
            buf.seek(0)
            await query.message.reply_photo(photo=buf, caption=_strip_md(caption))
        return

    await query.answer()                  # ---- filter / sort / page change
    view = await asyncio.to_thread(_build_view, st)
    text, kb = _render(st, view)
    await _safe_edit(query, text, kb)