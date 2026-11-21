"""考点匹配服务（阶段五）。

流程
----
1. **考点切分**：把粘贴的整段文本切成一条条考点（编号 / 项目符号 / 换行 / 分号）；
2. **考点提取**：分词（领域词典正向最大匹配）+ 同义词替换后归一化；
3. **知识点匹配**：编辑距离**初筛** → sentence-transformers（或离线哈希）**精筛**；
4. **优先级排序**：结合学生的 FSRS 掌握度与阶段二薄弱点，判定 高危 / 巩固 / 确认；
5. **复习清单**：按优先级生成清单，高危考点可一键插入 FSRS 复习队列。

分层：上半部分为**纯函数**（同义词、分词、编辑距离、匹配打分、优先级判定），
不接触数据库，可直接单测；下半部分为 DB 层。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..limits import MAX_SHORT_TEXT_LENGTH, clamp_text
from ..embeddings import EmbeddingBackend, cosine_similarity, resolve_backend
from ..logging_config import get_logger
from ..models import (
    PRIORITY_CONFIRM,
    PRIORITY_CONSOLIDATE,
    PRIORITY_HIGH,
    PRIORITY_ORDER,
    Card,
    ExamPoint,
    KnowledgeGraph,
    WrongQuestion,
)
from ..tags import KEY_CHAPTERS
from ..timeutils import utcnow

logger = get_logger(__name__)

# 掌握程度
MASTERY_WEAK = "weak"          # 薄弱
MASTERY_MODERATE = "moderate"  # 一般
MASTERY_MASTERED = "mastered"  # 已掌握

# 候选来源权重（知识图谱与错题知识点更可信）
SOURCE_WEIGHT = {
    "knowledge_graph": 1.0,
    "wrong_question": 0.98,
    "card_tag": 0.95,
}

# 考点切分：编号、项目符号、括号编号
_POINT_SPLIT_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:"
    r"\d+\s*[.、)）:：]"          # 1. / 1、 / 1) / 1）
    r"|[一二三四五六七八九十]+\s*[、.)）]"  # 一、/ 二)
    r"|[（(]\s*\d+\s*[)）]"        # (1)
    r"|[•·▪●○※*\-–—]\s*"          # 项目符号
    r")"
)
# 行内分隔：分号也视为考点边界
_INLINE_SEPARATOR = re.compile(r"[;；]\s*")

# 归一化时要清理的标点与空白
_PUNCT_PATTERN = re.compile(r"[\s，,。.、:：;；!！?？\"'“”‘’()（）\[\]【】<>《》/\\|~`]+")
# 中文字符
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]+")
# 英文/数字词
_ASCII_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]*|\d+(?:\.\d+)?")


# --- 同义词表（药学领域） ---------------------------------------------------

# 形如 canonical: (变体...)，匹配时统一替换为 canonical
SYNONYMS: dict[str, tuple[str, ...]] = {
    "阿司匹林": ("乙酰水杨酸", "aspirin", "acetylsalicylic acid", "asa"),
    "环氧化酶": ("cox", "环氧合酶", "cyclooxygenase"),
    # COX-1 / COX-2 在药理学上是**有区分意义**的，不能都归并成「环氧化酶」
    "环氧化酶1": ("cox-1", "cox1", "环氧合酶1", "cox-1抑制剂"),
    "环氧化酶2": ("cox-2", "cox2", "环氧合酶2", "cox-2抑制剂", "cox-2选择性抑制剂"),
    "半衰期": ("t1/2", "t½", "half-life", "halflife", "生物半衰期", "消除半衰期"),
    "生物利用度": ("bioavailability", "ba", "生物利用程度"),
    "血药浓度": ("血浓", "plasma concentration", "血浆浓度"),
    "不良反应": ("adr", "adverse reaction", "adverse effect", "副作用", "不良事件"),
    "受体": ("receptor",),
    "激动剂": ("agonist", "兴奋剂", "激动药"),
    "拮抗剂": ("antagonist", "阻断剂", "拮抗药", "阻断药"),
    "药物代谢": ("metabolism", "生物转化", "代谢"),
    "药物排泄": ("excretion", "排泄"),
    "药物分布": ("distribution", "分布"),
    "药物吸收": ("absorption", "吸收"),
    "药效学": ("pd", "pharmacodynamics", "药物效应动力学"),
    "药动学": ("pk", "pharmacokinetics", "药物代谢动力学"),
    "首过效应": ("首关效应", "first-pass effect", "first pass effect"),
    "肝药酶": ("cyp450", "cyp", "细胞色素p450", "细胞色素p-450"),
    "构效关系": ("sar", "structure-activity relationship", "构效"),
    "手性药物": ("chiral", "对映体", "光学异构体", "手性"),
    "缓释制剂": ("sustained release", "缓释", "sr"),
    "控释制剂": ("controlled release", "控释", "cr"),
    "靶向制剂": ("targeting", "靶向给药", "靶向"),
    "包封率": ("entrapment efficiency", "ee", "包封"),
    "溶出度": ("dissolution", "释放度", "溶出"),
    "含量测定": ("assay", "含量", "含量检测", "含量测定法"),
    "有关物质": ("related substances", "杂质", "有关物质检查"),
    "高效液相色谱": ("hplc", "高效液相", "液相色谱", "高效液相色谱法"),
    "气相色谱": ("gc", "气相色谱法"),
    "紫外分光光度法": ("uv", "紫外", "紫外可见分光光度法", "紫外分光光度"),
    "红外光谱": ("ir", "红外", "红外分光光度法"),
    "核磁共振": ("nmr", "核磁", "核磁共振波谱"),
    "质谱": ("ms", "质谱法", "液质联用", "lc-ms"),
    "稳定性": ("stability", "稳定性和影响因素"),
    "处方设计": ("formulation", "处方筛选", "处方"),
    "制备工艺": ("工艺", "process", "制备方法"),
    "急性毒性": ("acute toxicity", "急性毒性试验"),
    "半数致死量": ("ld50", "ld₅₀"),
    "半数有效量": ("ed50", "ed₅₀"),
    "治疗指数": ("therapeutic index", "ti"),
    "抗菌活性": ("antibacterial activity", "抗菌作用"),
    "耐药性": ("resistance", "抗药性"),
    "配伍禁忌": ("incompatibility", "配伍变化"),
}

# 变体 -> 规范词 的查找表
_SYNONYM_LOOKUP: dict[str, str] = {}
for _canonical, _variants in SYNONYMS.items():
    _SYNONYM_LOOKUP[_canonical.lower()] = _canonical
    for _variant in _variants:
        _SYNONYM_LOOKUP.setdefault(_variant.lower(), _canonical)

# 单趟替换用的正则：长变体优先，保证每个位置只被替换一次。
#
# 注意：不能用「循环 str.replace」逐条替换 —— 替换产生的文本会被后续更短的
# 变体再次命中，导致级联污染。例如 HPLC->高效液相色谱 之后，"液相色谱" 又会
# 命中并再次展开，得到「高效高效液相色谱色谱」。用一次 re.sub 扫描即可避免。
_SYNONYM_PATTERN = re.compile(
    "|".join(
        re.escape(variant)
        for variant in sorted(set(_SYNONYM_LOOKUP), key=len, reverse=True)
        if variant
    )
)


def synonym_dictionary() -> tuple[str, ...]:
    """分词用的领域词典（规范词 + 全部变体）。"""
    return tuple(_SYNONYM_LOOKUP)


def apply_synonyms(text: str) -> str:
    """同义词替换：把各种写法统一成规范词（单趟、长变体优先）。"""
    if not text:
        return ""
    lowered = text.lower()
    if not _SYNONYM_PATTERN.pattern:
        return lowered
    return _SYNONYM_PATTERN.sub(
        lambda match: _SYNONYM_LOOKUP.get(match.group(0), match.group(0)), lowered
    )


def normalize_text(text: str) -> str:
    """考点归一化：小写 → 同义词替换 → 去标点空白。"""
    return _PUNCT_PATTERN.sub("", apply_synonyms(text or ""))


# --- 分词（正向最大匹配） ---------------------------------------------------


def tokenize(
    text: str, dictionary: Sequence[str] | None = None, max_term_length: int = 12
) -> list[str]:
    """中英混合分词。

    - 英文/数字按词切分；
    - 中文使用**领域词典正向最大匹配**（最长优先），未登录片段退化为 2-gram，
      因此在没有 jieba 等分词库的情况下也能得到可用结果。
    """
    if not text:
        return []

    terms = {term.lower() for term in (dictionary if dictionary is not None else synonym_dictionary())}
    longest = min(max_term_length, max((len(t) for t in terms), default=0) or 1)

    tokens: list[str] = []
    normalized = apply_synonyms(text)

    for ascii_token in _ASCII_TOKEN.findall(normalized):
        tokens.append(ascii_token.lower())

    for run in _CHINESE_RUN.findall(normalized):
        index = 0
        length = len(run)
        while index < length:
            matched = ""
            upper = min(longest, length - index)
            for size in range(upper, 1, -1):
                piece = run[index : index + size]
                if piece in terms:
                    matched = piece
                    break
            if matched:
                tokens.append(matched)
                index += len(matched)
            else:
                # 未登录：单个汉字 + 与下一个字组成 2-gram
                tokens.append(run[index])
                if index + 2 <= length:
                    tokens.append(run[index : index + 2])
                index += 1

    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


# --- 编辑距离 ---------------------------------------------------------------


def levenshtein_distance(a: str, b: str) -> int:
    """字符级编辑距离（滚动数组，O(min(n,m)) 空间）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current.append(
                min(
                    previous[j] + 1,        # 删除
                    current[j - 1] + 1,     # 插入
                    previous[j - 1] + cost,  # 替换
                )
            )
        previous = current
    return previous[-1]


