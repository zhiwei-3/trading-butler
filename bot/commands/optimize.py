import time
import logging
import asyncio
from telegram import Update
from telegram.ext import ContextTypes

from config import ALERT_STATE, TIMEFRAME_PRESETS, STRATEGY_PRESETS, save_settings
from database import (
    get_sweep_run, get_latest_sweep_run, get_sweep_results,
    count_sweep_runs, get_sweep_history, get_sweep_history_ranked,
    log_sweep_run,
)
from mt5_engine import get_gold_symbol
from strategy.backtester import run_backtest_sweep, MIN_SWEEP_SAMPLE
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

AXIS_FLAGS = ("strategies", "sl", "rsi", "score", "rrr")
RANK_FLAGS = ("calmar", "avgr")


def _resolve_sort_key(token):
    """Returns the canonical SORT_FIELDS key for a token, or None if it's not a sort keyword."""
    return token if token in SORT_FIELDS else None


def _format_sweep_rows(rows, min_sample, rank_mode, show_axes):
    """Shared renderer for a sweep's top-10 rows — used by both the just-finished
    /optimize output and /optimize view <run_id> so the two never drift out of
    sync with each other."""
    def sample_size(g):
        return (g.get("wins") or 0) + (g.get("losses") or 0)

    def calmar_val(g):
        return g.get("calmar")

    def describe(g):
        parts = []
        if show_axes.get("strategies") and g.get("strategy"):
            parts.append(f"`{g['strategy'].replace('_', ' ').title()}`")
        if show_axes.get("rrr") and g.get("min_rrr") is not None:
            parts.append(f"RRR `1:{g['min_rrr']}`")
        if show_axes.get("score") and g.get("min_confluence_score") is not None:
            parts.append(f"Score `{g['min_confluence_score']}`")
        if show_axes.get("sl") and g.get("sl_atr_mult") is not None:
            parts.append(f"SL `{g['sl_atr_mult']}x`")
        if show_axes.get("rsi") and g.get("rsi_buy") is not None:
            parts.append(f"RSI `{g['rsi_buy']}/{g['rsi_sell']}`")
        return " ".join(parts) if parts else "(single combination)"

    lines = []
    for i, g in enumerate(rows, 1):
        thin_flag = " ⚠️*low sample*" if sample_size(g) < min_sample else ""
        calmar_str = f" | Calmar `{round(calmar_val(g), 2)}`" if rank_mode == "calmar" and calmar_val(g) is not None else ""
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
        rows = sorted(
            rows,
            key=lambda r: (r.get(spec["column"]) if r.get(spec["column"]) is not None else float("-inf")),
            reverse=spec["reverse"],
        )
        rank_label = spec["label"]
    else:
        rank_label = "Net R ÷ Max Drawdown (risk-adjusted)" if run["rank_mode"] == "calmar" \
            else "Avg R per Trade" if run["rank_mode"] == "avgr" else "Net R"

    axes = {
        "strategies": bool(run["swept_strategies"]), "sl": bool(run["swept_sl"]),
        "rsi": bool(run["swept_rsi"]), "rrr": bool(run["swept_rrr"]), "score": bool(run["swept_score"]),
    }
    if not any(axes.values()):
        axes = {"rrr": True, "score": True}  # legacy bare-default sweep

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
        axes = [name for name, flag in (
            ("strategies", run["swept_strategies"]), ("sl", run["swept_sl"]),
            ("rsi", run["swept_rsi"]), ("rrr", run["swept_rrr"]), ("score", run["swept_score"]),
        ) if flag]
        axes_str = "+".join(axes) if axes else "rrr+score"

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
            f"[{axes_str}] rank=`{run['rank_mode']}`{thin_flag} — top: {top_str}\n"
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
                f"❌ No sweep run `#{run_id}` found for this chat.", parse_mode="Markdown"
            )
            return
    else:
        rank = int(apply_args[0])
        run = get_latest_sweep_run(chat_id)
        if not run:
            await update.effective_message.reply_text(
                "❌ No sweep results saved for this chat yet. Run `/optimize` first.",
                parse_mode="Markdown"
            )
            return
        run_id = run["id"]

    rows = get_sweep_results(run_id)
    if not rows or not (1 <= rank <= len(rows)):
        max_rank = len(rows) if rows else 0
        await update.effective_message.reply_text(
            f"❌ Rank must be between `1` and `{max_rank}` for sweep `#{run_id}`.", parse_mode="Markdown"
        )
        return

    row = rows[rank - 1]
    old = {
        "active_strategy": ALERT_STATE.get("active_strategy"),
        "min_rrr": ALERT_STATE.get("min_rrr"),
        "min_confluence_score": ALERT_STATE.get("min_confluence_score"),
        "sl_atr_mult": ALERT_STATE.get("sl_atr_mult"),
        "rsi_buy_threshold": ALERT_STATE.get("rsi_buy_threshold"),
        "rsi_sell_threshold": ALERT_STATE.get("rsi_sell_threshold"),
        "timeframe_mode": ALERT_STATE.get("timeframe_mode"),
    }

    ALERT_STATE["active_strategy"] = row["strategy"]
    ALERT_STATE["min_rrr"] = row["min_rrr"]
    ALERT_STATE["min_confluence_score"] = row["min_confluence_score"]
    ALERT_STATE["sl_atr_mult"] = row["sl_atr_mult"]
    ALERT_STATE["rsi_buy_threshold"] = row["rsi_buy"]
    ALERT_STATE["rsi_sell_threshold"] = row["rsi_sell"]

    tf_note = ""
    sweep_mode = run.get("timeframe_mode")
    if sweep_mode in TIMEFRAME_PRESETS and sweep_mode != old["timeframe_mode"]:
        preset = TIMEFRAME_PRESETS[sweep_mode]
        ALERT_STATE["timeframe_mode"] = sweep_mode
        ALERT_STATE["entry_tf"] = preset["entry"]
        ALERT_STATE["trend_tf"] = preset["trend"]
        ALERT_STATE["macro_tf"] = preset["macro"]
        tf_note = f"\n• **Timeframe:** `{old['timeframe_mode']}` ➡️ `{sweep_mode}` *(matches the backtest this result came from)*"

    ALERT_STATE["last_rsi_signal"] = None
    save_settings()

    await update.effective_message.reply_text(
        f"✅ **Applied Sweep `#{run_id}` Result #{rank}**\n\n"
        f"• **Strategy:** `{old['active_strategy']}` ➡️ `{row['strategy']}`\n"
        f"• **Min RRR:** `1:{old['min_rrr']}` ➡️ `1:{row['min_rrr']}`\n"
        f"• **Min Score:** `{old['min_confluence_score']}` ➡️ `{row['min_confluence_score']}`\n"
        f"• **SL ATR Mult:** `{old['sl_atr_mult']}x` ➡️ `{row['sl_atr_mult']}x`\n"
        f"• **RSI:** `{old['rsi_buy_threshold']}/{old['rsi_sell_threshold']}` ➡️ "
        f"`{row['rsi_buy']}/{row['rsi_sell']}`"
        f"{tf_note}\n\n"
        f"📊 *Backtested: Net `{row['net_r']}R` | WR `{row['win_rate']}%` "
        f"({row['wins']}W/{row['losses']}L) | DD `{row['max_drawdown_r']}R`*\n\n"
        "⚠️ *Live-armed execution now uses these parameters immediately — "
        "watch `/diagnose` or a few live signals before trusting size.*",
        parse_mode="Markdown"
    )


