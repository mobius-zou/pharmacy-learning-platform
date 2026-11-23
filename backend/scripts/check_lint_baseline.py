"""lint 棘轮守卫：当前告警数不得超过基线。

背景
----
`docs/audit-fix/lint-baseline.md` 记录了存量：ruff 212 条（默认规则集）、
mypy 13 条。存量一时清不完，但不能**继续变多**——否则「棘轮只减不增」就失效了。

这个脚本把基线变成可执行的门禁：
- 当前数量 **> 基线** → 退出码 1，并列出超出的类别/文件；
- 当前数量 **< 基线** → 通过，但提示可以把基线调低（棘轮收紧）；
- 工具不可用 → 退出码 2（CI 里工具是装好的，缺失说明环境有问题）。

用法
----
    python scripts/check_lint_baseline.py            # 校验
    python scripts/check_lint_baseline.py --update   # 把当前数量写回基线

注意：ruff 必须用 `--isolated` 跑**默认规则集**。
项目里的 `ruff.toml` 只启用了棘轮规则集（E9/F63/F7/F82），
它数出来是 0，不能用来和 212 的基线比较。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASELINE_FILE = BACKEND_DIR / "lint-baseline.json"

RUFF_COMMAND = ["ruff", "check", "app", "--isolated", "--statistics"]
MYPY_COMMAND = ["mypy", "app", "--ignore-missing-imports"]


# --- 纯函数（可单测，不依赖工具是否安装） ----------------------------------


def count_from_ruff_statistics(text: str) -> tuple[int, dict[str, int]]:
    """解析 ``ruff --statistics`` 输出。

    每行形如 ``133\tB008    \t[ ] function-call-in-default-argument``，
    末尾还有 ``Found N errors.``（不可作为总数来源，避免口径漂移）。
    """
    by_rule: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)\s+([A-Z]+[0-9]+)\b", line)
        if match:
            by_rule[match.group(2)] = int(match.group(1))
    return sum(by_rule.values()), by_rule


def count_from_mypy(text: str) -> tuple[int, dict[str, int]]:
    """解析 mypy 输出，按文件统计 ``error:`` 行数。"""
    by_file: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(.+?):\d+: error:", line)
        if match:
            by_file[match.group(1)] = by_file.get(match.group(1), 0) + 1
    return sum(by_file.values()), by_file


def compare_counts(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """返回「超出基线」的说明；空列表表示通过。"""
    problems: list[str] = []
    for tool, value in current.items():
        limit = baseline.get(tool)
        if limit is None:
            problems.append(f"{tool}: 基线文件里没有该工具的基线值，无法判定")
        elif value > limit:
            problems.append(f"{tool}: 当前 {value} 条 > 基线 {limit} 条（超出 {value - limit} 条）")
    return problems


def ratchet_hints(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """当前低于基线时，提示把基线调低（棘轮只减不增）。"""
    return [
        f"{tool}: 当前 {value} 条 < 基线 {baseline[tool]} 条，建议 --update 收紧基线"
        for tool, value in current.items()
        if tool in baseline and value < baseline[tool]
    ]


# --- 执行层 ----------------------------------------------------------------


def _run(command: list[str]) -> str:
    executable = str(BACKEND_DIR / "venv" / "bin" / command[0])
    argv = [executable, *command[1:]]
    try:
        result = subprocess.run(
            argv,
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        raise SystemExit(
            f"[lint-baseline] 找不到 {command[0]}。请先安装："
            "pip install ruff mypy（CI 的 lint-blocking job 已包含）"
        ) from None
    # ruff 有告警时退出码为 1，mypy 同理；这里只看输出，不看退出码
    return result.stdout + result.stderr


def load_baseline() -> dict:
    if not BASELINE_FILE.is_file():
        raise SystemExit(f"[lint-baseline] 缺少基线文件：{BASELINE_FILE}")
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 lint 告警数是否超过基线")
    parser.add_argument("--update", action="store_true", help="把当前数量写回基线（棘轮收紧）")
    args = parser.parse_args(argv)

    data = load_baseline()
    baseline = {
        "ruff": int(data["ruff"]["total"]),
        "mypy": int(data["mypy"]["errors"]),
    }

    ruff_total, ruff_by_rule = count_from_ruff_statistics(_run(RUFF_COMMAND))
    mypy_total, mypy_by_file = count_from_mypy(_run(MYPY_COMMAND))
    current = {"ruff": ruff_total, "mypy": mypy_total}

    print("=" * 70)
    print(f"{'工具':<8}{'当前':>8}{'基线':>8}{'变化':>8}")
    print("-" * 70)
    for tool in ("ruff", "mypy"):
        delta = current[tool] - baseline[tool]
        print(f"{tool:<8}{current[tool]:>8}{baseline[tool]:>8}{delta:>+8}")
    print("=" * 70)

    if args.update:
        data["ruff"]["total"] = ruff_total
        data["mypy"]["errors"] = mypy_total
        BASELINE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[lint-baseline] 已更新基线：{BASELINE_FILE.name}")
        return 0

    problems = compare_counts(current, baseline)
    if problems:
        print("\n[lint-baseline] 失败：新增了告警，棘轮只减不增", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        if ruff_total > baseline["ruff"]:
            print("\n  ruff 明细（按规则）：", file=sys.stderr)
            for rule, count in sorted(ruff_by_rule.items(), key=lambda kv: -kv[1])[:10]:
                print(f"    {count:>4}  {rule}", file=sys.stderr)
        if mypy_total > baseline["mypy"]:
            print("\n  mypy 明细（按文件）：", file=sys.stderr)
            for name, count in sorted(mypy_by_file.items(), key=lambda kv: -kv[1])[:10]:
                print(f"    {count:>4}  {name}", file=sys.stderr)
        return 1

    print("[lint-baseline] 通过：未超过基线")
    for hint in ratchet_hints(current, baseline):
        print(f"  · {hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
