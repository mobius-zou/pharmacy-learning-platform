"""lint 棘轮守卫的回归测试。

守卫本身的逻辑必须可信：如果它「永远通过」，就等于没有门禁。
这里不依赖 ruff/mypy 是否安装 —— 只测解析与比较的纯逻辑，
以及基线文件的口径是否正确。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = BACKEND / "scripts" / "check_lint_baseline.py"
BASELINE = BACKEND / "lint-baseline.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_lint_baseline", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load_script()


# --- 解析 ------------------------------------------------------------------


def test_parses_ruff_statistics(guard) -> None:
    text = """133\tB008    \t[ ] function-call-in-default-argument
 16\tUP037   \t[*] quoted-annotation
 11\tI001    \t[*] unsorted-imports
Found 160 errors.
[*] 50 fixable with `--fix` (3 unsafe fixes available with `--fix --unsafe-fixes`)."""

    total, by_rule = guard.count_from_ruff_statistics(text)

    assert total == 160
    assert by_rule == {"B008": 133, "UP037": 16, "I001": 11}


def test_ruff_parser_ignores_summary_line(guard) -> None:
    """`Found N errors.` 不能参与计数，否则口径会随格式漂移。"""
    only_summary = "Found 212 errors."
    assert guard.count_from_ruff_statistics(only_summary) == (0, {})


def test_parses_mypy_errors_per_file(guard) -> None:
    text = """app/a.py:1: error: bad thing  [arg-type]
app/a.py:9: error: another  [attr-defined]
app/b.py:3: error: third  [assignment]
app/c.py:4: note: not an error
Found 3 errors in 2 files (checked 10 source files)"""

    total, by_file = guard.count_from_mypy(text)

    assert total == 3
    assert by_file == {"app/a.py": 2, "app/b.py": 1}


# --- 比较 ------------------------------------------------------------------


def test_compare_passes_when_at_or_below_baseline(guard) -> None:
    assert guard.compare_counts({"ruff": 212, "mypy": 13}, {"ruff": 212, "mypy": 13}) == []
    assert guard.compare_counts({"ruff": 200, "mypy": 10}, {"ruff": 212, "mypy": 13}) == []


def test_compare_fails_when_above_baseline(guard) -> None:
    problems = guard.compare_counts({"ruff": 213, "mypy": 14}, {"ruff": 212, "mypy": 13})

    assert len(problems) == 2
    assert "ruff" in problems[0] and "213" in problems[0]
    assert "mypy" in problems[1] and "14" in problems[1]


def test_compare_fails_on_single_tool_regression(guard) -> None:
    """只涨一个工具也必须拦下，不能因为另一个没变就放过。"""
    problems = guard.compare_counts({"ruff": 212, "mypy": 14}, {"ruff": 212, "mypy": 13})

    assert len(problems) == 1
    assert "mypy" in problems[0]


def test_compare_requires_baseline_for_every_tool(guard) -> None:
    problems = guard.compare_counts({"ruff": 0, "mypy": 0}, {"ruff": 212})

    assert any("mypy" in problem and "基线" in problem for problem in problems)


def test_ratchet_hint_when_below_baseline(guard) -> None:
    hints = guard.ratchet_hints({"ruff": 100, "mypy": 13}, {"ruff": 212, "mypy": 13})

    assert len(hints) == 1
    assert "ruff" in hints[0] and "--update" in hints[0]


# --- 基线文件本身 ----------------------------------------------------------


def test_baseline_file_exists_and_is_valid() -> None:
    assert BASELINE.is_file(), "缺少 lint-baseline.json"

    data = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert isinstance(data["ruff"]["total"], int) and data["ruff"]["total"] > 0
    assert isinstance(data["mypy"]["errors"], int) and data["mypy"]["errors"] > 0


def test_baseline_ruff_command_uses_isolated() -> None:
    """必须用 --isolated 跑默认规则集。

    项目 ruff.toml 只启用棘轮规则集，数出来是 0；
    若基线改用项目配置，守卫会永远通过，形同虚设。
    """
    data = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert "--isolated" in data["ruff"]["command"]


def test_guard_commands_match_baseline_commands(guard) -> None:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert "--isolated" in guard.RUFF_COMMAND
    assert data["mypy"]["command"].split()[0] == guard.MYPY_COMMAND[0]


def test_baseline_doc_matches_json() -> None:
    """文档里的数字若与基线文件不一致，读文档的人会被误导。"""
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    doc = (BACKEND.parent / "docs" / "audit-fix" / "lint-baseline.md").read_text(encoding="utf-8")

    assert f"**{data['ruff']['total']}**" in doc, "lint-baseline.md 的 ruff 数字与基线不一致"
    assert f"**{data['mypy']['errors']}**" in doc, "lint-baseline.md 的 mypy 数字与基线不一致"
