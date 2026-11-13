# bot/commands/optimize.py

import time
import json
import logging
import asyncio
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, save_settings
from strategies import STRATEGIES, STRATEGY_PRESETS, get_params, plan_sweep, reset_runtime
from core.database import (
    get_sweep_run, get_latest_sweep_run, get_sweep_results,
    count_sweep_runs, get_sweep_history, get_sweep_history_ranked,
    log_sweep_run,
)
from core.mt5_engine import get_gold_symbol
from core.backtester import run_backtest_sweep, MIN_SWEEP_SAMPLE
from ._common import admin_only

# Shared by /optimize view and /optimize history. "column" feeds the DB query
# (history) or the dict key (view, sorted in Python); "reverse"/"sql_dir" both
# encode the same direction in each engine's own vocabulary. dd sorts ascending
# (lower drawdown is better) — everything else sorts descending.
SORT_FIELDS = {
    "netr":     {"column": "net_r",          "sql_dir": "DESC", "reverse": True,  "label": "Net R"},
    "trades":   {"column": "total_trades",   "sql_dir": "DESC", "reverse": True,  "label": "# Trades"},
    "avgr":     {"column": "avg_r",          "sql_dir": "DESC", "reverse": True,  "label": "Avg R"},
    "dd":       {"column": "max_drawdown_r", "sql_dir": "ASC",  "reverse": False, "label": "Max Drawdown"},
    "winrate":  {"column": "win_rate",       "sql_dir": "DESC", "reverse": True,  "label": "Win Rate"},
    "strategy": {"column": "strategy",       "sql_dir": "ASC",  "reverse": False, "label": "Strategy"},
}

RANK_FLAGS = ("calmar", "avgr")

_USAGE = (
    "**Usage:** `/optimize [days] [scalp|intraday|swing] [axes...] [strategy=<key>] [strategies] [calmar|avgr]`\n"
    "• Axes are the active strategy's own sweepable params (🔁 in `/set strategies`), up to 3.\n"
    "• `strategies` sweeps every installed strategy at once.\n\n"
    "**Other subcommands:**\n"
    "• `/optimize history [n]` — past sweeps for this chat\n"
    "• `/optimize apply <rank>` · `/optimize apply <run_id> <rank>` — promote a result"
)

def _run_axes(run):
    """Axis names of a saved run (legacy rows fall back to their old swept_* flags)."""
    raw = run.get("axes_json")
    if raw:
        try:
            return list(json.loads(raw))
        except ValueError:
            pass
    legacy = (("swept_strategies", "strategies"), ("swept_rrr", "min_rrr"), ("swept_score", "min_score"),
              ("swept_sl", "sl_mult"), ("swept_rsi", "rsi_buy"), ("swept_rsi", "rsi_sell"))
    return [name for flag, name in legacy if run.get(flag)] or ["min_rrr", "min_score"]

def _resolve_sort_key(token):
    """Returns the canonical SORT_FIELDS key for a token, or None if it's not a sort keyword."""
    return token if token in SORT_FIELDS else None


def _format_sweep_rows(rows, min_sample, rank_mode, axes):
    """Shared renderer for a sweep's top rows — used by the just-finished run and by /optimize view."""
    def sample_size(g):
        return (g.get("wins") or 0) + (g.get("losses") or 0)

    def describe(g):
        parts = []
        if "strategies" in axes and g.get("strategy"):
            parts.append(f"`{g['strategy'].replace('_', ' ').title()}`")
        params = g.get("params") or {}
        for k in axes:
            if k != "strategies" and k in params:
                parts.append(f"`{k}={params[k]}`")
        return " ".join(parts) if parts else "(single combination)"

    lines = []
    for i, g in enumerate(rows, 1):
        thin_flag = " ⚠️*low sample*" if sample_size(g) < min_sample else ""
        calmar_str = f" | Calmar `{round(g['calmar'], 2)}`" if rank_mode == "calmar" and g.get("calmar") is not None else ""
        lines.append(
            f"`{i}.` {describe(g)}{thin_flag}\n"
            f"    → Net `{g['net_r']}R` | WR `{g['win_rate']}%` "
            f"({g['wins']}W/{g['losses']}L) | Avg `{g['avg_r']}R` | DD `{g['max_drawdown_r']}R`{calmar_str}"
        )
    return lines