def edit_similarity(a: str, b: str) -> float:
    """归一化编辑距离相似度（0~1）。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    distance = levenshtein_distance(a, b)
    return max(0.0, 1.0 - distance / max(len(a), len(b)))


# 包含关系的相似度下限：短考点常常是长知识点的一部分
CONTAINMENT_SIMILARITY = 0.72


def similarity_with_containment(a: str, b: str) -> float:
    """编辑距离相似度，但**互相包含**时给一个较高的下限。

    例：「阿司匹林的作用机制」与「阿司匹林作用机制」编辑距离相似度不高，
    但语义上高度相关；纯粹按编辑距离会漏掉这类考点。
    """
    base = edit_similarity(a, b)
    if not a or not b:
        return base
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        ratio = len(shorter) / len(longer)
        # 被包含的比例越高，分数越高
        return max(base, CONTAINMENT_SIMILARITY + (1 - CONTAINMENT_SIMILARITY) * ratio)
    return base


# --- 考点切分 ---------------------------------------------------------------


def split_exam_points(text: str, max_points: int | None = None) -> list[str]:
    """把导入的正文切成一条条考点。

    支持：编号（1. / 一、/ (1)）、项目符号、换行、分号。
    """
    if not text or not text.strip():
        return []

    # 先按编号/项目符号切，再按换行与分号切
    chunks: list[str] = []
    for part in _POINT_SPLIT_PATTERN.split(text):
        if part is None:
            continue
        for line in part.splitlines():
            for piece in _INLINE_SEPARATOR.split(line):
                cleaned = piece.strip()
                if cleaned:
                    chunks.append(cleaned)

    # 去重保序；单条过长要截断 —— 否则一整段没有换行的文本会变成一条超长考点，
    # 插入 TEXT 列时 MySQL 直接抛 DataError（最终表现为 500）。
    seen: set[str] = set()
    points: list[str] = []
    truncated = 0
    for chunk in chunks:
        cleaned, was_truncated = clamp_text(chunk, MAX_SHORT_TEXT_LENGTH)
        if was_truncated:
            truncated += 1
        key = normalize_text(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        points.append(cleaned)
        if max_points and len(points) >= max_points:
            break

    if truncated:
        logger.warning(
            "有 %d 条考点超过 %d 字符已被截断（原文过长，请检查导入内容）",
            truncated,
            MAX_SHORT_TEXT_LENGTH,
        )
    return points


# --- 匹配（纯逻辑） ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个候选知识点。"""

    knowledge_point: str
    normalized: str
    source: str = "knowledge_graph"

    @property
    def weight(self) -> float:
        return SOURCE_WEIGHT.get(self.source, 0.9)


