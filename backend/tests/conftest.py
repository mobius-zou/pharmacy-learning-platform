"""pytest 全局配置。

数据库集成测试使用**独立的测试库**（默认在 DATABASE_URL 的库名后加 ``_test``），
并且绝不会连接到生产库；若本机没有可用的 MySQL，这些测试会被自动跳过，
而 FSRS 调度等纯逻辑测试始终运行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

# 允许测试读取使用者维护的 .env（仅通过程序读取，不打印内容）
load_dotenv(BACKEND_DIR / ".env", override=False)
load_dotenv(BACKEND_DIR / ".env.local", override=False)


# 允许复用业务库跑集成测试（显式 opt-in，且带「空表」护栏）
ALLOW_PROD_DB_TESTS = os.getenv("PHARMACY_ALLOW_PROD_DB_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _candidate_urls() -> tuple[str, str]:
    """返回 (优先的独立测试库 URL, 业务库 URL)。

    **隔离优先**：业务库复用（``PHARMACY_ALLOW_PROD_DB_TESTS=1``）只在
    独立测试库确实无法准备时才作为兜底启用，避免「允许标志」把更安全的
    隔离路径盖掉。
    """
    explicit = os.getenv("TEST_DATABASE_URL", "").strip()
    base = os.getenv("DATABASE_URL", "").strip()

    if explicit:
        return explicit, base

    if not base:
        return "", ""

    try:
        from sqlalchemy.engine import make_url

        url = make_url(base)
    except Exception:
        return "", base

    name = url.database or "pharmacy_db"
    derived = url.set(database=f"{name}_test").render_as_string(hide_password=False)
    return derived, base


TEST_DATABASE_URL, _PROD_DATABASE_URL = _candidate_urls()
USING_PROD_DB = False

# 安全闸门：独立测试库名必须与业务库名不同
if TEST_DATABASE_URL and TEST_DATABASE_URL != _PROD_DATABASE_URL:
    from sqlalchemy.engine import make_url as _make_url

    _prod_name = (_make_url(_PROD_DATABASE_URL).database or "") if _PROD_DATABASE_URL else ""
    _test_name = _make_url(TEST_DATABASE_URL).database or ""
    if _prod_name and _prod_name == _test_name:
        raise RuntimeError(
            f"测试库与生产库同名（{_test_name}），拒绝运行以免污染数据。"
            "请设置 TEST_DATABASE_URL 或让 DATABASE_URL 指向可丢弃的库。"
        )

# ChromaDB：测试使用独立目录，绝不触碰 backend/chroma_db 中的真实向量数据
import tempfile  # noqa: E402

_CHROMA_TMP = tempfile.mkdtemp(prefix="pharm-chroma-test-")
os.environ["CHROMA_DIR"] = _CHROMA_TMP

# 测试期间不做启动预热：TestClient 会触发 lifespan，预热会让每次测试进程都加载模型
os.environ.setdefault("ST_WARMUP_ON_STARTUP", "false")


def _prepare_isolated_database() -> str | None:
    """准备独立测试库；成功返回 None，失败返回原因。

    只有这一步失败时，才考虑（且必须显式允许）退回复用业务库。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DATABASE_URL)
    dbname = url.database
    if not dbname:
        return "测试连接字符串缺少数据库名"

    server_url = url.set(database=None)
    # 仅用于测试库引导的 DDL（业务代码不写原生 SQL）
    engine = create_engine(server_url, connect_args={"charset": "utf8mb4"})
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
            conn.commit()
    finally:
        engine.dispose()
    return None


def _check_connectable(url: str) -> str | None:
    from sqlalchemy import create_engine, text

    engine = create_engine(url, connect_args={"charset": "utf8mb4"})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return None


def _ensure_test_database() -> tuple[bool, str, str, bool]:
    """准备测试库，返回 ``(可用, 原因, 使用的 URL, 是否复用业务库)``。

    顺序：独立测试库 → （显式允许时）业务库兜底 → 跳过。
    """
    if not TEST_DATABASE_URL:
        return (
            False,
            "未配置 DATABASE_URL / TEST_DATABASE_URL，跳过 MySQL 集成测试。"
            "请在 backend/.env 中配置 DATABASE_URL。",
            "",
            False,
        )

    isolated = TEST_DATABASE_URL != _PROD_DATABASE_URL
    if isolated:
        try:
            reason = _prepare_isolated_database()
            if reason is None:
                reason = _check_connectable(TEST_DATABASE_URL)
        except Exception as exc:  # 无权限建库 / 无 MySQL / 网络不可达
            reason = f"{type(exc).__name__}: {exc}"

        if reason is None:
            return True, "", TEST_DATABASE_URL, False

        if not ALLOW_PROD_DB_TESTS:
            return (
                False,
                f"独立测试库不可用（{reason}）。"
                "请让数据库账号有权创建 <dbname>_test，或设置 TEST_DATABASE_URL；"
                "确实要在业务库上跑测试时，显式设置 PHARMACY_ALLOW_PROD_DB_TESTS=1。",
                "",
                False,
            )

        # 显式允许时退回复用业务库（db_engine 中仍有空表护栏）
        try:
            reason = _check_connectable(_PROD_DATABASE_URL)
        except Exception as exc:
            return False, f"MySQL 不可用：{type(exc).__name__}: {exc}", "", False
        if reason is not None:
            return False, reason, "", False
        return True, "", _PROD_DATABASE_URL, True

    try:
        reason = _check_connectable(TEST_DATABASE_URL)
    except Exception as exc:
        return False, f"MySQL 不可用，跳过集成测试：{type(exc).__name__}: {exc}", "", False
    if reason is not None:
        return False, reason, "", False
    return True, "", TEST_DATABASE_URL, False