# ---------------------------------------------------------------- /optimize (run a sweep)

async def _optimize_run(update: Update, args, chat_id):
    days = 30
    mode = None
    sweep_axes = set()
    rank_mode = "net_r"

    if args:
        remaining_args = list(args)
        if remaining_args[0].isdigit():
            days = int(remaining_args.pop(0))

        unprocessed = []
        for arg in remaining_args:
            arg_lower = arg.lower()
            if arg_lower in AXIS_FLAGS:
                sweep_axes.add(arg_lower)
            elif arg_lower in RANK_FLAGS:
                rank_mode = arg_lower
            elif arg_lower in TIMEFRAME_PRESETS:
                mode = arg_lower
            else:
                unprocessed.append(arg)

        if unprocessed:
            await update.message.reply_text(
                f"⚠️ **Unknown argument(s):** `{', '.join(unprocessed)}`\n\n"
                "**Usage:** `/optimize [days] [scalp|intraday|swing] [flags]`\n"
                f"**Sweep flags (combine up to 3):** `{'`, `'.join(AXIS_FLAGS)}`\n"
                f"**Ranking flags:** `{'`, `'.join(RANK_FLAGS)}`\n\n"
                "**Other subcommands:**\n"
                "• `/optimize history [n]` — list past sweeps saved for this chat\n"
                "• `/optimize apply <rank>` — promote a result from your most recent sweep\n"
                "• `/optimize apply <run_id> <rank>` — promote a result from any past sweep",
                parse_mode="Markdown"
            )
            return

    days = max(1, min(days, 180))
    used_default_axes = not sweep_axes
    if used_default_axes:
        sweep_axes = {"rrr", "score"}

    if len(sweep_axes) > 3:
        await update.message.reply_text(
            "⚠️ Combine at most 3 axes at once (`strategies`/`sl`/`rsi`/`score`/`rrr`) "
            "to keep the grid size manageable.",
            parse_mode="Markdown"
        )
        return

    symbol = await asyncio.to_thread(get_gold_symbol)
    if not symbol:
        await update.message.reply_text("❌ MT5 Gold symbol not found.")
        return

    label = mode or ALERT_STATE.get('timeframe_mode', 'scalp')

    pool_tier = 1 if used_default_axes else min(len(sweep_axes), 3)
    RRR_POOLS = {1: [1.3, 1.5, 2.0, 2.5, 3.0], 2: [1.5, 2.0, 2.5, 3.0], 3: [1.5, 2.0, 3.0]}
    SCORE_POOLS = {1: [20, 25, 30, 35, 40], 2: [25, 30, 35, 40], 3: [25, 35]}
    SL_POOLS = {1: [1.2, 1.5, 1.7, 2.0, 2.5], 2: [1.2, 1.5, 2.0, 2.5], 3: [1.5, 2.0, 2.5]}
    RSI_POOL = [(30, 60), (35, 60), (40, 60), (30, 65)]

    rrr_values = RRR_POOLS[pool_tier] if "rrr" in sweep_axes else None
    score_values = SCORE_POOLS[pool_tier] if "score" in sweep_axes else None
    sl_values = SL_POOLS[pool_tier] if "sl" in sweep_axes else None
    rsi_pairs = RSI_POOL if "rsi" in sweep_axes else None
    strategy_values = list(STRATEGY_PRESETS.keys()) if "strategies" in sweep_axes else None

    flag_str = "" if used_default_axes else f" [{', '.join(sorted(sweep_axes))}]"
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
            run_backtest_sweep, symbol, days, mode,
            rrr_values=rrr_values, score_values=score_values,
            strategy_values=strategy_values, sl_values=sl_values, rsi_pairs=rsi_pairs,
            progress_callback=progress_callback
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

    # Save every combo that clears the reliability bar for THIS sweep's window —
    # not a fixed top-10. If literally nothing is reliable, fall back to saving a
    # small flagged sample rather than leaving history/apply empty for that run.
    reliable_rows = [g for g in valid_sorted if sample_size(g) >= min_sample]
    if reliable_rows:
        saved_rows = reliable_rows
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
    run_id = log_sweep_run(
        chat_id=chat_id, symbol=symbol, timeframe_mode=label, days=days, rank_mode=rank_mode,
        swept_flags={
            "strategies": result.get("swept_strategies", False),
            "sl": result.get("swept_sl", False),
            "rsi": result.get("swept_rsi", False),
            "rrr": result.get("swept_rrr", False),
            "score": result.get("swept_score", False),
        },
        min_sample=min_sample, elapsed_seconds=elapsed,
        rows=[{**g, "calmar": round(calmar_score(g), 4)} for g in saved_rows],
    )

    axes_shown = {
        "strategies": result.get("swept_strategies", False),
        "sl": result.get("swept_sl", False),
        "rsi": result.get("swept_rsi", False),
        "rrr": result.get("swept_rrr", False),
        "score": result.get("swept_score", False),
    }
    if not any(axes_shown.values()):
        axes_shown = {"rrr": True, "score": True}

    base_strat = ALERT_STATE.get("active_strategy", "smc_confluence").replace("_", " ").title()
    base_rsi = f"{ALERT_STATE.get('rsi_buy_threshold', 30)}/{ALERT_STATE.get('rsi_sell_threshold', 70)}"
    base_sl = f"{ALERT_STATE.get('sl_atr_mult', 1.5)}x"
    base_rrr = f"1:{ALERT_STATE.get('min_rrr', 1.5)}"
    base_score = f"{ALERT_STATE.get('min_confluence_score', 35)}/100"
    struct_gate = "ON ✅" if ALERT_STATE.get("require_structure_break") else "OFF ❌"
    vol_gate = "ON ✅" if ALERT_STATE.get("require_volume_atr_filter") else "OFF ❌"

    run_tag = f" | 💾 `#{run_id}`" if run_id else " | ⚠️ *not saved (DB error)*"
    lines = [f"🧪 **OPTIMIZATION SWEEP — {label.upper()}** ({days}d | ⚡ `{elapsed}s`{run_tag})\n"]

    baselines = []
    if not axes_shown.get("strategies"): baselines.append(f"Strategy: `{base_strat}`")
    if not axes_shown.get("rrr"): baselines.append(f"Min RRR: `{base_rrr}`")
    if not axes_shown.get("score"): baselines.append(f"Min Score: `{base_score}`")
    if not axes_shown.get("sl"): baselines.append(f"SL: `{base_sl}`")
    if not axes_shown.get("rsi"): baselines.append(f"RSI: `{base_rsi}`")

    lines.append("📌 **Fixed Baselines:** " + (" | ".join(baselines) if baselines else "*(none — every axis swept)*"))
    lines.append(f"⚙️ **Hard Gates:** Structure: {struct_gate} | Vol/ATR: {vol_gate}")
    lines.append(f"📏 *Combos with < {min_sample} closed trades are shown but ranked last (unreliable sample size).*")
    lines.append(reliability_note + "\n")

    rank_label = ("Net R ÷ Max Drawdown (risk-adjusted)" if rank_mode == "calmar"
                  else "Avg R per Trade" if rank_mode == "avgr" else "Net R")
    display_rows = saved_rows[:10]
    lines.append(f"🏆 **Top {len(display_rows)} Results (by {rank_label}, reliable samples first):**")

    normalized = [{**g, "rsi_buy": g.get("rsi_pair", (None, None))[0],
                   "rsi_sell": g.get("rsi_pair", (None, None))[1],
                   "calmar": round(calmar_score(g), 4)} for g in display_rows]
    lines.extend(_format_sweep_rows(normalized, min_sample, rank_mode, axes_shown))

    if run_id:
        lines.append(f"\n💡 `/optimize apply <rank>` to promote a result · `/optimize view {run_id}` or `/optimize history` to browse past sweeps.")

    disclaimer = (
        "\n⚠️ *Backtested on historical bars with synthetic spread. Sweeping multiple dimensions "
        "increases overfit risk — validate on out-of-sample data before live deployment.*"
    )

    budget = 3800 - len(disclaimer) - 40
    final_lines = []
    current_len = 0
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