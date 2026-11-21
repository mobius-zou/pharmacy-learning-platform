"""实验档案服务（阶段四）：药学专用 ELN + 偏差回溯规则引擎。

设计原则
--------
**纯规则引擎 + MySQL 查询，不依赖 AI。** 本模块不 import 任何 LLM / LangChain /
向量库代码，偏差回溯完全由确定性规则与 SQL 查询完成。

分层
----
- 上半部分：**纯函数规则引擎**（``ExperimentFacts`` / ``DeviationQuery`` /
  ``evaluate_match`` / ``rank_deviation_matches``），不接触数据库，可直接单测。
- 下半部分：DB 层（CRUD、模板、按类型检索）与模板定义。

偏差回溯匹配规则
----------------
必要条件（规范要求「偏差类型相同 + 实验类型相同」）：
  1. ``experiment_type`` 相同；
  2. ``deviation_type`` 相同 —— 当查询未指定偏差类型时（例如刚进入模板、
     尚未发生偏差）该条不适用，仅按实验类型召回，用于「主动提示」。

排序加分项（相关度，越大越靠前）：
  - 药物名称完全相同 / 互为子串；
  - 共用试剂（按名称）；批号相同额外加分；
  - 共用仪器（按名称）；
  - 处理方式存在（可供直接复用）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..logging_config import get_logger
from ..models import (
    DEVIATION_TYPES,
    EXPERIMENT_ANALYSIS,
    EXPERIMENT_PHARMACOLOGY,
    EXPERIMENT_PHARMACEUTICS,
    EXPERIMENT_SYNTHESIS,
    EXPERIMENT_TYPES,
    Experiment,
)
from ..limits import like_contains
from ..timeutils import utcnow

logger = get_logger(__name__)

# --- 相关度权重 -------------------------------------------------------------

W_BASE = 1.0                 # 必要条件命中
W_DRUG_EXACT = 1.0           # 药物名称完全相同
W_DRUG_PARTIAL = 0.4         # 药物名称相近（互为子串）
W_SHARED_REAGENT = 0.35      # 每种共用试剂
W_SAME_REAGENT_LOT = 0.3     # 共用试剂且批号一致
W_SHARED_INSTRUMENT = 0.25   # 每台共用仪器
W_HAS_HANDLING = 0.2         # 历史记录写明了处理方式
MAX_SHARED_BONUS_COUNT = 3   # 同类加分最多累计 3 项，避免堆料压制核心匹配

# 相似度分级阈值
RELEVANCE_HIGH = 2.0
RELEVANCE_MEDIUM = 1.3


# --- 纯数据模型 -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReagentFact:
    name: str
    lot: str = ""


@dataclass(frozen=True, slots=True)
class ExperimentFacts:
    """一条历史实验记录的匹配事实（不依赖 ORM，便于单测）。"""

    experiment_id: int
    experiment_type: str
    drug_name: str
    deviation_type: str
    deviation_description: str
    deviation_handling: str = ""
    created_at: datetime | None = None
    reagents: tuple[ReagentFact, ...] = ()
    instruments: tuple[str, ...] = ()

    @property
    def reagent_names(self) -> tuple[str, ...]:
        return tuple(reagent.name for reagent in self.reagents)


@dataclass(frozen=True, slots=True)
class DeviationQuery:
    """一次回溯查询。

    ``deviation_type`` 为空表示「尚未发生偏差」（进入模板时的主动提示），
    此时仅按实验类型召回。
    """

    experiment_type: str
    deviation_type: str | None = None
    drug_name: str = ""
    reagents: tuple[ReagentFact, ...] = ()
    instruments: tuple[str, ...] = ()
    exclude_experiment_id: int | None = None


@dataclass(frozen=True, slots=True)
class DeviationMatch:
    """一条命中的历史偏差。"""

    facts: ExperimentFacts
    relevance: float
    reasons: tuple[str, ...]

    @property
    def experiment_id(self) -> int:
        return self.facts.experiment_id

    @property
    def level(self) -> str:
        if self.relevance >= RELEVANCE_HIGH:
            return "high"
        if self.relevance >= RELEVANCE_MEDIUM:
            return "medium"
        return "low"

    @property
    def is_same_drug(self) -> bool:
        return any(reason.startswith("药物名称") for reason in self.reasons)


@dataclass(slots=True)
class TracebackResult:
    """回溯结果汇总。"""

    matches: list[DeviationMatch] = field(default_factory=list)
    deviation_type_counts: dict[str, int] = field(default_factory=dict)
    total_scanned: int = 0

    @property
    def total(self) -> int:
        return len(self.matches)

    @property
    def top_deviation_type(self) -> str | None:
        if not self.deviation_type_counts:
            return None
        return max(self.deviation_type_counts.items(), key=lambda item: (item[1], item[0]))[0]


# --- 归一化 -----------------------------------------------------------------


def normalize_name(value: str | None) -> str:
    """归一化名称：压缩空白、去括号注记与常见剂型后缀，便于模糊比对。"""
    if not value:
        return ""
    text = " ".join(str(value).split()).strip()
    for char in ("（", "(", "【", "["):
        index = text.find(char)
        if index > 0:
            text = text[:index]
    return text.strip(" ，,、。:：;；-—_/").lower()


def normalize_reagent_name(value: str | None) -> str:
    """试剂名归一化：去掉浓度/规格等括号内容与空白。"""
    return normalize_name(value)


def normalize_reagent_list(raw: Iterable[Any] | None) -> list[ReagentFact]:
    """把多种输入形式规范化为 ReagentFact 列表。

    支持：
    - ``{"name": "盐酸", "lot": "20240101"}``
    - ``"盐酸|20240101"``（名称|批号）
    - ``"盐酸"``
    """
    facts: list[ReagentFact] = []
    seen: set[tuple[str, str]] = set()

    for item in _iter_entries(raw):
        name = ""
        lot = ""
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("试剂") or "").strip()
            lot = str(item.get("lot") or item.get("batch") or item.get("批号") or "").strip()
        elif isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            if "|" in text:
                head, _, tail = text.partition("|")
                name, lot = head.strip(), tail.strip()
            else:
                name = text
        else:
            continue

        if not name:
            continue
        key = (normalize_reagent_name(name), lot)
        if key in seen:
            continue
        seen.add(key)
        facts.append(ReagentFact(name=name, lot=lot))

    return facts


def normalize_instrument_list(raw: Iterable[Any] | None) -> list[str]:
    """仪器名称列表（校准信息保留在原始 JSON 中，匹配只用名称）。"""
    names: list[str] = []
    for item in _iter_entries(raw):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("仪器") or "").strip()
        elif isinstance(item, str):
            name = item.strip()
        else:
            continue
        if name:
            names.append(name)
    return names


def _iter_entries(raw: Iterable[Any] | None) -> list[Any]:
    """统一成可迭代的条目列表（兼容传单个 dict 或字符串）。"""
    if raw is None:
        return []
    if isinstance(raw, (dict, str)):
        return [raw]
    return list(raw)


# --- 规则引擎（纯函数） -----------------------------------------------------


def evaluate_match(
    facts: ExperimentFacts, query: DeviationQuery
) -> DeviationMatch | None:
    """判断一条历史偏差是否命中，并计算相关度。

    返回 ``None`` 表示不满足必要条件（实验类型不同，或偏差类型不同）。
    """
    # 必要条件 1：实验类型相同
    if normalize_name(facts.experiment_type) != normalize_name(query.experiment_type):
        return None

    # 必要条件 2：偏差类型相同（查询未指定时跳过，用于模板主动提示）
    if query.deviation_type:
        if normalize_name(facts.deviation_type) != normalize_name(query.deviation_type):
            return None

    # 不该把自己（正在记录的这一条）当成历史
    if query.exclude_experiment_id is not None and facts.experiment_id == query.exclude_experiment_id:
        return None

    score = W_BASE
    reasons: list[str] = ["实验类型相同"]
    if query.deviation_type:
        reasons.append("偏差类型相同")

    # 药物名称
    fact_drug = normalize_name(facts.drug_name)
    query_drug = normalize_name(query.drug_name)
    if fact_drug and query_drug:
        if fact_drug == query_drug:
            score += W_DRUG_EXACT
            reasons.append(f"药物名称相同（{facts.drug_name}）")
        elif fact_drug in query_drug or query_drug in fact_drug:
            score += W_DRUG_PARTIAL
            reasons.append(f"药物名称相近（{facts.drug_name}）")

    # 共用试剂（批号一致再加分）
    query_reagents = {normalize_reagent_name(r.name): r for r in query.reagents}
    shared: list[str] = []
    for reagent in facts.reagents:
        key = normalize_reagent_name(reagent.name)
        if key and key in query_reagents:
            shared.append(reagent.name)
            if reagent.lot and query_reagents[key].lot and reagent.lot == query_reagents[key].lot:
                score += W_SAME_REAGENT_LOT
    if shared:
        bonus_count = min(len(shared), MAX_SHARED_BONUS_COUNT)
        score += W_SHARED_REAGENT * bonus_count
        reasons.append("共用试剂：" + "、".join(shared[:4]))
        same_lot = [
            r.name
            for r in facts.reagents
            if normalize_reagent_name(r.name) in query_reagents
            and r.lot
            and query_reagents[normalize_reagent_name(r.name)].lot == r.lot
        ]
        if same_lot:
            reasons.append("批号相同：" + "、".join(same_lot[:3]))

    # 共用仪器
    query_instruments = {normalize_name(name) for name in query.instruments if name}
    shared_instruments = [
        name for name in facts.instruments if normalize_name(name) in query_instruments
    ]
    if shared_instruments:
        score += W_SHARED_INSTRUMENT * min(len(shared_instruments), MAX_SHARED_BONUS_COUNT)
        reasons.append("共用仪器：" + "、".join(shared_instruments[:3]))

    # 历史记录写明了处理方式 → 更有参考价值
    if (facts.deviation_handling or "").strip():
        score += W_HAS_HANDLING
        reasons.append("已有处理方式可复用")

    return DeviationMatch(facts=facts, relevance=round(score, 4), reasons=tuple(reasons))


def rank_deviation_matches(
    facts_list: Sequence[ExperimentFacts],
    query: DeviationQuery,
    limit: int = 10,
) -> list[DeviationMatch]:
    """对所有历史记录评分排序。

    排序：相关度降序 → 时间降序（新的更相关）→ id 降序，保证结果稳定。
    """
    matches = [
        match
        for facts in facts_list
        if (match := evaluate_match(facts, query)) is not None
    ]
    matches.sort(
        key=lambda m: (
            -m.relevance,
            -(m.facts.created_at.timestamp() if m.facts.created_at else 0.0),
            -m.facts.experiment_id,
        )
    )
    return matches[:limit] if limit > 0 else matches


def build_traceback(
    facts_list: Sequence[ExperimentFacts], query: DeviationQuery, limit: int = 10
) -> TracebackResult:
    """回溯 + 统计：同时给出按偏差类型的分布，用于主动提示。"""
    matches = rank_deviation_matches(facts_list, query, limit=limit)
    counts = Counter(
        facts.deviation_type
        for facts in facts_list
        if facts.deviation_type
        and normalize_name(facts.experiment_type) == normalize_name(query.experiment_type)
    )
    return TracebackResult(
        matches=matches,
        deviation_type_counts=dict(counts.most_common()),
        total_scanned=len(facts_list),
    )


# --- 模板定义（药学专用 ELN） -----------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentTemplate:
    """某类实验的记录骨架与风险清单。"""

    experiment_type: str
    label: str
    typical_reagents: tuple[str, ...]
    typical_instruments: tuple[str, ...]
    procedure_steps: tuple[str, ...]
    key_parameters: tuple[str, ...]
    risk_points: tuple[str, ...]


TEMPLATES: dict[str, ExperimentTemplate] = {
    EXPERIMENT_SYNTHESIS: ExperimentTemplate(
        experiment_type=EXPERIMENT_SYNTHESIS,
        label="合成实验",
        typical_reagents=("起始原料", "溶剂", "催化剂", "碱", "淬灭剂", "重结晶溶剂"),
        typical_instruments=("磁力搅拌器", "旋转蒸发仪", "熔点仪", "HPLC", "NMR"),
        procedure_steps=(
            "核对起始原料批号与纯度",
            "按投料比加料并记录实际称量值",
            "控温反应并记录温度曲线",
            "TLC/LC-MS 监控反应进程",
            "淬灭、萃取、干燥、浓缩",
            "重结晶或柱层析纯化",
            "称重计算收率并测熔点/纯度",
        ),
        key_parameters=("投料摩尔比", "反应温度", "反应时间", "溶剂用量", "收率", "纯度"),
        risk_points=(
            "试剂批号变更导致杂质谱变化",
            "无水/惰性气氛未保持，原料水解",
            "控温失灵导致副反应增加",
            "重结晶溶剂选择不当，收率偏低",
        ),
    ),
    EXPERIMENT_ANALYSIS: ExperimentTemplate(
        experiment_type=EXPERIMENT_ANALYSIS,
        label="分析实验",
        typical_reagents=("标准品", "流动相", "缓冲盐", "内标物", "衍生化试剂"),
        typical_instruments=("HPLC/UPLC", "紫外分光光度计", "天平", "pH 计", "移液器"),
        procedure_steps=(
            "确认色谱柱型号与使用次数",
            "现配流动相并脱气、调 pH",
            "系统适用性试验（理论板数/拖尾因子/重复性）",
            "标准曲线与空白测定",
            "样品平行测定（n≥2）并记录",
            "计算回收率与 RSD",
        ),
        key_parameters=("波长", "流速", "柱温", "进样体积", "标准曲线 R²", "回收率", "RSD"),
        risk_points=(
            "流动相未现配或未脱气，基线漂移",
            "标准品过期/降解，标准曲线漂移",
            "色谱柱柱效下降，峰形拖尾",
            "天平未校准，称量偏差放大",
        ),
    ),
    EXPERIMENT_PHARMACOLOGY: ExperimentTemplate(
        experiment_type=EXPERIMENT_PHARMACOLOGY,
        label="药理实验",
        typical_reagents=("受试药物", "溶媒", "阳性对照药", "麻醉剂", "生理盐水"),
        typical_instruments=("动物天平", "灌胃针/注射器", "酶标仪", "行为学装置"),
        procedure_steps=(
            "核对动物伦理批件与动物合格证",
            "动物适应期与随机分组",
            "按体重计算给药剂量并记录",
            "给药并记录时间点",
            "指标观察/取样/检测",
            "数据统计与组间比较",
        ),
        key_parameters=("动物种属品系", "样本量 n", "给药剂量", "给药途径", "给药频次", "观察时间点"),
        risk_points=(
            "分组未随机化造成基线不齐",
            "给药体积超过动物耐受上限",
            "溶媒与受试物不相容导致沉淀",
            "样本量不足，统计效能不够",
        ),
    ),
    EXPERIMENT_PHARMACEUTICS: ExperimentTemplate(
        experiment_type=EXPERIMENT_PHARMACEUTICS,
        label="药剂实验",
        typical_reagents=("原料药", "辅料", "包衣材料", "表面活性剂", "介质"),
        typical_instruments=("高速剪切乳化机", "高压均质机", "粒径仪", "溶出仪", "稳定性试验箱"),
        procedure_steps=(
            "按处方核算各辅料用量",
            "制备初乳/混悬液并记录剪切参数",
            "均质/整粒并记录压力与次数",
            "测定粒径分布与 Zeta 电位",
            "测定包封率与载药量",
            "溶出度/释放度测定",
            "加速稳定性考察取样",
        ),
        key_parameters=("处方比例", "剪切转速", "均质压力", "均质次数", "粒径 PDI", "包封率", "释放度"),
        risk_points=(
            "辅料批号间差异导致粒径波动",
            "均质压力过高引起药物降解",
            "包封率偏低，突释明显",
            "释放介质选择不当，区分力不足",
        ),
    ),
}


def get_template(experiment_type: str) -> ExperimentTemplate | None:
    return TEMPLATES.get(experiment_type)


def list_templates() -> list[ExperimentTemplate]:
    return [TEMPLATES[key] for key in EXPERIMENT_TYPES if key in TEMPLATES]


# --- 提示文案（规则拼装，不使用 AI） ----------------------------------------


def build_advice(result: TracebackResult, experiment_type: str = "") -> str:
    """把回溯结果拼装成一句人话提示（纯模板，不调用任何模型）。"""
    scope = f"「{experiment_type}」类" if experiment_type else ""

    if not result.matches:
        return f"暂无{scope}历史偏差记录，本次可作为基线记录。" if scope else "暂无历史偏差记录。"

    parts = [f"发现 {result.total} 条{scope}历史偏差"]
    top = result.top_deviation_type
    if top:
        parts.append(f"其中「{top}」最多（{result.deviation_type_counts.get(top, 0)} 次）")

    strongest = result.matches[0]
    if strongest.is_same_drug:
        parts.append(f"有同药物（{strongest.facts.drug_name}）的偏差记录，建议重点复查")

    text = "，".join(parts) + "。"

    handling = next(
        (m.facts.deviation_handling for m in result.matches if (m.facts.deviation_handling or "").strip()),
        "",
    )
    if handling:
        text += f" 历史处理方式可参考：{handling[:100]}"
    return text


# --- ORM 适配 ---------------------------------------------------------------


def to_facts(record: Experiment) -> ExperimentFacts:
    """ORM 记录 → 匹配事实。"""
    return ExperimentFacts(
        experiment_id=record.id,
        experiment_type=record.experiment_type or "",
        drug_name=record.drug_name or "",
        deviation_type=record.deviation_type or "",
        deviation_description=record.deviation_description or "",
        deviation_handling=record.deviation_handling or "",
        created_at=record.created_at,
        reagents=tuple(normalize_reagent_list(record.reagents)),
        instruments=tuple(normalize_instrument_list(record.instruments)),
    )


# --- DB 层 ------------------------------------------------------------------


def list_deviation_candidates(
    db: Session,
    user_id: int,
    experiment_type: str | None = None,
    exclude_experiment_id: int | None = None,
) -> list[Experiment]:
    """取出参与回溯的历史记录（**仅含有偏差的记录**）。

    规范要求按「偏差类型 + 实验类型」匹配，因此无偏差的记录不参与；
    这里先把候选集交给纯规则引擎评分。
    """
    stmt = select(Experiment).where(
        Experiment.user_id == user_id,
        Experiment.deviation_type.is_not(None),
        Experiment.deviation_description.is_not(None),
    )
    if experiment_type:
        stmt = stmt.where(Experiment.experiment_type == experiment_type)
    if exclude_experiment_id is not None:
        stmt = stmt.where(Experiment.id != exclude_experiment_id)
    return list(db.execute(stmt).scalars().all())


def find_related_deviations(
    db: Session,
    user_id: int,
    query: DeviationQuery,
    limit: int = 10,
) -> TracebackResult:
    """偏差回溯：检索历史记录并返回相关度排序后的结果。"""
    candidates = list_deviation_candidates(
        db,
        user_id,
        experiment_type=query.experiment_type,
        exclude_experiment_id=query.exclude_experiment_id,
    )
    facts = [to_facts(record) for record in candidates]
    result = build_traceback(facts, query, limit=limit)
    logger.info(
        "偏差回溯 user_id=%s type=%s deviation=%s 扫描 %d 条，命中 %d 条",
        user_id,
        query.experiment_type,
        query.deviation_type,
        result.total_scanned,
        result.total,
    )
    return result


def template_warnings(
    db: Session,
    user_id: int,
    experiment_type: str,
    drug_name: str = "",
    reagents: Iterable[Any] | None = None,
    instruments: Iterable[Any] | None = None,
    limit: int = 10,
) -> TracebackResult:
    """进入某类实验模板时的**主动提示**。

    此时尚未发生偏差，因此只按实验类型（+ 药物/试剂相似度排序）召回历史偏差，
    并给出偏差类型分布。
    """
    query = DeviationQuery(
        experiment_type=experiment_type,
        deviation_type=None,  # 主动提示阶段未知偏差类型
        drug_name=drug_name,
        reagents=tuple(normalize_reagent_list(reagents)),
        instruments=tuple(normalize_instrument_list(instruments)),
    )
    return find_related_deviations(db, user_id, query, limit=limit)


# --- CRUD -------------------------------------------------------------------


def create_experiment(
    db: Session,
    user_id: int,
    experiment_type: str,
    drug_name: str,
    drug_structure: str | None = None,
    reagents: Iterable[Any] | None = None,
    instruments: Iterable[Any] | None = None,
    procedure: str | None = None,
    expected_result: str | None = None,
    actual_result: str | None = None,
    deviation_description: str | None = None,
    deviation_type: str | None = None,
    deviation_handling: str | None = None,
) -> Experiment:
    """创建实验记录。

    偏差字段可留空（无偏差实验）；一旦填写偏差描述就必须给出偏差类型，
    否则回溯引擎无法按类型匹配。
    """
    if experiment_type not in EXPERIMENT_TYPES:
        raise ValueError(
            f"实验类型必须是 {'、'.join(EXPERIMENT_TYPES)} 之一，收到：{experiment_type!r}"
        )
    if deviation_type and deviation_type not in DEVIATION_TYPES:
        raise ValueError(
            f"偏差类型必须是 {'、'.join(DEVIATION_TYPES)} 之一，收到：{deviation_type!r}"
        )

    description = (deviation_description or "").strip()
    if description and not deviation_type:
        raise ValueError("填写了偏差描述就必须选择偏差类型，否则无法参与偏差回溯")

    record = Experiment(
        user_id=user_id,
        experiment_type=experiment_type,
        drug_name=drug_name.strip(),
        drug_structure=(drug_structure or "").strip() or None,
        reagents=_reagents_to_json(reagents),
        instruments=_instruments_to_json(instruments),
        procedure=(procedure or "").strip() or None,
        expected_result=(expected_result or "").strip() or None,
        actual_result=(actual_result or "").strip() or None,
        deviation_description=description or None,
        deviation_type=deviation_type or None,
        deviation_handling=(deviation_handling or "").strip() or None,
        created_at=utcnow(),
    )
    db.add(record)
    db.flush()
    logger.info(
        "创建实验记录 id=%s type=%s drug=%s deviation=%s",
        record.id,
        record.experiment_type,
        record.drug_name,
        record.deviation_type,
    )
    return record


def _reagent_to_json(item: Any) -> dict[str, Any] | None:
    """试剂 → 落库 JSON。

    与匹配用的 ``ReagentFact`` 不同，这里**保留调用方给出的全部字段**
    （规格、供应商等），只把中文键名归一化为英文键，避免写入时丢数据。
    """
    if isinstance(item, dict):
        data = {str(key): value for key, value in item.items() if value is not None}
        if "试剂" in data and "name" not in data:
            data["name"] = data.pop("试剂")
        if "批号" in data and "lot" not in data:
            data["lot"] = data.pop("批号")
        return data if str(data.get("name", "")).strip() else None

    text = str(item or "").strip()
    if not text:
        return None
    if "|" in text:
        head, _, tail = text.partition("|")
        head, tail = head.strip(), tail.strip()
        return {"name": head, "lot": tail} if head else None
    return {"name": text}


def _reagents_to_json(raw: Iterable[Any] | None) -> list[dict[str, Any]]:
    """批量转换试剂，去重且丢弃空条目。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _iter_entries(raw):
        data = _reagent_to_json(item)
        if not data:
            continue
        key = normalize_reagent_name(str(data.get("name")))
        if key in seen:
            continue
        seen.add(key)
        out.append(data)
    return out


