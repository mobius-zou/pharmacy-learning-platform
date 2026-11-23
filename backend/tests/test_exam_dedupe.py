"""考点重复导入合并（P2 → 已实现）的回归测试。

规则：去首尾空白 → NFKC 统一全半角 → 统一小写 → 压缩连续空白。
去重必须发生在**匹配/LLM 之前**，并且响应里要有 ``duplicates_skipped``。
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy import func, select

from app.models import ExamPoint
from app.services import exam_match_service as exam
from tests.conftest import requires_db

API = "/api/v1"
BACKEND = pathlib.Path(__file__).resolve().parent.parent


# --- 判重键（纯函数） -------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("阿司匹林的作用机制", "  阿司匹林的作用机制  "),          # 首尾空白
        ("Aspirin COX-1", "aspirin cox-1"),                          # 大小写
        ("Ａｓｐｉｒｉｎ", "aspirin"),                                # 全角 → 半角
        ("阿司匹林   的作用机制", "阿司匹林 的作用机制"),            # 连续空白压缩
        ("ＡＳＰＩＲＩＮ\tＣＯＸ", "aspirin cox"),                    # 制表符 + 全角 + 大小写
    ],
)
def test_dedupe_key_normalization(left: str, right: str) -> None:
    assert exam.dedupe_key(left) == exam.dedupe_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("阿司匹林 与 乙酰水杨酸", "阿司匹林与乙酰水杨酸"),  # 空白差异会保留，不误合并
        ("阿司匹林的作用机制", "青霉素的抗菌机制"),
        ("COX-1", "COX-2"),                                  # 近义但不同靶点
    ],
)
def test_dedupe_key_does_not_over_merge(left: str, right: str) -> None:
    """判重只回答「是否同一条文本」，不做同义词归并。"""
    assert exam.dedupe_key(left) != exam.dedupe_key(right)


def test_dedupe_hash_is_sha256_hex() -> None:
    digest = exam.dedupe_hash("阿司匹林")

    assert len(digest) == 64
    assert all(ch in "0123456789abcdef" for ch in digest)


def test_dedupe_points_drops_batch_duplicates() -> None:
    kept, dropped = exam.dedupe_points(["Ａ", "a", "b", "  ", "B"])

    assert kept == ["Ａ", "b"]
    assert dropped == 3


def test_dedupe_points_keeps_first_occurrence_order() -> None:
    kept, _ = exam.dedupe_points(["第一", "第二", "第一"])

    assert kept == ["第一", "第二"]


# --- 迁移内的实现必须与运行时一致 ------------------------------------------


def _load_migration_module():
    candidates = list((BACKEND / "alembic" / "versions").glob("*add_exam_points_dedupe_hash*.py"))
    assert candidates, "找不到 dedupe_hash 迁移文件"
    spec = importlib.util.spec_from_file_location("mig_dedupe", candidates[0])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_dedupe_matches_service() -> None:
    """迁移里的副本若与运行时漂移，历史回填出来的哈希就会与新增行不一致。"""
    migration = _load_migration_module()
    samples = [
        "阿司匹林的作用机制",
        "  Ａspirin   COX-1  ",
        "混合 Mixed 大小写\t制表符",
        "",
        "ＡＢＣ １２３",
    ]

    for text in samples:
        assert migration._dedupe_key(text) == exam.dedupe_key(text), text
        assert migration._dedupe_hash(text) == exam.dedupe_hash(text), text


# --- API 行为 ---------------------------------------------------------------


@requires_db
@pytest.mark.db
def test_reimport_merges_instead_of_inserting(client, auth_headers, db_session, user_id) -> None:
    body = {"text": "阿司匹林的作用机制", "auto_insert_high_risk": False}

    first = client.post(f"{API}/exam-points/import", json=body, headers=auth_headers)
    assert first.status_code == 201
    assert first.json()["duplicates_skipped"] == 0

    second = client.post(f"{API}/exam-points/import", json=body, headers=auth_headers)
    assert second.status_code == 201
    assert second.json()["duplicates_skipped"] == 1, "第二次导入应被合并，而不是新增"

    total = db_session.execute(
        select(func.count()).select_from(ExamPoint).where(ExamPoint.user_id == user_id)
    ).scalar_one()
    assert total == 1, "同一考点只应存在一行"


@requires_db
@pytest.mark.db
def test_reimport_with_normalization_variants_merges(client, auth_headers, db_session, user_id) -> None:
    """全角/空白/大小写差异不应产生第二行。"""
    variants = [
        "  Ａspirin   COX-1 的作用机制 ",
        "aspirin cox-1 的作用机制",
        "ASPIRIN COX-1 的作用机制",
    ]

    for text in variants:
        resp = client.post(
            f"{API}/exam-points/import",
            json={"text": text, "auto_insert_high_risk": False},
            headers=auth_headers,
        )
        assert resp.status_code == 201

    total = db_session.execute(
        select(func.count()).select_from(ExamPoint).where(ExamPoint.user_id == user_id)
    ).scalar_one()
    assert total == 1


@requires_db
@pytest.mark.db
def test_different_points_are_not_merged(client, auth_headers, db_session, user_id) -> None:
    for text in ["阿司匹林的作用机制", "青霉素的抗菌机制"]:
        client.post(
            f"{API}/exam-points/import",
            json={"text": text, "auto_insert_high_risk": False},
            headers=auth_headers,
        )

    total = db_session.execute(
        select(func.count()).select_from(ExamPoint).where(ExamPoint.user_id == user_id)
    ).scalar_one()
    assert total == 2


@requires_db
@pytest.mark.db
def test_merge_refreshes_priority(client, auth_headers, db_session, user_id) -> None:
    """合并时应刷新匹配结果/优先级，而不是保留旧值。"""
    text = "阿司匹林的作用机制"
    client.post(
        f"{API}/exam-points/import",
        json={"text": text, "auto_insert_high_risk": False},
        headers=auth_headers,
    )
    row = db_session.execute(
        select(ExamPoint).where(ExamPoint.user_id == user_id)
    ).scalar_one()
    original_created = row.created_at
    row.priority = "确认"
    db_session.flush()

    client.post(
        f"{API}/exam-points/import",
        json={"text": text, "auto_insert_high_risk": False},
        headers=auth_headers,
    )
    db_session.expire_all()
    refreshed = db_session.execute(
        select(ExamPoint).where(ExamPoint.user_id == user_id)
    ).scalar_one()

    assert refreshed.created_at == original_created, "合并应保留首次导入时间"
    assert refreshed.priority != "确认", "合并应刷新优先级"


@requires_db
@pytest.mark.db
def test_preview_reports_batch_duplicates(client, auth_headers) -> None:
    """试匹配同样在匹配前去重，并把丢弃数回报给调用方。"""
    resp = client.post(
        f"{API}/exam-points/match",
        json={"text": "阿司匹林的作用机制"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["duplicates_skipped"] == 0
    assert "duplicates_skipped" in resp.json()


@requires_db
@pytest.mark.db
def test_unique_constraint_exists() -> None:
    names = {
        c.name for c in ExamPoint.__table__.constraints if getattr(c, "name", None)
    }
    assert "uq_exam_points_user_dedupe" in names


@requires_db
@pytest.mark.db
def test_duplicate_rows_are_rejected_by_database(db_session, user_id) -> None:
    """应用层去重之外，数据库唯一约束是第二道防线。"""
    from sqlalchemy.exc import IntegrityError

    digest = exam.dedupe_hash("同一条考点")
    db_session.add(
        ExamPoint(user_id=user_id, point_text="同一条考点", dedupe_hash=digest, priority="高危")
    )
    db_session.flush()

    db_session.add(
        ExamPoint(user_id=user_id, point_text="同一条考点", dedupe_hash=digest, priority="高危")
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