@dataclass(frozen=True, slots=True)
class MatchResult:
    """一条考点的匹配结果。"""

    point_text: str
    knowledge_point: str
    matched: bool
    score: float
    edit_similarity: float
    semantic_similarity: float
    backend: str
    alternatives: tuple[str, ...] = ()

    @property
    def confidence_label(self) -> str:
        if not self.matched:
            return "未匹配"
        if self.score >= 0.75:
            return "高置信"
        if self.score >= 0.55:
            return "中置信"
        return "低置信"


def build_candidates(entries: Iterable[tuple[str, str]]) -> list[Candidate]:
    """由 (知识点, 来源) 去重构造候选列表。"""
    seen: dict[str, Candidate] = {}
    for knowledge_point, source in entries:
        display = " ".join((knowledge_point or "").split()).strip()
        if not display:
            continue
        normalized = normalize_text(display)
        if not normalized:
            continue
        existing = seen.get(normalized)
        # 同一知识点保留来源权重更高的那条
        candidate = Candidate(knowledge_point=display, normalized=normalized, source=source)
        if existing is None or candidate.weight > existing.weight:
            seen[normalized] = candidate
    return list(seen.values())


def prefilter_candidates(
    normalized_point: str, candidates: Sequence[Candidate], limit: int
) -> list[tuple[float, Candidate]]:
    """编辑距离初筛：返回按相似度降序的 (相似度, 候选)。"""
    scored = [
        (similarity_with_containment(normalized_point, candidate.normalized), candidate)
        for candidate in candidates
    ]
    scored.sort(key=lambda item: (-item[0], item[1].knowledge_point))
    return scored[:limit] if limit > 0 else scored


@dataclass(slots=True)
class CandidateIndex:
    """候选知识点 + **预计算好的向量**。

    匹配时最常见的性能陷阱是「每个考点都把全部候选重新编码一遍」：
    200 个考点 × 30 个候选 = 6000 次编码（其中候选被重复编码 200 次）。
    把候选向量缓存下来后，整批匹配只需 2 次编码：
    1 次编码全部候选、1 次编码全部考点。
    """

    candidates: list[Candidate] = field(default_factory=list)
    vectors: list[Any] = field(default_factory=list)
    backend_name: str = ""

    def __len__(self) -> int:
        return len(self.candidates)

    @property
    def has_vectors(self) -> bool:
        return len(self.vectors) == len(self.candidates) and bool(self.candidates)


def build_index(
    candidates: Sequence[Candidate], backend: EmbeddingBackend | None = None
) -> CandidateIndex:
    """把候选知识点建成带向量的索引（候选只编码这一次）。"""
    engine = backend or resolve_backend()
    items = list(candidates)
    if not items:
        return CandidateIndex([], [], engine.name)

    try:
        vectors = engine.encode([candidate.normalized for candidate in items])
    except Exception as exc:  # 语义后端异常：退化为纯编辑距离匹配
        logger.warning("候选向量编码失败，改用纯编辑距离：%s: %s", type(exc).__name__, exc)
        vectors = []

    if len(vectors) != len(items):
        vectors = []
    return CandidateIndex(items, list(vectors), engine.name)


