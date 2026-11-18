# tests/test_architecture.py
import ast, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
STRATEGY_FORBIDDEN = ("core", "bot", "MetaTrader5", "telegram", "sqlite3", "matplotlib", "requests", "utils.chart")
UTILS_FORBIDDEN = ("core", "bot", "telegram")


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def _violations(folder, forbidden):
    return [f"{p.relative_to(ROOT)} imports {m}"
            for p in (ROOT / folder).rglob("*.py") for m in _imports(p)
            if any(m == f or m.startswith(f + ".") for f in forbidden)]


def test_strategies_are_pure():
    assert not (v := _violations("strategies", STRATEGY_FORBIDDEN)), "\n".join(v)


def test_utils_do_not_reach_up():
    assert not (v := _violations("utils", UTILS_FORBIDDEN)), "\n".join(v)


def test_analysis_is_pure():
    assert not (v := _violations("analysis", ("core", "bot", "MetaTrader5", "telegram", "sqlite3", "matplotlib"))), "\n".join(v)


def test_outcomes_is_pure():
    path = ROOT / "core" / "outcomes.py"
    forbidden = ("core", "bot", "config", "MetaTrader5", "telegram", "sqlite3", "matplotlib", "requests")
    bad = [m for m in _imports(path) if any(m == f or m.startswith(f + ".") for f in forbidden)]
    assert not bad, f"core/outcomes.py must stay pure, but imports: {bad}"

def test_core_does_not_reach_up():
    assert not (v := _violations("core", ("bot", "telegram"))), "\n".join(v)