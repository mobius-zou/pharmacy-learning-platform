"""测试库隔离测试（批次 5）。

审计发现：集成测试默认被跳过 151 项，只能靠 ``PHARMACY_ALLOW_PROD_DB_TESTS=1``
在**业务库**上跑。现在改为「隔离优先」：

1. 默认使用派生出的 ``<dbname>_test`` 独立库，测试全量运行、0 跳过；
2. 业务库复用降级为**兜底**，且必须显式设置开关，还要过空表护栏；
3. 任何情况下测试库都不能与业务库同名。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url

from tests import conftest
from tests.conftest import requires_db


def _db_name(url: str) -> str:
    return make_url(url).database or ""


# --- 选择逻辑的不变量（无需数据库） -----------------------------------------


def test_production_fallback_requires_explicit_opt_in() -> None:
    """兜底开关默认必须是关闭的，不能悄悄在业务库上跑测试。"""
    assert conftest.ALLOW_PROD_DB_TESTS is False or os.getenv(
        "PHARMACY_ALLOW_PROD_DB_TESTS"
    ), "业务库兜底必须显式开启"


def test_candidate_urls_prefers_isolated_database(monkeypatch) -> None:
    """即使开启了兜底开关，候选顺序仍以独立测试库为先。"""
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@h:3306/pharmacy_db")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    preferred, production = conftest._candidate_urls()

    assert _db_name(preferred) == "pharmacy_db_test"
    assert _db_name(production) == "pharmacy_db"


def test_candidate_urls_respects_explicit_test_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@h:3306/pharmacy_db")
    monkeypatch.setenv("TEST_DATABASE_URL", "mysql+pymysql://u:p@h:3306/scratch_db")

    preferred, production = conftest._candidate_urls()

    assert _db_name(preferred) == "scratch_db"
    assert _db_name(production) == "pharmacy_db"


def test_candidate_urls_without_database_url(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    assert conftest._candidate_urls() == ("", "")


# --- 实际生效的隔离（需要数据库） -------------------------------------------


@requires_db
@pytest.mark.db
def test_tests_run_on_isolated_database() -> None:
    """默认必须跑在独立测试库上，而不是业务库。"""
    assert conftest.DB_AVAILABLE, conftest.DB_SKIP_REASON
    assert conftest.USING_PROD_DB is False, (
        "独立测试库可用时不应退回复用业务库；"
        f"当前 TEST_DATABASE_URL={conftest.TEST_DATABASE_URL}"
    )

    test_name = _db_name(conftest.TEST_DATABASE_URL)
    production_name = _db_name(conftest._PROD_DATABASE_URL)

    assert test_name, "测试库名不能为空"
    assert test_name != production_name, "测试库不能与业务库同名"
    assert test_name.endswith("_test"), f"测试库应形如 <dbname>_test，实际 {test_name}"


@requires_db
@pytest.mark.db
def test_application_engine_points_at_test_database() -> None:
    """应用在测试期间连接的必须是测试库。"""
    from app.database import get_engine

    engine_db = _db_name(str(get_engine().url))
    test_db = _db_name(conftest.TEST_DATABASE_URL)

    assert engine_db == test_db, "应用引擎没有指向测试库，会污染业务数据"


@requires_db
@pytest.mark.db
def test_production_database_is_not_touched_by_suite() -> None:
    """抽查业务库：应用表应当仍为空（测试全程不该写它）。

    这条断言的意义在于「测试不会污染业务库」，而不是断言业务库必须为空 ——
    如果用户自己往业务库里写了数据，本测试不应该失败，因此只在
    **业务库与测试库不同** 且能连上时做宽松检查。
    """
    from sqlalchemy import func, select

    from app.database import build_engine
    from app.models import User

    if not conftest._PROD_DATABASE_URL:
        pytest.skip("未配置业务库连接")

    production_name = _db_name(conftest._PROD_DATABASE_URL)
    if production_name == _db_name(conftest.TEST_DATABASE_URL):
        pytest.skip("测试与业务共用同一个库")

    # 测试用的用户名都带 tester_ 前缀，业务库里不应出现它们
    engine = build_engine(conftest._PROD_DATABASE_URL)
    try:
        with engine.connect() as conn:
            tester_count = conn.execute(
                select(func.count()).select_from(User).where(User.username.like("tester\\_%"))
            ).scalar_one()
    finally:
        engine.dispose()

    assert tester_count == 0, (
        f"业务库 {production_name} 中出现了测试账号（{tester_count} 个），说明测试写到了业务库"
    )
