"""数据库迁移（Alembic）测试（批次 2）。

覆盖：
1. 迁移脚本与模型定义**完全一致**（无 drift）—— 这是「迁移可用」的硬条件；
2. 迁移生成的 DDL 保留了阶段一/二的关键细节（DATETIME(6)、utf8mb4、CASCADE）；
3. ``migration_status()`` 能正确区分 unmanaged / unstamped / managed 三种状态，
   其中 ``unstamped``（create_all 建表但没写 alembic_version）会让
   ``alembic upgrade head`` 直接失败，属于必须被发现的部署坑。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.database as database
from tests.conftest import requires_db

BACKEND_DIR = Path(__file__).resolve().parent.parent
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_ENV = BACKEND_DIR / "alembic" / "env.py"


def _migration_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))


# --- 文件与配置（无需数据库） -----------------------------------------------


def test_alembic_layout_exists() -> None:
    assert ALEMBIC_INI.is_file(), "缺少 alembic.ini"
    assert ALEMBIC_ENV.is_file(), "缺少 alembic/env.py"
    assert _migration_files(), "缺少迁移脚本"


def test_env_does_not_hardcode_credentials() -> None:
    """连接串必须来自环境变量/应用配置，不能硬编码。"""
    env_source = ALEMBIC_ENV.read_text(encoding="utf-8")
    ini_source = ALEMBIC_INI.read_text(encoding="utf-8")

    assert "os.getenv" in env_source or "settings.database_url" in env_source
    # ini 里不能出现带账号密码的 URL
    assert not re.search(r"mysql\+pymysql://[^\s]*:[^\s]*@", ini_source), (
        "alembic.ini 中不应出现含凭据的连接串"
    )
    assert "sqlalchemy.url" not in ini_source.lower() or "sqlalchemy.url =" not in ini_source, (
        "alembic.ini 不应写死 sqlalchemy.url"
    )


def _script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_migration_chain_is_linear_with_single_baseline() -> None:
    """迁移链必须是一条直线：唯一基线 + 单一 head（无分叉、无重复父节点）。

    用 Alembic 自己的 ScriptDirectory 解析，比正则匹配文件内容可靠。
    """
    script = _script_directory()

    heads = script.get_heads()
    assert len(heads) == 1, f"迁移链出现分叉，heads={heads}"

    revisions = list(script.walk_revisions())
    baselines = [rev for rev in revisions if rev.down_revision is None]
    assert len(baselines) == 1, f"应只有 1 个基线迁移，实际 {[r.revision for r in baselines]}"

    # 每个 revision 只能被一个下游引用（线性）
    parents = [rev.down_revision for rev in revisions if rev.down_revision is not None]
    assert len(parents) == len(set(parents)), "存在多个迁移共用同一个 down_revision（分叉）"
    assert len(parents) == len(revisions) - 1, "迁移链不连续"


def test_every_revision_declares_type_annotations() -> None:
    """模板产出的脚本应带注解，便于 mypy/阅读。"""
    for path in _migration_files():
        source = path.read_text(encoding="utf-8")
        assert "revision: str =" in source
        assert "def upgrade() -> None:" in source
        assert "def downgrade() -> None:" in source


def test_migration_preserves_microsecond_datetime_and_charset() -> None:
    """回归：MySQL DATETIME 默认秒级且会四舍五入，必须保持 DATETIME(6)。"""
    # 基线迁移里包含全部建表语句，取含 create_table 的那个文件
    source = next(
        p.read_text(encoding="utf-8")
        for p in _migration_files()
        if "create_table" in p.read_text(encoding="utf-8")
    )

    assert "fsp=6" in source, "迁移丢了 DATETIME(6) 精度"
    assert "mysql_charset='utf8mb4'" in source
    assert "mysql_engine='InnoDB'" in source


def test_migration_preserves_fk_cascade() -> None:
    source = next(
        p.read_text(encoding="utf-8")
        for p in _migration_files()
        if "create_table" in p.read_text(encoding="utf-8")
    )
    assert "ondelete='CASCADE'" in source


def test_migration_creates_all_model_tables() -> None:
    from app.database import Base
    from app import models  # noqa: F401

    source = next(
        p.read_text(encoding="utf-8")
        for p in _migration_files()
        if "create_table" in p.read_text(encoding="utf-8")
    )
    for table in Base.metadata.tables:
        assert f"create_table('{table}'" in source, f"迁移缺少表 {table}"


def test_migration_has_downgrade() -> None:
    """必须能回滚，否则线上出问题只能靠手改。"""
    source = next(
        p.read_text(encoding="utf-8")
        for p in _migration_files()
        if "create_table" in p.read_text(encoding="utf-8")
    )
    downgrade = source.split("def downgrade() -> None:", 1)[-1]
    assert "drop_table" in downgrade


# --- 迁移与模型一致性（需要数据库） -----------------------------------------


@requires_db
@pytest.mark.db
def test_no_schema_drift_between_models_and_migrations() -> None:
    """``alembic check`` 必须报告没有待生成的变更。

    这是「迁移可用」的核心断言：模型改了但忘了写迁移，这里会立刻失败。
    """
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.database import Base, build_engine
    from app import models  # noqa: F401

    engine = build_engine()
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"模型与迁移不一致，需要新增迁移：{diff}"


@requires_db
@pytest.mark.db
def test_migration_status_reports_managed() -> None:
    from app.database import migration_status

    status = migration_status()

    assert status["state"] == "managed", f"测试库应已由迁移接管，实际 {status}"
    assert status["revision"], "应有当前迁移版本号"


# --- migration_status 的三种状态（用替身模拟，不依赖真实库状态） ------------


class _FakeInspector:
    def __init__(self, tables: list[str]) -> None:
        self._tables = tables

    def get_table_names(self) -> list[str]:
        return list(self._tables)


def _patch_inspector(monkeypatch, tables: list[str]) -> None:
    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "inspect", lambda _engine: _FakeInspector(tables))
    monkeypatch.setattr(database, "get_engine", lambda: object())


def test_migration_status_unmanaged_on_empty_database(monkeypatch) -> None:
    """空库：还没有任何表。"""
    _patch_inspector(monkeypatch, [])

    assert database.migration_status() == {"state": "unmanaged", "revision": None}


def test_migration_status_unstamped_when_tables_but_no_version(monkeypatch) -> None:
    """create_all 建了表但没写 alembic_version —— 必须先 stamp，否则 upgrade 会失败。"""
    _patch_inspector(monkeypatch, ["users", "cards", "review_logs"])

    assert database.migration_status() == {"state": "unstamped", "revision": None}


def test_log_migration_status_warns_when_unstamped(monkeypatch, caplog) -> None:
    """未接管时必须给出可执行的指引，而不是静默通过。"""
    import logging

    _patch_inspector(monkeypatch, ["users", "cards"])

    with caplog.at_level(logging.WARNING):
        database.log_migration_status()

    assert any("alembic stamp head" in record.message for record in caplog.records)


def test_log_migration_status_swallows_db_errors(monkeypatch, caplog) -> None:
    """数据库不可用时只记录告警，不能影响服务启动。"""
    import logging

    def exploding():
        raise RuntimeError("数据库连不上")

    monkeypatch.setattr(database, "get_engine", exploding)

    with caplog.at_level(logging.WARNING):
        database.log_migration_status()  # 不应抛出

    assert any("无法检查数据库迁移状态" in record.message for record in caplog.records)
