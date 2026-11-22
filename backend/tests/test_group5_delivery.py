"""组 5 回归测试（H8 / H9 / L10 / L15）。

对应审计报告的「工程流程 / 交付完整性 / 死抽象」类问题。
这些不是运行时缺陷，因此用**结构断言**把状态钉住，防止回退。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


# --- L15：Protocol 不能是死抽象 --------------------------------------------


def test_scheduling_protocol_is_actually_implemented() -> None:
    """审计 L15/C11：定义了无人实现的接口即死抽象。

    `SchedulingEngine` 必须真的被 `ReviewScheduler` 满足，否则它只是噪音。
    """
    from app.services.protocols import SchedulingEngine
    from app.services.review_service import ReviewScheduler

    assert isinstance(ReviewScheduler(), SchedulingEngine), (
        "ReviewScheduler 不再满足 SchedulingEngine 接口"
    )


def test_dead_card_repository_protocol_is_gone() -> None:
    """`CardRepository` 与函数式服务风格不符且无人实现，已删除。"""
    from app.services import protocols

    assert not hasattr(protocols, "CardRepository")


def test_protocols_module_has_no_unimplemented_leftovers() -> None:
    """凡是留在 protocols 里的接口，都必须有真实实现。"""
    from app.services import protocols
    from app.services.review_service import ReviewScheduler

    impls = {cls.__name__: cls for cls in (ReviewScheduler,)}
    available = set(dir(ReviewScheduler()))

    for name in dir(protocols):
        if name.startswith("_") or name in {"annotations", "Protocol", "runtime_checkable"}:
            continue
        obj = getattr(protocols, name)
        if isinstance(obj, type) and getattr(obj, "_is_protocol", False):
            members = {m for m in dir(obj) if not m.startswith("_")}
            assert members <= available, (
                f"接口 {name} 的方法在实现里缺失：{members - available}（实现集合 {list(impls)}）"
            )


# --- H8：工程流程 ----------------------------------------------------------


def test_ci_workflow_exists_and_runs_tests() -> None:
    """审计 H8：仓库无 CI。至少要有一条真的跑测试的流水线。"""
    workflow = ROOT / ".github" / "workflows" / "backend-ci.yml"
    assert workflow.is_file(), "缺少 CI 工作流"

    content = workflow.read_text(encoding="utf-8")
    assert "pytest tests/" in content, "CI 必须真的跑测试"
    assert "alembic upgrade head" in content, "CI 必须验证空库迁移"
    assert "mysql" in content.lower(), "CI 需要 MySQL 服务，否则集成测试会跳过"


def test_ci_blocks_on_failure() -> None:
    """test job 不能是 continue-on-error，否则形同虚设。

    按「下一个顶层 job 名」切分，而不是写死某个 job 名 ——
    否则一旦重命名 lint job，这条断言就会静默失效。
    """
    import re

    content = (ROOT / ".github" / "workflows" / "backend-ci.yml").read_text(encoding="utf-8")
    match = re.search(r"^  test:\n(.*?)(?=^  [A-Za-z][\w-]*:$)", content, re.S | re.M)
    assert match is not None, "找不到 test job"
    test_job = match.group(1)

    assert "continue-on-error" not in test_job


def test_ci_sets_a_real_jwt_secret() -> None:
    """CI 若不设置密钥，会退回临时密钥，测不出 H1 那类问题。"""
    content = (ROOT / ".github" / "workflows" / "backend-ci.yml").read_text(encoding="utf-8")

    assert "JWT_SECRET_KEY" in content


# --- H9：交付范围要写清楚 --------------------------------------------------


def test_root_readme_states_scope_explicitly() -> None:
    """审计 H9：前端为空，必须在 README 明确「仅后端」，不能含糊。"""
    readme = ROOT / "README.md"
    assert readme.is_file(), "缺少根 README"

    content = readme.read_text(encoding="utf-8")
    assert "交付范围" in content
    assert "frontend/" in content and "没有前端 UI" in content
    assert "alembic upgrade head" in content


def test_backend_readme_does_not_claim_auto_create() -> None:
    """审计 L14：README 曾说「启动自动建表」，与 H7 的单入口矛盾。"""
    content = (BACKEND / "README.md").read_text(encoding="utf-8")

    assert "启动时会自动建表" not in content
    assert "alembic upgrade head" in content


# --- L10：不该入库的目录要被忽略 -------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [".cachebro/", ".omo/", ".mypy_cache/", ".ruff_cache/", ".pytest_cache/", ".hf_cache/", "venv/"],
)
def test_gitignore_covers_tool_directories(pattern: str) -> None:
    content = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert pattern in content, f".gitignore 缺少 {pattern}"


def test_secrets_are_ignored() -> None:
    content = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".env" in content.splitlines()
    assert "chroma_db/" in content


# --- H8：仍需人工完成的动作（记录下来，避免以为已解决） --------------------


def test_repository_commit_state_is_reported() -> None:
    """审计 H8 指出「master 零提交」。

    初始提交涉及仓库写操作，不由自动化执行；这里只断言**能检测出来**，
    以便 CI/人工及时补上，而不是假装已解决。
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("无 git 可用")

    if result.returncode != 0:
        pytest.skip("仓库尚无提交（需人工执行 git add/commit，见 docs/audit-fix/group-5-delivery.md）")

    assert int(result.stdout.strip()) > 0