# ---------------------------------------------------------------- /optimize view

async def _optimize_view(update: Update, args, chat_id):
    if len(args) < 2 or not args[1].isdigit():
        await update.effective_message.reply_text(
            "⚠️ **Usage:** `/optimize view <run_id> [sort]`\n\n"
            f"**Sort keys:** `{'`, `'.join(SORT_FIELDS)}` (default: however the sweep itself ranked)\n"
            "See `/optimize history` for run ids.",
            parse_mode="Markdown"
        )
        return

    run_id = int(args[1])
    sort_key = None
    if len(args) >= 3:
        sort_key = _resolve_sort_key(args[2].lower())
        if sort_key is None:
            await update.effective_message.reply_text(
                f"❌ Unknown sort key `{args[2]}`. Valid: `{'`, `'.join(SORT_FIELDS)}`.",
                parse_mode="Markdown"
            )
            return

    run = get_sweep_run(run_id)
    if not run or run["chat_id"] != chat_id:
        await update.effective_message.reply_text(
            f"❌ No sweep run `#{run_id}` found for this chat.", parse_mode="Markdown"
        )
        return

    rows = get_sweep_results(run_id)
    if not rows:
        await update.effective_message.reply_text(
            f"❌ Sweep `#{run_id}` has no saved results.", parse_mode="Markdown"
        )
        return

    if sort_key:
        spec = SORT_FIELDS[sort_key]
        col = spec["column"]
        present = [r for r in rows if r.get(col) is not None]
        missing = [r for r in rows if r.get(col) is None]
        rows = sorted(present, key=lambda r: r[col], reverse=spec["reverse"]) + missing
        rank_label = spec["label"]
    else:
        rank_label = "Net R ÷ Max Drawdown (risk-adjusted)" if run["rank_mode"] == "calmar" \
            else "Avg R per Trade" if run["rank_mode"] == "avgr" else "Net R"

    axes = _run_axes(run)

    lines = [
        f"🧪 **SWEEP `#{run_id}`** — `{run['timeframe_mode'].upper()}` "
        f"({run['days']}d | ⚡ `{run['elapsed_seconds']}s` | {run['created_at']}) "
        f"— `{len(rows)}` saved result(s)\n",
        f"🏆 **Results (by {rank_label}):**",
    ]
    lines.extend(_format_sweep_rows(rows, run["min_sample"], run["rank_mode"], axes))
    lines.append(f"\n💡 `/optimize apply {run_id} <rank>` to promote one of these.")
    if not sort_key:
        lines.append(f"*(add a sort key to reorder: `{'`, `'.join(SORT_FIELDS)}` — e.g.* `/optimize view {run_id} avgr`*)*")

    budget = 3900
    final_lines, current_len = [], 0
    for line in lines:
        if current_len + len(line) + 1 > budget:
            final_lines.append(f"\n... *(truncated — {len(rows)} results saved; ranks beyond this are still usable with* `/optimize apply {run_id} <rank>` *)*")
            break
        final_lines.append(line)
        current_len += len(line) + 1

    await update.effective_message.reply_text("\n".join(final_lines), parse_mode="Markdown")


# ---------------------------------------------------------------- /optimize history