def _shortlist(
    normalized_point: str, index: CandidateIndex, limit: int
) -> list[tuple[float, int]]:
    """编辑距离初筛，返回 (相似度, 候选下标) 降序。"""
    scored = [
        (similarity_with_containment(normalized_point, candidate.normalized), position)
        for position, candidate in enumerate(index.candidates)
    ]
    scored.sort(key=lambda item: (-item[0], index.candidates[item[1]].knowledge_point))
    return scored[:limit] if limit > 0 else scored


def match_many(
    point_texts: Sequence[str],
    index: CandidateIndex | Sequence[Candidate],
    backend: EmbeddingBackend | None = None,
    prefilter_limit: int | None = None,
    min_score: float | None = None,
) -> list[MatchResult]:
    """批量匹配「编辑距离初筛 → 语义精筛」。

    与逐条调用 ``match_point`` 等价，但候选与考点各只编码一次。
    """
    engine = backend or resolve_backend()
    limit = settings.exam_match_prefilter_limit if prefilter_limit is None else prefilter_limit
    threshold = settings.exam_match_min_score if min_score is None else min_score

    if not isinstance(index, CandidateIndex):
        index = build_index(list(index), engine)

    results: list[MatchResult] = [
        MatchResult(
            point_text=text,
            knowledge_point="",
            matched=False,
            score=0.0,
            edit_similarity=0.0,
            semantic_similarity=0.0,
            backend=engine.name,
        )
        for text in point_texts
    ]
    if not index.candidates:
        return results

    # 1) 编辑距离初筛（纯字符串计算，无需编码）
    normalized = [normalize_text(text) for text in point_texts]
    shortlists: list[list[tuple[float, int]]] = [
        _shortlist(text, index, limit) if text else [] for text in normalized
    ]

    # 2) 语义精筛：把**所有待匹配考点**一次性编码
    semantic_available = index.has_vectors
    query_vectors: dict[int, Any] = {}
    pending = [
        (position, normalized[position])
        for position, items in enumerate(shortlists)
        if items and normalized[position] and semantic_available
    ]
    if pending:
        try:
            vectors = engine.encode([text for _position, text in pending])
        except Exception as exc:
            logger.warning("考点向量编码失败，改回纯编辑距离：%s: %s", type(exc).__name__, exc)
            vectors = []
        if len(vectors) == len(pending):
            for (position, _text), vector in zip(pending, vectors):
                query_vectors[position] = vector
        else:
            semantic_available = False

    # 3) 打分
    weight_semantic = settings.exam_match_semantic_weight
    weight_edit = settings.exam_match_edit_weight
    if semantic_available:
        total_weight = (weight_semantic + weight_edit) or 1.0
        w_semantic, w_edit = weight_semantic, weight_edit
    else:
        # 语义不可用时权重必须整体让给编辑距离，否则最高分只有 weight_edit
        # （默认 0.4），永远低于阈值，降级路径会彻底失效。
        total_weight, w_semantic, w_edit = 1.0, 0.0, 1.0

    for position, items in enumerate(shortlists):
        if not items:
            continue
        query_vector = query_vectors.get(position)
        ranked: list[tuple[float, float, float, Candidate]] = []
        for edit_score, candidate_position in items:
            candidate = index.candidates[candidate_position]
            if query_vector is not None and index.has_vectors:
                semantic = max(0.0, cosine_similarity(query_vector, index.vectors[candidate_position]))
            else:
                semantic = 0.0
            final = (w_semantic * semantic + w_edit * edit_score) / total_weight
            final *= candidate.weight
            ranked.append((final, edit_score, semantic, candidate))

        ranked.sort(key=lambda item: (-item[0], item[3].knowledge_point))
        best_score, best_edit, best_semantic, best = ranked[0]
        matched = best_score >= threshold
        if matched:
            alternatives = tuple(c.knowledge_point for _s, _e, _m, c in ranked[1:4])
        else:
            alternatives = tuple(c.knowledge_point for _s, _e, _m, c in ranked[:3])

        results[position] = MatchResult(
            point_text=point_texts[position],
            knowledge_point=best.knowledge_point if matched else "",
            matched=matched,
            score=round(best_score, 4),
            edit_similarity=round(best_edit, 4),
            semantic_similarity=round(best_semantic, 4),
            backend=engine.name,
            alternatives=alternatives,
        )
    return results


def match_point(
    point_text: str,
    candidates: CandidateIndex | Sequence[Candidate],
    backend: EmbeddingBackend | None = None,
    prefilter_limit: int | None = None,
    min_score: float | None = None,
) -> MatchResult:
    """对单条考点执行匹配（单条便利封装；批量场景请用 ``match_many``）。"""
    return match_many(
        [point_text],
        candidates,
        backend=backend,
        prefilter_limit=prefilter_limit,
        min_score=min_score,
    )[0]


# --- 掌握度与优先级（纯逻辑） -----------------------------------------------


@dataclass(frozen=True, slots=True)
class MasteryEvidence:
    """判定掌握程度所需的证据（来自阶段一 FSRS 与阶段二错题）。"""

    knowledge_point: str
    card_count: int = 0
    new_card_count: int = 0
    reviewed_card_count: int = 0
    avg_stability: float | None = None
    max_stability: float | None = None
    wrong_question_count: int = 0
    total_errors: int = 0
    weak_point_hit: bool = False

    @property
    def has_cards(self) -> bool:
        return self.card_count > 0


