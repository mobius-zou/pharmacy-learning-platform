"""应用配置。

统一从环境变量读取配置，禁止在代码中硬编码任何密码、密钥或连接字符串。
`.env` 由使用者自行维护；本模块只通过 python-dotenv 加载它，不读取、不修改、不生成。
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# backend/ 目录
BACKEND_DIR = Path(__file__).resolve().parent.parent

# 加载使用者维护的 .env（若存在）。override=False：真实环境变量优先。
load_dotenv(BACKEND_DIR / ".env", override=False)
load_dotenv(BACKEND_DIR / ".env.local", override=False)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    """运行期配置快照。"""

    def __init__(self) -> None:
        # --- 数据库 ---
        # 连接字符串格式：mysql+pymysql://<user>:<password>@<host>:<port>/<dbname>
        self.database_url: str = os.getenv("DATABASE_URL", "").strip()
        # 是否在启动时 create_all 建表。
        # **默认关闭**：表结构以 Alembic 迁移为唯一事实来源；开启会与迁移形成双轨，
        # 导致「新库先建表 → alembic upgrade head 报”表已存在”」。
        self.auto_create_schema: bool = _env_bool("AUTO_CREATE_SCHEMA", False)

        # --- 运行环境 ---
        # production 下会做更严格的 fail-fast 校验（如禁止临时 JWT 密钥）
        self.environment: str = (
            os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).strip().lower()
            or "development"
        )

        # --- 认证 ---
        # 密钥键名兼容：JWT_SECRET / JWT_SECRET_KEY / SECRET_KEY。
        # 早期只认 JWT_SECRET，而 .env 里写的是 JWT_SECRET_KEY，导致静默回退到
        # 进程内随机密钥（重启后全部 token 失效、多 worker 互相 401）。
        provided_secret = (
            os.getenv("JWT_SECRET", "").strip()
            or os.getenv("JWT_SECRET_KEY", "").strip()
            or os.getenv("SECRET_KEY", "").strip()
        )
        self.jwt_secret_is_ephemeral: bool = not provided_secret
        self.jwt_secret: str = provided_secret or secrets.token_urlsafe(48)
        self.jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256").strip() or "HS256"
        self.jwt_expire_minutes: int = _env_int("JWT_EXPIRE_MINUTES", 60 * 24)
        self.password_max_bytes: int = 72  # bcrypt 硬上限
        # 登录失败限流（进程内滑动窗口；多 worker 时阈值约为 配置值 × worker 数）
        self.login_max_attempts: int = max(1, _env_int("LOGIN_MAX_ATTEMPTS", 5))
        self.login_window_seconds: float = _env_float("LOGIN_WINDOW_SECONDS", 300.0)
        self.login_lockout_seconds: float = _env_float("LOGIN_LOCKOUT_SECONDS", 300.0)
        # 同一 IP 在窗口内最多注册几个账号。
        # 默认取 20：5 个会让「连跑 5 个验收脚本 / CI 多轮」直接撞 429，
        # 而注册限流的目的只是拦住脚本批量刷号，20/5min 已足够。
        # 生产环境可按需调低（REGISTER_MAX_PER_WINDOW）。
        self.register_max_per_window: int = max(1, _env_int("REGISTER_MAX_PER_WINDOW", 20))
        # 请求体大小上限（字节）。multipart 上传走各自的端点级限制
        self.max_request_body_bytes: int = max(
            1024, _env_int("MAX_REQUEST_BODY_BYTES", 2 * 1024 * 1024)
        )
        # 文本文件导入上限（字节）
        self.max_text_file_bytes: int = max(
            1024, _env_int("MAX_TEXT_FILE_BYTES", 5 * 1024 * 1024)
        )
        # multipart 请求的总上限（图片端点自身是 8MiB，故默认略高）
        self.max_multipart_bytes: int = max(
            1024, _env_int("MAX_MULTIPART_BYTES", 12 * 1024 * 1024)
        )

        # --- FSRS 调度 ---
        self.fsrs_desired_retention: float = _env_float("FSRS_DESIRED_RETENTION", 0.9)
        self.fsrs_enable_fuzzing: bool = _env_bool("FSRS_ENABLE_FUZZING", True)
        self.fsrs_maximum_interval: int = _env_int("FSRS_MAXIMUM_INTERVAL", 36500)
        # 幂等记录保留时长：客户端重发通常在秒级，24h 足够且不会让表无限增长
        self.idempotency_ttl_seconds: int = max(60, _env_int("IDEMPOTENCY_TTL_SECONDS", 86400))
        # 补录复习时间最多可提前多少天（防止伪造调度；日常复习留足余量）
        self.review_max_future_days: int = max(0, _env_int("REVIEW_MAX_FUTURE_DAYS", 365))

        # --- 错题诊断（阶段二）---
        # 同一知识点累计错误次数达到该阈值即触发薄弱点警报
        self.weak_point_threshold: int = max(1, _env_int("WEAK_POINT_THRESHOLD", 2))

        # --- LLM（可选，用于知识点识别与苏格拉底式提问）---
        # 未配置时进入「无 Agent 降级模式」：使用本地启发式，核心功能不受影响。
        self.llm_api_base: str = os.getenv("LLM_API_BASE", "").strip().rstrip("/")
        self.llm_api_key: str = os.getenv("LLM_API_KEY", "").strip()
        self.llm_model: str = os.getenv("LLM_MODEL", "").strip()
        self.llm_timeout_seconds: float = _env_float("LLM_TIMEOUT_SECONDS", 15.0)
        self.llm_enabled: bool = bool(self.llm_api_base and self.llm_model)

        # --- 文献工作台（阶段三）---
        # ChromaDB 持久化目录（文献向量 + 待校对草稿）
        self.chroma_dir: Path = Path(
            os.getenv("CHROMA_DIR", str(BACKEND_DIR / "chroma_db"))
        )
        self.chroma_collection_literature: str = (
            os.getenv("CHROMA_COLLECTION_LITERATURE", "literature").strip() or "literature"
        )
        self.chroma_collection_drafts: str = (
            os.getenv("CHROMA_COLLECTION_DRAFTS", "literature_drafts").strip()
            or "literature_drafts"
        )
        # 本地嵌入维度（字符 n-gram 哈希，离线可用、跨进程稳定）
        self.embedding_dim: int = max(32, _env_int("EMBEDDING_DIM", 256))

        # PubMed E-utilities（api_key / email 可选，用于提高速率上限）
        self.pubmed_base_url: str = (
            os.getenv("PUBMED_BASE_URL", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils").strip()
            or "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        )
        self.pubmed_api_key: str = os.getenv("PUBMED_API_KEY", "").strip()
        self.pubmed_email: str = os.getenv("PUBMED_EMAIL", "").strip()
        self.pubmed_tool: str = os.getenv("PUBMED_TOOL", "pharmacy-learning-platform").strip()
        self.pubmed_timeout_seconds: float = _env_float("PUBMED_TIMEOUT_SECONDS", 20.0)
        # 单次 E-utilities 响应的字节上限：PubMed 正常情况下 efetch 只有几十 KB，
        # 给 8MiB 足够宽裕，同时避免异常/恶意响应把内存吃满（审计 M16）。
        self.pubmed_max_response_bytes: int = max(
            1024, _env_int("PUBMED_MAX_RESPONSE_BYTES", 8 * 1024 * 1024)
        )

        # 知识图谱抽取上限（防止 LLM 产出爆炸）
        self.kg_max_triples: int = max(1, _env_int("KG_MAX_TRIPLES", 80))

        # --- 考点匹配（阶段五）---
        # 匹配打分权重：最终分 = 语义相似度 * semantic + 编辑距离相似度 * edit
        self.exam_match_semantic_weight: float = _env_float("EXAM_MATCH_SEMANTIC_WEIGHT", 0.6)
        self.exam_match_edit_weight: float = _env_float("EXAM_MATCH_EDIT_WEIGHT", 0.4)
        # 低于该分数视为未匹配
        self.exam_match_min_score: float = _env_float("EXAM_MATCH_MIN_SCORE", 0.42)
        # 编辑距离初筛保留的候选数量（精筛只在这些候选上做）
        self.exam_match_prefilter_limit: int = max(1, _env_int("EXAM_MATCH_PREFILTER_LIMIT", 30))
        # 卡片→知识点关联的匹配策略（H6 实测后的选择）：
        #   never    —— 只按 chapters 标签成员匹配（走多值索引，最快，实测 5.7ms/2万卡）
        #   fallback —— 先按标签精确匹配；无命中时回落到文本全表扫描（默认，兼顾覆盖率）
        #   always   —— 始终做文本扫描的并集（旧行为，最慢，实测 32.7ms/2万卡）
        self.related_cards_match_mode: str = (
            os.getenv("RELATED_CARDS_MATCH_MODE", "fallback").strip().lower() or "fallback"
        )
        # OCR 并发上限：Tesseract 是**子进程**，并发过高会把 CPU 打满并拖慢所有请求。
        # 超出上限的图片导入会短暂排队，排队超时则返回 503 + 重试建议。
        self.ocr_max_concurrency: int = max(1, _env_int("OCR_MAX_CONCURRENCY", 2))
        self.ocr_queue_timeout_seconds: float = max(
            0.0, _env_float("OCR_QUEUE_TIMEOUT_SECONDS", 20.0)
        )
        # 一次导入最多处理的考点数量
        self.exam_max_points_per_import: int = max(1, _env_int("EXAM_MAX_POINTS_PER_IMPORT", 200))
        # 一次批量录入的最多错题行数（防止单请求生成数万行 + 数百次 LLM 调用）
        self.wrong_question_bulk_max_lines: int = max(
            1, _env_int("WRONG_QUESTION_BULK_MAX_LINES", 500)
        )
        # FSRS 稳定性达到该值（天）且无历史错误 => 视为「已掌握」
        self.exam_mastered_stability: float = _env_float("EXAM_MASTERED_STABILITY", 21.0)

        # sentence-transformers 模型名（为空则只用离线嵌入）
        self.st_model_name: str = os.getenv(
            "ST_MODEL_NAME", "paraphrase-multilingual-MiniLM-L12-v2"
        ).strip()
        # 是否允许下载模型（离线环境可设为 false）
        self.st_allow_download: bool = _env_bool("ST_ALLOW_DOWNLOAD", True)
        # 是否启用语义精筛（关闭后只用编辑距离 + 离线哈希，启动更快）
        self.st_enabled: bool = _env_bool("ST_ENABLED", True)
        # 启动时后台预热嵌入模型（避免首个匹配请求同步等待模型加载）
        self.st_warmup_on_startup: bool = _env_bool("ST_WARMUP_ON_STARTUP", True)

        # HuggingFace 模型缓存与镜像。
        # 本机无法直连 huggingface.co（DNS 不通），默认走 hf-mirror.com 镜像；
        # 如需使用官方源，设 HF_ENDPOINT=https://huggingface.co。
        self.hf_endpoint: str = os.getenv("HF_ENDPOINT", "https://hf-mirror.com").strip()
        # 默认放在仓库根的 .hf_cache（该目录已含下载好的多语言模型）
        self.hf_home: Path = Path(
            os.getenv("HF_HOME", str(BACKEND_DIR.parent / ".hf_cache"))
        )

        # --- 日志 ---
        self.log_level: str = (os.getenv("LOG_LEVEL", "INFO").strip() or "INFO").upper()
        self.log_file: Path = Path(
            os.getenv("LOG_FILE", str(BACKEND_DIR / "logs" / "app.log"))
        )

        # --- 应用 ---
        self.api_prefix: str = "/api/v1"
        self.cors_origins: list[str] = [
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
            if o.strip()
        ]
        self.bcrypt_rounds: int = _env_int("BCRYPT_ROUNDS", 12)


def validate_settings(settings: "Settings") -> list[str]:
    """配置自检：返回问题列表（空表示没问题）。

    这些值一旦错配，往往表现为「登录随机失效」「浏览器 CORS 报错」等
    难以定位的现象，因此在启动时集中检查。
    """
    problems: list[str] = []

    if settings.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
        problems.append(
            f"JWT_ALGORITHM={settings.jwt_algorithm} 不受支持（仅 HS256/HS384/HS512）"
        )
    if settings.jwt_expire_minutes <= 0:
        problems.append("JWT_EXPIRE_MINUTES 必须为正数")
    if "*" in settings.cors_origins and settings.cors_origins != ["*"]:
        problems.append("CORS_ORIGINS 混用了 * 与具体域名")
    if settings.login_max_attempts <= 0:
        problems.append("LOGIN_MAX_ATTEMPTS 必须为正数")
    if settings.max_multipart_bytes < settings.max_request_body_bytes:
        problems.append("MAX_MULTIPART_BYTES 不应小于 MAX_REQUEST_BODY_BYTES")
    return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例，避免重复解析环境变量。"""
    return Settings()


settings = get_settings()