def _instruments_to_json(raw: Iterable[Any] | None) -> list[dict[str, Any]]:
    """仪器 → 落库 JSON，保留校准信息等全部字段。"""
    out: list[dict[str, Any]] = []
    for item in _iter_entries(raw):
        data = _instrument_to_json(item)
        if str(data.get("name", "")).strip():
            out.append(data)
    return out


def _instrument_to_json(item: Any) -> dict[str, Any]:
    """仪器保留调用方给出的校准信息等附加字段。"""
    if isinstance(item, dict):
        return {str(key): value for key, value in item.items() if value is not None}
    return {"name": str(item).strip()}


def get_experiment(db: Session, user_id: int, experiment_id: int) -> Experiment | None:
    stmt = select(Experiment).where(
        Experiment.id == experiment_id, Experiment.user_id == user_id
    )
    return db.execute(stmt).scalar_one_or_none()


def list_experiments(
    db: Session,
    user_id: int,
    offset: int = 0,
    limit: int = 20,
    experiment_type: str | None = None,
    drug_name: str | None = None,
    deviation_type: str | None = None,
    only_deviations: bool = False,
    keyword: str | None = None,
) -> tuple[list[Experiment], int]:
    """分页查询实验记录。"""
    stmt = select(Experiment).where(Experiment.user_id == user_id)

    if experiment_type:
        stmt = stmt.where(Experiment.experiment_type == experiment_type)
    if drug_name:
        pattern, escape = like_contains(drug_name.strip())
        stmt = stmt.where(Experiment.drug_name.ilike(pattern, escape=escape))
    if deviation_type:
        stmt = stmt.where(Experiment.deviation_type == deviation_type)
    if only_deviations:
        stmt = stmt.where(Experiment.deviation_type.is_not(None))
    if keyword:
        pattern, escape = like_contains(keyword.strip())
        stmt = stmt.where(
            Experiment.drug_name.ilike(pattern, escape=escape)
            | Experiment.procedure.ilike(pattern, escape=escape)
            | Experiment.deviation_description.ilike(pattern, escape=escape)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    page = (
        stmt.order_by(Experiment.created_at.desc(), Experiment.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.execute(page).scalars().all()), total


def update_experiment(
    db: Session,
    record: Experiment,
    **fields: Any,
) -> Experiment:
    """更新实验记录（只更新显式传入的字段）。"""
    if "experiment_type" in fields and fields["experiment_type"] is not None:
        if fields["experiment_type"] not in EXPERIMENT_TYPES:
            raise ValueError(f"实验类型非法：{fields['experiment_type']!r}")
        record.experiment_type = fields["experiment_type"]

    if "deviation_type" in fields:
        value = fields["deviation_type"]
        if value is not None and value not in DEVIATION_TYPES:
            raise ValueError(f"偏差类型非法：{value!r}")
        record.deviation_type = value

    scalar_fields = (
        "drug_name",
        "drug_structure",
        "procedure",
        "expected_result",
        "actual_result",
        "deviation_description",
        "deviation_handling",
    )
    for name in scalar_fields:
        # 注意：只要调用方显式传了这个键（哪怕值是 None），就应当写入。
        # 旧实现 `fields[name] is not None` 会让「想把某个可空字段清空」变得做不到。
        if name in fields:
            value = fields[name]
            setattr(record, name, None if value is None else (str(value).strip() or None))

    if "reagents" in fields and fields["reagents"] is not None:
        record.reagents = _reagents_to_json(fields["reagents"])
    if "instruments" in fields and fields["instruments"] is not None:
        record.instruments = _instruments_to_json(fields["instruments"])

    if (record.deviation_description or "").strip() and not record.deviation_type:
        raise ValueError("填写了偏差描述就必须选择偏差类型")

    db.flush()
    logger.info("更新实验记录 id=%s", record.id)
    return record


def delete_experiment(db: Session, record: Experiment) -> None:
    record_id = record.id
    db.delete(record)
    db.flush()
    logger.info("删除实验记录 id=%s", record_id)


# --- 统计 -------------------------------------------------------------------


def deviation_summary(db: Session, user_id: int) -> dict[str, Any]:
    """偏差统计：按实验类型与偏差类型交叉汇总，用于主动提示与回顾。"""
    by_type = db.execute(
        select(Experiment.experiment_type, func.count(Experiment.id))
        .where(Experiment.user_id == user_id)
        .group_by(Experiment.experiment_type)
    ).all()

    by_deviation = db.execute(
        select(Experiment.deviation_type, func.count(Experiment.id))
        .where(Experiment.user_id == user_id, Experiment.deviation_type.is_not(None))
        .group_by(Experiment.deviation_type)
    ).all()

    cross = db.execute(
        select(Experiment.experiment_type, Experiment.deviation_type, func.count(Experiment.id))
        .where(Experiment.user_id == user_id, Experiment.deviation_type.is_not(None))
        .group_by(Experiment.experiment_type, Experiment.deviation_type)
    ).all()

    top_drugs = db.execute(
        select(Experiment.drug_name, func.count(Experiment.id))
        .where(Experiment.user_id == user_id, Experiment.deviation_type.is_not(None))
        .group_by(Experiment.drug_name)
        .order_by(func.count(Experiment.id).desc(), Experiment.drug_name.asc())
        .limit(10)
    ).all()

    return {
        "total": sum(count for _key, count in by_type),
        "deviation_total": sum(count for _key, count in by_deviation),
        "by_experiment_type": {key: count for key, count in by_type},
        "by_deviation_type": {key: count for key, count in by_deviation if key},
        "cross": [
            {"experiment_type": exp_type, "deviation_type": dev_type, "count": count}
            for exp_type, dev_type, count in cross
            if dev_type
        ],
        "top_drugs_with_deviations": [
            {"drug_name": name, "count": count} for name, count in top_drugs
        ],
        # 无偏差记录的实验类型不应出现「0 偏差」的误导性结论，这里保留原始计数供前端判断
        "unknown_deviations": sum(
            count for dev_type, count in by_deviation if not dev_type
        ),
        "generated_at": utcnow().isoformat(),
    }