def compute_mastery(
    evidence: MasteryEvidence,
    weak_threshold: int | None = None,
    mastered_stability: float | None = None,
) -> str:
    """掌握程度判定（纯规则）。

    - ``weak``：命中薄弱点、完全没有任何卡片、或卡片全是从未复习的新卡；
    - ``mastered``：有已复习的卡片且平均稳定性达到阈值，且没有历史错误；
    - 其余为 ``moderate``。
    """
    threshold = settings.weak_point_threshold if weak_threshold is None else weak_threshold
    mastered_min = (
        settings.exam_mastered_stability if mastered_stability is None else mastered_stability
    )

    if evidence.weak_point_hit or evidence.total_errors >= threshold:
        return MASTERY_WEAK
    if not evidence.has_cards:
        return MASTERY_WEAK
    if evidence.reviewed_card_count == 0:
        return MASTERY_WEAK

    if (
        evidence.avg_stability is not None
        and evidence.avg_stability >= mastered_min
        and evidence.total_errors == 0
    ):
        return MASTERY_MASTERED

    return MASTERY_MODERATE


def classify_priority(matched: bool, mastery: str) -> str:
    """掌握程度 → 复习优先级。

    未匹配到知识点的考点按**高危**处理：考纲里出现但知识图谱中没有对应内容，
    意味着缺少复习材料，属于需要优先补上的风险项。
    """
    if not matched:
        return PRIORITY_HIGH
    if mastery == MASTERY_WEAK:
        return PRIORITY_HIGH
    if mastery == MASTERY_MASTERED:
        return PRIORITY_CONFIRM
    return PRIORITY_CONSOLIDATE


def build_priority_reason(matched: bool, mastery: str, evidence: MasteryEvidence) -> str:
    """给出一条人类可读的判定理由（规则拼装）。"""
    if not matched:
        return "知识图谱中暂无对应知识点，缺少复习材料，建议优先补充"

    if mastery == MASTERY_WEAK:
        if evidence.weak_point_hit:
            return f"该知识点是薄弱点（累计错误 {evidence.total_errors} 次）"
        if not evidence.has_cards:
            return "该知识点还没有任何复习卡片"
        if evidence.reviewed_card_count == 0:
            return f"有 {evidence.new_card_count} 张卡片但从未复习过"
        return f"累计错误 {evidence.total_errors} 次"

    if mastery == MASTERY_MASTERED:
        stability = evidence.avg_stability or 0.0
        return f"已复习 {evidence.reviewed_card_count} 张卡片，平均稳定性 {stability:.1f} 天，无历史错误"

    stability = evidence.avg_stability or 0.0
    return f"已有复习记录但掌握一般（平均稳定性 {stability:.1f} 天）"


def sort_findings(findings: Sequence["ExamFinding"]) -> list["ExamFinding"]:
    """复习清单排序：高危 → 巩固 → 确认；同级按匹配分降序。"""
    return sorted(
        findings,
        key=lambda f: (
            PRIORITY_ORDER.get(f.priority, 99),
            -f.match.score,
            f.point_text,
        ),
    )


# --- 结果结构 ---------------------------------------------------------------


@dataclass(slots=True)
class ExamFinding:
    """一条考点的完整匹配结论。"""

    exam_point_id: int
    point_text: str
    match: MatchResult
    evidence: MasteryEvidence
    mastery: str
    priority: str
    reason: str
    card_ids: list[int] = field(default_factory=list)

    @property
    def matched_knowledge_point(self) -> str | None:
        return self.match.knowledge_point or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exam_point_id": self.exam_point_id,
            "point_text": self.point_text,
            "matched_knowledge_point": self.matched_knowledge_point,
            "matched": self.match.matched,
            "match_score": self.match.score,
            "edit_similarity": self.match.edit_similarity,
            "semantic_similarity": self.match.semantic_similarity,
            "match_backend": self.match.backend,
            "confidence": self.match.confidence_label,
            "alternatives": list(self.match.alternatives),
            "priority": self.priority,
            "mastery": self.mastery,
            "reason": self.reason,
            "card_ids": list(self.card_ids),
        }


# --- DB 层：知识点索引 ------------------------------------------------------


def build_knowledge_index(db: Session, user_id: int) -> list[Candidate]:
    """构建该用户的知识点候选集。

    来源（即「知识图谱中的知识点」）：
    1. ``knowledge_graph`` 中**已人工确认**的三元组实体与目标（阶段三）；
    2. ``wrong_questions`` 的知识点（阶段二）；
    3. 卡片的三维标签——章节维度（阶段一/三转卡片时写入）。
    """
    entries: list[tuple[str, str]] = []

    for entity, target in db.execute(
        select(KnowledgeGraph.entity, KnowledgeGraph.target).where(
            KnowledgeGraph.user_id == user_id
        )
    ).all():
        entries.append((entity, "knowledge_graph"))
        entries.append((target, "knowledge_graph"))

    for (knowledge_point,) in db.execute(
        select(WrongQuestion.knowledge_point).where(WrongQuestion.user_id == user_id).distinct()
    ).all():
        entries.append((knowledge_point, "wrong_question"))

    for (chapters,) in db.execute(
        select(Card.tags[KEY_CHAPTERS]).where(Card.user_id == user_id)
    ).all():
        if isinstance(chapters, list):
            for chapter in chapters:
                entries.append((str(chapter), "card_tag"))

    candidates = build_candidates(entries)
    logger.info("知识点索引 user_id=%s 候选 %d 个", user_id, len(candidates))
    return candidates


