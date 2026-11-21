"""实验档案模型（阶段四：药学专用 ELN）。

字段说明（对既定数据模型的两处扩展）
------------------------------------
「实验模板字段」明确要求 **药物结构** 与 **处理方式**，但既定列清单中没有它们，
且这两项都是单一标量、不适合塞进 JSON，因此本表增列：

- ``drug_structure``：药物名称与结构（结构式/分子式/SMILES）
- ``deviation_handling``：偏差处理方式

「试剂与批号」「仪器与校准信息」属于**一对多的结构化子字段**，
因此沿用既定列 ``reagents`` / ``instruments``，以 JSON 承载：

    reagents    = [{"name": "...", "lot": "...", "grade": "...", "supplier": "..."}]
    instruments = [{"name": "...", "model": "...", "calibration": "...", "calibrated_at": "..."}]

参考 AI4Green4Students（诺丁汉大学开源 ELN）的记录粒度：
实验类型 / 试剂与批号 / 仪器与校准 / 操作步骤 / 预期与实际结果 / 偏差与处理。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..timeutils import utcnow
from ._common import MYSQL_TABLE_ARGS, UTCDateTime

if TYPE_CHECKING:
    from .user import User

# 实验类型
EXPERIMENT_SYNTHESIS = "合成"
EXPERIMENT_ANALYSIS = "分析"
EXPERIMENT_PHARMACOLOGY = "药理"
EXPERIMENT_PHARMACEUTICS = "药剂"

EXPERIMENT_TYPES: tuple[str, ...] = (
    EXPERIMENT_SYNTHESIS,
    EXPERIMENT_ANALYSIS,
    EXPERIMENT_PHARMACOLOGY,
    EXPERIMENT_PHARMACEUTICS,
)

# 偏差类型
DEVIATION_OPERATION = "操作误差"
DEVIATION_REAGENT = "试剂问题"
DEVIATION_INSTRUMENT = "仪器异常"
DEVIATION_DESIGN = "方案设计缺陷"

DEVIATION_TYPES: tuple[str, ...] = (
    DEVIATION_OPERATION,
    DEVIATION_REAGENT,
    DEVIATION_INSTRUMENT,
    DEVIATION_DESIGN,
)


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 实验类型：合成 / 分析 / 药理 / 药剂（偏差回溯的第一匹配维度）
    experiment_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # --- 药物 ---
    drug_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    # 扩展列：药物结构（结构式 / 分子式 / SMILES）
    drug_structure: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 试剂与批号 / 仪器与校准（JSON 数组） ---
    reagents: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    instruments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    # --- 操作与结果 ---
    procedure: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 偏差（偏差类型是回溯引擎的第二匹配维度）---
    deviation_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deviation_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 扩展列：处理方式
    deviation_handling: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, index=True
    )

    user: Mapped["User"] = relationship(back_populates="experiments")

    @property
    def has_deviation(self) -> bool:
        """是否记录了偏差（无偏差的实验不参与回溯）。"""
        return bool(self.deviation_type) and bool((self.deviation_description or "").strip())

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<Experiment id={self.id} type={self.experiment_type} "
            f"drug={self.drug_name!r} deviation={self.deviation_type!r}>"
        )