async def _optimize_history(update: Update, args, chat_id):
    PAGE_SIZE = 10
    sub_args = [a.lower() for a in args[1:]]

    sort_key = None
    filtered = []
    for a in sub_args:
        resolved = _resolve_sort_key(a) if sort_key is None else None
        if resolved:
            sort_key = resolved
        else:
            filtered.append(a)

    show_all = bool(filtered) and filtered[0] == "all"
    page = 1
    if not show_all and filtered and filtered[0].isdigit():
        page = max(1, int(filtered[0]))

    total = count_sweep_runs(chat_id)
    if total == 0:
        await update.effective_message.reply_text(
            "📭 No sweep history yet. Run `/optimize` to create one.", parse_mode="Markdown"
        )
        return

    limit = total if show_all else PAGE_SIZE
    offset = 0 if show_all else (page - 1) * PAGE_SIZE

    if sort_key:
        spec = SORT_FIELDS[sort_key]
        runs = get_sweep_history_ranked(chat_id, sort_column=spec["column"], sort_dir=spec["sql_dir"],
                                         limit=limit, offset=offset)
    else:
        runs = get_sweep_history(chat_id, limit=limit, offset=offset)

    if not runs:
        last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        await update.effective_message.reply_text(
            f"📭 No sweeps on page `{page}` — there are `{total}` total "
            f"(`{last_page}` page(s) of `{PAGE_SIZE}`). Try `/optimize history 1`.",
            parse_mode="Markdown"
        )
        return

    sort_label = f" — sorted by {SORT_FIELDS[sort_key]['label']}" if sort_key else ""
    header = f"🗂️ **SWEEP HISTORY**{sort_label} — `{total}` total"
    if not show_all:
        last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        header += f" (page `{page}`/`{last_page}`)"
    lines = [header + "\n"]

    for run in runs:
        axes_str = "+".join(_run_axes(run))

        if sort_key:
            top_strategy = run.get("top_strategy") or "?"
            strat_label = top_strategy.replace("_", " ").title()
            top_str = (
                f"`{strat_label}` — Net `{run['top_net_r']}R` | Trades `{run['top_trades']}` | "
                f"Avg `{run['top_avg_r']}R` | WR `{run['top_win_rate']}%` | DD `{run['top_dd']}R`"
            )
            thin_flag = " ⚠️*low sample*" if (run["top_wins"] + run["top_losses"]) < run["min_sample"] else ""
        else:
            top = get_sweep_results(run["id"], limit=1)
            if top:
                strat_label = (top[0].get("strategy") or "?").replace("_", " ").title()
                top_str = f"`{strat_label}` — Net `{top[0]['net_r']}R`"
            else:
                top_str = "n/a"
            thin_flag = ""

        lines.append(
            f"`#{run['id']}` {run['created_at']} — `{run['timeframe_mode']}` {run['days']}d "
            f"`{axes_str}` rank=`{run['rank_mode']}`{thin_flag} — top: {top_str}\n"
            f"    → `/optimize view {run['id']}` · `/optimize apply {run['id']} 1`"
        )

    if not show_all:
        nav_prefix = f"history {sort_key}" if sort_key else "history"
        nav = []
        if page > 1:
            nav.append(f"`/optimize {nav_prefix} {page - 1}` ◀️ newer")
        if offset + len(runs) < total:
            nav.append(f"▶️ older `/optimize {nav_prefix} {page + 1}`")
        if nav:
            lines.append("\n" + " · ".join(nav))
        lines.append(f"*(or* `/optimize {nav_prefix} all` *to list every sweep in one message)*")
        if not sort_key:
            lines.append(f"*(add a sort key to reorder: `{'`, `'.join(SORT_FIELDS)}` — e.g.* `/optimize history netr`*)*")

    budget = 3900
    final_lines, current_len = [], 0
    for line in lines:
        if current_len + len(line) + 1 > budget:
            final_lines.append(f"\n... *(truncated — {total} total sweeps; use* `/optimize history <page>` *to page through)*")
            break
        final_lines.append(line)
        current_len += len(line) + 1

    await update.effective_message.reply_text("\n".join(final_lines), parse_mode="Markdown")


# ---------------------------------------------------------------- /optimize apply

