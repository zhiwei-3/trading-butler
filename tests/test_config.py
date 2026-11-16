# tests/test_config.py
import ast
import pathlib

from config import ALERT_STATE, PERSISTENT_KEYS, SCHEMA, to_text

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKIP_DIRS = {".venv", "venv", "env", "__pycache__", ".git"}


def test_schema_defaults_round_trip_through_their_parsers():
    for s in SCHEMA:
        if s.parse is not None:
            assert s.parse(to_text(s.default)) == s.default, f"{s.key}: default fails its own parser"


def test_keys_are_unique_and_persisted_keys_exist():
    keys = [s.key for s in SCHEMA]
    assert len(keys) == len(set(keys))
    assert set(PERSISTENT_KEYS) <= set(ALERT_STATE)


def _state_keys_used():
    for path in ROOT.rglob("*.py"):
        if _SKIP_DIRS & set(path.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id == "ALERT_STATE"
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                yield path, node.slice.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == "ALERT_STATE"
                  and node.func.attr in ("get", "setdefault", "pop") and node.args
                  and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                yield path, node.args[0].value


def test_every_alert_state_key_used_in_the_code_exists():
    missing = sorted({f"{p.relative_to(ROOT)}: {k}" for p, k in _state_keys_used() if k not in ALERT_STATE})
    assert not missing, "ALERT_STATE keys used but not declared in config.SCHEMA / RUNTIME_STATE:\n" + "\n".join(missing)