"""API 端到端集成测试（需要 MySQL，未配置时自动跳过）。

核心验收：「录入卡片 → 到期查询 → 评分 → due 时间正确更新」完整闭环。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import Card, ReviewLog, User
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]

API = "/api/v1"


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --- 系统 -------------------------------------------------------------------


def test_health_endpoint(client) -> None:
    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database_configured"] is True


def test_root_endpoint(client) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["api_prefix"] == API


def test_openapi_schema_generates(client) -> None:
    """确保 api_guard 包装后签名仍可被 FastAPI 解析。"""
    resp = client.get("/openapi.json")

    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert f"{API}/review/{{card_id}}/grade" in paths
    assert f"{API}/cards" in paths


# --- 认证 -------------------------------------------------------------------


def test_register_and_login_and_me(client, user_credentials) -> None:
    resp = client.post(f"{API}/auth/register", json=user_credentials)
    assert resp.status_code == 201
    assert resp.json()["username"] == user_credentials["username"]
    assert "password" not in resp.json()
    assert "password_hash" not in resp.json()

    resp = client.post(f"{API}/auth/login", json=user_credentials)
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    assert resp.json()["token_type"] == "bearer"
    assert resp.json()["expires_in"] > 0

    resp = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == user_credentials["username"]


def test_duplicate_username_conflict(client, user_credentials) -> None:
    assert client.post(f"{API}/auth/register", json=user_credentials).status_code == 201

    resp = client.post(f"{API}/auth/register", json=user_credentials)

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    assert set(resp.json()) == {"error", "message", "detail"}


def test_login_with_wrong_password(client, user_credentials) -> None:
    client.post(f"{API}/auth/register", json=user_credentials)

    resp = client.post(
        f"{API}/auth/login",
        json={"username": user_credentials["username"], "password": "wrong-password"},
    )

    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_missing_token_rejected(client) -> None:
    resp = client.get(f"{API}/cards")

    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"
    assert set(resp.json()) == {"error", "message", "detail"}


def test_garbage_token_rejected(client) -> None:
    resp = client.get(f"{API}/cards", headers={"Authorization": "Bearer not.a.token"})

    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_validation_error_shape(client) -> None:
    """统一错误体：{"error","message","detail"}。"""
    resp = client.post(f"{API}/auth/register", json={"username": "ab", "password": "123"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert set(body) == {"error", "message", "detail"}
    assert body["detail"]["errors"]


# --- 核心验收：录入 → 到期 → 评分 → due 更新 --------------------------------

def test_full_review_loop_updates_due(client, auth_headers) -> None:
    # 1) 录入卡片
    resp = client.post(
        f"{API}/cards",
        json={
            "front": "阿司匹林的作用机制是什么？",
            "back": "不可逆抑制环氧化酶（COX），减少前列腺素合成",
            "tags": {
                "subjects": ["药理学"],
                "chapters": ["解热镇痛药"],
                "point_types": ["机制"],
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    card = resp.json()
    card_id = card["id"]

    assert card["is_new"] is True
    assert card["is_due"] is True
    assert card["fsrs_due"] is None, "新卡的 due 必须为空"
    assert card["fsrs_stability"] is None
    assert card["tags"]["subjects"] == ["药理学"]

    # 2) 到期查询包含这张卡
    resp = client.get(f"{API}/review/due", headers=auth_headers)
    assert resp.status_code == 200
    due_list = resp.json()
    assert due_list["total_due"] == 1
    assert due_list["items"][0]["id"] == card_id

    # 3) 评分（简单）
    resp = client.post(
        f"{API}/review/{card_id}/grade", json={"rating": 4}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    graded = resp.json()

    assert graded["rating"] == 4
    assert graded["rating_label"] == "简单"
    assert graded["interval_days"] > 0
    assert graded["due"] is not None

    # 4) 通过独立请求确认 due 已真正持久化
    resp = client.get(f"{API}/cards/{card_id}", headers=auth_headers)
    after = resp.json()

    assert after["fsrs_due"] == graded["due"], "due 必须与评分响应一致"
    assert _iso(after["fsrs_due"]) > _iso(after["fsrs_last_review"])
    assert after["fsrs_stability"] is not None
    assert after["fsrs_difficulty"] is not None
    assert after["fsrs_state"] == 2, "「简单」应使新卡毕业进入 Review 态"
    assert after["is_new"] is False
    assert after["is_due"] is False, "刚复习过的卡片不应再到期"

    # 5) 到期队列里不再出现
    resp = client.get(f"{API}/review/due", headers=auth_headers)
    assert resp.json()["total_due"] == 0

    # 6) 复习日志已写入
    resp = client.get(f"{API}/review/{card_id}/logs", headers=auth_headers)
    logs = resp.json()
    assert len(logs) == 1
    assert logs[0]["rating"] == 4
    assert logs[0]["rating_label"] == "简单"
    assert logs[0]["elapsed_days"] == 0.0


@pytest.mark.parametrize(
    ("rating", "label"),
    [(1, "忘了"), (2, "困难"), (3, "一般"), (4, "简单")],
)
def test_all_four_ratings_update_due(
    client, auth_headers, rating: int, label: str
) -> None:
    """四种评分都能推进 due，且评分按钮语义正确。"""
    resp = client.post(
        f"{API}/cards",
        json={"front": f"问题-{rating}", "back": f"答案-{rating}"},
        headers=auth_headers,
    )
    card_id = resp.json()["id"]

    resp = client.post(
        f"{API}/review/{card_id}/grade", json={"rating": rating}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["rating_label"] == label
    assert body["due"] is not None
    assert body["interval_days"] > 0

    resp = client.get(f"{API}/cards/{card_id}", headers=auth_headers)
    stored = resp.json()
    assert stored["fsrs_due"] == body["due"]
    assert stored["fsrs_last_review"] is not None
    assert stored["fsrs_stability"] is not None


def test_four_rating_intervals_are_ordered(client, auth_headers) -> None:
    """同一张新卡上，四档间隔必须递增：忘了 < 困难 < 一般 < 简单。"""
    intervals: list[float] = []
    for rating in (1, 2, 3, 4):
        resp = client.post(
            f"{API}/cards",
            json={"front": f"新卡-{rating}", "back": "x"},
            headers=auth_headers,
        )
        card_id = resp.json()["id"]
        resp = client.post(
            f"{API}/review/{card_id}/grade",
            json={"rating": rating},
            headers=auth_headers,
        )
        intervals.append(resp.json()["interval_days"])

    assert intervals == sorted(intervals)
    assert len(set(intervals)) == 4


def test_preview_returns_four_options(client, auth_headers) -> None:
    resp = client.post(
        f"{API}/cards", json={"front": "预览", "back": "预览答案"}, headers=auth_headers
    )
    card_id = resp.json()["id"]

    resp = client.get(f"{API}/review/{card_id}/preview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["card_id"] == card_id
    assert body["is_new"] is True
    assert body["retrievability"] is None, "新卡没有可回忆度"
    assert [o["label"] for o in body["options"]] == ["忘了", "困难", "一般", "简单"]
    assert [o["rating"] for o in body["options"]] == [1, 2, 3, 4]


def test_preview_does_not_consume_the_card(client, auth_headers) -> None:
    """预览不能改变卡片状态，否则复习队列会被预览「吃掉」。"""
    resp = client.post(f"{API}/cards", json={"front": "p", "back": "a"}, headers=auth_headers)
    card_id = resp.json()["id"]

    client.get(f"{API}/review/{card_id}/preview", headers=auth_headers)

    resp = client.get(f"{API}/cards/{card_id}", headers=auth_headers)
    assert resp.json()["is_new"] is True
    assert resp.json()["fsrs_due"] is None


def test_grade_persists_through_orm(client, auth_headers, db_session) -> None:
    """直接查库确认调度字段与日志真的落到了 MySQL。"""
    resp = client.post(
        f"{API}/cards",
        json={"front": "落库校验", "back": "答案"},
        headers=auth_headers,
    )
    card_id = resp.json()["id"]

    client.post(
        f"{API}/review/{card_id}/grade", json={"rating": 3}, headers=auth_headers
    )

    db_session.expire_all()
    row = db_session.execute(select(Card).where(Card.id == card_id)).scalar_one()

    assert row.fsrs_stability is not None
    assert row.fsrs_difficulty is not None
    assert row.fsrs_due is not None
    assert row.fsrs_last_review is not None
    assert row.fsrs_due > row.fsrs_last_review
    assert row.tags == {"subjects": [], "chapters": [], "point_types": []}

    logs = db_session.execute(
        select(ReviewLog).where(ReviewLog.card_id == card_id)
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].rating == 3
    assert logs[0].elapsed_days == 0.0


def test_invalid_rating_rejected(client, auth_headers) -> None:
    resp = client.post(f"{API}/cards", json={"front": "f", "back": "b"}, headers=auth_headers)
    card_id = resp.json()["id"]

    resp = client.post(
        f"{API}/review/{card_id}/grade", json={"rating": 9}, headers=auth_headers
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


def test_grade_unknown_card_returns_404(client, auth_headers) -> None:
    resp = client.post(
        f"{API}/review/999999/grade", json={"rating": 3}, headers=auth_headers
    )

    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# --- 卡片 CRUD --------------------------------------------------------------


def test_card_crud_lifecycle(client, auth_headers) -> None:
    resp = client.post(
        f"{API}/cards",
        json={
            "front": "原题",
            "back": "原答案",
            "tags": {"subjects": ["药剂学"], "chapters": ["片剂"], "point_types": ["应用"]},
        },
        headers=auth_headers,
    )
    card_id = resp.json()["id"]

    # 读
    assert client.get(f"{API}/cards/{card_id}", headers=auth_headers).status_code == 200

    # 改
    resp = client.patch(
        f"{API}/cards/{card_id}",
        json={"front": "新题", "tags": {"subjects": ["药物化学"]}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["front"] == "新题"
    assert resp.json()["back"] == "原答案", "未提供的字段不应被清空"
    assert resp.json()["tags"]["subjects"] == ["药物化学"]

    # 删
    assert client.delete(f"{API}/cards/{card_id}", headers=auth_headers).status_code == 204
    assert client.get(f"{API}/cards/{card_id}", headers=auth_headers).status_code == 404


def test_reset_clears_progress(client, auth_headers) -> None:
    resp = client.post(f"{API}/cards", json={"front": "r", "back": "a"}, headers=auth_headers)
    card_id = resp.json()["id"]
    client.post(f"{API}/review/{card_id}/grade", json={"rating": 4}, headers=auth_headers)

    resp = client.post(f"{API}/cards/{card_id}/reset", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["is_new"] is True
    assert body["fsrs_due"] is None
    assert body["fsrs_stability"] is None

    # 复习日志一并清空
    resp = client.get(f"{API}/review/{card_id}/logs", headers=auth_headers)
    assert resp.json() == []


def test_card_tags_are_validated(client, auth_headers) -> None:
    resp = client.post(
        f"{API}/cards",
        json={"front": "f", "back": "b", "tags": {"subjects": ["内科学"]}},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


def test_bulk_paste_creates_cards(client, auth_headers) -> None:
    text = "\n".join(
        [
            "# 这是注释，应被忽略",
            "青霉素 | 抑制细胞壁合成",
            "地西泮 :: 增强 GABA 作用",
            "阿托品\t阻断 M 胆碱受体",
            "",
            "这一行没有分隔符，应当报错",
        ]
    )

    resp = client.post(
        f"{API}/cards/bulk",
        json={"text": text, "tags": {"subjects": ["药理学"], "point_types": ["机制"]}},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] == 3
    assert len(body["errors"]) == 1
    assert "第 6 行" in body["errors"][0]
    assert body["items"][0]["front"] == "青霉素"
    assert body["items"][0]["tags"]["subjects"] == ["药理学"]


def test_bulk_paste_reports_all_bad_lines(client, auth_headers) -> None:
    resp = client.post(
        f"{API}/cards/bulk",
        json={"text": "没有分隔符的一行\n还有一行也不行"},
        headers=auth_headers,
    )

    assert resp.status_code == 201
    assert resp.json()["created"] == 0
    assert len(resp.json()["errors"]) == 2


# --- 查询 / 分页 / 过滤 -----------------------------------------------------


def test_pagination(client, auth_headers) -> None:
    for i in range(5):
        client.post(
            f"{API}/cards", json={"front": f"题{i}", "back": f"答{i}"}, headers=auth_headers
        )

    resp = client.get(f"{API}/cards?offset=0&limit=2", headers=auth_headers)
    assert resp.json()["total"] == 5
    assert len(resp.json()["items"]) == 2

    resp = client.get(f"{API}/cards?offset=4&limit=2", headers=auth_headers)
    assert len(resp.json()["items"]) == 1


def test_keyword_search(client, auth_headers) -> None:
    for front, back in [("阿司匹林", "解热镇痛"), ("青霉素", "抗生素"), ("阿莫西林", "抗生素")]:
        client.post(
            f"{API}/cards", json={"front": front, "back": back}, headers=auth_headers
        )

    resp = client.get(f"{API}/cards?keyword=阿", headers=auth_headers)
    assert resp.json()["total"] == 2

    resp = client.get(f"{API}/cards?keyword=抗生素", headers=auth_headers)
    assert resp.json()["total"] == 2


def test_filter_by_subject_tag(client, auth_headers) -> None:
    for front, subject in [("A", "药理学"), ("B", "药物化学"), ("C", "药理学")]:
        client.post(
            f"{API}/cards",
            json={"front": front, "back": "x", "tags": {"subjects": [subject]}},
            headers=auth_headers,
        )

    resp = client.get(f"{API}/cards?subjects=药理学", headers=auth_headers)
    assert resp.json()["total"] == 2

    # 逗号分隔的多值
    resp = client.get(f"{API}/cards?subjects=药理学,药物化学", headers=auth_headers)
    assert resp.json()["total"] == 3


def test_filter_by_point_type_tag(client, auth_headers) -> None:
    client.post(
        f"{API}/cards",
        json={"front": "计算题", "back": "x", "tags": {"point_types": ["计算"]}},
        headers=auth_headers,
    )
    client.post(
        f"{API}/cards",
        json={"front": "机制题", "back": "x", "tags": {"point_types": ["机制"]}},
        headers=auth_headers,
    )

    resp = client.get(f"{API}/cards?point_types=计算", headers=auth_headers)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["front"] == "计算题"


# --- 多用户隔离 -------------------------------------------------------------


def test_users_cannot_see_each_others_cards(client, auth_headers, user_credentials) -> None:
    resp = client.post(
        f"{API}/cards", json={"front": "机密题", "back": "机密答案"}, headers=auth_headers
    )
    card_id = resp.json()["id"]

    # 换一个用户
    other = {
        "username": user_credentials["username"] + "_other",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    assert client.get(f"{API}/cards", headers=other_headers).json()["total"] == 0
    assert client.get(f"{API}/cards/{card_id}", headers=other_headers).status_code == 404
    assert (
        client.post(
            f"{API}/review/{card_id}/grade", json={"rating": 3}, headers=other_headers
        ).status_code
        == 404
    )
    assert (
        client.delete(f"{API}/cards/{card_id}", headers=other_headers).status_code == 404
    )


def test_password_hash_never_exposed(client, auth_headers, db_session) -> None:
    db_session.expire_all()
    users = db_session.execute(select(User)).scalars().all()

    assert users
    for user in users:
        assert user.password_hash.startswith("$2")  # bcrypt 前缀
        assert user.password_hash != "test-password-123"
