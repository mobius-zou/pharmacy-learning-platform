"""性能回归测试（批次 3）。

两个曾经真实存在的问题：

1. **候选被重复编码**：每个考点匹配时都把全部候选重新编码一遍。
   20 个考点 × 30 个候选 = 620 条文本，而唯一文本只有 50 条。
2. **热路径 N+1**：``list_weak_points`` 对每个知识点各跑一次
   ``related_card_ids``（JSON_CONTAINS + ILIKE 全表扫描），
   于是 ``/review/due`` 的查询数随知识点数量线性增长。

这里用「编码计数替身」和「SQL 语句计数」把两条都钉死。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.embeddings import HashingBackend
from app.models import Card, WrongQuestion
from app.services import exam_match_service as exam_service
from app.services import wrong_question_service as wq_service
from tests.conftest import requires_db


class CountingBackend(HashingBackend):
    """记录 encode 调用次数与编码文本数。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.texts = 0

    def encode(self, texts):  # noqa: ANN001
        self.calls += 1
        self.texts += len(texts)
        return super().encode(texts)


@contextmanager
def count_sql(engine) -> Iterator[dict[str, int]]:
    """统计在上下文内执行的 SQL 语句条数。"""
    counter = {"count": 0}

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter["count"] += 1

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


# --- 编码次数（无需数据库） -------------------------------------------------


def test_candidates_and_points_encoded_exactly_once() -> None:
    """整批匹配只应编码「候选一次 + 考点一次」，与考点数量无关。"""
    candidates = exam_service.build_candidates(
        [(f"知识点{i}的作用机制", "knowledge_graph") for i in range(30)]
    )
    points = [f"考点{i}的作用机制" for i in range(20)]

    backend = CountingBackend()
    index = exam_service.build_index(candidates, backend)
    exam_service.match_many(points, index, backend=backend)

    encoded_once = len(candidates) + len(points)  # 50 条唯一文本
    assert backend.calls == 2, f"应只编码两次（候选+考点），实际 {backend.calls} 次"
    assert backend.texts == encoded_once, (
        f"应只编码 {encoded_once} 条唯一文本，实际 {backend.texts} 条（候选被重复编码）"
    )


def test_match_many_matches_one_by_one_results() -> None:
    """批量匹配与逐条匹配结果必须完全一致（重构不得改变行为）。"""
    candidates = exam_service.build_candidates(
        [
            ("阿司匹林的作用机制", "knowledge_graph"),
            ("青霉素的抗菌机制", "knowledge_graph"),
            ("半衰期的计算方法", "knowledge_graph"),
        ]
    )
    points = ["阿司匹林的作用机制", "aspirin 的作用机制", "完全无关的实验室安全制度"]

    batched = exam_service.match_many(
        points, exam_service.build_index(candidates, HashingBackend()), backend=HashingBackend()
    )
    one_by_one = [
        exam_service.match_point(point, candidates, backend=HashingBackend())
        for point in points
    ]

    for left, right in zip(batched, one_by_one):
        assert left.matched == right.matched
        assert left.knowledge_point == right.knowledge_point
        assert left.score == right.score
        assert left.alternatives == right.alternatives


def test_match_many_with_empty_candidates() -> None:
    results = exam_service.match_many(["任意考点"], [], backend=HashingBackend())

    assert len(results) == 1
    assert results[0].matched is False


def test_match_many_handles_backend_failure() -> None:
    """语义后端失败时批量路径同样要退化为纯编辑距离。"""
    class BrokenBackend:
        name = "broken"
        is_semantic = True

        def encode(self, texts):  # noqa: ANN001
            raise RuntimeError("模型挂了")

    candidates = exam_service.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])
    results = exam_service.match_many(
        ["阿司匹林的作用机制"], candidates, backend=BrokenBackend()
    )

    assert results[0].matched is True, "编辑距离相似度为 1.0，应仍然匹配"
    assert results[0].semantic_similarity == 0.0


# --- 查询次数（需要数据库） -------------------------------------------------


def _seed_weak_points(db_session, user_id: int, count: int) -> None:
    """造 count 个薄弱知识点，各自关联一张卡片。"""
    from app.timeutils import utcnow

    for index in range(count):
        point = f"知识点{index}"
        db_session.add(
            WrongQuestion(
                user_id=user_id,
                question_text=f"{point} 的题目",
                knowledge_point=point,
                error_count=5,  # 远超阈值
                created_at=utcnow(),
            )
        )
        db_session.add(
            Card(
                user_id=user_id,
                front=f"{point} 的内容",
                back="答案",
                tags={"subjects": [], "chapters": [point], "point_types": []},
                created_at=utcnow(),
            )
        )
    db_session.flush()


@requires_db
@pytest.mark.db
def test_weak_point_query_count_does_not_grow_with_points(db_session, db_engine, user_id) -> None:
    """查询数不得随知识点数量线性增长（原先每个知识点一次关联查询）。"""
    _seed_weak_points(db_session, user_id, 2)
    with count_sql(db_engine) as small:
        points_small = wq_service.list_weak_points(db_session, user_id, weak_only=True)
    assert len(points_small) == 2

    _seed_weak_points(db_session, user_id, 4)
    # 上述 seed 会再插入「知识点0/1」重复行，这里统一按聚合口径比较
    with count_sql(db_engine) as large:
        points_large = wq_service.list_weak_points(db_session, user_id, weak_only=True)

    assert len(points_large) > len(points_small)
    assert large["count"] == small["count"], (
        f"知识点从 {len(points_small)} 增到 {len(points_large)} 时查询数应保持不变，"
        f"实际 {small['count']} -> {large['count']}（存在 N+1）"
    )


@requires_db
@pytest.mark.db
def test_bulk_attribution_uses_single_query(db_session, db_engine, user_id) -> None:
    """N 个知识点的关联归属只应产生 1 条 SQL。"""
    _seed_weak_points(db_session, user_id, 5)
    points = [f"知识点{i}" for i in range(5)]

    with count_sql(db_engine) as counter:
        mapping = wq_service.related_card_ids_bulk(db_session, user_id, points)

    assert counter["count"] == 1, f"应为 1 条查询，实际 {counter['count']} 条"
    assert set(mapping) == set(points)
    assert all(len(ids) == 1 for ids in mapping.values())


@requires_db
@pytest.mark.db
def test_bulk_attribution_matches_single_point_lookup(db_session, user_id) -> None:
    """批量归属的结果必须与逐点查询一致（重构不得改变语义）。"""
    _seed_weak_points(db_session, user_id, 3)
    points = [f"知识点{i}" for i in range(3)]

    bulk = wq_service.related_card_ids_bulk(db_session, user_id, points)
    for point in points:
        single = wq_service.related_card_ids(db_session, user_id, [point])
        assert bulk[point] == single, f"{point} 的归属结果不一致"


@requires_db
@pytest.mark.db
def test_due_endpoint_query_count_is_bounded(client, auth_headers, db_session, user_id) -> None:
    """/review/due 的查询数不应随薄弱知识点数量增长（去掉重复聚合后）。"""
    from app.database import get_engine

    _seed_weak_points(db_session, user_id, 2)
    with count_sql(get_engine()) as small:
        client.get("/api/v1/review/due", headers=auth_headers)

    _seed_weak_points(db_session, user_id, 6)
    with count_sql(get_engine()) as large:
        client.get("/api/v1/review/due", headers=auth_headers)

    assert large["count"] == small["count"], (
        f"薄弱点增多后 /review/due 的查询数应保持不变，"
        f"实际 {small['count']} -> {large['count']}"
    )
