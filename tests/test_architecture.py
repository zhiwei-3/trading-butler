# tests/test_architecture.py
import ast, pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
STRATEGY_FORBIDDEN = ("core", "bot", "MetaTrader5", "telegram", "sqlite3", "matplotlib", "requests", "charts")
UTILS_FORBIDDEN = ("core", "bot", "telegram")
_SKIP = {".venv", "venv", "env", ".git", "__pycache__"}

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


def test_only_the_gateway_imports_metatrader5():
    allowed = ROOT / "core" / "mt5" / "gateway.py"
    offenders = [
        str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
        if p != allowed and not (_SKIP & set(p.parts))
        and any(m == "MetaTrader5" or m.startswith("MetaTrader5.") for m in _imports(p))
    ]
    assert not offenders, "import the lock-protected proxy instead (`from core.mt5 import mt5`):\n" + "\n".join(offenders)


def test_pure_layers_import_without_metatrader5():
    code = ("import sys; sys.modules['MetaTrader5'] = None; "      # a None entry makes `import MetaTrader5` fail
            "import config, analysis, strategies, utils.common, core.outcomes")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    

def test_charts_are_pure():
    assert not (v := _violations("charts", ("core", "bot", "MetaTrader5", "telegram", "sqlite3", "requests"))), "\n".join(v)