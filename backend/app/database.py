"""数据库引擎与会话管理。

所有数据库操作通过 SQLAlchemy ORM；本模块不建立任何连接，直到首次被真正使用，
这样纯逻辑（如 FSRS 调度）可以在没有数据库的环境下被导入和测试。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Column, Engine, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings
from .logging_config import get_logger

logger = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
# 惰性单例的初始化锁：并发首次请求必须只创建一个引擎/会话工厂。
# 用两把独立的锁，且绝不嵌套获取（threading.Lock 不可重入）。
_engine_lock = threading.Lock()
_session_lock = threading.Lock()

# MySQL 连接参数：统一 utf8mb4
_CONNECT_ARGS = {"charset": "utf8mb4"}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def build_engine(database_url: str | None = None, **overrides) -> Engine:
    """按连接字符串构建引擎（不立即连接）。"""
    url = (database_url or settings.database_url or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未配置。请在 backend/.env 中设置 "
            "mysql+pymysql://<user>:<password>@<host>:<port>/<dbname>"
        )
    kwargs: dict = {
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "future": True,
        "connect_args": dict(_CONNECT_ARGS),
    }
    kwargs.update(overrides)
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    """进程内惰性单例引擎（**加锁**，避免并发首次请求创建多个连接池）。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            # 双重检查：等锁期间可能已有线程完成初始化
            if _engine is None:
                _engine = build_engine()
                logger.info("数据库引擎已创建：%s", _safe_url(_engine.url))
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """惰性单例会话工厂（加锁）。

    注意：必须在**进入锁之前**先取引擎。``get_engine()`` 自身要拿
    ``_engine_lock``，而 ``threading.Lock`` 不可重入 —— 在锁内调用它会自锁死。
    """
    global _session_factory
    if _session_factory is None:
        engine = get_engine()  # 先取引擎（其内部自行加锁，避免嵌套获取同一把锁）
        with _session_lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=engine,
                    autoflush=False,
                    autocommit=False,
                    expire_on_commit=False,
                )
    return _session_factory


def reset_engine_cache() -> None:
    """释放缓存的引擎/会话工厂（测试或多环境切换用）。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def _safe_url(url) -> str:
    """日志用：隐去密码。"""
    try:
        return url.render_as_string(hide_password=True)
    except Exception:  # pragma: no cover - 仅日志降级
        return "<unrenderable url>"


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话上下文：提交或回滚后关闭。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话。

    请求成功自动提交，抛出异常自动回滚 —— 路由层因此无需显式 commit，
    也不会因为忘记回滚而留下脏事务。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """建表（幂等）。需先导入所有模型以完成注册。

    注意：**生产环境应以 Alembic 迁移为准**（``alembic upgrade head``）。
    保留 ``create_all`` 只是为了本地开发开箱即用；它建表但不会写
    ``alembic_version``，因此新库建完后建议执行一次 ``alembic stamp head``。
    ``log_migration_status()`` 会在启动时把这种状态报出来。
    """
    from . import models  # noqa: F401  —— 触发模型注册

    Base.metadata.create_all(bind=get_engine())
    logger.info("数据库表结构已就绪：%s", ", ".join(sorted(Base.metadata.tables)))


def _alembic_version_table() -> Table:
    """迁移版本表的 Core 定义（避免手写原生 SQL）。"""
    return Table(
        "alembic_version",
        MetaData(),
        Column("version_num", String(255), primary_key=True),
    )


def migration_status() -> dict[str, str | None]:
    """检查 Alembic 迁移状态，返回 ``{"state": ..., "revision": ...}``。

    - ``unmanaged``：库中还没有任何表；
    - ``unstamped``：表已建但没有 ``alembic_version``（多由 create_all 产生）。
      此时直接 ``alembic upgrade head`` 会因表已存在而失败，需先 ``alembic stamp head``；
    - ``managed``：迁移已接管，``revision`` 为当前版本号。
    """
    from sqlalchemy import inspect as sa_inspect

    tables = set(sa_inspect(get_engine()).get_table_names())
    if not tables:
        return {"state": "unmanaged", "revision": None}
    if "alembic_version" not in tables:
        return {"state": "unstamped", "revision": None}

    stmt = select(_alembic_version_table().c.version_num)
    with session_scope() as session:
        revision = session.execute(stmt).scalar_one_or_none()
    return {"state": "managed", "revision": revision}


def log_migration_status() -> None:
    """把迁移状态写进日志，并在需要人工介入时给出明确指引。"""
    try:
        status = migration_status()
    except Exception:  # pragma: no cover - 数据库不可用时不影响启动
        logger.warning("无法检查数据库迁移状态", exc_info=True)
        return

    state = status["state"]
    if state == "managed":
        logger.info("数据库迁移版本：%s", status["revision"])
    elif state == "unstamped":
        logger.warning(
            "数据库表已存在但没有 alembic_version —— 迁移尚未接管。"
            "请执行一次：cd backend && ./venv/bin/alembic stamp head"
        )
    else:
        logger.info("数据库为空，将按模型建表；生产环境请改用 alembic upgrade head")