# --- DB 层：学生掌握度证据 --------------------------------------------------


@dataclass(slots=True)
class MasteryContext:
    """一次请求内复用的掌握度证据集合。

    审计 H6：``mastery_evidence`` 原先对**每个知识点**都执行
    「关联卡片查询 + 错题聚合 + 全量薄弱点计算」，一次导入 200 条考点就是
    600 次查询（其中关联卡片是 JSON_CONTAINS + ILIKE 全表扫描）。
    现在把这些数据一次性取好，按知识点在内存里查表。
    """

    weak_names: set[str] = field(default_factory=set)
    related_cards: dict[str, set[int]] = field(default_factory=dict)
    errors: dict[str, tuple[int, int]] = field(default_factory=dict)
    card_stats: dict[int, tuple[bool, float | None]] = field(default_factory=dict)

    def evidence(self, knowledge_point: str) -> MasteryEvidence:
        if not knowledge_point:
            return MasteryEvidence(knowledge_point="")

        card_ids = self.related_cards.get(knowledge_point, set())
        card_count = new_count = reviewed_count = 0
        stabilities: list[float] = []
        for card_id in card_ids:
            stat = self.card_stats.get(card_id)
            if stat is None:
                continue
            is_new, stability = stat
            card_count += 1
            if is_new:
                new_count += 1
            else:
                reviewed_count += 1
            if stability is not None:
                stabilities.append(float(stability))

        wrong_question_count, total_errors = self.errors.get(knowledge_point, (0, 0))
        return MasteryEvidence(
            knowledge_point=knowledge_point,
            card_count=card_count,
            new_card_count=new_count,
            reviewed_card_count=reviewed_count,
            avg_stability=(sum(stabilities) / len(stabilities)) if stabilities else None,
            max_stability=max(stabilities) if stabilities else None,
            wrong_question_count=wrong_question_count,
            total_errors=total_errors,
            weak_point_hit=knowledge_point in self.weak_names,
        )


def build_mastery_context(
    db: Session, user_id: int, knowledge_points: Sequence[str]
) -> MasteryContext:
    """批量预取掌握度证据：固定 4 条查询，与知识点数量无关。"""
    from .wrong_question_service import list_weak_points, related_card_ids_bulk

    points = [point for point in dict.fromkeys(knowledge_points) if point]

    weak_names = {
        point.knowledge_point
        for point in list_weak_points(db, user_id, weak_only=True, with_related_cards=False)
    }

    related = related_card_ids_bulk(db, user_id, points) if points else {}

    all_card_ids: set[int] = set()
    for ids in related.values():
        all_card_ids.update(ids)

    card_stats: dict[int, tuple[bool, float | None]] = {}
    if all_card_ids:
        rows = db.execute(
            select(Card.id, Card.fsrs_last_review, Card.fsrs_stability).where(
                Card.user_id == user_id, Card.id.in_(all_card_ids)
            )
        ).all()
        for card_id, last_review, stability in rows:
            card_stats[int(card_id)] = (
                last_review is None,
                float(stability) if stability is not None else None,
            )

    errors: dict[str, tuple[int, int]] = {}
    if points:
        rows = db.execute(
            select(
                WrongQuestion.knowledge_point,
                func.count(WrongQuestion.id),
                func.coalesce(func.sum(WrongQuestion.error_count), 0),
            )
            .where(WrongQuestion.user_id == user_id, WrongQuestion.knowledge_point.in_(points))
            .group_by(WrongQuestion.knowledge_point)
        ).all()
        for point, count, total in rows:
            errors[point] = (int(count or 0), int(total or 0))

    return MasteryContext(
        weak_names=weak_names,
        related_cards=related,
        errors=errors,
        card_stats=card_stats,
    )


def mastery_evidence(db: Session, user_id: int, knowledge_point: str) -> MasteryEvidence:
    """单知识点的掌握度证据（保留单点接口，内部复用批量上下文）。"""
    if not knowledge_point:
        return MasteryEvidence(knowledge_point="")
    return build_mastery_context(db, user_id, [knowledge_point]).evidence(knowledge_point)


# --- DB 层：导入与清单 ------------------------------------------------------


def evaluate_match(
    db: Session,
    user_id: int,
    match: MatchResult,
    context: MasteryContext | None = None,
) -> tuple[MasteryEvidence, str, str]:
    """由**已算好的匹配结果**推导掌握度与优先级（不再重复编码）。"""
    if not match.matched:
        evidence = MasteryEvidence(knowledge_point="")
    elif context is not None:
        evidence = context.evidence(match.knowledge_point)
    else:
        evidence = mastery_evidence(db, user_id, match.knowledge_point)
    mastery = compute_mastery(evidence)
    priority = classify_priority(match.matched, mastery)
    reason = build_priority_reason(match.matched, mastery, evidence)
    return evidence, mastery, priority