async def _optimize_apply(update: Update, args, chat_id):
    apply_args = args[1:]
    if not apply_args or not all(a.isdigit() for a in apply_args) or len(apply_args) > 2:
        await update.effective_message.reply_text(
            "⚠️ **Usage:** `/optimize apply <rank>` (from your most recent sweep)\n"
            "or `/optimize apply <run_id> <rank>` (from a past sweep — see `/optimize history`).",
            parse_mode="Markdown"
        )
        return

    if len(apply_args) == 2:
        run_id, rank = int(apply_args[0]), int(apply_args[1])
        run = get_sweep_run(run_id)
        if not run or run["chat_id"] != chat_id:
            await update.effective_message.reply_text(
                f"❌ No sweep run `#{run_id}` found for this chat.", parse_mode="Markdown")
            return
    else:
        rank = int(apply_args[0])
        run = get_latest_sweep_run(chat_id)
        if not run:
            await update.effective_message.reply_text(
                "❌ No sweep results saved for this chat yet. Run `/optimize` first.", parse_mode="Markdown")
            return
        run_id = run["id"]

    rows = get_sweep_results(run_id)
    if not rows or not (1 <= rank <= len(rows)):
        max_rank = len(rows) if rows else 0
        await update.effective_message.reply_text(
            f"❌ Rank must be between `1` and `{max_rank}` for sweep `#{run_id}`.", parse_mode="Markdown")
        return

    row = rows[rank - 1]
    skey = row["strategy"]
    strat = STRATEGIES.get(skey)
    if strat is None:
        await update.effective_message.reply_text(
            f"❌ Strategy `{skey}` is not installed anymore — nothing applied.", parse_mode="Markdown")
        return

    store = ALERT_STATE.setdefault("strategy_params", {}).setdefault(skey, {})
    known = {prm.key for prm in strat.params}
    changes = []
    for k, v in (row.get("params") or {}).items():
        if k in known and store.get(k) != v:
            changes.append(f"• `{k}`: `{store.get(k)}` ➡️ `{v}`")
            store[k] = v

    old_strategy = ALERT_STATE.get("active_strategy")
    ALERT_STATE["active_strategy"] = skey

    tf_note = ""
    sweep_mode = run.get("timeframe_mode")
    old_mode = ALERT_STATE.get("timeframe_mode")
    if sweep_mode in TIMEFRAME_PRESETS and sweep_mode != old_mode:
        preset = TIMEFRAME_PRESETS[sweep_mode]
        ALERT_STATE["timeframe_mode"] = sweep_mode
        ALERT_STATE["entry_tf"] = preset["entry"]
        ALERT_STATE["trend_tf"] = preset["trend"]
        ALERT_STATE["macro_tf"] = preset["macro"]
        tf_note = f"\n• **Timeframe:** `{old_mode}` ➡️ `{sweep_mode}` *(matches the backtest this result came from)*"

    reset_runtime()
    save_settings()

    changes_txt = "\n".join(changes) if changes else "• _(parameters already at these values)_"
    await update.effective_message.reply_text(
        f"✅ **Applied Sweep `#{run_id}` Result #{rank}**\n\n"
        f"• **Strategy:** `{old_strategy}` ➡️ `{skey}`\n"
        f"{changes_txt}"
        f"{tf_note}\n\n"
        f"📊 *Backtested: Net `{row['net_r']}R` | WR `{row['win_rate']}%` "
        f"({row['wins']}W/{row['losses']}L) | DD `{row['max_drawdown_r']}R`*\n\n"
        "⚠️ *Live-armed execution now uses these parameters immediately — "
        "watch `/diagnose` or a few live signals before trusting size.*",
        parse_mode="Markdown"
    )


# ---------------------------------------------------------------- /optimize (run a sweep)

