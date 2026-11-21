"""组 3 回归测试（M1/M2/M3/M4/M5/M6/M7/M8/M18/M19）。

对应外部审计报告 P2 中的「数据一致性 / 逻辑错误」类问题。
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.llm import LLMClient, LLMResponse
from app.models import Card, ExamPoint, KnowledgeGraph, ReviewLog, WrongQuestion
from app.services import card_service, kg_service, wrong_question_service as wq
from app.timeutils import utcnow
from tests.conftest import requires_db

API = "/api/v1"


# --- M2：llm.py 重复定义 complete_json --------------------------------------


def test_llm_client_exposes_complete_text_and_json_separately() -> None:
    """回归：complete_text 曾被误写成第二个 complete_json（前者成死代码）。"""
    source = inspect.getsource(LLMClient)
    assert source.count("def complete_json(") == 1, "complete_json 只能定义一次"
    assert source.count("def complete_text(") == 1, "complete_text 必须存在且唯一"
    assert hash(LLMClient.complete_text) != hash(LLMClient.complete_json)


def test_complete_text_does_not_parse_json(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    def fake_chat(self, messages, temperature=0.2, max_tokens=800, parse_json=False):  # noqa: ANN001
        captured["parse_json"] = parse_json
        return LLMResponse(ok=True, text="not json")

    monkeypatch.setattr(LLMClient, "_chat", fake_chat)

    client = LLMClient(api_base="http://x", model="m")
    client.complete_text("s", "u")

    assert captured["parse_json"] is False


def test_complete_json_parses_json(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    def fake_chat(self, messages, temperature=0.2, max_tokens=800, parse_json=False):  # noqa: ANN001
        captured["parse_json"] = parse_json
        return LLMResponse(ok=True, data={"a": 1})

    monkeypatch.setattr(LLMClient, "_chat", fake_chat)

    client = LLMClient(api_base="http://x", model="m")
    client.complete_json("s", "u")

    assert captured["parse_json"] is True


# --- M3：last_error_at 语义 -------------------------------------------------


@requires_db
@pytest.mark.db
def test_last_error_at_is_set_on_create(db_session, user_id) -> None:
    record, _s, _q = wq.create_wrong_question(
        db_session, user_id, "题目", knowledge_point="知识点A", use_llm=False
    )

    assert record.last_error_at is not None
    assert record.last_error_at >= record.created_at.replace(microsecond=0)


@requires_db
@pytest.mark.db
def test_increment_error_advances_last_error_at(db_session, user_id) -> None:
    """回归：原先只加计数，时间仍停留在 created_at。"""
    record, _s, _q = wq.create_wrong_question(
        db_session, user_id, "题目", knowledge_point="知识点B", use_llm=False
    )
    record.last_error_at = utcnow() - timedelta(days=5)
    db_session.flush()
    before = record.last_error_at

    wq.increment_error(db_session, record, 1)

    assert record.last_error_at > before


@requires_db
@pytest.mark.db
def test_weak_point_last_seen_uses_last_error_at(db_session, user_id) -> None:
    record, _s, _q = wq.create_wrong_question(
        db_session, user_id, "题目", knowledge_point="知识点C", use_llm=False
    )
    wq.increment_error(db_session, record, 3)

    points = wq.list_weak_points(db_session, user_id, weak_only=True, with_related_cards=False)
    point = next(p for p in points if p.knowledge_point == "知识点C")

    from app.timeutils import to_aware_utc

    assert point.last_error_at is not None
    assert to_aware_utc(point.last_error_at) >= to_aware_utc(record.created_at)


# --- M4：知识点归一化后聚合 -------------------------------------------------


def test_summary_merges_normalized_variants() -> None:
    """「阿司匹林」与「 阿司匹林。」应合并为同一个知识点。"""
    points = wq.summarize_weak_points(
        {"阿司匹林": 1, " 阿司匹林。": 1}, threshold=2, weak_only=True
    )

    assert len(points) == 1
    assert points[0].knowledge_point == "阿司匹林"
    assert points[0].total_errors == 2
    assert points[0].is_weak is True


# --- M5：LIKE 通配符转义 ----------------------------------------------------


@requires_db
@pytest.mark.db
def test_percent_in_knowledge_point_does_not_match_everything(db_session, user_id) -> None:
    """回归：未转义时知识点 ``%`` 会匹配所有卡片，伪造出「关联全部」的假弱点。"""
    for index in range(3):
        db_session.add(
            Card(
                user_id=user_id,
                front=f"普通卡片{index}",
                back="答案",
                tags={"subjects": [], "chapters": [], "point_types": []},
                created_at=utcnow(),
            )
        )
    db_session.flush()

    matched = wq.related_card_ids(db_session, user_id, ["%"])

    assert matched == set(), f"通配符不应匹配到任何卡片，实际 {len(matched)} 张"


@requires_db
@pytest.mark.db
def test_underscore_in_knowledge_point_is_literal(db_session, user_id) -> None:
    db_session.add(
        Card(
            user_id=user_id,
            front="axb",
            back="答案",
            tags={"subjects": [], "chapters": [], "point_types": []},
            created_at=utcnow(),
        )
    )
    db_session.flush()

    assert wq.related_card_ids(db_session, user_id, ["a_b"]) == set()


# --- M6：reviewed_at 校验 / elapsed_days 服务端计算 -------------------------


@requires_db
@pytest.mark.db
def test_review_cannot_go_back_in_time(client, auth_headers) -> None:
    card = client.post(f"{API}/cards", json={"front": "f", "back": "b"}, headers=auth_headers).json()
    client.post(f"{API}/review/{card['id']}/grade", json={"rating": 3}, headers=auth_headers)

    earlier = (utcnow() - timedelta(days=10)).isoformat()
    resp = client.post(
        f"{API}/review/{card['id']}/grade",
        json={"rating": 3, "reviewed_at": earlier},
        headers=auth_headers,
    )

    assert resp.status_code == 422, f"早于上次复习应被拒，实际 {resp.status_code}"


@requires_db
@pytest.mark.db
def test_review_rejects_far_future_reviewed_at(client, auth_headers) -> None:
    card = client.post(f"{API}/cards", json={"front": "f", "back": "b"}, headers=auth_headers).json()

    far = (utcnow() + timedelta(days=400)).isoformat()
    resp = client.post(
        f"{API}/review/{card['id']}/grade",
        json={"rating": 3, "reviewed_at": far},
        headers=auth_headers,
    )

    assert resp.status_code == 422


@requires_db
@pytest.mark.db
def test_client_elapsed_days_is_ignored(client, auth_headers, db_session) -> None:
    """客户端传入的 elapsed_days 不应影响落库值（服务端按 FSRS 计算）。"""
    card = client.post(f"{API}/cards", json={"front": "f", "back": "b"}, headers=auth_headers).json()

    client.post(
        f"{API}/review/{card['id']}/grade",
        json={"rating": 3, "elapsed_days": 9999},
        headers=auth_headers,
    )

    db_session.expire_all()
    log = db_session.execute(
        select(ReviewLog).where(ReviewLog.card_id == card["id"])
    ).scalar_one()
    assert log.elapsed_days == pytest.approx(0.0), "首刷 elapsed_days 应为 0，而不是客户端传的 9999"


# --- M1：评分幂等（唯一约束） ----------------------------------------------


@requires_db
@pytest.mark.db
def test_same_reviewed_at_cannot_be_logged_twice(client, auth_headers) -> None:
    card = client.post(f"{API}/cards", json={"front": "f", "back": "b"}, headers=auth_headers).json()
    moment = utcnow().isoformat()

    first = client.post(
        f"{API}/review/{card['id']}/grade",
        json={"rating": 3, "reviewed_at": moment},
        headers=auth_headers,
    )
    second = client.post(
        f"{API}/review/{card['id']}/grade",
        json={"rating": 3, "reviewed_at": moment},
        headers=auth_headers,
    )

    assert first.status_code == 200
    assert second.status_code == 409, f"重复评分应冲突，实际 {second.status_code}"
    assert second.json()["error"] == "conflict"


@requires_db
@pytest.mark.db
def test_review_log_unique_constraint_exists() -> None:
    names = {
        constraint.name
        for constraint in ReviewLog.__table__.constraints
        if getattr(constraint, "name", None)
    }
    assert "uq_review_log_card_time" in names


# --- M8：知识图谱唯一性 -----------------------------------------------------


@requires_db
@pytest.mark.db
def test_knowledge_graph_fingerprint_is_autofilled(db_session, user_id) -> None:
    """直接构造也应自动补指纹（否则 NOT NULL 约束会炸）。"""
    record = KnowledgeGraph(
        user_id=user_id,
        source_type="pubmed",
        source_id="1",
        entity="实体A",
        relation="关系B",
        target="目标C",
    )
    db_session.add(record)
    db_session.flush()

    assert record.fingerprint
    assert len(record.fingerprint) == 40


@requires_db
@pytest.mark.db
def test_knowledge_graph_unique_constraint_exists() -> None:
    names = {
        constraint.name
        for constraint in KnowledgeGraph.__table__.constraints
        if getattr(constraint, "name", None)
    }
    assert "uq_knowledge_graph_user_fingerprint" in names


@requires_db
@pytest.mark.db
def test_confirm_triples_skips_duplicates_instead_of_failing(db_session, user_id) -> None:
    from app.services.kg_service import Triple

    triple = Triple("实体X", "关系Y", "目标Z")
    db_session.flush()

    created, skipped = kg_service.confirm_triples(
        db_session, user_id, [triple], source_type="pubmed", source_id="9"
    )
    assert len(created) == 1 and skipped == 0

    created2, skipped2 = kg_service.confirm_triples(
        db_session, user_id, [triple], source_type="pubmed", source_id="9"
    )
    assert len(created2) == 0 and skipped2 == 1


# --- M18：服务层 clamp（绕过 Pydantic 的路径） ------------------------------


@requires_db
@pytest.mark.db
def test_create_card_clamps_overlong_text(db_session, user_id) -> None:
    """文献/考点转卡片直接调 create_card，必须自己 clamp。"""
    from app.limits import MAX_CONTENT_LENGTH

    card = card_service.create_card(
        db_session, user_id, front="阿" * (MAX_CONTENT_LENGTH + 5_000), back="答案"
    )

    assert len(card.front) == MAX_CONTENT_LENGTH


# --- M19：删除考点级联清理卡片 ---------------------------------------------


@requires_db
@pytest.mark.db
def test_deleting_exam_point_removes_its_generated_cards(client, auth_headers, db_session) -> None:
    """回归：原先删考点会留下 front 带「【考点】」的孤儿卡片。"""
    body = client.post(
        f"{API}/exam-points/import",
        json={"text": "实验室消防安全管理制度"},
        headers=auth_headers,
    ).json()
    exam_point_id = body["findings"][0]["exam_point_id"]
    card_id = body["findings"][0]["card_ids"][0]
    assert client.get(f"{API}/cards/{card_id}", headers=auth_headers).status_code == 200

    assert client.delete(f"{API}/exam-points/{exam_point_id}", headers=auth_headers).status_code == 204

    assert client.get(f"{API}/cards/{card_id}", headers=auth_headers).status_code == 404, (
        "考点删除后其生成的卡片也应被清理"
    )


# --- M7：转卡片幂等 --------------------------------------------------------


@requires_db
@pytest.mark.db
def test_to_cards_is_idempotent(client, auth_headers, monkeypatch) -> None:
    from tests.test_literature_service import FakePubMed

    monkeypatch.setattr("app.api.v1.literature._pubmed", FakePubMed())
    draft = client.post(
        f"{API}/literature/extract", json={"pmid": "33301246"}, headers=auth_headers
    ).json()
    client.post(
        f"{API}/literature/reviews/{draft['draft_id']}/confirm", json={}, headers=auth_headers
    )

    first = client.post(
        f"{API}/literature/reviews/{draft['draft_id']}/to-cards", json={}, headers=auth_headers
    ).json()
    second = client.post(
        f"{API}/literature/reviews/{draft['draft_id']}/to-cards", json={}, headers=auth_headers
    ).json()

    assert first["created"] > 0
    assert second["created"] == 0, "重复转卡片不应重复建卡"
    assert second["skipped"] >= 1