def _match_all_points(
    db: Session, user_id: int, points: Sequence[str], backend: EmbeddingBackend | None
) -> tuple[list[MatchResult], str]:
    """一次建索引 + 一次批量匹配（候选与考点各编码一次）。"""
    engine = backend or resolve_backend()
    index = build_index(build_knowledge_index(db, user_id), engine)
    return match_many(points, index, backend=engine), engine.name


def import_exam_points(
    db: Session,
    user_id: int,
    text: str,
    max_points: int | None = None,
    backend: EmbeddingBackend | None = None,
) -> list[ExamFinding]:
    """导入考点文本：切分 → 匹配 → 判定优先级 → 落库。"""
    limit = max_points or settings.exam_max_points_per_import
    points = split_exam_points(text, max_points=limit)
    if not points:
        return []

    matches, backend_name = _match_all_points(db, user_id, points, backend)
    # 一次预取全部掌握度证据（原先是每个考点各查一次，O(N) 全表扫描）
    context = build_mastery_context(
        db, user_id, [m.knowledge_point for m in matches if m.matched]
    )
    findings: list[ExamFinding] = []

    for point_text, match in zip(points, matches):
        evidence, mastery, priority = evaluate_match(db, user_id, match, context)
        record = ExamPoint(
            user_id=user_id,
            point_text=point_text,
            matched_knowledge_point=match.knowledge_point or None,
            priority=priority,
            created_at=utcnow(),
        )
        db.add(record)
        db.flush()
        findings.append(
            ExamFinding(
                exam_point_id=record.id,
                point_text=point_text,
                match=match,
                evidence=evidence,
                mastery=mastery,
                priority=priority,
                reason=build_priority_reason(match.matched, mastery, evidence),
            )
        )

    logger.info(
        "导入考点 user_id=%s 共 %d 条（高危 %d / 巩固 %d / 确认 %d），匹配后端=%s",
        user_id,
        len(findings),
        sum(1 for f in findings if f.priority == PRIORITY_HIGH),
        sum(1 for f in findings if f.priority == PRIORITY_CONSOLIDATE),
        sum(1 for f in findings if f.priority == PRIORITY_CONFIRM),
        backend_name,
    )
    return sort_findings(findings)


def preview_matches(
    db: Session,
    user_id: int,
    text: str,
    max_points: int | None = None,
    backend: EmbeddingBackend | None = None,
) -> list[ExamFinding]:
    """试匹配：切分 + 匹配 + 判定优先级，但**不写入数据库**。

    用于「导入前预览」，也让使用者可以先确认匹配质量再决定是否入库。
    """
    limit = max_points or settings.exam_max_points_per_import
    points = split_exam_points(text, max_points=limit)
    if not points:
        return []

    matches, _backend_name = _match_all_points(db, user_id, points, backend)
    context = build_mastery_context(
        db, user_id, [m.knowledge_point for m in matches if m.matched]
    )

    findings: list[ExamFinding] = []
    for point_text, match in zip(points, matches):
        evidence, mastery, priority = evaluate_match(db, user_id, match, context)
        findings.append(
            ExamFinding(
                exam_point_id=0,
                point_text=point_text,
                match=match,
                evidence=evidence,
                mastery=mastery,
                priority=priority,
                reason=build_priority_reason(match.matched, mastery, evidence),
            )
        )
    return sort_findings(findings)


