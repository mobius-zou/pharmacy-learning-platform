"""评分幂等键（Idempotency-Key）回归测试。

场景：客户端因网络超时重发同一次评分。没有幂等键时 FSRS 会被推进两次，
复习间隔与统计都会失真。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import ReviewLog
from tests.conftest import requires_db

API = "/api/v1"
KEY = "Idempotency-Key"


def _make_card(client, auth_headers, front: str = "阿司匹林的作用机制") -> int:
    resp = client.post(f"{API}/cards", json={"front": front, "back": "抑制 COX"}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@requires_db
@pytest.mark.db
def test_same_key_grades_once(client, auth_headers, db_session) -> None:
    """同一键重发：只写一条评分日志，第二次是重放。"""
    card_id = _make_card(client, auth_headers)

    first = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "grade-once-1"},
    )
    second = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "grade-once-1"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json() == first.json(), "重放必须返回与首次完全一致的结果"

    logs = db_session.execute(
        select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == card_id)
    ).scalar_one()
    assert logs == 1, "同一幂等键不应写第二条评分日志"


@requires_db
@pytest.mark.db
def test_different_keys_grade_twice(client, auth_headers, db_session) -> None:
    """不同键 = 两次真实操作，应各自生效。"""
    card_id = _make_card(client, auth_headers, "青霉素的抗菌机制")

    for key in ("k-1", "k-2"):
        resp = client.post(
            f"{API}/review/{card_id}/grade",
            json={"rating": 3},
            headers={**auth_headers, KEY: key},
        )
        assert resp.status_code == 200, resp.text

    logs = db_session.execute(
        select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == card_id)
    ).scalar_one()
    assert logs == 2


@requires_db
@pytest.mark.db
def test_no_key_keeps_original_behaviour(client, auth_headers, db_session) -> None:
    """不带键时保持旧行为（向后兼容）。"""
    card_id = _make_card(client, auth_headers, "布洛芬的作用机制")

    resp = client.post(f"{API}/review/{card_id}/grade", json={"rating": 3}, headers=auth_headers)

    assert resp.status_code == 200
    assert "Idempotent-Replay" not in resp.headers, "未提供幂等键时不应有重放标记"
    logs = db_session.execute(
        select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == card_id)
    ).scalar_one()
    assert logs == 1


@requires_db
@pytest.mark.db
def test_same_key_with_different_body_conflicts(client, auth_headers) -> None:
    """同键不同请求是客户端误用：必须报错，不能把上次结果张冠李戴。"""
    card_id = _make_card(client, auth_headers, "对乙酰氨基酚的作用机制")

    first = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "reused-key"},
    )
    assert first.status_code == 200

    conflicting = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 1},  # 评分不同 => 不同请求
        headers={**auth_headers, KEY: "reused-key"},
    )

    assert conflicting.status_code == 409
    assert conflicting.json()["error"] == "conflict"


@requires_db
@pytest.mark.db
def test_replay_does_not_advance_fsrs(client, auth_headers) -> None:
    """重放不能改变卡片的调度状态。"""
    card_id = _make_card(client, auth_headers, "奥美拉唑的作用机制")

    first = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "fsrs-key"},
    ).json()
    replay = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "fsrs-key"},
    ).json()

    assert replay["due"] == first["due"]
    assert replay["interval_days"] == first["interval_days"]


@requires_db
@pytest.mark.db
def test_key_is_scoped_per_user(client, auth_headers, user_credentials) -> None:
    """幂等键按用户隔离：别人的同名键不能拿到我的结果。"""
    card_id = _make_card(client, auth_headers, "二甲双胍的作用机制")
    mine = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "shared-key"},
    )
    assert mine.status_code == 200

    other = {
        "username": user_credentials["username"] + "_idem",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    # 另一个用户用同名键，但访问不到我的卡片 → 404（而不是拿到我的重放结果）
    resp = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**other_headers, KEY: "shared-key"},
    )
    assert resp.status_code == 404


@requires_db
@pytest.mark.db
def test_expired_key_is_not_replayed(client, auth_headers, db_session, monkeypatch) -> None:
    """过期记录不再重放，避免永久锁死某个键。"""
    from app.services import idempotency_service

    card_id = _make_card(client, auth_headers, "阿托伐他汀的作用机制")
    client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "expiring-key"},
    )

    # 把 TTL 压到 0，令刚写入的记录立即视为过期
    monkeypatch.setattr(idempotency_service.settings, "idempotency_ttl_seconds", 1)
    from datetime import timedelta

    from app.timeutils import utcnow

    from app.models import IdempotencyRecord

    record = db_session.execute(select(IdempotencyRecord)).scalar_one()
    record.created_at = utcnow() - timedelta(seconds=10)
    db_session.flush()

    resp = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "expiring-key"},
    )

    assert resp.status_code == 200
    # 过期后不再重放：这是一次全新的执行
    assert resp.headers.get("Idempotent-Replay") == "false"
    logs = db_session.execute(
        select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == card_id)
    ).scalar_one()
    assert logs == 2, "过期后应重新执行一次评分"


@requires_db
@pytest.mark.db
def test_overlong_key_rejected(client, auth_headers) -> None:
    card_id = _make_card(client, auth_headers, "硝苯地平的作用机制")

    resp = client.post(
        f"{API}/review/{card_id}/grade",
        json={"rating": 3},
        headers={**auth_headers, KEY: "x" * 201},
    )

    assert resp.status_code == 422
