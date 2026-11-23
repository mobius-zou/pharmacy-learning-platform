"""文献工作台 API 集成测试（需要 MySQL，未配置时自动跳过）。

覆盖阶段三三条验收标准：
1. 输入一个 PubMed ID，能返回 PICO-D 结构化摘要
2. 结构化结果需人工确认后才能写入数据库
3. 确认后的知识点可一键转为复习卡片

PubMed 传输被替换为假客户端，因此测试离线、确定、快速；
真实 PubMed 连通性另由 test_literature_service.py 中标记 ``network`` 的测试覆盖。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Card, KnowledgeGraph
from app.services import kg_service
from tests.conftest import requires_db
from tests.test_literature_service import SAMPLE_SEARCH, SAMPLE_XML, FakePubMed

pytestmark = [requires_db, pytest.mark.db]

API = "/api/v1"
LIT = f"{API}/literature"


@pytest.fixture
def pubmed(monkeypatch):
    """把路由中的 PubMed 客户端替换为假实现（离线确定性）。"""
    fake = FakePubMed()
    monkeypatch.setattr("app.api.v1.literature._pubmed", fake)
    return fake


@pytest.fixture
def draft(client, auth_headers, pubmed):
    """抽取一份草稿，返回响应体。"""
    resp = client.post(f"{LIT}/extract", json={"pmid": "33301246"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _graph_rows(db_session) -> list[KnowledgeGraph]:
    db_session.expire_all()
    return list(db_session.execute(select(KnowledgeGraph)).scalars().all())


# --- 验收 1：输入 PMID 返回 PICO-D 结构化摘要 -------------------------------


def test_extract_returns_pico_d_structure(client, auth_headers, pubmed) -> None:
    """验收 1：输入一个 PubMed ID，能返回 PICO-D 结构化摘要。"""
    resp = client.post(f"{LIT}/extract", json={"pmid": "33301246"}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 文献元数据
    assert body["pmid"] == "33301246"
    assert "BNT162b2" in body["article"]["title"]
    assert body["article"]["journal"]
    assert body["article"]["year"] == "2020"
    assert body["article"]["url"].endswith("/33301246/")

    # PICO-D 五个字段齐备
    pico = body["pico_d"]
    for key in ("population", "intervention", "comparator", "outcome", "design"):
        assert key in pico, f"缺少 PICO-D 字段：{key}"
        assert "value" in pico[key] and "confidence" in pico[key]

    assert pico["design"]["value"] == "随机对照试验（RCT）"
    assert pico["population"]["value"]
    assert pico["intervention"]["value"]
    assert body["extraction_source"] in {"heuristic", "llm"}

    # 同时给出三元组与知识点建议
    assert body["triples"], "应抽取到实体关系"
    assert body["knowledge_points"], "应给出知识点建议"
    assert body["status"] == "pending"
    assert body["draft_id"]


def test_extract_requires_numeric_pmid(client, auth_headers, pubmed) -> None:
    resp = client.post(f"{LIT}/extract", json={"pmid": "abc"}, headers=auth_headers)

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


def test_pubmed_error_maps_to_502(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.v1.literature._pubmed", FakePubMed(fail="PubMed 请求失败：超时")
    )

    resp = client.post(f"{LIT}/extract", json={"pmid": "1"}, headers=auth_headers)

    assert resp.status_code == 502
    assert resp.json()["error"] == "pubmed_unavailable"


def test_get_article_by_pmid(client, auth_headers, pubmed) -> None:
    resp = client.get(f"{LIT}/pubmed/33301246", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["abstract"]
    assert resp.json()["doi"] == "10.1056/NEJMoa2034577"


def test_search_endpoint(client, auth_headers, pubmed) -> None:
    resp = client.get(f"{LIT}/search?q=covid+vaccine&retmax=5", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1  # 假客户端只返回一篇
    assert body["items"][0]["pmid"] == "33301246"


def test_search_requires_query(client, auth_headers) -> None:
    assert client.get(f"{LIT}/search", headers=auth_headers).status_code == 422


# --- 验收 2：必须人工确认后才入库 -------------------------------------------


def test_extraction_does_not_touch_database(client, auth_headers, db_session, pubmed) -> None:
    """验收 2 的核心：抽取只产生草稿，MySQL 中必须没有任何三元组。"""
    resp = client.post(f"{LIT}/extract", json={"pmid": "33301246"}, headers=auth_headers)
    assert resp.status_code == 200

    assert _graph_rows(db_session) == [], "AI 抽取结果绝不能自动入库"

    # 图谱接口此时应为空
    graph = client.get(f"{LIT}/graph", headers=auth_headers).json()
    assert graph["nodes"] == []
    assert graph["stats"]["node_count"] == 0


def test_confirm_writes_triples_to_database(client, auth_headers, db_session, draft) -> None:
    resp = client.post(f"{LIT}/reviews/{draft['draft_id']}/confirm", json={}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "indexed"
    assert body["added"] > 0
    assert body["source_id"] == "33301246"

    rows = _graph_rows(db_session)
    assert len(rows) == body["added"]
    assert all(row.user_id > 0 for row in rows)
    assert all(row.source_type == "pubmed" for row in rows)
    for row in rows:
        assert row.entity and row.relation and row.target


def test_confirm_is_idempotent(client, auth_headers, db_session, draft) -> None:
    """重复确认同一份草稿不应产生重复三元组。"""
    first = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/confirm", json={}, headers=auth_headers
    ).json()
    count_after_first = len(_graph_rows(db_session))

    # 新建一份相同 pmid 的草稿再确认
    second_draft = client.post(
        f"{LIT}/extract", json={"pmid": "33301246"}, headers=auth_headers
    ).json()
    second = client.post(
        f"{LIT}/reviews/{second_draft['draft_id']}/confirm", json={}, headers=auth_headers
    ).json()

    assert first["added"] > 0
    assert second["added"] == 0, "完全相同的三元组应被去重"
    assert second["skipped"] == first["added"]
    assert len(_graph_rows(db_session)) == count_after_first


def test_confirm_accepts_human_corrected_triples(client, auth_headers, db_session, draft) -> None:
    """人工修正后的三元组才是最终入库内容。"""
    corrected = [
        {"entity": "BNT162b2", "relation": "预防", "target": "Covid-19", "confidence": 0.95}
    ]

    resp = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/confirm",
        json={"triples": corrected},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["added"] == 1
    rows = _graph_rows(db_session)
    assert len(rows) == 1
    assert (rows[0].entity, rows[0].relation, rows[0].target) == ("BNT162b2", "预防", "Covid-19")


def test_confirm_without_triples_rejected(client, auth_headers, draft) -> None:
    resp = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/confirm",
        json={"triples": []},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


def test_reject_never_writes_to_database(client, auth_headers, db_session, draft) -> None:
    resp = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/reject",
        json={"reason": "抽取质量不可用"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert _graph_rows(db_session) == []

    # 被拒绝后也不再出现在待校对列表里
    pending = client.get(f"{LIT}/reviews?status=pending", headers=auth_headers).json()
    assert draft["draft_id"] not in [item["draft_id"] for item in pending["items"]]


def test_patch_saves_human_edits_without_persisting(client, auth_headers, db_session, draft) -> None:
    """人工校对只改草稿，MySQL 仍为空。"""
    edited_pico = dict(draft["pico_d"])
    edited_pico["intervention"] = {
        "value": "人工修正后的干预措施",
        "confidence": 1.0,
        "evidence": "人工填写",
    }

    resp = client.patch(
        f"{LIT}/reviews/{draft['draft_id']}",
        json={
            "pico_d": edited_pico,
            "triples": [
                {"entity": "人工实体", "relation": "干预", "target": "人工目标", "confidence": 1.0}
            ],
            "note": "已核对原文",
        },
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["pico_d"]["intervention"]["value"] == "人工修正后的干预措施"
    assert body["triples"][0]["entity"] == "人工实体"
    assert body["note"] == "已核对原文"
    assert body["status"] == "pending"
    assert _graph_rows(db_session) == [], "保存校对不应入库"

    # 确认后入库的应是人工修改后的内容
    client.post(f"{LIT}/reviews/{draft['draft_id']}/confirm", json={}, headers=auth_headers)
    rows = _graph_rows(db_session)
    assert any(row.entity == "人工实体" for row in rows)


# --- 验收 3：知识点一键转复习卡片 -------------------------------------------


def test_knowledge_points_convert_to_cards(client, auth_headers, db_session, draft) -> None:
    """验收 3：确认后的知识点可一键转为复习卡片。"""
    chosen = draft["knowledge_points"][0]["knowledge_point"]

    resp = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/to-cards",
        json={"knowledge_points": [chosen], "tags": {"subjects": ["药理学"]}},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 1
    assert len(body["card_ids"]) == 1

    # 卡片确实进入了复习系统（可被到期队列查到）
    card_id = body["card_ids"][0]
    card = client.get(f"{API}/cards/{card_id}", headers=auth_headers).json()
    assert card["is_new"] is True
    assert chosen in card["tags"]["chapters"], "知识点应写入章节标签"
    assert card["tags"]["subjects"] == ["药理学"]

    due = client.get(f"{API}/review/due", headers=auth_headers).json()
    assert card_id in [item["id"] for item in due["items"]]


def test_to_cards_without_selection_converts_all(client, auth_headers, draft) -> None:
    resp = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/to-cards", json={}, headers=auth_headers
    )

    assert resp.status_code == 200
    assert resp.json()["created"] == len(draft["knowledge_points"])


def test_to_cards_creates_fsrs_schedulable_cards(client, auth_headers, draft) -> None:
    """转出来的卡片必须是可正常评分的 FSRS 卡片。"""
    body = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/to-cards", json={}, headers=auth_headers
    ).json()
    card_id = body["card_ids"][0]

    graded = client.post(
        f"{API}/review/{card_id}/grade", json={"rating": 3}, headers=auth_headers
    )

    assert graded.status_code == 200
    assert graded.json()["card"]["fsrs_due"] is not None


def test_to_cards_unknown_point_converts_nothing(client, auth_headers, draft) -> None:
    resp = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/to-cards",
        json={"knowledge_points": ["根本不存在的知识点"]},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["created"] == 0
    assert resp.json()["skipped"] == 1


# --- 草稿列表与详情 ---------------------------------------------------------


def test_list_and_get_reviews(client, auth_headers, draft) -> None:
    listing = client.get(f"{LIT}/reviews?status=pending", headers=auth_headers).json()
    assert listing["total"] == 1
    assert listing["items"][0]["draft_id"] == draft["draft_id"]

    detail = client.get(f"{LIT}/reviews/{draft['draft_id']}", headers=auth_headers).json()
    assert detail["pmid"] == "33301246"
    assert detail["article"]["title"]


def test_review_status_filter_validated(client, auth_headers, draft) -> None:
    resp = client.get(f"{LIT}/reviews?status=bogus", headers=auth_headers)
    assert resp.status_code == 422


def test_unknown_draft_returns_404(client, auth_headers) -> None:
    assert client.get(f"{LIT}/reviews/deadbeef", headers=auth_headers).status_code == 404
    resp = client.post(f"{LIT}/reviews/deadbeef/confirm", json={}, headers=auth_headers)
    assert resp.status_code == 404


# --- 知识图谱 ---------------------------------------------------------------


def _confirm(client, headers, draft_id: str) -> dict:
    resp = client.post(f"{LIT}/reviews/{draft_id}/confirm", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_graph_reflects_confirmed_triples(client, auth_headers, draft) -> None:
    _confirm(client, auth_headers, draft["draft_id"])

    graph = client.get(f"{LIT}/graph", headers=auth_headers).json()

    assert graph["nodes"], "确认后图谱应有节点"
    assert graph["edges"]
    assert graph["stats"]["node_count"] == len(graph["nodes"])
    assert graph["stats"]["edge_count"] == len(graph["edges"])
    node_ids = {node["id"] for node in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["source"] in node_ids and edge["target"] in node_ids


def test_graph_triples_listing_and_delete(client, auth_headers, db_session, draft) -> None:
    _confirm(client, auth_headers, draft["draft_id"])

    triples = client.get(f"{LIT}/graph/triples", headers=auth_headers).json()
    assert triples
    record_id = triples[0]["id"]

    assert client.delete(f"{LIT}/graph/triples/{record_id}", headers=auth_headers).status_code == 204
    assert record_id not in [
        item["id"] for item in client.get(f"{LIT}/graph/triples", headers=auth_headers).json()
    ]


def test_graph_export_formats(client, auth_headers, draft) -> None:
    _confirm(client, auth_headers, draft["draft_id"])

    json_resp = client.get(f"{LIT}/graph/export?fmt=json", headers=auth_headers)
    assert json_resp.status_code == 200
    assert json_resp.json()["nodes"]

    graphml = client.get(f"{LIT}/graph/export?fmt=graphml", headers=auth_headers)
    assert graphml.status_code == 200
    assert "<graphml" in graphml.text

    gexf = client.get(f"{LIT}/graph/export?fmt=gexf", headers=auth_headers)
    assert gexf.status_code == 200
    assert "<gexf" in gexf.text

    assert client.get(f"{LIT}/graph/export?fmt=bogus", headers=auth_headers).status_code == 422


def test_vector_search_endpoint(client, auth_headers, pubmed) -> None:
    client.get(f"{LIT}/pubmed/33301246", headers=auth_headers)  # 先入向量库

    resp = client.get(f"{LIT}/vector-search?q=BNT162b2 vaccine", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["items"]


# --- 多用户隔离 -------------------------------------------------------------


def test_drafts_and_graph_isolated_between_users(
    client, auth_headers, user_credentials, draft
) -> None:
    _confirm(client, auth_headers, draft["draft_id"])

    other = {
        "username": user_credentials["username"] + "_lit",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    # 他人看不到我的草稿
    assert client.get(f"{LIT}/reviews?status=all", headers=other_headers).json()["total"] == 0
    assert (
        client.get(f"{LIT}/reviews/{draft['draft_id']}", headers=other_headers).status_code == 404
    )
    assert (
        client.post(
            f"{LIT}/reviews/{draft['draft_id']}/confirm", json={}, headers=other_headers
        ).status_code
        == 404
    )

    # 他人看不到我的图谱
    other_graph = client.get(f"{LIT}/graph", headers=other_headers).json()
    assert other_graph["nodes"] == []

    # 他人不能转我的知识点
    assert (
        client.post(
            f"{LIT}/reviews/{draft['draft_id']}/to-cards", json={}, headers=other_headers
        ).status_code
        == 404
    )


def test_graph_delete_isolated_between_users(client, auth_headers, user_credentials, draft) -> None:
    _confirm(client, auth_headers, draft["draft_id"])
    record_id = client.get(f"{LIT}/graph/triples", headers=auth_headers).json()[0]["id"]

    other = {
        "username": user_credentials["username"] + "_lit2",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    assert (
        client.delete(f"{LIT}/graph/triples/{record_id}", headers=other_headers).status_code == 404
    )


def test_unauthorized_access_rejected(client, draft) -> None:
    # `draft` fixture 已用同一个 client 登录过，Cookie 罐里有会话；
    # 这里清掉 Cookie 来模拟「全新浏览器」，验证的仍是未认证请求会被拒绝。
    client.cookies.clear()
    assert client.get(f"{LIT}/reviews").status_code == 401
    assert client.get(f"{LIT}/graph").status_code == 401
    assert client.post(f"{LIT}/extract", json={"pmid": "1"}).status_code == 401


# --- 人工校对界面 -----------------------------------------------------------


def test_review_ui_page_is_served(client) -> None:
    """阶段三要求必须有人工校对界面。"""
    resp = client.get("/ui/literature")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "人工校对" in body
    assert "确认入库" in body
    assert "知识点转卡片" in body


def test_confirmed_data_survives_in_card_system(client, auth_headers, draft) -> None:
    """闭环校验：确认入库 → 转卡片 → 复习评分，全链路可用。"""
    _confirm(client, auth_headers, draft["draft_id"])
    cards = client.post(
        f"{LIT}/reviews/{draft['draft_id']}/to-cards", json={}, headers=auth_headers
    ).json()

    assert cards["created"] > 0
    card_id = cards["card_ids"][0]

    preview = client.get(f"{API}/review/{card_id}/preview", headers=auth_headers).json()
    assert len(preview["options"]) == 4
    assert preview["is_new"] is True

    graph = client.get(f"{LIT}/graph", headers=auth_headers).json()
    assert graph["stats"]["node_count"] > 0
