"""日志配置：同时输出到控制台与 backend/logs/app.log。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False
# 本模块安装过的 handler：重复调用时只清理自己，不影响宿主环境的日志配置
_own_handlers: list[logging.Handler] = []


def configure_logging(log_file: Path | None = None) -> None:
    """初始化根 logger（幂等）。"""
    global _configured
    if _configured:
        return

    target = Path(log_file) if log_file is not None else settings.log_file
    target.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(settings.log_level)

    # 只移除**本模块自己**先前安装的 handler，绝不清空 root.handlers。
    # 早期实现直接清空 root handlers，会把宿主环境（pytest caplog、uvicorn、
    # 上层应用的日志采集）安装的 handler 一起干掉，导致日志丢失且无法测试。
    for handler in list(_own_handlers):
        if handler in root.handlers:
            root.removeHandler(handler)
        if handler in _own_handlers:
            _own_handlers.remove(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    _own_handlers.append(stream_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        target,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    _own_handlers.append(file_handler)

    # SQLAlchemy 的 INFO 日志过于嘈杂
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger。"""
    return logging.getLogger(name)
