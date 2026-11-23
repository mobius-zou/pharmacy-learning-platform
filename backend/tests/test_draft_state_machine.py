"""草稿状态机（pending → indexing → indexed）的回归测试。

为什么需要这个中间态：ChromaDB（草稿）与 MySQL（正式三元组）不在同一事务里。
先置 indexing 再写库，崩溃后扫描 indexing 重放即可；否则会出现
「MySQL 已写入、草稿仍 pending」或反之的不一致，重试时无法判断是否该重放。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import KnowledgeGraph
from app.services import kg_service
from tests.conftest import requires_db

API = "/api/v1"


def _payload(knowledge_point: str = "阿司匹林") -> dict:
    return {
        "pmid": "12345",
        "article": {"title": "测试文献", "journal": "Test J", "year": "2024"},
        "pico_d": {},
        "triples": [
            {"entity": knowledge_point, "relation": "治疗", "target": "疼痛", "confidence": 0.9}
        ],
        "knowledge_points": [],
        "source_type": "pubmed",
    }


def _make_draft(user_id: int, knowledge_point: str = "阿司匹林"):
    return kg_service.save_draft(user_id, "12345", _payload(knowledge_point))


# --- 状态机基本属性 ---------------------------------------------------------


def test_state_machine_statuses() -> None:
    assert kg_service.DRAFT_PENDING == "pending"
    assert kg_service.DRAFT_INDEXING == "indexing"
    assert kg_service.DRAFT_INDEXED == "indexed"
    assert kg_service.DRAFT_STATUSES == ("pending", "indexing", "indexed", "rejected")


def test_legacy_confirmed_maps_to_indexed() -> None:
    """引入状态机之前草稿里存的是 confirmed。"""
    assert kg_service.normalize_draft_status("confirmed") == "indexed"
    assert kg_service.normalize_draft_status("indexed") == "indexed"
    assert kg_service.normalize_draft_status("pending") == "pending"


def test_set_status_rejects_legacy_name() -> None:
    """写入侧不再接受历史名，避免新旧状态混用。"""
    draft = _make_draft(9_001)

    with pytest.raises(ValueError, match="非法草稿状态"):
        kg_service.set_draft_status(draft.draft_id, 9_001, "confirmed")


def test_new_draft_starts_pending() -> None:
    draft = _make_draft(9_002)

    assert draft.status == "pending"


def test_status_transitions_are_persisted() -> None:
    draft = _make_draft(9_003)

    kg_service.set_draft_status(draft.draft_id, 9_003, kg_service.DRAFT_INDEXING)
    assert kg_service.get_draft(draft.draft_id, 9_003).status == "indexing"

    kg_service.set_draft_status(draft.draft_id, 9_003, kg_service.DRAFT_INDEXED)
    assert kg_service.get_draft(draft.draft_id, 9_003).status == "indexed"


def test_legacy_draft_is_readable_through_status_filter() -> None:
    """历史草稿（metadata 里还是 confirmed）要能被 indexed 过滤到。"""
    draft = _make_draft(9_004)
    # 直接写回旧状态，模拟引入状态机之前的数据
    kg_service.draft_collection().update(
        ids=[draft.draft_id],
        metadatas=[{"user_id": 9_004, "pmid": "12345", "status": "confirmed",
                    "created_at": draft.created_at, "updated_at": draft.updated_at,
                    "extraction_source": "heuristic"}],
    )

    assert kg_service.get_draft(draft.draft_id, 9_004).status == "indexed"
    listed = kg_service.list_drafts(9_004, status=kg_service.DRAFT_INDEXED)
    assert [d.draft_id for d in listed] == [draft.draft_id]


# --- 确认入库走状态机 ------------------------------------------------------


@requires_db
@pytest.mark.db
def test_confirm_ends_in_indexed(client, auth_headers, db_session, user_id) -> None:
    draft = _make_draft(user_id, "确认入库药")

    resp = client.post(
        f"{API}/literature/reviews/{draft.draft_id}/confirm",
        json={"source_type": "pubmed", "source_id": "12345"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "indexed"
    assert kg_service.get_draft(draft.draft_id, user_id).status == "indexed"

    total = db_session.execute(
        select(func.count())
        .select_from(KnowledgeGraph)
        .where(KnowledgeGraph.user_id == user_id, KnowledgeGraph.entity == "确认入库药")
    ).scalar_one()
    assert total == 1


@requires_db
@pytest.mark.db
def test_failed_write_leaves_draft_in_indexing(client, auth_headers, user_id, monkeypatch) -> None:
    """写库失败时必须停在 indexing —— 这是可重放的前提。"""
    draft = _make_draft(user_id, "会失败的药")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("模拟写库崩溃")

    monkeypatch.setattr(kg_service, "confirm_triples", boom)

    resp = client.post(
        f"{API}/literature/reviews/{draft.draft_id}/confirm",
        json={"triples": [{"entity": "会失败的药", "relation": "治疗", "target": "发热"}]},
        headers=auth_headers,
    )

    assert resp.status_code == 500
    assert kg_service.get_draft(draft.draft_id, user_id).status == "indexing"


@requires_db
@pytest.mark.db
def test_recovery_replays_indexing_draft(db_session, user_id) -> None:
    """崩溃后扫描 indexing 重放：三元组落库，状态置为 indexed。"""
    draft = _make_draft(user_id, "待恢复药")
    kg_service.set_draft_status(draft.draft_id, user_id, kg_service.DRAFT_INDEXING)

    recovered = kg_service.recover_indexing_drafts(db_session)

    assert recovered == 1
    assert kg_service.get_draft(draft.draft_id, user_id).status == "indexed"
    total = db_session.execute(
        select(func.count())
        .select_from(KnowledgeGraph)
        .where(KnowledgeGraph.user_id == user_id, KnowledgeGraph.entity == "待恢复药")
    ).scalar_one()
    assert total == 1


@requires_db
@pytest.mark.db
def test_recovery_is_idempotent(db_session, user_id) -> None:
    """重复恢复不会产生重复三元组（写库本身幂等）。"""
    draft = _make_draft(user_id, "幂等药")
    kg_service.set_draft_status(draft.draft_id, user_id, kg_service.DRAFT_INDEXING)

    kg_service.recover_indexing_drafts(db_session)
    kg_service.set_draft_status(draft.draft_id, user_id, kg_service.DRAFT_INDEXING)
    kg_service.recover_indexing_drafts(db_session)

    total = db_session.execute(
        select(func.count())
        .select_from(KnowledgeGraph)
        .where(KnowledgeGraph.user_id == user_id, KnowledgeGraph.entity == "幂等药")
    ).scalar_one()
    assert total == 1


@requires_db
@pytest.mark.db
def test_recovery_without_triples_returns_to_pending(db_session, user_id) -> None:
    """没有可重放载荷的草稿退回 pending，交回人工处理，而不是永远卡住。"""
    draft = kg_service.save_draft(user_id, "999", {"triples": [], "source_type": "pubmed"})
    kg_service.set_draft_status(draft.draft_id, user_id, kg_service.DRAFT_INDEXING)

    recovered = kg_service.recover_indexing_drafts(db_session)

    assert recovered == 0
    assert kg_service.get_draft(draft.draft_id, user_id).status == "pending"


@requires_db
@pytest.mark.db
def test_recovery_ignores_other_statuses(db_session, user_id) -> None:
    pending = _make_draft(user_id, "待校对药")
    indexed = _make_draft(user_id, "已入库药")
    kg_service.set_draft_status(indexed.draft_id, user_id, kg_service.DRAFT_INDEXED)

    assert kg_service.recover_indexing_drafts(db_session) == 0
    assert kg_service.get_draft(pending.draft_id, user_id).status == "pending"
    assert kg_service.get_draft(indexed.draft_id, user_id).status == "indexed"