async def _optimize_run(update: Update, args, chat_id):
    if not STRATEGY_PRESETS:
        await update.message.reply_text(
            "📭 **No strategies installed** — nothing to optimize. Add a package to `strategies/` first.\n"
            "`/optimize history` and `/optimize view` still work for past sweeps.",
            parse_mode="Markdown"
        )
        return

    days, mode, rank_mode = 30, None, "net_r"
    sweep_all, only_key, axes = False, None, []
    remaining = list(args)
    if remaining and remaining[0].isdigit():
        days = int(remaining.pop(0))
    for arg in remaining:
        a = arg.lower()
        if a == "strategies":
            sweep_all = True
        elif a in RANK_FLAGS:
            rank_mode = a
        elif a in TIMEFRAME_PRESETS:
            mode = a
        elif a.startswith("strategy="):
            only_key = a.split("=", 1)[1]
        else:
            axes.append(a)
    days = max(1, min(days, 180))

    keys = list(STRATEGY_PRESETS) if sweep_all else [only_key or ALERT_STATE.get("active_strategy")]
    if not sweep_all and keys[0] not in STRATEGY_PRESETS:
        await update.message.reply_text(
            "⚠️ **No active strategy.** Pick one with `/strategy`, pass `strategy=<key>`, "
            "or add the `strategies` flag.\n\n" + _USAGE, parse_mode="Markdown")
        return

    plan, used_default, err = plan_sweep(keys, axes)
    if err:
        await update.message.reply_text(f"⚠️ {err}\n\n{_USAGE}", parse_mode="Markdown")
        return

    symbol = await asyncio.to_thread(get_gold_symbol)
    if not symbol:
        await update.message.reply_text("❌ MT5 Gold symbol not found.")
        return

    label = mode or ALERT_STATE.get('timeframe_mode', 'scalp')
    flag_str = "" if used_default else f" `[{', '.join(axes)}]`"
    rank_str = " · calmar" if rank_mode == "calmar" else ""
    progress_msg = await update.message.reply_text(
        f"⏳ Running optimization sweep — `{days}d` on `{label.upper()}`{flag_str}{rank_str}... `0%`",
        parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()
    last_edit_time = [0.0]
    last_pct = [-1]

    def progress_callback(pct):
        now = time.time()
        if pct != last_pct[0] and (now - last_edit_time[0] >= 1.5 or pct == 100):
            last_edit_time[0] = now
            last_pct[0] = pct

            async def _edit():
                try:
                    await progress_msg.edit_text(
                        f"⏳ Running optimization sweep — `{days}d` on `{label.upper()}`{flag_str}{rank_str}... `{pct}%`",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(_edit(), loop)

    start_time = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            run_backtest_sweep, symbol, days, mode, plan=plan, progress_callback=progress_callback
        )
    except Exception as e:
        logging.exception("Optimization sweep crashed")
        await update.message.reply_text(f"❌ **Sweep crashed:** `{e}`", parse_mode="Markdown")
        return
    elapsed = round(time.perf_counter() - start_time, 1)

    if "error" in result:
        await update.message.reply_text(f"❌ **Sweep rejected:** {result['error']}", parse_mode="Markdown")
        return

    grid = result.get("grid", [])
    valid = [g for g in grid if "error" not in g]
    if not valid:
        first_error = grid[0].get("error", "No valid parameter combinations produced signals.") if grid else "No results returned."
        await update.message.reply_text(f"❌ **Sweep failed:** {first_error}", parse_mode="Markdown")
        return

    min_sample = result.get("min_sample", MIN_SWEEP_SAMPLE)
    run_axes = result.get("axes", [])

    def sample_size(g):
        return g.get("wins", 0) + g.get("losses", 0)

    def calmar_score(g):
        dd = g.get("max_drawdown_r", 0.0)
        return (g["net_r"] / dd) if dd > 0.01 else g["net_r"]

    if rank_mode == "calmar":
        sort_key = calmar_score
    elif rank_mode == "avgr":
        sort_key = lambda g: g["avg_r"]
    else:
        sort_key = lambda g: g["net_r"]

    reliable = sorted([g for g in valid if sample_size(g) >= min_sample], key=sort_key, reverse=True)
    thin = sorted([g for g in valid if sample_size(g) < min_sample], key=sort_key, reverse=True)
    valid_sorted = reliable + thin

    if reliable:
        saved_rows = reliable
        reliability_note = (
            f"`{len(saved_rows)}` combo(s) met the reliability bar "
            f"(≥ `{min_sample}` closed trades — 50% of the `{days}`-day window) and were saved."
        )
    else:
        saved_rows = valid_sorted[:3]
        reliability_note = (
            f"⚠️ *No combo reached the `{min_sample}`-trade reliability bar for this `{days}`-day "
            f"window — saving the top `{len(saved_rows)}` anyway, flagged as low-sample.*"
        )

    saved_rows = [{**g, "calmar": round(calmar_score(g), 4)} for g in saved_rows]
    run_id = log_sweep_run(
        chat_id=chat_id, symbol=symbol, timeframe_mode=label, days=days, rank_mode=rank_mode,
        axes=run_axes, min_sample=min_sample, elapsed_seconds=elapsed, rows=saved_rows,
    )

    struct_gate = "ON ✅" if ALERT_STATE.get("require_structure_break") else "OFF ❌"
    vol_gate = "ON ✅" if ALERT_STATE.get("require_volume_atr_filter") else "OFF ❌"
    run_tag = f" | 💾 `#{run_id}`" if run_id else " | ⚠️ *not saved (DB error)*"
    lines = [f"🧪 **OPTIMIZATION SWEEP — {label.upper()}** ({days}d | ⚡ `{elapsed}s`{run_tag})\n"]

    if len(keys) == 1:
        base_txt = f"`{keys[0]}` · `{STRATEGIES[keys[0]].summary(get_params(keys[0]))}`"
    else:
        base_txt = "each strategy at its saved settings"
    lines.append(f"📌 **Base settings:** {base_txt} *(swept params override these)*")
    lines.append(f"⚙️ **Hard Gates:** Structure: {struct_gate} | Vol/ATR: {vol_gate}")
    lines.append(f"📏 *Combos with < {min_sample} closed trades are shown but ranked last (unreliable sample size).*")
    lines.append(reliability_note + "\n")

    rank_label = ("Net R ÷ Max Drawdown (risk-adjusted)" if rank_mode == "calmar"
                  else "Avg R per Trade" if rank_mode == "avgr" else "Net R")
    display_rows = saved_rows[:10]
    lines.append(f"🏆 **Top {len(display_rows)} Results (by {rank_label}, reliable samples first):**")
    lines.extend(_format_sweep_rows(display_rows, min_sample, rank_mode, run_axes))

    if run_id:
        lines.append(f"\n💡 `/optimize apply <rank>` to promote a result · `/optimize view {run_id}` or `/optimize history` to browse past sweeps.")

    disclaimer = (
        "\n⚠️ *Backtested on historical bars with synthetic spread. Sweeping multiple dimensions "
        "increases overfit risk — validate on out-of-sample data before live deployment.*"
    )
    budget = 3800 - len(disclaimer) - 40
    final_lines, current_len = [], 0
    for line in lines:
        if current_len + len(line) + 1 > budget:
            final_lines.append("\n... *(results truncated for length)*")
            break
        final_lines.append(line)
        current_len += len(line) + 1
    final_lines.append(disclaimer)

    await update.message.reply_text("\n".join(final_lines), parse_mode="Markdown")


# ---------------------------------------------------------------- dispatcher

@admin_only
async def optimize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    chat_id = update.effective_chat.id
    sub = args[0].lower() if args else None

    if sub == "view":
        await _optimize_view(update, args, chat_id)
    elif sub == "history":
        await _optimize_history(update, args, chat_id)
    elif sub == "apply":
        await _optimize_apply(update, args, chat_id)
    else:
        await _optimize_run(update, args, chat_id)