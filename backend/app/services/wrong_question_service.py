"""错题诊断服务（阶段二）。

职责
----
1. 错题录入（单条 + 粘贴文本批量）
2. 知识点识别：LLM 单次调用，**识别结果需用户确认**；LLM 不可用时退化为本地启发式
3. 知识点与 cards 表自动关联（按标签/正文匹配，无需额外关联表）
4. 薄弱点警报：同一知识点累计错误次数达到阈值即触发
5. 为复习队列提供「薄弱点卡片优先」所需的卡片 id

分层
----
- **纯函数**（本文件上半部分）：不依赖数据库，可直接单测。
- **DB 层**：通过 SQLAlchemy ORM 操作，不手写原生 SQL。

阈值语义：``error_count`` 为**累计**错误次数，达到 ``threshold``（默认 2）即触发。
验收标准「同一知识点错 2 次后触发」对应 ``threshold=2``，故判定为 ``>=``。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..llm import LLMClient, default_client
from ..limits import LIKE_ESCAPE_CHAR, MAX_CONTENT_LENGTH, clamp_text, like_contains
from ..logging_config import get_logger
from ..models import Card, WrongQuestion
from ..tags import KEY_CHAPTERS, KEY_POINT_TYPES, KEY_SUBJECTS, SUBJECTS
from ..timeutils import utcnow

logger = get_logger(__name__)

# --- 苏格拉底式问题类型 -----------------------------------------------------

SOCRATIC_PROCEDURAL = "procedural"  # 程序性问题（有答案）
SOCRATIC_PREFERENCE = "preference"  # 偏好性问题（无答案）
SOCRATIC_JUDGMENT = "judgment"      # 判断性问题（整合知识）

SOCRATIC_TYPE_LABELS: dict[str, str] = {
    SOCRATIC_PROCEDURAL: "程序性问题",
    SOCRATIC_PREFERENCE: "偏好性问题",
    SOCRATIC_JUDGMENT: "判断性问题",
}

# 无标准答案的类型：学生作答后不展示「解析」，只做回看
SOCRATIC_TYPES_WITHOUT_ANSWER = frozenset({SOCRATIC_PREFERENCE})

VALID_SOCRATIC_TYPES = tuple(SOCRATIC_TYPE_LABELS)

# 知识点长度上限（与 WrongQuestion.knowledge_point 的列宽保持一致）
MAX_KNOWLEDGE_POINT_LENGTH = 200
UNCLASSIFIED_KNOWLEDGE_POINT = "未分类知识点"


def socratic_type_label(value: str | None) -> str | None:
    return SOCRATIC_TYPE_LABELS.get(value or "", None)


def socratic_has_answer(value: str | None) -> bool:
    """该类型是否「有答案」（答完可以显示解析）。"""
    return (value or "") not in SOCRATIC_TYPES_WITHOUT_ANSWER


# --- 纯逻辑：知识点推断 -----------------------------------------------------

# 疑问词/题型标记：出现在知识点之前时就地截断
_TRAILING_MARKERS = (
    "是什么",
    "什么是",
    "有哪些",
    "为什么",
    "怎么样",
    "怎样",
    "怎么",
    "如何",
    "简述",
    "试述",
    "请说明",
    "请简述",
    "试比较",
    "比较",
    "？",
    "?",
)

# 出现在句首的疑问词，需要剥离
_LEADING_MARKERS = (
    "为什么",
    "什么是",
    "简述",
    "试述",
    "请说明",
    "请简述",
    "如何",
    "怎样",
    "怎么",
    "请",
)

_SENTENCE_SEPARATORS = ("。", "？", "?", "！", "!", "\n", "；", ";")


def normalize_knowledge_point(raw: str | None) -> str:
    """规范化知识点文本：压缩空白、去首尾标点（含问号）、限长。

    问号必须去掉：知识点是陈述性名词短语；否则「？？？」这类纯疑问字符
    会被当成一个知识点，污染薄弱点聚合与卡片关联。
    """
    if not raw:
        return ""
    text = " ".join(str(raw).split()).strip()
    text = text.strip(" ，,、。:：;；-—_/|？！?!")
    if len(text) > MAX_KNOWLEDGE_POINT_LENGTH:
        text = text[:MAX_KNOWLEDGE_POINT_LENGTH].strip()
    return text


def heuristic_knowledge_point(
    question_text: str, known_points: Sequence[str] | None = None
) -> str:
    """本地启发式知识点提取（无 Agent 降级模式）。

    策略（按优先级）：
    1. 命中已有知识点/章节标签 —— 复用同一字符串，薄弱点才能正确聚合；
    2. 取第一句，截断到首个疑问词之前；
    3. 剥离句首疑问词；
    4. 兜底为「未分类知识点」。
    """
    text = " ".join((question_text or "").split()).strip()
    if not text:
        return UNCLASSIFIED_KNOWLEDGE_POINT

    # 1) 复用已知知识点：最长匹配优先，避免「受体」压过「肾上腺素受体」
    candidates = sorted(
        {normalize_knowledge_point(p) for p in (known_points or []) if p},
        key=len,
        reverse=True,
    )
    for candidate in candidates:
        if candidate and candidate in text:
            return candidate

    # 2) 取第一句
    core = text
    for sep in _SENTENCE_SEPARATORS:
        if sep in core:
            core = core.split(sep, 1)[0]

    # 3) 截断到首个疑问词之前（位置 > 0 才有信息量）
    cut = len(core)
    for marker in _TRAILING_MARKERS:
        idx = core.find(marker)
        if idx > 0:
            cut = min(cut, idx)
    core = core[:cut].strip()

    # 4) 剥离句首疑问词
    changed = True
    while changed:
        changed = False
        for marker in _LEADING_MARKERS:
            if core.startswith(marker) and len(core) > len(marker):
                core = core[len(marker) :].strip(" ，,、:：")
                changed = True
                break

    core = normalize_knowledge_point(core)
    if not core:
        # 整段都是疑问词时，退回原始文本
        core = normalize_knowledge_point(text)
    return core or UNCLASSIFIED_KNOWLEDGE_POINT


def infer_subject(question_text: str) -> str | None:
    """题干中出现的专业课维度（用于给 LLM 提示 / 前端辅助）。"""
    for subject in SUBJECTS:
        if subject in (question_text or ""):
            return subject
    return None


# --- 纯逻辑：问题类型判定 ---------------------------------------------------


def classify_socratic_type(question_text: str) -> str:
    """按题干特征判定苏格拉底问题类型（确定性规则，可单测）。"""
    text = question_text or ""

    # 偏好性优先：这类措辞最具体，且决定「无标准答案」
    if any(
        k in text
        for k in ("更好", "优先", "你倾向", "你认为哪个", "利弊", "值得", "应该选", "偏好", "更适合")
    ):
        return SOCRATIC_PREFERENCE

    if any(
        k in text
        for k in (
            "计算",
            "步骤",
            "顺序",
            "流程",
            "如何",
            "怎样",
            "推导",
            "合成",
            "剂量",
            "机制",
            "为什么",
            "如何计算",
        )
    ):
        return SOCRATIC_PROCEDURAL

    return SOCRATIC_JUDGMENT


def heuristic_socratic_question(
    question_text: str, knowledge_point: str, question_type: str | None = None
) -> str:
    """本地模板生成苏格拉底式引导问题（不直接给解析）。"""
    qtype = question_type or classify_socratic_type(question_text)
    kp = knowledge_point or UNCLASSIFIED_KNOWLEDGE_POINT

    if qtype == SOCRATIC_PROCEDURAL:
        return (
            f"先别急着看答案。针对「{kp}」，你能按顺序说出正确的推导或操作步骤吗？"
            "第一步是什么，为什么它必须排在最前面？"
        )
    if qtype == SOCRATIC_PREFERENCE:
        return (
            f"「{kp}」这类问题往往没有唯一正确答案。你会怎么选？"
            "支撑你判断的依据是什么，又在什么条件下你会改变选择？"
        )
    return (
        f"把「{kp}」和你已学过的相邻知识点放在一起看：它们的分界线在哪里？"
        "如果题目条件改动一处，你的结论还成立吗？"
    )


# --- 纯逻辑：薄弱点判定 -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeakPoint:
    """一个知识点的错误汇总。"""

    knowledge_point: str
    total_errors: int
    question_count: int
    threshold: int
    related_card_ids: tuple[int, ...] = ()
    last_error_at: datetime | None = None

    @property
    def is_weak(self) -> bool:
        """累计错误次数达到阈值即为薄弱点。"""
        return self.total_errors >= self.threshold

    @property
    def errors_to_threshold(self) -> int:
        """距离触发还差几次（已触发为 0）。"""
        return max(0, self.threshold - self.total_errors)

    @property
    def severity(self) -> str:
        if not self.is_weak:
            return "normal"
        if self.total_errors >= self.threshold * 2:
            return "critical"
        return "warning"


def is_weak_point(total_errors: int, threshold: int) -> bool:
    """薄弱点判定：累计错误次数 >= 阈值。"""
    return total_errors >= threshold


def summarize_weak_points(
    error_counts: Mapping[str, int],
    threshold: int | None = None,
    question_counts: Mapping[str, int] | None = None,
    related_card_ids: Mapping[str, Iterable[int]] | None = None,
    last_error_at: Mapping[str, datetime] | None = None,
    *,
    weak_only: bool = False,
) -> list[WeakPoint]:
    """把「知识点 -> 累计错误次数」汇总为薄弱点列表（按错误次数降序）。

    纯函数：不接触数据库，便于对阈值逻辑做单元测试。
    """
    limit = settings.weak_point_threshold if threshold is None else threshold
    q_counts = question_counts or {}
    related = related_card_ids or {}
    last_seen = last_error_at or {}

    # 先按**归一化后的 key** 合并，避免「阿司匹林」「 阿司匹林。」被算成两个弱项
    merged: dict[str, dict] = {}
    for raw_kp, total in error_counts.items():
        kp = normalize_knowledge_point(raw_kp) or UNCLASSIFIED_KNOWLEDGE_POINT
        entry = merged.setdefault(
            kp, {"total": 0, "count": 0, "cards": set(), "last": None}
        )
        entry["total"] += int(total)
        entry["count"] += int(q_counts.get(raw_kp, q_counts.get(kp, 0)))
        entry["cards"].update(related.get(raw_kp, ()) or related.get(kp, ()))
        candidate = last_seen.get(raw_kp) or last_seen.get(kp)
        if candidate is not None and (entry["last"] is None or candidate > entry["last"]):
            entry["last"] = candidate

    points: list[WeakPoint] = []
    for kp, entry in merged.items():
        point = WeakPoint(
            knowledge_point=kp,
            total_errors=entry["total"],
            question_count=entry["count"],
            threshold=limit,
            related_card_ids=tuple(sorted(entry["cards"])),
            last_error_at=entry["last"],
        )
        if weak_only and not point.is_weak:
            continue
        points.append(point)

    # 错误次数降序；同次数时知识点名称升序，保证结果稳定
    points.sort(key=lambda p: (-p.total_errors, p.knowledge_point))
    return points


# --- 纯逻辑：LLM 结果整理 ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgePointSuggestion:
    """知识点识别建议（需用户确认后才落库）。"""

    knowledge_point: str
    source: str  # "llm" | "heuristic"
    confidence: float
    alternatives: tuple[str, ...] = ()
    llm_error: str | None = None
    requires_confirmation: bool = True

    @property
    def from_llm(self) -> bool:
        return self.source == "llm"


@dataclass(frozen=True, slots=True)
class SocraticSuggestion:
    """苏格拉底式问题建议。"""

    question: str
    question_type: str
    source: str  # "llm" | "heuristic"

    @property
    def has_answer(self) -> bool:
        return socratic_has_answer(self.question_type)

    @property
    def type_label(self) -> str:
        return SOCRATIC_TYPE_LABELS.get(self.question_type, self.question_type)


@dataclass(frozen=True, slots=True)
class QuestionAnalysis:
    """一次「分析」的完整结果（识别 + 引导问题），供用户确认。"""

    question_text: str
    suggestion: KnowledgePointSuggestion
    socratic: SocraticSuggestion

    @property
    def requires_confirmation(self) -> bool:
        return self.suggestion.requires_confirmation


# --- LLM 调用（单次） -------------------------------------------------------

_KP_SYSTEM_PROMPT = (
    "你是药学教学助理。请从学生的错题中提取**一个**最小可复习的知识点，"
    "用于间隔重复复习与薄弱点统计。要求：\n"
    "1. 知识点必须是药学专业术语，长度不超过 20 个汉字，不要包含疑问词；\n"
    "2. 同一知识点的表述要稳定，便于长期聚合统计；\n"
    "3. 只输出 JSON，不要输出任何解释。\n"
    '输出格式：{"knowledge_point": "...", "alternatives": ["...", "..."], '
    '"confidence": 0.0-1.0}'
)

_SOCRATIC_SYSTEM_PROMPT = (
    "你是采用苏格拉底式教学的药学老师。学生刚做错了一道题，"
    "你要**通过提问引导他自己发现错误原因**，绝对不要直接给出答案或解析。\n"
    "问题类型必须是以下三种之一：\n"
    "- procedural：程序性问题（有答案），引导学生复述正确的推导/操作步骤；\n"
    "- preference：偏好性问题（无标准答案），引导学生在权衡中表达立场与依据；\n"
    "- judgment：判断性问题（整合知识），引导学生辨析边界、迁移条件。\n"
    "只输出 JSON，不要输出任何解释。\n"
    '输出格式：{"socratic_question": "...", "question_type": "procedural"}'
)


def identify_knowledge_point(
    question_text: str,
    known_points: Sequence[str] | None = None,
    client: LLMClient | None = None,
) -> KnowledgePointSuggestion:
    """识别知识点：LLM 单次调用；失败或未配置时退化为本地启发式。"""
    text = (question_text or "").strip()
    if not text:
        return KnowledgePointSuggestion(
            knowledge_point=UNCLASSIFIED_KNOWLEDGE_POINT,
            source="heuristic",
            confidence=0.0,
        )

    llm = client if client is not None else default_client
    llm_error: str | None = None

    if llm.available:
        hint = ""
        if known_points:
            sample = "、".join(list(known_points)[:20])
            hint = f"\n该学生已有知识点（优先复用其中的表述）：{sample}"
        response = llm.complete_json(_KP_SYSTEM_PROMPT, f"错题：{text}{hint}")
        if response.ok and response.data:
            kp = normalize_knowledge_point(str(response.data.get("knowledge_point", "")))
            if kp:
                raw_alts = response.data.get("alternatives") or []
                alternatives = tuple(
                    alt
                    for alt in (
                        normalize_knowledge_point(str(a)) for a in raw_alts if a
                    )
                    if alt and alt != kp
                )[:3]
                try:
                    confidence = float(response.data.get("confidence", 0.8))
                except (TypeError, ValueError):
                    confidence = 0.8
                logger.info("知识点识别完成（LLM）kp=%s", kp)
                return KnowledgePointSuggestion(
                    knowledge_point=kp,
                    source="llm",
                    confidence=max(0.0, min(1.0, confidence)),
                    alternatives=alternatives,
                )
            llm_error = "LLM 返回的知识点为空"
        else:
            llm_error = response.error
        logger.info("知识点识别回退到启发式：%s", llm_error)

    fallback = heuristic_knowledge_point(text, known_points)
    return KnowledgePointSuggestion(
        knowledge_point=fallback,
        source="heuristic",
        confidence=0.35 if fallback != UNCLASSIFIED_KNOWLEDGE_POINT else 0.0,
        llm_error=llm_error,
    )


def generate_socratic_question(
    question_text: str,
    knowledge_point: str,
    client: LLMClient | None = None,
) -> SocraticSuggestion:
    """生成苏格拉底式引导问题：LLM 单次调用；失败时退化为本地模板。"""
    llm = client if client is not None else default_client

    if llm.available:
        prompt = f"知识点：{knowledge_point}\n学生的错题：{question_text}"
        response = llm.complete_json(_SOCRATIC_SYSTEM_PROMPT, prompt, temperature=0.5)
        if response.ok and response.data:
            question = str(response.data.get("socratic_question", "")).strip()
            qtype = str(response.data.get("question_type", "")).strip().lower()
            if qtype not in SOCRATIC_TYPE_LABELS:
                qtype = classify_socratic_type(question_text)
            if question:
                logger.info("苏格拉底问题生成完成（LLM）type=%s", qtype)
                return SocraticSuggestion(question=question, question_type=qtype, source="llm")
        logger.info("苏格拉底问题回退到模板：%s", response.error)

    qtype = classify_socratic_type(question_text)
    return SocraticSuggestion(
        question=heuristic_socratic_question(question_text, knowledge_point, qtype),
        question_type=qtype,
        source="heuristic",
    )


def analyze_question(
    question_text: str,
    known_points: Sequence[str] | None = None,
    client: LLMClient | None = None,
) -> QuestionAnalysis:
    """识别知识点 + 生成引导问题（**不落库**，等待用户确认）。

    这是「LLM 单次调用」的入口：知识点与问题在同一次分析中给出，
    用户确认知识点后再创建错题。
    """
    suggestion = identify_knowledge_point(question_text, known_points, client)
    socratic = generate_socratic_question(question_text, suggestion.knowledge_point, client)
    return QuestionAnalysis(
        question_text=question_text, suggestion=suggestion, socratic=socratic
    )


# --- DB 层：知识点与卡片自动关联 --------------------------------------------


def _exact_tag_condition(point: str):
    """标签成员匹配（走生成列上的多值索引）。

    ``Card.chapters_json`` 是 ``tags.chapters`` 的生成列；对它做
    ``JSON_CONTAINS`` 才能命中多值索引（实测 5.7ms vs 32.7ms / 2 万张卡）。
    """
    return func.json_contains(Card.chapters_json, json.dumps(point, ensure_ascii=False))


def _fuzzy_conditions(point: str) -> list:
    """旧的文本 / JSON 文本模糊匹配（全表扫描，仅在回落模式下使用）。"""
    pattern, escape = like_contains(point)
    conditions = [
        Card.front.ilike(pattern, escape=escape),
        Card.back.ilike(pattern, escape=escape),
    ]
    for key in (KEY_SUBJECTS, KEY_CHAPTERS, KEY_POINT_TYPES):
        conditions.append(
            func.json_contains(Card.tags[key], json.dumps(point, ensure_ascii=False))
        )
        conditions.append(cast(Card.tags[key], String).ilike(pattern, escape=escape))
    return conditions


def _card_matches_point(
    front: str, back: str, tags: Any, point: str, *, fuzzy: bool = False
) -> bool:
    """与 SQL 谓词等价的**内存判定**（一次查询后按知识点归属）。

    ``fuzzy=False`` 时只认 ``chapters`` 标签的精确成员，与
    ``_exact_tag_condition`` 对应；``fuzzy=True`` 时额外镜像文本匹配。
    """
    if not point:
        return False
    if not isinstance(tags, dict):
        return False

    chapters = tags.get(KEY_CHAPTERS)
    if isinstance(chapters, list) and point in chapters:
        return True
    if not fuzzy:
        return False

    lowered = point.lower()
    if lowered in (front or "").lower() or lowered in (back or "").lower():
        return True
    for key in (KEY_SUBJECTS, KEY_CHAPTERS, KEY_POINT_TYPES):
        value = tags.get(key)
        if isinstance(value, list) and point in value:
            return True
        if value and lowered in json.dumps(value, ensure_ascii=False).lower():
            return True
    return False


def related_card_ids(
    db: Session, user_id: int, knowledge_points: Sequence[str]
) -> set[int]:
    """找出与给定知识点关联的卡片 id（多个知识点取并集）。

    委托给 :func:`related_card_ids_bulk`，保证单点与批量走**同一套**匹配策略，
    避免两条路径的语义漂移。
    """
    mapping = related_card_ids_bulk(db, user_id, knowledge_points)
    merged: set[int] = set()
    for ids in mapping.values():
        merged.update(ids)
    return merged


def related_card_ids_bulk(
    db: Session, user_id: int, knowledge_points: Sequence[str]
) -> dict[str, set[int]]:
    """按知识点分别给出关联卡片 id，返回 ``{知识点: {卡片 id, ...}}``。

    匹配策略由 ``settings.related_cards_match_mode`` 决定（H6 实测后确定）：

    - ``never``    —— 只按 ``chapters`` 标签做**成员匹配**，命中生成列上的多值索引
      （2 万张卡片实测 5.7ms）；
    - ``fallback`` —— 先做上面的精确匹配；**只有完全没命中**时才回落到旧的
      文本/JSON 模糊扫描（2 万卡实测 32.7ms），兼顾手写卡片的覆盖率；
    - ``always``   —— 始终取两者并集（旧行为，最慢）。

    为什么不再默认走并集：文本 ``LIKE '%…%'`` 无法使用索引，会把每次匹配变成
    全表扫描；而且它把「正文里恰好提到该词」的卡片也算作关联，正是审计指出的
    「假弱项」来源。
    """
    points = [normalize_knowledge_point(p) for p in knowledge_points]
    points = list(dict.fromkeys(p for p in points if p))
    if not points:
        return {}

    mode = (settings.related_cards_match_mode or "fallback").lower()
    if mode not in {"never", "fallback", "always"}:
        mode = "fallback"

    result: dict[str, set[int]] = {point: set() for point in points}

    def _collect(points_subset: Sequence[str], *, fuzzy: bool, exact: bool) -> None:
        conditions = []
        for point in points_subset:
            if exact:
                conditions.append(_exact_tag_condition(point))
            if fuzzy:
                conditions.extend(_fuzzy_conditions(point))
        if not conditions:
            return
        rows = db.execute(
            select(Card.id, Card.front, Card.back, Card.tags).where(
                Card.user_id == user_id, or_(*conditions)
            )
        ).all()
        for card_id, front, back, tags in rows:
            for point in points_subset:
                if _card_matches_point(front or "", back or "", tags, point, fuzzy=fuzzy):
                    result[point].add(int(card_id))

    if mode == "always":
        _collect(points, fuzzy=True, exact=True)
        return result

    _collect(points, fuzzy=False, exact=True)

    if mode == "fallback":
        missing = [point for point in points if not result[point]]
        if missing:
            # 仅在「标签完全没命中」时才付全表扫描的代价
            _collect(missing, fuzzy=True, exact=False)
    return result


def related_cards(
    db: Session, user_id: int, knowledge_point: str, limit: int = 10
) -> list[Card]:
    """返回与某知识点关联的卡片（用于展示解析材料）。"""
    ids = related_card_ids(db, user_id, [knowledge_point])
    if not ids:
        return []
    stmt = (
        select(Card)
        .where(Card.user_id == user_id, Card.id.in_(ids))
        .order_by(Card.fsrs_due.is_(None).desc(), Card.fsrs_due.asc(), Card.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


# --- DB 层：错题 CRUD -------------------------------------------------------


def known_knowledge_points(db: Session, user_id: int) -> list[str]:
    """该用户已有的知识点 + 卡片章节标签，用于启发式复用与 LLM 提示。"""
    stmt = (
        select(WrongQuestion.knowledge_point)
        .where(WrongQuestion.user_id == user_id)
        .distinct()
    )
    points = [p for p in db.execute(stmt).scalars().all() if p]

    chapter_rows = db.execute(
        select(Card.tags[KEY_CHAPTERS]).where(Card.user_id == user_id)
    ).scalars().all()
    for row in chapter_rows:
        if isinstance(row, list):
            points.extend(str(item) for item in row if item)
    return points


def error_aggregates(
    db: Session, user_id: int
) -> tuple[dict[str, int], dict[str, int], dict[str, datetime]]:
    """按知识点聚合：累计错误次数、错题数量、最近一次错误时间。"""
    stmt = (
        select(
            WrongQuestion.knowledge_point,
            func.sum(WrongQuestion.error_count),
            func.count(WrongQuestion.id),
            func.max(func.coalesce(WrongQuestion.last_error_at, WrongQuestion.created_at)),
        )
        .where(WrongQuestion.user_id == user_id)
        .group_by(WrongQuestion.knowledge_point)
    )

    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    last_seen: dict[str, datetime] = {}
    for kp, total, count, latest in db.execute(stmt).all():
        # 先归一化再聚合：库里可能存在「 阿司匹林」「阿司匹林。」等变体，
        # 直接按原值分组会把同一个知识点拆成多个弱项，导致阈值/排序/关联卡片失真。
        key = normalize_knowledge_point(kp) or UNCLASSIFIED_KNOWLEDGE_POINT
        totals[key] = totals.get(key, 0) + int(total or 0)
        counts[key] = counts.get(key, 0) + int(count or 0)
        if latest is not None:
            previous = last_seen.get(key)
            last_seen[key] = latest if previous is None else max(previous, latest)
    return totals, counts, last_seen


def list_weak_points(
    db: Session,
    user_id: int,
    threshold: int | None = None,
    *,
    weak_only: bool = True,
    with_related_cards: bool = True,
) -> list[WeakPoint]:
    """薄弱点列表（默认只返回已达阈值的）。"""
    totals, counts, last_seen = error_aggregates(db, user_id)

    related: dict[str, set[int]] = {}
    if with_related_cards and totals:
        # 一次查询完成全部知识点的关联归属（原先每个知识点各查一次）
        related = related_card_ids_bulk(db, user_id, list(totals))

    return summarize_weak_points(
        totals,
        threshold=threshold,
        question_counts=counts,
        related_card_ids=related,
        last_error_at=last_seen,
        weak_only=weak_only,
    )


def weak_card_ids(
    db: Session, user_id: int, threshold: int | None = None
) -> list[int]:
    """所有「属于薄弱知识点」的卡片 id（供复习队列排序使用）。"""
    points = list_weak_points(
        db, user_id, threshold, weak_only=True, with_related_cards=True
    )
    ids: set[int] = set()
    for point in points:
        ids.update(point.related_card_ids)
    return sorted(ids)


class _NoLLMClient:
    """强制降级的客户端替身：``available=False`` 时所有识别都走本地启发式。"""

    available = False

    def complete_json(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("该路径不允许调用 LLM")


def create_wrong_question(
    db: Session,
    user_id: int,
    question_text: str,
    knowledge_point: str | None = None,
    error_count: int = 1,
    socratic_question: str | None = None,
    socratic_question_type: str | None = None,
    client: LLMClient | None = None,
    known_points: Sequence[str] | None = None,
    use_llm: bool = True,
) -> tuple[WrongQuestion, KnowledgePointSuggestion, SocraticSuggestion]:
    """创建错题。

    - ``knowledge_point`` 为空时自动识别（LLM → 启发式），返回值中附带识别来源；
    - ``socratic_question`` 为空时自动生成引导问题；
    - ``known_points`` 可由批量导入预先算好传进来，避免每行都做一次全表扫描；
    - ``use_llm=False`` 时**强制走本地启发式**（批量导入默认如此，见下）。
    """
    text = (question_text or "").strip()
    known = list(known_points) if known_points is not None else known_knowledge_points(db, user_id)
    # 批量场景下不逐条打 LLM：一条错题最多 2 次调用（识别 + 提问），
    # 100 行就是 200 次模型调用，既慢又烧钱。这里用离线启发式，可信度足够，
    # 且结果仍需用户确认/修正。
    effective_client = client if use_llm else _NoLLMClient()

    if knowledge_point and normalize_knowledge_point(knowledge_point):
        # 用户已确认知识点：不再调用 LLM 识别
        suggestion = KnowledgePointSuggestion(
            knowledge_point=normalize_knowledge_point(knowledge_point),
            source="user",
            confidence=1.0,
            requires_confirmation=False,
        )
    else:
        suggestion = identify_knowledge_point(text, known, effective_client)

    if socratic_question and socratic_question.strip():
        qtype = socratic_question_type or classify_socratic_type(text)
        socratic = SocraticSuggestion(
            question=socratic_question.strip(), question_type=qtype, source="user"
        )
    else:
        socratic = generate_socratic_question(
            text, suggestion.knowledge_point, effective_client
        )

    record = WrongQuestion(
        user_id=user_id,
        question_text=text,
        knowledge_point=suggestion.knowledge_point,
        error_count=max(1, int(error_count)),
        created_at=utcnow(),
        last_error_at=utcnow(),
        socratic_question=socratic.question,
        socratic_question_type=socratic.question_type,
        user_answer=None,
    )
    db.add(record)
    db.flush()
    logger.info(
        "录入错题 id=%s user_id=%s kp=%s 来源=%s errors=%s",
        record.id,
        user_id,
        record.knowledge_point,
        suggestion.source,
        record.error_count,
    )
    return record, suggestion, socratic


def parse_bulk_text(text: str) -> tuple[list[str], list[str]]:
    """解析粘贴的错题文本：每行一道错题（``#`` 开头为注释，空行忽略）。

    支持 ``题干 | 知识点`` 形式显式指定知识点。
    """
    items: list[str] = []
    errors: list[str] = []
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 单行过长要截断，避免撞上 TEXT 列的字节上限（否则整批 500）
        cleaned, was_truncated = clamp_text(line, MAX_CONTENT_LENGTH)
        if was_truncated:
            errors.append(f"第 {lineno} 行超过 {MAX_CONTENT_LENGTH} 字符，已截断")
        items.append(cleaned)
    if not items:
        errors.append("没有解析到任何错题（每行一道，空行与 # 注释会被忽略）")
    return items, errors


def create_wrong_questions_bulk(
    db: Session,
    user_id: int,
    text: str,
    knowledge_point: str | None = None,
    client: LLMClient | None = None,
    use_llm: bool = False,
) -> tuple[list[WrongQuestion], list[str]]:
    """粘贴文本批量录入错题。

    逐行解析；``题干 | 知识点`` 可单独指定该行知识点，否则用 ``knowledge_point``
    参数，仍为空时自动识别。
    """
    lines, errors = parse_bulk_text(text)
    if len(lines) > settings.wrong_question_bulk_max_lines:
        errors.append(
            f"单次最多导入 {settings.wrong_question_bulk_max_lines} 条，"
            f"超出部分已忽略（共 {len(lines)} 条）"
        )
        lines = lines[: settings.wrong_question_bulk_max_lines]

    # 已知知识点只取一次：原先每行都调 known_knowledge_points（全表扫描）
    known = known_knowledge_points(db, user_id)
    created: list[WrongQuestion] = []

    for line in lines:
        if "|" in line:
            question, _, explicit = line.partition("|")
            question = question.strip()
            explicit_kp = normalize_knowledge_point(explicit)
        else:
            question = line
            explicit_kp = ""

        if not question:
            errors.append(f"空题干已跳过：{line[:40]}")
            continue

        record, _suggestion, _socratic = create_wrong_question(
            db,
            user_id,
            question,
            knowledge_point=explicit_kp or knowledge_point,
            client=client,
            known_points=known,
            # 批量默认不逐条调用 LLM（可显式 use_llm=True 覆盖）
            use_llm=use_llm,
        )
        created.append(record)

    if created:
        logger.info("批量录入错题 %d 条 user_id=%s", len(created), user_id)
    return created, errors


def get_wrong_question(db: Session, user_id: int, wrong_question_id: int) -> WrongQuestion | None:
    stmt = select(WrongQuestion).where(
        WrongQuestion.id == wrong_question_id, WrongQuestion.user_id == user_id
    )
    return db.execute(stmt).scalar_one_or_none()


def list_wrong_questions(
    db: Session,
    user_id: int,
    offset: int = 0,
    limit: int = 20,
    knowledge_point: str | None = None,
    keyword: str | None = None,
    weak_only: bool = False,
    threshold: int | None = None,
) -> tuple[list[WrongQuestion], int]:
    """分页查询错题。"""
    stmt = select(WrongQuestion).where(WrongQuestion.user_id == user_id)

    if knowledge_point:
        stmt = stmt.where(
            WrongQuestion.knowledge_point == normalize_knowledge_point(knowledge_point)
        )

    if weak_only:
        weak = list_weak_points(
            db, user_id, threshold, weak_only=True, with_related_cards=False
        )
        names = [p.knowledge_point for p in weak] or [""]
        stmt = stmt.where(WrongQuestion.knowledge_point.in_(names))

    if keyword:
        pattern, escape = like_contains(keyword.strip())
        stmt = stmt.where(
            or_(
                WrongQuestion.question_text.ilike(pattern, escape=escape),
                WrongQuestion.knowledge_point.ilike(pattern, escape=escape),
            )
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    page = (
        stmt.order_by(WrongQuestion.created_at.desc(), WrongQuestion.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.execute(page).scalars().all()), total


def update_wrong_question(
    db: Session,
    record: WrongQuestion,
    question_text: str | None = None,
    knowledge_point: str | None = None,
    socratic_question: str | None = None,
    socratic_question_type: str | None = None,
) -> WrongQuestion:
    """修改错题；主要用于「用户修正识别出的知识点」。"""
    if question_text is not None:
        record.question_text = question_text.strip()
    if knowledge_point is not None:
        normalized = normalize_knowledge_point(knowledge_point)
        if normalized:
            record.knowledge_point = normalized
    if socratic_question is not None:
        record.socratic_question = socratic_question.strip()
    if socratic_question_type is not None:
        if socratic_question_type not in SOCRATIC_TYPE_LABELS:
            raise ValueError(f"非法的问题类型：{socratic_question_type}")
        record.socratic_question_type = socratic_question_type
    db.flush()
    logger.info("更新错题 id=%s kp=%s", record.id, record.knowledge_point)
    return record


def delete_wrong_question(db: Session, record: WrongQuestion) -> None:
    record_id = record.id
    db.delete(record)
    db.flush()
    logger.info("删除错题 id=%s", record_id)


def increment_error(
    db: Session, record: WrongQuestion, delta: int = 1
) -> WrongQuestion:
    """再次做错：累加错误次数（可能触发薄弱点警报）。"""
    step = int(delta)
    if step < 1:
        raise ValueError("delta 必须为正整数")
    record.error_count = int(record.error_count or 0) + step
    # 做错时间必须刷新，否则薄弱点的时间排序/展示会停留在创建时间
    record.last_error_at = utcnow()
    db.flush()
    logger.info(
        "错题累计错误 id=%s kp=%s errors=%s", record.id, record.knowledge_point, record.error_count
    )
    return record


def answer_wrong_question(
    db: Session, record: WrongQuestion, user_answer: str
) -> WrongQuestion:
    """记录学生对苏格拉底式问题的回答，并刷新 last_reviewed_at。"""
    record.user_answer = (user_answer or "").strip()
    record.last_reviewed_at = utcnow()
    db.flush()
    logger.info("记录苏格拉底回答 id=%s", record.id)
    return record


@dataclass
class Explanation:
    """作答后展示的「解析」材料。

    本平台不存储单独的解析字段，解析来自**关联卡片**的正确答案；
    偏好性问题没有标准答案，只给出回看提示。
    """

    knowledge_point: str
    has_standard_answer: bool
    reference_answers: list[dict] = field(default_factory=list)
    guidance: str = ""


def build_explanation(
    db: Session, record: WrongQuestion, limit: int = 5
) -> Explanation:
    """构造作答后的解析材料。"""
    cards = related_cards(db, record.user_id, record.knowledge_point, limit=limit)
    references = [
        {"card_id": card.id, "front": card.front, "back": card.back} for card in cards
    ]
    has_answer = socratic_has_answer(record.socratic_question_type)

    if has_answer:
        guidance = (
            f"对照以上关联卡片，检查你在「{record.knowledge_point}」上的推理链条："
            "哪一步与正确答案出现了偏差？"
        )
    else:
        guidance = (
            f"「{record.knowledge_point}」属于偏好性问题，没有唯一正确答案。"
            "请对照关联卡片检查你的依据是否成立、边界条件是否说清。"
        )

    return Explanation(
        knowledge_point=record.knowledge_point,
        has_standard_answer=has_answer,
        reference_answers=references,
        guidance=guidance,
    )