def list_exam_points(
    db: Session,
    user_id: int,
    offset: int = 0,
    limit: int = 50,
    priority: str | None = None,
) -> tuple[list[ExamPoint], int]:
    """分页查询已导入的考点。"""
    stmt = select(ExamPoint).where(ExamPoint.user_id == user_id)
    if priority:
        stmt = stmt.where(ExamPoint.priority == priority)

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    page = (
        stmt.order_by(ExamPoint.created_at.desc(), ExamPoint.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.execute(page).scalars().all()), total


def get_exam_point(db: Session, user_id: int, exam_point_id: int) -> ExamPoint | None:
    return db.execute(
        select(ExamPoint).where(
            ExamPoint.id == exam_point_id, ExamPoint.user_id == user_id
        )
    ).scalar_one_or_none()


def review_list(
    db: Session,
    user_id: int,
    priorities: Sequence[str] | None = None,
    limit: int = 200,
) -> list[ExamFinding]:
    """生成针对性复习清单（按优先级排序）。"""
    stmt = select(ExamPoint).where(ExamPoint.user_id == user_id)
    if priorities:
        stmt = stmt.where(ExamPoint.priority.in_(list(priorities)))

    records = list(
        db.execute(stmt.order_by(ExamPoint.created_at.desc(), ExamPoint.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    # 一次性建索引 + 批量匹配（此前是每个考点各自编码全部候选）
    matches, backend_name = _match_all_points(
        db, user_id, [record.point_text for record in records], None
    )

    # 一次预取掌握度证据（原先每条记录各查一次，O(N) 全表扫描）
    context = build_mastery_context(
        db,
        user_id,
        [r.matched_knowledge_point for r in records if r.matched_knowledge_point],
    )

    findings: list[ExamFinding] = []
    for record, match in zip(records, matches):
        if record.matched_knowledge_point:
            if not match.matched:
                # 库里已记过匹配结果，但重新匹配未达标：沿用库中的知识点
                match = MatchResult(
                    point_text=record.point_text,
                    knowledge_point=record.matched_knowledge_point,
                    matched=True,
                    score=0.0,
                    edit_similarity=0.0,
                    semantic_similarity=0.0,
                    backend=backend_name,
                )

        evidence = context.evidence(record.matched_knowledge_point or "")
        mastery = compute_mastery(evidence)
        findings.append(
            ExamFinding(
                exam_point_id=record.id,
                point_text=record.point_text,
                match=match,
                evidence=evidence,
                mastery=mastery,
                priority=record.priority,
                reason=build_priority_reason(match.matched, mastery, evidence),
            )
        )

    return sort_findings(findings)


def priority_summary(db: Session, user_id: int) -> dict[str, Any]:
    """考点优先级分布统计。"""
    rows = db.execute(
        select(ExamPoint.priority, func.count(ExamPoint.id))
        .where(ExamPoint.user_id == user_id)
        .group_by(ExamPoint.priority)
    ).all()
    counts = {priority: int(count) for priority, count in rows}
    matched_row = db.execute(
        select(func.count(ExamPoint.id)).where(
            ExamPoint.user_id == user_id, ExamPoint.matched_knowledge_point.is_not(None)
        )
    ).scalar_one()

    return {
        "total": sum(counts.values()),
        "matched": int(matched_row or 0),
        "unmatched": sum(counts.values()) - int(matched_row or 0),
        "by_priority": {
            PRIORITY_HIGH: counts.get(PRIORITY_HIGH, 0),
            PRIORITY_CONSOLIDATE: counts.get(PRIORITY_CONSOLIDATE, 0),
            PRIORITY_CONFIRM: counts.get(PRIORITY_CONFIRM, 0),
        },
        "generated_at": utcnow().isoformat(),
    }


# --- DB 层：插入复习队列 ----------------------------------------------------


def _existing_card_fronts(db: Session, user_id: int) -> set[str]:
    rows = db.execute(select(Card.front).where(Card.user_id == user_id)).scalars().all()
    return {str(front) for front in rows}


def build_card_payload(
    finding: ExamFinding, related_cards: Sequence[Card]
) -> tuple[str, str]:
    """由考点生成复习卡片的正反面。"""
    front = f"【考点】{finding.point_text}"
    knowledge_point = finding.match.knowledge_point

    if related_cards:
        lines = []
        for card in related_cards[:3]:
            lines.append(f"· {card.front} → {card.back}")
        back = f"关联知识点：{knowledge_point}\n" + "\n".join(lines)
    elif knowledge_point:
        back = f"关联知识点：{knowledge_point}\n（暂无关联卡片内容，请补充答案要点）"
    else:
        back = "该考点在知识图谱中暂无对应知识点，请补充答案要点"

    return front, back


def insert_into_review_queue(
    db: Session,
    user_id: int,
    exam_point_ids: Sequence[int] | None = None,
    priorities: Sequence[str] = (PRIORITY_HIGH,),
    limit: int = 100,
) -> tuple[list[ExamFinding], int]:
    """把考点插入 FSRS 复习队列（默认只插入高危考点）。

    返回 ``(已插入的考点结论, 因重复而跳过的数量)``。
    通过「卡片正面已存在」判定重复，因此重复调用是幂等的。
    """
    from .card_service import create_card
    from .wrong_question_service import related_cards

    findings = review_list(db, user_id, priorities=priorities, limit=limit)
    if exam_point_ids:
        wanted = set(exam_point_ids)
        findings = [f for f in findings if f.exam_point_id in wanted]

    existing_fronts = _existing_card_fronts(db, user_id)
    inserted: list[ExamFinding] = []
    skipped = 0

    for finding in findings:
        front, back = build_card_payload(
            finding,
            related_cards(db, user_id, finding.match.knowledge_point, limit=3)
            if finding.match.knowledge_point
            else [],
        )
        if front in existing_fronts:
            skipped += 1
            continue

        card = create_card(
            db,
            user_id,
            front=front,
            back=back,
            tags={
                "subjects": [],
                "chapters": [finding.match.knowledge_point] if finding.match.knowledge_point else [],
                "point_types": [],
            },
        )
        existing_fronts.add(front)
        finding.card_ids = [card.id]
        inserted.append(finding)

    logger.info(
        "考点插入复习队列 user_id=%s 新增 %d 张，跳过重复 %d 张",
        user_id,
        len(inserted),
        skipped,
    )
    return inserted, skipped


def delete_exam_point(db: Session, record: ExamPoint) -> int:
    """删除考点，并清理由它生成的复习卡片（正面为 ``【考点】<point_text>``）。"""
    from .card_service import delete_card as _delete_card

    front = f"【考点】{record.point_text}"
    orphans = list(
        db.execute(
            select(Card).where(Card.user_id == record.user_id, Card.front == front)
        ).scalars()
    )
    for card in orphans:
        _delete_card(db, card)

    db.delete(record)
    db.flush()
    logger.info("删除考点 id=%s 并清理卡片 %d 张", record.id, len(orphans))
    return len(orphans)