DB_AVAILABLE, DB_SKIP_REASON, TEST_DATABASE_URL, USING_PROD_DB = _ensure_test_database()

if TEST_DATABASE_URL:
    # 让应用在测试期间指向测试库（必须在导入 app.config 之前设置）
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason=DB_SKIP_REASON or "需要 MySQL")


# --- fixtures ---------------------------------------------------------------


def _guard_existing_data(engine) -> None:
    """复用业务库时的护栏：只要**应用自己的表**有数据就立即中止，绝不清空。

    只检查 ``Base.metadata`` 里的表 —— 基础设施表（如 alembic_version）
    本来就该有内容，也不在 drop/truncate 的范围内。
    """
    from sqlalchemy import func, inspect, select

    from app import models  # noqa: F401
    from app.database import Base

    existing = set(inspect(engine).get_table_names())
    app_tables = set(Base.metadata.tables)
    populated: list[str] = []
    with engine.connect() as conn:
        for name, table in sorted(Base.metadata.tables.items()):
            if name not in existing:
                continue
            count = conn.execute(select(func.count()).select_from(table)).scalar() or 0
            if count:
                populated.append(f"{name}({count})")

    if populated:
        raise RuntimeError(
            "检测到业务库中已有数据，拒绝运行集成测试以免清空："
            + "、".join(populated)
            + "。请改用独立的 TEST_DATABASE_URL。"
        )
    print(
        f"\n[护栏] 应用表均为空，继续运行：{', '.join(sorted(app_tables & existing))}"
    )


def _build_schema_via_migrations(engine) -> None:
    """用 **Alembic 迁移**建表，而不是 create_all。

    这样整轮集成测试都在「迁移产出的 schema」上运行，
    ``test_migrations.py`` 的 drift 检查才真正验证了迁移本身，
    而不是拿 create_all 的结果自我印证。
    """
    from alembic import command
    from alembic.config import Config

    from app.database import Base, _alembic_version_table

    # 清掉应用表与迁移版本表，回到「全新库」状态
    Base.metadata.drop_all(bind=engine)
    _alembic_version_table().drop(bind=engine, checkfirst=True)

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def db_engine():
    """会话级测试引擎：建表一次，结束后清理。"""
    if not DB_AVAILABLE:
        pytest.skip(DB_SKIP_REASON)

    from app import models  # noqa: F401  注册模型
    from app.database import Base, build_engine

    engine = build_engine(TEST_DATABASE_URL)

    if USING_PROD_DB:
        print(
            "\n[警告] 正在复用业务库运行集成测试 "
            "(PHARMACY_ALLOW_PROD_DB_TESTS=1)，仅允许在空库上执行。"
        )
        _guard_existing_data(engine)

    _build_schema_via_migrations(engine)
    try:
        yield engine
    finally:
        if USING_PROD_DB:
            # 复用业务库：保留表结构（应用启动即可用），只清空数据
            from sqlalchemy import delete

            with engine.begin() as conn:
                for table in reversed(Base.metadata.sorted_tables):
                    conn.execute(delete(table))
        else:
            # 连同迁移版本表一起删除：只删应用表会留下「已 stamp 到 head」
            # 的脏状态，下一轮 upgrade head 会空转，schema 根本不会建出来。
            from app.database import _alembic_version_table

            Base.metadata.drop_all(bind=engine)
            _alembic_version_table().drop(bind=engine, checkfirst=True)
        engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """函数级会话：每个测试结束后清空所有表。"""
    from sqlalchemy import delete
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        # 清空数据，保证测试之间互不影响
        from app.database import Base

        with db_engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(delete(table))


@pytest.fixture
def client(db_session):
    """FastAPI 测试客户端，数据库依赖被替换为测试会话。"""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def user_credentials() -> dict[str, str]:
    import uuid

    return {
        "username": f"tester_{uuid.uuid4().hex[:12]}",
        "password": "test-password-123",
    }


@pytest.fixture
def auth_headers(client, user_credentials) -> dict[str, str]:
    """注册并登录一个随机用户，返回带 token 的请求头。"""
    resp = client.post("/api/v1/auth/register", json=user_credentials)
    assert resp.status_code == 201, resp.text

    resp = client.post("/api/v1/auth/login", json=user_credentials)
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """每个用例前重置进程内限流器。

    限流按「用户名/客户端地址」计数，而测试全部来自同一个测试客户端地址，
    不重置的话第 6 个用例起就会 429（这不是限流逻辑的问题，是测试隔离问题）。
    """
    from app.ratelimit import reset_login_limiter, reset_register_limiter

    reset_login_limiter(None)
    reset_register_limiter(None)
    yield
    reset_login_limiter(None)
    reset_register_limiter(None)


@pytest.fixture
def user_id(client, auth_headers) -> int:
    """当前登录用户的 id（多个测试模块共用）。"""
    return client.get("/api/v1/auth/me", headers=auth_headers).json()["id"]


@pytest.fixture(scope="session", autouse=True)
def _cleanup_chroma_tmp():
    """会话结束后清理测试用的 ChromaDB 临时目录。"""
    yield
    import shutil

    shutil.rmtree(_CHROMA_TMP, ignore_errors=True)
