#!/usr/bin/env python
"""阶段一验收脚本：在真实 MySQL 上跑通「录入 → 到期 → 评分 → due 更新」闭环。

特点：
- 使用 FastAPI TestClient 走真实 HTTP 路由 + 真实 MySQL；
- 通过 ``DATABASE_URL`` 读取连接（不硬编码任何凭据）；
- 只创建一个一次性用户，结束时删除该用户（外键 CASCADE 连带清掉卡片与日志），
  不会残留任何业务数据。

用法：
    cd backend && ./venv/bin/python scripts/verify_phase1_loop.py
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.database import Base, get_engine, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Card, ReviewLog, User  # noqa: E402

API = "/api/v1"

PASS = "  [PASS]"
FAIL = "  [FAIL]"

def _ensure_schema() -> None:
    """按 Alembic 迁移建表（建表唯一入口，不再用 create_all）。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")


_checks = {"passed": 0, "failed": 0}


def check(label: str, condition: bool, extra: str = "") -> None:
    if condition:
        _checks["passed"] += 1
        print(f"{PASS} {label}" + (f"  ({extra})" if extra else ""))
    else:
        _checks["failed"] += 1
        print(f"{FAIL} {label}" + (f"  ({extra})" if extra else ""))


def main() -> int:
    print("=" * 72)
    print("阶段一验收：复习面板 MVP —— FSRS-6 调度闭环")
    print("=" * 72)

    engine = get_engine()
    print(f"\n[1] 数据库连接\n  URL: {engine.url.render_as_string(hide_password=True)}")
    _ensure_schema()
    tables = sorted(Base.metadata.tables)
    print(f"  已就绪的表: {', '.join(tables)}")
    phase1_tables = {"users", "cards", "review_logs"}
    check(
        "阶段一三张表已创建",
        phase1_tables.issubset(set(tables)),
        ", ".join(tables),
    )

    username = f"verify_{uuid.uuid4().hex[:10]}"
    password = "verify-password-123"

    with TestClient(app) as client:
        print("\n[2] 注册并登录一次性验收用户")
        r = client.post(f"{API}/auth/register", json={"username": username, "password": password})
        check("注册成功 (201)", r.status_code == 201, f"status={r.status_code}")
        r = client.post(f"{API}/auth/login", json={"username": username, "password": password})
        check("登录成功并取得 JWT", r.status_code == 200 and "access_token" in r.json())
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n[3] 录入卡片（含三维度标签）")
        r = client.post(
            f"{API}/cards",
            json={
                "front": "阿司匹林的作用机制？",
                "back": "不可逆抑制环氧化酶（COX）",
                "tags": {
                    "subjects": ["药理学"],
                    "chapters": ["解热镇痛抗炎药"],
                    "point_types": ["机制"],
                },
            },
            headers=headers,
        )
        check("录入成功 (201)", r.status_code == 201, f"status={r.status_code}")
        card = r.json()
        card_id = card["id"]
        check("新卡 fsrs_due 为空", card["fsrs_due"] is None)
        check("新卡标记为 is_new/is_due", card["is_new"] and card["is_due"])
        check("标签按三维度存储", card["tags"]["subjects"] == ["药理学"])

        print("\n[4] 到期查询")
        r = client.get(f"{API}/review/due", headers=headers)
        due_list = r.json()
        check(
            "新卡出现在到期队列",
            due_list["total_due"] == 1 and due_list["items"][0]["id"] == card_id,
            f"total_due={due_list['total_due']}",
        )

        print("\n[5] 预览四档评分间隔")
        r = client.get(f"{API}/review/{card_id}/preview", headers=headers)
        options = r.json()["options"]
        for opt in options:
            print(f"      {opt['label']}: {opt['interval_label']}")
        intervals = [o["interval_days"] for o in options]
        check("四档间隔递增（忘了<困难<一般<简单）", intervals == sorted(intervals))

        print("\n[6] 评分（简单）并校验 due 更新")
        r = client.post(
            f"{API}/review/{card_id}/grade", json={"rating": 4}, headers=headers
        )
        check("评分成功 (200)", r.status_code == 200, f"status={r.status_code}")
        graded = r.json()
        print(f"      评分={graded['rating_label']}  间隔={graded['interval_label']}  due={graded['due']}")

        r = client.get(f"{API}/cards/{card_id}", headers=headers)
        after = r.json()
        check("fsrs_due 已持久化且与评分响应一致", after["fsrs_due"] == graded["due"])
        check("fsrs_stability 已更新", after["fsrs_stability"] is not None,
              f"stability={after['fsrs_stability']:.4f}")
        check("fsrs_difficulty 已更新", after["fsrs_difficulty"] is not None,
              f"difficulty={after['fsrs_difficulty']:.4f}")
        check("fsrs_last_review 已更新", after["fsrs_last_review"] is not None)
        check(
            "due 晚于 last_review",
            datetime.fromisoformat(after["fsrs_due"])
            > datetime.fromisoformat(after["fsrs_last_review"]),
        )
        check("卡片毕业进入 Review 态", after["fsrs_state"] == 2, f"state={after['fsrs_state']}")
        check("卡片不再到期", after["is_due"] is False)

        print("\n[7] 到期队列已清空 & 复习日志落库")
        r = client.get(f"{API}/review/due", headers=headers)
        check("到期队列为空", r.json()["total_due"] == 0)
        r = client.get(f"{API}/review/{card_id}/logs", headers=headers)
        logs = r.json()
        check("复习日志已写入", len(logs) == 1,
              f"rating={logs[0]['rating'] if logs else '-'} label={logs[0]['rating_label'] if logs else '-'}")

        print("\n[8] 到期后二次评分：验证调度持续推进")
        # 关键：在「到期时刻」复习。若提前复习（elapsed≈0），FSRS 给出更短的间隔是
        # 正确行为，因此这里用 reviewed_at 模拟到期后再来复习。
        r = client.post(
            f"{API}/review/{card_id}/grade",
            json={"rating": 3, "reviewed_at": graded["due"]},
            headers=headers,
        )
        second = r.json()
        print(f"      评分={second['rating_label']}  间隔={second['interval_label']}  due={second['due']}")
        print(f"      距上次复习 {second['elapsed_days']:.2f} 天")
        check("到期后答对，间隔变长", second["interval_days"] > graded["interval_days"],
              f"{graded['interval_days']:.1f} 天 -> {second['interval_days']:.1f} 天")
        check("复习日志累计为 2 条",
              len(client.get(f"{API}/review/{card_id}/logs", headers=headers).json()) == 2)
        check("elapsed_days 记录了真实间隔", second["elapsed_days"] > 1,
              f"elapsed={second['elapsed_days']:.2f} 天")

        print("\n[9] 清理一次性验收数据")
        with session_scope() as db:
            user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if user is not None:
                db.delete(user)  # ORM 级联删除卡片与日志

    # 复核清理结果（外键 ON DELETE CASCADE 亦为兜底）
    try:
        with session_scope() as db:
            db.execute(delete(ReviewLog).where(ReviewLog.card_id == card_id))
            db.execute(delete(Card).where(Card.id == card_id))
            db.execute(delete(User).where(User.username == username))
    except Exception:
        pass

    with session_scope() as db:
        leftover_user = db.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        leftover_card = db.execute(select(Card).where(Card.id == card_id)).scalar_one_or_none()
    check("一次性用户已删除", leftover_user is None)
    check("一次性卡片已删除", leftover_card is None)

    print("\n" + "=" * 72)
    print(f"结果：{_checks['passed']} 项通过，{_checks['failed']} 项失败")
    print("=" * 72)
    return 0 if _checks["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
