"""服务层标准接口（Python Protocol）。

架构原则：核心服务层定义标准接口，便于替换实现（例如无 Agent 降级模式、
测试替身、未来切换其他调度算法）而无需改动路由层。

接口必须**真的被实现**，否则就是死抽象。`SchedulingEngine` 由
`review_service.FsrsScheduler` 实现，并由 `tests/test_group5_delivery.py`
用 `isinstance` 结构校验锁定；原先的 `CardRepository` 与实际的函数式
服务风格不符且无人实现，已删除（审计 L15/C11）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .review_service import FsrsSnapshot, ScheduleResult


@runtime_checkable
class SchedulingEngine(Protocol):
    """复习调度引擎接口。"""

    def new_snapshot(self, card_id: int | None = None) -> FsrsSnapshot:
        """创建一个全新卡片的调度快照。"""
        ...

    def schedule(
        self,
        snapshot: FsrsSnapshot,
        rating: int,
        reviewed_at: datetime | None = None,
    ) -> ScheduleResult:
        """按评分推进调度状态。"""
        ...

    def preview(
        self, snapshot: FsrsSnapshot, now: datetime | None = None
    ) -> dict[int, ScheduleResult]:
        """预览四种评分各自的结果。"""
        ...

    def retrievability(
        self, snapshot: FsrsSnapshot, now: datetime | None = None
    ) -> float | None:
        """当前可回忆概率。"""
        ...
