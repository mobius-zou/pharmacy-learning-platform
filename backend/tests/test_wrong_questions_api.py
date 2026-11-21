"""错题诊断 API 集成测试（需要 MySQL，未配置时自动跳过）。

覆盖阶段二三条验收标准：
1. 能录入一道错题并识别知识点
2. 同一知识点错 2 次后触发薄弱点警报
3. 薄弱点卡片在复习队列中排在最前
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import WrongQuestion
from app.services.wrong_question_service import UNCLASSIFIED_KNOWLEDGE_POINT
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]

API = "/api/v1"
WQ = f"{API}/wrong-questions"


def _create_card(client, headers, front: str, back: str, chapter: str | None = None):
    tags = {"subjects": ["药理学"], "point_types": ["机制"]}
    if chapter:
        tags["chapters"] = [chapter]
    resp = client.post(
        f"{API}/cards", json={"front": front, "back": back, "tags": tags}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_wrong(client, headers, text: str, kp: str | None = None, error_count: int = 1):
    payload: dict = {"question_text": text, "error_count": error_count}
    if kp is not None:
        payload["knowledge_point"] = kp
    resp = client.post(WQ, json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- 验收 1：录入错题并识别知识点 -------------------------------------------


def test_create_wrong_question_identifies_knowledge_point(client, auth_headers) -> None:
    """验收 1：录入一道错题并识别知识点。"""
    resp = client.post(
        WQ,
        json={"question_text": "阿司匹林的作用机制是什么？"},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["knowledge_point"] == "阿司匹林的作用机制", "应自动识别出知识点"
    assert body["knowledge_point_source"] in {"llm", "heuristic"}
    assert body["requires_confirmation"] is True, "识别结果需用户确认"
    assert body["error_count"] == 1
    assert body["id"] > 0
    assert body["is_weak"] is False

    # 苏格拉底式问题已生成，且不直接给答案
    assert body["socratic_question"]
    assert body["socratic_question"].endswith(("？", "?"))
    assert body["socratic_question_type"] in {"procedural", "preference", "judgment"}
    assert body["socratic_type_label"] in {"程序性问题", "偏好性问题", "判断性问题"}
    assert body["has_standard_answer"] is True
    assert body["user_answer"] is None


def test_analyze_endpoint_does_not_persist(client, auth_headers, db_session) -> None:
    """识别接口只给建议，不落库 —— 必须等用户确认。"""
    resp = client.post(
        f"{WQ}/analyze",
        json={"question_text": "青霉素的抗菌机制是什么？"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["knowledge_point"] == "青霉素的抗菌机制"
    assert body["requires_confirmation"] is True
    assert body["socratic_question"]

    db_session.expire_all()
    assert db_session.execute(select(WrongQuestion)).scalars().all() == [], "不应落库"


def test_user_confirmed_knowledge_point_is_respected(client, auth_headers) -> None:
    """用户确认/修正后的知识点优先，不再走自动识别。"""
    body = _create_wrong(
        client, auth_headers, "阿司匹林的作用机制是什么？", kp="环氧酶抑制"
    )

    assert body["knowledge_point"] == "环氧酶抑制"
    assert body["knowledge_point_source"] == "user"
    assert body["requires_confirmation"] is False


def test_patch_confirms_or_corrects_knowledge_point(client, auth_headers) -> None:
    created = _create_wrong(client, auth_headers, "地西泮的作用机制是什么？")
    original_kp = created["knowledge_point"]

    resp = client.patch(
        f"{WQ}/{created['id']}", json={"knowledge_point": "GABA-A 受体增强"}, headers=auth_headers
    )

    assert resp.status_code == 200
    assert resp.json()["knowledge_point"] == "GABA-A 受体增强"
    assert resp.json()["knowledge_point"] != original_kp


def test_preference_question_has_no_standard_answer(client, auth_headers) -> None:
    body = _create_wrong(client, auth_headers, "这两种剂型哪个更好？")

    assert body["socratic_question_type"] == "preference"
    assert body["has_standard_answer"] is False


# --- 验收 2：同一知识点错 2 次触发薄弱点警报 --------------------------------


def test_two_errors_on_same_knowledge_point_triggers_alert(client, auth_headers) -> None:
    """验收 2：同一知识点错 2 次后触发薄弱点警报。"""
    kp = "阿司匹林的作用机制"
    first = _create_wrong(client, auth_headers, "阿司匹林的作用机制是什么？", kp=kp)

    # 第 1 次错误：尚未触发
    resp = client.get(f"{WQ}/weak-points", headers=auth_headers)
    assert resp.json()["total"] == 0, "错 1 次不应触发警报"
    assert first["is_weak"] is False

    # 第 2 次错误（同一知识点）
    second = _create_wrong(client, auth_headers, "再问一次阿司匹林的作用机制", kp=kp)
    assert second["knowledge_point"] == kp

    resp = client.get(f"{WQ}/weak-points", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1, "同一知识点错 2 次应触发警报"
    assert body["threshold"] == 2

    point = body["items"][0]
    assert point["knowledge_point"] == kp
    assert point["total_errors"] == 2
    assert point["is_weak"] is True
    assert point["severity"] == "warning"
    assert point["errors_to_threshold"] == 0
    assert point["question_count"] == 2


def test_single_question_repeated_errors_trigger_alert(client, auth_headers) -> None:
    """同一道错题反复做错同样会累计触发。"""
    kp = "半衰期计算"
    created = _create_wrong(client, auth_headers, "如何计算半衰期？", kp=kp)

    resp = client.post(
        f"{WQ}/{created['id']}/errors", json={"delta": 1}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["error_count"] == 2
    assert resp.json()["is_weak"] is True

    points = client.get(f"{WQ}/weak-points", headers=auth_headers).json()["items"]
    assert len(points) == 1
    assert points[0]["total_errors"] == 2


def test_weak_point_escalates_to_critical(client, auth_headers) -> None:
    created = _create_wrong(client, auth_headers, "阿托品的作用", kp="M 受体阻断")

    client.post(f"{WQ}/{created['id']}/errors", json={"delta": 3}, headers=auth_headers)

    points = client.get(f"{WQ}/weak-points", headers=auth_headers).json()["items"]
    assert points[0]["total_errors"] == 4
    assert points[0]["severity"] == "critical"


def test_weak_points_isolated_between_knowledge_points(client, auth_headers) -> None:
    _create_wrong(client, auth_headers, "题目一", kp="知识点甲")
    _create_wrong(client, auth_headers, "题目二", kp="知识点甲")
    _create_wrong(client, auth_headers, "题目三", kp="知识点乙")

    items = client.get(f"{WQ}/weak-points", headers=auth_headers).json()["items"]

    assert [p["knowledge_point"] for p in items] == ["知识点甲"]
    assert items[0]["total_errors"] == 2


def test_threshold_can_be_overridden_per_request(client, auth_headers) -> None:
    _create_wrong(client, auth_headers, "题目", kp="知识点甲")

    assert client.get(f"{WQ}/weak-points", headers=auth_headers).json()["total"] == 0
    strict = client.get(f"{WQ}/weak-points?threshold=1", headers=auth_headers).json()
    assert strict["total"] == 1
    assert strict["items"][0]["errors_to_threshold"] == 0


def test_all_points_listed_when_weak_only_false(client, auth_headers) -> None:
    _create_wrong(client, auth_headers, "题目", kp="知识点甲")

    body = client.get(f"{WQ}/weak-points?weak_only=false", headers=auth_headers).json()

    assert body["total"] == 1
    assert body["items"][0]["is_weak"] is False
    assert body["items"][0]["errors_to_threshold"] == 1


# --- 验收 3：薄弱点卡片在复习队列中排在最前 --------------------------------


def test_weak_point_cards_rank_first_in_review_queue(client, auth_headers) -> None:
    """验收 3：薄弱点卡片在复习队列中排在最前。"""
    # 两张卡：一张关联薄弱知识点，一张是普通新卡
    weak_card = _create_card(
        client, auth_headers, "阿司匹林的作用机制是什么？", "抑制 COX", chapter="阿司匹林的作用机制"
    )
    normal_card = _create_card(client, auth_headers, "普通题目", "普通答案", chapter="其他章节")

    # 让「阿司匹林的作用机制」成为薄弱知识点（错 2 次）
    _create_wrong(client, auth_headers, "阿司匹林的作用机制是什么？", kp="阿司匹林的作用机制")
    _create_wrong(client, auth_headers, "再错一次", kp="阿司匹林的作用机制")

    resp = client.get(f"{API}/review/due", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    ids = [item["id"] for item in body["items"]]
    assert len(ids) >= 2
    assert ids[0] == weak_card["id"], "薄弱点卡片必须排在最前"
    assert body["weak_point_count"] == 1
    assert weak_card["id"] in body["weak_card_ids"]
    assert normal_card["id"] not in body["weak_card_ids"]


def test_non_weak_user_queue_keeps_original_order(client, auth_headers) -> None:
    """没有薄弱点时，队列仍是「新卡优先」。"""
    _create_card(client, auth_headers, "题一", "答一")

    body = client.get(f"{API}/review/due", headers=auth_headers).json()

    assert body["weak_point_count"] == 0
    assert body["weak_card_ids"] == []


def test_weak_priority_does_not_pull_in_not_due_cards(client, auth_headers) -> None:
    """薄弱点只调整顺序，不改变 FSRS 到期判定。"""
    card = _create_card(client, auth_headers, "阿司匹林的作用机制", "抑制 COX", chapter="阿司匹林的作用机制")
    _create_wrong(client, auth_headers, "错题一", kp="阿司匹林的作用机制")
    _create_wrong(client, auth_headers, "错题二", kp="阿司匹林的作用机制")

    # 复习该卡（简单）→ 8 天后才到期
    client.post(
        f"{API}/review/{card['id']}/grade", json={"rating": 4}, headers=auth_headers
    )

    body = client.get(f"{API}/review/due", headers=auth_headers).json()

    assert body["total_due"] == 0, "未到期的薄弱卡片不应被强行提前"
    assert body["items"] == []


# --- 知识点与 cards 自动关联 ------------------------------------------------


def test_knowledge_point_links_to_cards_by_chapter_tag(client, auth_headers) -> None:
    linked = _create_card(
        client, auth_headers, "阿司匹林的作用机制", "抑制 COX", chapter="阿司匹林的作用机制"
    )
    _create_card(client, auth_headers, "无关题目", "无关答案", chapter="其他章节")

    created = _create_wrong(client, auth_headers, "错题", kp="阿司匹林的作用机制")

    resp = client.get(f"{WQ}/{created['id']}/related-cards", headers=auth_headers)
    assert resp.status_code == 200
    related_ids = [card["id"] for card in resp.json()]
    assert linked["id"] in related_ids

    # 薄弱点响应里也带关联卡片 id（此处只错 1 次，故用 weak_only=false 查询）
    points = client.get(
        f"{WQ}/weak-points?weak_only=false", headers=auth_headers
    ).json()["items"]
    assert linked["id"] in points[0]["related_card_ids"]


def test_knowledge_point_links_to_cards_by_body_text(client, auth_headers) -> None:
    card = _create_card(client, auth_headers, "半衰期如何计算", "t1/2 = 0.693/k")

    created = _create_wrong(client, auth_headers, "错题", kp="半衰期")

    related = client.get(f"{WQ}/{created['id']}/related-cards", headers=auth_headers).json()
    assert card["id"] in [item["id"] for item in related]


def test_related_cards_empty_when_no_match(client, auth_headers) -> None:
    created = _create_wrong(client, auth_headers, "错题", kp="完全不存在的知识点")

    resp = client.get(f"{WQ}/{created['id']}/related-cards", headers=auth_headers)
    assert resp.json() == []


# --- 苏格拉底式作答 ---------------------------------------------------------


def test_answer_flow_records_answer_then_reveals_explanation(client, auth_headers) -> None:
    _create_card(client, auth_headers, "阿司匹林的作用机制", "不可逆抑制 COX")
    created = _create_wrong(
        client, auth_headers, "阿司匹林的作用机制是什么？", kp="阿司匹林的作用机制"
    )

    # 作答前没有回答内容
    assert created["user_answer"] is None
    assert created["is_answered"] is False

    resp = client.post(
        f"{WQ}/{created['id']}/answer",
        json={"user_answer": "应该是抑制环氧化酶，但我不确定是否可逆"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["wrong_question"]["user_answer"].startswith("应该是抑制环氧化酶")
    assert body["wrong_question"]["is_answered"] is True
    assert body["wrong_question"]["last_reviewed_at"] is not None

    explanation = body["explanation"]
    assert explanation["has_standard_answer"] is True
    assert explanation["knowledge_point"] == "阿司匹林的作用机制"
    assert explanation["guidance"]
    assert any("COX" in ref["back"] for ref in explanation["reference_answers"])


def test_preference_answer_returns_no_standard_answer(client, auth_headers) -> None:
    created = _create_wrong(client, auth_headers, "哪种剂型更好？")

    resp = client.post(
        f"{WQ}/{created['id']}/answer",
        json={"user_answer": "我认为片剂更好，因为便于服用"},
        headers=auth_headers,
    )

    explanation = resp.json()["explanation"]
    assert explanation["has_standard_answer"] is False
    assert "没有唯一正确答案" in explanation["guidance"]


def test_answer_does_not_increment_error_count(client, auth_headers) -> None:
    """作答是引导环节，不等于又做错一次。"""
    created = _create_wrong(client, auth_headers, "题目", kp="知识点")

    resp = client.post(
        f"{WQ}/{created['id']}/answer", json={"user_answer": "我的回答"}, headers=auth_headers
    )

    assert resp.json()["wrong_question"]["error_count"] == 1


def test_answer_requires_non_empty_text(client, auth_headers) -> None:
    created = _create_wrong(client, auth_headers, "题目", kp="知识点")

    resp = client.post(f"{WQ}/{created['id']}/answer", json={"user_answer": ""}, headers=auth_headers)

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


# --- CRUD / 批量 / 过滤 -----------------------------------------------------


def test_list_and_filter_wrong_questions(client, auth_headers) -> None:
    _create_wrong(client, auth_headers, "阿司匹林题目", kp="知识点甲")
    _create_wrong(client, auth_headers, "青霉素题目", kp="知识点乙")
    _create_wrong(client, auth_headers, "阿司匹林再错", kp="知识点甲")

    body = client.get(f"{WQ}?limit=10", headers=auth_headers).json()
    assert body["total"] == 3

    filtered = client.get(f"{WQ}?knowledge_point=知识点甲", headers=auth_headers).json()
    assert filtered["total"] == 2

    searched = client.get(f"{WQ}?keyword=青霉素", headers=auth_headers).json()
    assert searched["total"] == 1
    assert "青霉素" in searched["items"][0]["question_text"]


def test_list_reports_weak_knowledge_points(client, auth_headers) -> None:
    _create_wrong(client, auth_headers, "题一", kp="弱点")
    _create_wrong(client, auth_headers, "题二", kp="弱点")
    _create_wrong(client, auth_headers, "题三", kp="普通")

    body = client.get(f"{WQ}", headers=auth_headers).json()

    assert body["weak_knowledge_points"] == ["弱点"]
    flags = {item["knowledge_point"]: item["is_weak"] for item in body["items"]}
    assert flags["弱点"] is True
    assert flags["普通"] is False


def test_list_weak_only_filter(client, auth_headers) -> None:
    _create_wrong(client, auth_headers, "题一", kp="弱点")
    _create_wrong(client, auth_headers, "题二", kp="弱点")
    _create_wrong(client, auth_headers, "题三", kp="普通")

    body = client.get(f"{WQ}?weak_only=true", headers=auth_headers).json()

    assert body["total"] == 2
    assert {item["knowledge_point"] for item in body["items"]} == {"弱点"}


def test_bulk_paste_wrong_questions(client, auth_headers) -> None:
    text = "\n".join(
        [
            "# 注释行",
            "阿司匹林的作用机制是什么？",
            "青霉素的抗菌机制 | 细胞壁合成抑制",
            "",
        ]
    )

    resp = client.post(f"{WQ}/bulk", json={"text": text}, headers=auth_headers)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["errors"] == []
    assert body["items"][0]["knowledge_point"] == "阿司匹林的作用机制"
    assert body["items"][1]["knowledge_point"] == "细胞壁合成抑制"


def test_bulk_paste_reports_empty_input(client, auth_headers) -> None:
    resp = client.post(f"{WQ}/bulk", json={"text": "# 只有注释\n\n"}, headers=auth_headers)

    assert resp.status_code == 201
    assert resp.json()["created"] == 0
    assert resp.json()["errors"]


def test_get_and_delete_wrong_question(client, auth_headers) -> None:
    created = _create_wrong(client, auth_headers, "题目", kp="知识点")

    assert client.get(f"{WQ}/{created['id']}", headers=auth_headers).status_code == 200
    assert client.delete(f"{WQ}/{created['id']}", headers=auth_headers).status_code == 204
    assert client.get(f"{WQ}/{created['id']}", headers=auth_headers).status_code == 404


def test_invalid_socratic_type_rejected(client, auth_headers) -> None:
    resp = client.post(
        WQ,
        json={"question_text": "题目", "socratic_question_type": "bogus"},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


def test_unknown_wrong_question_returns_404(client, auth_headers) -> None:
    assert client.get(f"{WQ}/999999", headers=auth_headers).status_code == 404
    resp = client.post(
        f"{WQ}/999999/errors", json={"delta": 1}, headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_blank_question_rejected(client, auth_headers) -> None:
    resp = client.post(WQ, json={"question_text": "   "}, headers=auth_headers)

    assert resp.status_code == 422


def test_persistence_through_orm(client, auth_headers, db_session) -> None:
    created = _create_wrong(client, auth_headers, "落库校验", kp="持久化知识点")

    db_session.expire_all()
    row = db_session.execute(
        select(WrongQuestion).where(WrongQuestion.id == created["id"])
    ).scalar_one()

    assert row.knowledge_point == "持久化知识点"
    assert row.error_count == 1
    assert row.socratic_question
    assert row.socratic_question_type in {"procedural", "preference", "judgment"}
    assert row.user_answer is None


def test_unclassified_knowledge_point_is_persisted(client, auth_headers) -> None:
    """纯疑问词题干也要有兜底知识点，不能写空串。"""
    body = _create_wrong(client, auth_headers, "？？？")

    assert body["knowledge_point"] == UNCLASSIFIED_KNOWLEDGE_POINT


# --- 多用户隔离 -------------------------------------------------------------


def test_users_cannot_access_each_others_wrong_questions(
    client, auth_headers, user_credentials
) -> None:
    created = _create_wrong(client, auth_headers, "机密错题", kp="机密知识点")

    other = {
        "username": user_credentials["username"] + "_wq",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    assert client.get(WQ, headers=other_headers).json()["total"] == 0
    assert client.get(f"{WQ}/{created['id']}", headers=other_headers).status_code == 404
    assert (
        client.patch(
            f"{WQ}/{created['id']}", json={"knowledge_point": "篡改"}, headers=other_headers
        ).status_code
        == 404
    )
    assert client.delete(f"{WQ}/{created['id']}", headers=other_headers).status_code == 404

    # 他人的错误不应影响本用户的薄弱点统计
    assert client.get(f"{WQ}/weak-points", headers=other_headers).json()["total"] == 0
    assert client.get(f"{WQ}/weak-points", headers=auth_headers).json()["total"] == 0


def test_weak_points_isolated_from_other_users_cards(client, auth_headers, user_credentials) -> None:
    """薄弱点只关联「自己的」卡片，不能命中他人卡片。"""
    created = _create_wrong(client, auth_headers, "错题", kp="专属知识点")
    client.post(f"{WQ}/{created['id']}/errors", json={"delta": 1}, headers=auth_headers)

    other = {
        "username": user_credentials["username"] + "_wq2",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    other_token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    # 另一个用户建一张同知识点的卡片
    other_card = _create_card(
        client, other_headers, "专属知识点", "答案", chapter="专属知识点"
    )

    related = client.get(f"{WQ}/{created['id']}/related-cards", headers=auth_headers).json()
    assert other_card["id"] not in [item["id"] for item in related]
