"""考点匹配 API 集成测试（需要 MySQL，未配置时自动跳过）。

覆盖阶段五三条验收标准：
1. 导入一段考点文本，能匹配到知识图谱中的知识点
2. 匹配结果按 高危 / 巩固 / 确认 三级分类
3. 高危考点自动插入复习队列
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Card, ExamPoint, KnowledgeGraph
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]

API = "/api/v1"
EXP = f"{API}/exam-points"


def _seed_kg(db_session, user_id: int, triples: list[tuple[str, str, str]]) -> None:
    """直接写入知识图谱（模拟阶段三人工确认后的结果）。"""
    for entity, relation, target in triples:
        db_session.add(
            KnowledgeGraph(
                user_id=user_id,
                source_type="pubmed",
                source_id="33301246",
                entity=entity,
                relation=relation,
                target=target,
            )
        )
    db_session.flush()


@pytest.fixture
def knowledge_graph(db_session, user_id):
    """一份已确认的知识图谱，含 4 个知识点。"""
    _seed_kg(
        db_session,
        user_id,
        [
            ("阿司匹林的作用机制", "作用机制", "环氧化酶抑制"),
            ("青霉素的抗菌机制", "作用机制", "细胞壁合成抑制"),
            ("半衰期的计算方法", "研究方法", "药动学参数"),
            ("高效液相色谱", "分析方法", "含量测定"),
        ],
    )
    return db_session


def _import(client, headers, text, **kwargs):
    payload = {"text": text, **kwargs}
    resp = client.post(f"{EXP}/import", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- 验收 1：导入考点文本 → 匹配知识图谱 ------------------------------------


def test_import_matches_knowledge_graph(client, auth_headers, knowledge_graph) -> None:
    """验收 1：导入一段考点文本，能匹配到知识图谱中的知识点。"""
    body = _import(
        client,
        auth_headers,
        "1. 阿司匹林的作用机制\n2. 半衰期的计算方法\n3. 高效液相色谱",
    )

    assert body["imported"] == 3
    matched = {f["point_text"]: f["matched_knowledge_point"] for f in body["findings"]}
    assert matched["阿司匹林的作用机制"] == "阿司匹林的作用机制"
    assert matched["半衰期的计算方法"] == "半衰期的计算方法"
    assert matched["高效液相色谱"] == "高效液相色谱"

    for finding in body["findings"]:
        assert finding["matched"] is True
        assert finding["match_score"] > 0
        assert finding["confidence"] in {"高置信", "中置信", "低置信"}
        assert finding["match_backend"]


def test_import_matches_via_synonyms(client, auth_headers, knowledge_graph) -> None:
    """同义词替换后，不同写法的考点也能匹配上。"""
    body = _import(client, auth_headers, "aspirin 的作用机制")

    finding = body["findings"][0]
    assert finding["matched"] is True
    assert finding["matched_knowledge_point"] == "阿司匹林的作用机制"


def test_import_matches_wrong_question_knowledge_points(client, auth_headers) -> None:
    """知识点来源也包括阶段二的错题知识点。"""
    client.post(
        f"{API}/wrong-questions",
        json={"question_text": "阿托品的作用", "knowledge_point": "M 受体阻断"},
        headers=auth_headers,
    )

    body = _import(client, auth_headers, "M 受体阻断")

    assert body["findings"][0]["matched_knowledge_point"] == "M 受体阻断"


def test_unmatched_point_is_kept_and_flagged(client, auth_headers, knowledge_graph) -> None:
    body = _import(client, auth_headers, "实验室消防安全管理制度")

    finding = body["findings"][0]
    assert finding["matched"] is False
    assert finding["matched_knowledge_point"] is None
    assert finding["priority"] == "高危", "未匹配 => 缺少复习材料，按高危处理"
    assert "暂无对应知识点" in finding["reason"]


def test_import_no_knowledge_graph_at_all(client, auth_headers) -> None:
    """知识图谱为空时不应报错，而是全部标为未匹配。"""
    body = _import(client, auth_headers, "阿司匹林的作用机制")

    assert body["imported"] == 1
    assert body["findings"][0]["matched"] is False
    assert body["summary"]["matched"] == 0


def test_match_endpoint_does_not_persist(client, auth_headers, knowledge_graph, db_session) -> None:
    """试匹配不落库。"""
    resp = client.post(
        f"{EXP}/match", json={"text": "阿司匹林的作用机制"}, headers=auth_headers
    )

    assert resp.status_code == 200
    assert resp.json()["findings"][0]["matched"] is True

    db_session.expire_all()
    assert db_session.execute(select(ExamPoint)).scalars().all() == []


# --- 验收 2：三级分类 -------------------------------------------------------


def test_priority_classification_all_three_levels(
    client, auth_headers, knowledge_graph
) -> None:
    """验收 2：匹配结果按 高危 / 巩固 / 确认 三级分类。

    - 高危：该知识点是阶段二薄弱点（累计错误 ≥ 阈值）
    - 巩固：有复习记录但稳定性一般
    - 确认：稳定性高且无错误
    """
    # ① 高危：制造薄弱点（同知识点错 2 次）
    for _ in range(2):
        client.post(
            f"{API}/wrong-questions",
            json={
                "question_text": "阿司匹林的作用机制是什么",
                "knowledge_point": "阿司匹林的作用机制",
            },
            headers=auth_headers,
        )

    # ② 巩固：一张卡刷一次（stability≈8.3 < 21）
    moderate_card = client.post(
        f"{API}/cards",
        json={
            "front": "青霉素的抗菌机制",
            "back": "抑制细胞壁合成",
            "tags": {"chapters": ["青霉素的抗菌机制"]},
        },
        headers=auth_headers,
    ).json()
    client.post(
        f"{API}/review/{moderate_card['id']}/grade", json={"rating": 4}, headers=auth_headers
    )

    # ③ 确认：两张 Easy（stability≈65 ≥ 21）且无错误
    mastered_card = client.post(
        f"{API}/cards",
        json={
            "front": "半衰期的计算方法",
            "back": "t1/2 = 0.693/k",
            "tags": {"chapters": ["半衰期的计算方法"]},
        },
        headers=auth_headers,
    ).json()
    first = client.post(
        f"{API}/review/{mastered_card['id']}/grade", json={"rating": 4}, headers=auth_headers
    ).json()
    client.post(
        f"{API}/review/{mastered_card['id']}/grade",
        json={"rating": 4, "reviewed_at": first["due"]},
        headers=auth_headers,
    )

    body = _import(
        client,
        auth_headers,
        "\n".join(
            [
                "阿司匹林的作用机制",
                "青霉素的抗菌机制",
                "半衰期的计算方法",
            ]
        ),
        auto_insert_high_risk=False,
    )

    by_point = {f["point_text"]: f for f in body["findings"]}
    assert by_point["阿司匹林的作用机制"]["priority"] == "高危"
    assert by_point["阿司匹林的作用机制"]["mastery"] == "weak"
    assert "薄弱点" in by_point["阿司匹林的作用机制"]["reason"]

    assert by_point["青霉素的抗菌机制"]["priority"] == "巩固"
    assert by_point["青霉素的抗菌机制"]["mastery"] == "moderate"

    assert by_point["半衰期的计算方法"]["priority"] == "确认"
    assert by_point["半衰期的计算方法"]["mastery"] == "mastered"

    # 汇总同时给出三级计数
    assert body["summary"]["by_priority"]["高危"] >= 1
    assert body["summary"]["by_priority"]["巩固"] >= 1
    assert body["summary"]["by_priority"]["确认"] >= 1


def test_weak_point_drives_high_risk(client, auth_headers, knowledge_graph) -> None:
    """单条错误不足以触发高危；达到阈值后才升级。"""
    create = lambda: client.post(  # noqa: E731
        f"{API}/wrong-questions",
        json={"question_text": "半衰期怎么算", "knowledge_point": "半衰期的计算方法"},
        headers=auth_headers,
    )

    create()
    first = _import(client, auth_headers, "半衰期的计算方法", auto_insert_high_risk=False)
    assert first["findings"][0]["priority"] != "高危" or first["findings"][0]["mastery"] != "mastered"

    create()  # 第二次错误 → 触发薄弱点
    second = _import(client, auth_headers, "半衰期的计算方法", auto_insert_high_risk=False)
    assert second["findings"][0]["priority"] == "高危"
    assert second["findings"][0]["mastery"] == "weak"


def test_priority_filter_on_list(client, auth_headers, knowledge_graph) -> None:
    _import(client, auth_headers, "实验室消防安全管理制度", auto_insert_high_risk=False)

    body = client.get(f"{EXP}?priority=高危", headers=auth_headers).json()
    assert body["total"] == 1
    assert body["items"][0]["priority"] == "高危"

    assert client.get(f"{EXP}?priority=确认", headers=auth_headers).json()["total"] == 0
    assert client.get(f"{EXP}?priority=不存在", headers=auth_headers).status_code == 422


# --- 验收 3：高危自动插入复习队列 -------------------------------------------


def test_high_risk_auto_inserted_into_review_queue(
    client, auth_headers, knowledge_graph
) -> None:
    """验收 3：高危考点自动插入复习队列。"""
    body = _import(client, auth_headers, "实验室消防安全管理制度")  # 未匹配 => 高危

    assert body["findings"][0]["priority"] == "高危"
    assert body["inserted_to_queue"] == 1, "高危考点应自动插入复习队列"
    assert body["findings"][0]["card_ids"]

    card_id = body["findings"][0]["card_ids"][0]
    due = client.get(f"{API}/review/due", headers=auth_headers).json()
    assert card_id in [item["id"] for item in due["items"]], "插入的卡片应出现在到期队列中"

    card = client.get(f"{API}/cards/{card_id}", headers=auth_headers).json()
    assert "考点" in card["front"]
    assert card["is_new"] is True


def test_non_high_risk_not_auto_inserted(client, auth_headers, db_session, user_id) -> None:
    """已掌握（确认）的考点不插入队列。"""
    _seed_kg(db_session, user_id, [("高效液相色谱", "分析方法", "含量测定")])
    card = client.post(
        f"{API}/cards",
        json={
            "front": "高效液相色谱",
            "back": "用于含量测定",
            "tags": {"chapters": ["高效液相色谱"]},
        },
        headers=auth_headers,
    ).json()
    first = client.post(
        f"{API}/review/{card['id']}/grade", json={"rating": 4}, headers=auth_headers
    ).json()
    client.post(
        f"{API}/review/{card['id']}/grade",
        json={"rating": 4, "reviewed_at": first["due"]},
        headers=auth_headers,
    )

    body = _import(client, auth_headers, "高效液相色谱")

    assert body["findings"][0]["priority"] == "确认"
    assert body["inserted_to_queue"] == 0
    assert body["queue_skipped"] == 0


def test_auto_insert_can_be_disabled(client, auth_headers, knowledge_graph) -> None:
    body = _import(client, auth_headers, "实验室消防安全管理制度", auto_insert_high_risk=False)

    assert body["inserted_to_queue"] == 0
    assert body["note"] == "未自动插入复习队列"
    assert client.get(f"{API}/review/due", headers=auth_headers).json()["total_due"] == 0


def test_queue_insert_is_idempotent(client, auth_headers, knowledge_graph) -> None:
    """重复插入不应产生重复卡片。"""
    _import(client, auth_headers, "实验室消防安全管理制度")

    resp = client.post(
        f"{EXP}/to-review-queue", json={"priorities": ["高危"]}, headers=auth_headers
    )

    assert resp.status_code == 200
    assert resp.json()["inserted"] == 0
    assert resp.json()["skipped"] == 1
    assert "无需重复插入" in resp.json()["note"]


def test_to_review_queue_by_explicit_ids(client, auth_headers, knowledge_graph) -> None:
    body = _import(client, auth_headers, "实验室消防安全管理制度", auto_insert_high_risk=False)
    exam_point_id = body["findings"][0]["exam_point_id"]

    resp = client.post(
        f"{EXP}/to-review-queue",
        json={"exam_point_ids": [exam_point_id], "priorities": ["高危"]},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["inserted"] == 1
    assert resp.json()["card_ids"]


def test_card_back_uses_related_card_content(client, auth_headers, knowledge_graph) -> None:
    """有关联卡片时，考点卡片的背面应引用其内容。"""
    client.post(
        f"{API}/cards",
        json={
            "front": "阿司匹林的作用机制",
            "back": "不可逆抑制环氧化酶",
            "tags": {"chapters": ["阿司匹林的作用机制"]},
        },
        headers=auth_headers,
    )

    body = _import(client, auth_headers, "阿司匹林的作用机制")
    card_id = body["findings"][0]["card_ids"][0]
    card = client.get(f"{API}/cards/{card_id}", headers=auth_headers).json()

    assert "不可逆抑制环氧化酶" in card["back"]


# --- 复习清单 ---------------------------------------------------------------


def test_review_list_groups_by_priority(client, auth_headers, knowledge_graph) -> None:
    _import(client, auth_headers, "阿司匹林的作用机制\n实验室消防安全管理制度")

    body = client.get(f"{EXP}/review-list", headers=auth_headers).json()

    assert body["total"] == 2
    assert "高危" in body["groups"]
    assert all(f["priority"] == "高危" for f in body["groups"]["高危"])
    assert body["items"][0]["priority"] == "高危", "高危排在最前"


def test_review_list_priority_filter(client, auth_headers, knowledge_graph) -> None:
    _import(client, auth_headers, "阿司匹林的作用机制")

    body = client.get(f"{EXP}/review-list?priorities=确认", headers=auth_headers).json()

    assert body["total"] == 0
    assert client.get(f"{EXP}/review-list?priorities=乱填", headers=auth_headers).status_code == 422


def test_summary_endpoint(client, auth_headers, knowledge_graph) -> None:
    _import(client, auth_headers, "阿司匹林的作用机制\n实验室消防安全管理制度")

    body = client.get(f"{EXP}/summary", headers=auth_headers).json()

    assert body["total"] == 2
    assert body["matched"] == 1
    assert body["unmatched"] == 1
    assert sum(body["by_priority"].values()) == 2


# --- 能力状态 / OCR ---------------------------------------------------------


def test_capabilities_endpoint(client, auth_headers) -> None:
    body = client.get(f"{EXP}/capabilities", headers=auth_headers).json()

    assert body["priorities"] == ["高危", "巩固", "确认"]
    assert "text" in body["import_sources"]
    assert "image_ocr" in body["import_sources"]
    assert body["embedding"]["backend"]
    assert isinstance(body["embedding"]["degraded"], bool)
    assert isinstance(body["ocr"]["available"], bool)
    if not body["ocr"]["available"]:
        assert body["ocr"]["reason"]
        assert body["ocr"]["install_hint"]


def test_image_import_reports_ocr_state(client, auth_headers) -> None:
    """OCR 不可用时必须返回明确的 503 与安装指引，而不是 500。"""
    capabilities = client.get(f"{EXP}/capabilities", headers=auth_headers).json()

    resp = client.post(
        f"{EXP}/import/image",
        files={"file": ("exam.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        headers=auth_headers,
    )

    if capabilities["ocr"]["available"]:
        # 有 OCR 时，假图片应被判定为识别失败（422）而不是崩溃
        assert resp.status_code in {422}
    else:
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "ocr_unavailable"
        assert body["detail"]["install_hint"]
        assert "text" in body["detail"]["alternatives"]


def test_text_file_import(client, auth_headers, knowledge_graph) -> None:
    resp = client.post(
        f"{EXP}/import/file",
        files={
            "file": (
                "exam.txt",
                "1. 阿司匹林的作用机制\n2. 青霉素的抗菌机制".encode("utf-8"),
                "text/plain",
            )
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["imported"] == 2
    assert body["findings"][0]["matched"] is True


def test_text_file_import_gbk_encoded(client, auth_headers, knowledge_graph) -> None:
    """中文 txt 常见 GBK 编码，必须能解码。"""
    resp = client.post(
        f"{EXP}/import/file",
        files={"file": ("exam.txt", "阿司匹林的作用机制".encode("gb18030"), "text/plain")},
        headers=auth_headers,
    )

    assert resp.status_code == 201
    assert resp.json()["imported"] == 1


def test_empty_file_rejected(client, auth_headers) -> None:
    resp = client.post(
        f"{EXP}/import/file",
        files={"file": ("empty.txt", b"", "text/plain")},
        headers=auth_headers,
    )

    assert resp.status_code == 422


def test_import_with_no_points_rejected(client, auth_headers) -> None:
    resp = client.post(f"{EXP}/import", json={"text": "，，，\n\n  "}, headers=auth_headers)

    assert resp.status_code == 422
    assert "没有解析到" in resp.json()["message"]


# --- 查询 / 删除 / 隔离 -----------------------------------------------------


def test_get_and_delete_exam_point(client, auth_headers, knowledge_graph) -> None:
    body = _import(client, auth_headers, "阿司匹林的作用机制", auto_insert_high_risk=False)
    exam_point_id = body["findings"][0]["exam_point_id"]

    detail = client.get(f"{EXP}/{exam_point_id}", headers=auth_headers).json()
    assert detail["point_text"] == "阿司匹林的作用机制"
    assert detail["matched_knowledge_point"] == "阿司匹林的作用机制"

    assert client.delete(f"{EXP}/{exam_point_id}", headers=auth_headers).status_code == 204
    assert client.get(f"{EXP}/{exam_point_id}", headers=auth_headers).status_code == 404


def test_persistence_through_orm(client, auth_headers, knowledge_graph, db_session) -> None:
    body = _import(client, auth_headers, "阿司匹林的作用机制", auto_insert_high_risk=False)

    db_session.expire_all()
    row = db_session.execute(
        select(ExamPoint).where(ExamPoint.id == body["findings"][0]["exam_point_id"])
    ).scalar_one()

    assert row.point_text == "阿司匹林的作用机制"
    assert row.matched_knowledge_point == "阿司匹林的作用机制"
    assert row.priority in {"高危", "巩固", "确认"}
    assert row.created_at is not None


def test_exam_points_isolated_between_users(
    client, auth_headers, user_credentials, knowledge_graph, user_id, db_session
) -> None:
    body = _import(client, auth_headers, "阿司匹林的作用机制", auto_insert_high_risk=False)
    exam_point_id = body["findings"][0]["exam_point_id"]

    other = {
        "username": user_credentials["username"] + "_ep",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    assert client.get(EXP, headers=other_headers).json()["total"] == 0
    assert client.get(f"{EXP}/{exam_point_id}", headers=other_headers).status_code == 404
    assert client.delete(f"{EXP}/{exam_point_id}", headers=other_headers).status_code == 404

    # 他人的复习清单与队列容量也互不影响
    assert client.get(f"{EXP}/review-list", headers=other_headers).json()["total"] == 0


def test_unauthorized_access_rejected(client) -> None:
    assert client.get(EXP).status_code == 401
    assert client.post(f"{EXP}/import", json={"text": "x"}).status_code == 401
    assert client.get(f"{EXP}/capabilities").status_code == 401


def test_cross_phase_integration_with_literature_graph(
    client, auth_headers, monkeypatch, db_session
) -> None:
    """跨阶段：阶段三确认入库的知识点，阶段五能匹配上。"""
    from tests.test_literature_service import SAMPLE_XML, FakePubMed

    monkeypatch.setattr("app.api.v1.literature._pubmed", FakePubMed())
    draft = client.post(
        f"{API}/literature/extract", json={"pmid": "33301246"}, headers=auth_headers
    ).json()
    confirmed = client.post(
        f"{API}/literature/reviews/{draft['draft_id']}/confirm", json={}, headers=auth_headers
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["added"] > 0

    triples = client.get(f"{API}/literature/graph/triples", headers=auth_headers).json()
    entity = triples[0]["entity"]

    body = _import(client, auth_headers, entity, auto_insert_high_risk=False)

    assert body["findings"][0]["matched"] is True
    assert body["findings"][0]["matched_knowledge_point"] == entity
