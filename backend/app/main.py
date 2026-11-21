"""FastAPI 应用入口。

启动：
    cd backend && ./venv/bin/uvicorn app.main:app --reload
文档：
    http://127.0.0.1:8000/docs
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import __version__
from .api.v1.router import api_router
from .config import settings, validate_settings
from .database import create_all, log_migration_status
from .errors import error_payload, register_exception_handlers
from .logging_config import configure_logging, get_logger
from .middleware import MaxBodySizeMiddleware

configure_logging()
logger = get_logger(__name__)


def _check_env_permissions() -> None:
    """检查 .env 权限：只 stat 元数据，不读取内容（项目约束：不读 .env）。

    640/644 意味着同机其他本地用户可以读到数据库密码与 JWT 密钥。
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    try:
        mode = env_file.stat().st_mode
    except OSError:
        return  # 文件不存在是允许的（可用真实环境变量）

    if mode & 0o077:
        logger.warning(
            "backend/.env 权限为 %o，同机其他用户可读（含数据库密码 / JWT 密钥）。"
            "建议执行：chmod 600 backend/.env",
            mode & 0o777,
        )

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭钩子。"""
    logger.info("=" * 60)
    logger.info("药学学习平台后端启动 (v%s)", __version__)

    if settings.jwt_secret_is_ephemeral:
        logger.warning(
            "JWT_SECRET 未配置，已生成进程内临时密钥。"
            "重启后旧 token 全部失效；请在 backend/.env 中设置 JWT_SECRET。"
        )

    # H1：生产环境禁止使用进程内临时 JWT 密钥（重启即全员掉线、多 worker 互相 401）
    if settings.jwt_secret_is_ephemeral and settings.environment == "production":
        raise RuntimeError(
            "生产环境必须显式配置 JWT 密钥（JWT_SECRET 或 JWT_SECRET_KEY）。"
            "当前会回退到进程内随机密钥，导致重启后全部 token 失效、多 worker 之间随机 401。"
        )
    if settings.jwt_secret_is_ephemeral:
        logger.warning(
            "JWT 密钥未配置（已兼容 JWT_SECRET / JWT_SECRET_KEY / SECRET_KEY），"
            "当前使用进程内临时密钥。重启后旧 token 全部失效；生产环境请务必配置。"
        )

    _check_env_permissions()

    # 配置自检：错配往往表现为「随机 401」「CORS 报错」等难查现象，启动时集中暴露
    for problem in validate_settings(settings):
        logger.error("配置问题：%s", problem)

    if not settings.database_url:
        logger.error(
            "DATABASE_URL 未配置，数据库功能不可用。"
            "请在 backend/.env 中设置 mysql+pymysql://<user>:<password>@<host>:<port>/<dbname>"
        )
    else:
        try:
            if settings.auto_create_schema:
                # 仅本地开发便利开关；生产必须走 Alembic（见 README「数据库迁移」）
                logger.warning(
                    "AUTO_CREATE_SCHEMA=true：正在用 create_all 建表。"
                    "该模式与 Alembic 双轨，生产环境请关闭并改用 alembic upgrade head。"
                )
                create_all()
            # 报告迁移状态；未接管/未建表时给出可执行指引
            log_migration_status()
        except Exception:
            logger.exception("初始化数据库表结构失败，请检查 DATABASE_URL 与 MySQL 服务")

    # 后台预热语义模型：避免首个「考点匹配」请求同步等待十几秒（甚至超时）。
    # 放在后台线程里，启动不被阻塞；预热失败也不影响服务（首次使用时重试）。
    if settings.st_warmup_on_startup:
        from .embeddings import warmup_async

        warmup_async()
        logger.info("已在后台线程预热嵌入后端（ST_WARMUP_ON_STARTUP=true）")

    logger.info("FSRS desired_retention=%s", settings.fsrs_desired_retention)
    logger.info("API 前缀 %s", settings.api_prefix)
    yield
    logger.info("药学学习平台后端关闭")


app = FastAPI(
    title="药学学习平台 API",
    description=(
        "阶段一：复习面板 MVP。FSRS-6 间隔重复调度闭环 —— "
        "录入卡片 → 到期查询 → 评分 → 更新调度。"
    ),
    version=__version__,
    lifespan=lifespan,
)

# 请求体大小限制（Starlette 默认不限，超大 body 会直接吃满内存）
app.add_middleware(MaxBodySizeMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", tags=["系统"], summary="服务信息")
async def root() -> dict:
    """返回服务基本信息。"""
    return {
        "name": "药学学习平台 API",
        "version": __version__,
        "api_prefix": settings.api_prefix,
        "docs": "/docs",
    }


@app.get("/health", tags=["系统"], summary="健康检查")
async def health() -> dict:
    """健康检查（不访问数据库，保证在数据库故障时仍可探活）。"""
    return {
        "status": "ok",
        "database_configured": bool(settings.database_url),
        "fsrs_desired_retention": settings.fsrs_desired_retention,
    }


@app.get("/ui/literature", include_in_schema=False)
async def literature_ui() -> FileResponse:
    """文献工作台的人工校对界面（自包含单页，无需构建步骤）。

    阶段三要求「必须有人工校对界面」。这里用零依赖的静态页面实现，
    后续可用 Vue 前端替换而不影响后端接口。
    """
    return FileResponse(STATIC_DIR / "literature_review.html", media_type="text/html")


# 供测试/调试引用
__all__ = ["app", "error_payload"]
