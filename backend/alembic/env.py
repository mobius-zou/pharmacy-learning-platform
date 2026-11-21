"""Alembic 运行环境。

要点
----
1. **不硬编码连接串**：通过 ``app.config.settings.database_url``
   （即 ``os.getenv("DATABASE_URL")``）获取，与运行时代码用同一份配置；
2. **不读取 .env 内容**：配置由 ``app.config`` 内部用 python-dotenv 加载，
   本文件只消费已解析好的 URL；
3. 支持 ``-x db_url=...`` 覆盖（便于在独立测试库上验证迁移）。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 `alembic` 命令能 import 到 app 包
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: E402,F401  —— 必须导入以注册全部表

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False 很关键：fileConfig 默认会**禁用已存在的 logger**，
    # 而 env.py 可能在同一进程内被调用（比如测试或应用内执行迁移），
    # 一旦禁用，应用自己的 logger（app.*）会静默失效，日志全丢。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# autogenerate 的目标元数据
target_metadata = Base.metadata


def _database_url() -> str:
    """优先取命令行 -x db_url=...，否则用应用配置。"""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    url = (override or settings.database_url or "").strip()
    if not url:
        raise RuntimeError(
            "未配置 DATABASE_URL，无法执行迁移。"
            "请在 backend/.env 中设置，或使用 alembic -x db_url=... 指定。"
        )
    return url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"charset": "utf8mb4"},
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
