#!/usr/bin/env python
"""阶段二验收脚本：在真实 MySQL 上跑通错题诊断闭环。

覆盖三条验收标准：
1. 能录入一道错题并识别知识点
2. 同一知识点错 2 次后触发薄弱点警报
3. 薄弱点卡片在复习队列中排在最前

同时演示：知识点与 cards 自动关联、苏格拉底式问答、无 Agent 降级模式。
脚本只创建一个一次性用户，结束时删除（外键 CASCADE 连带清理），不留业务数据。

用法：
    cd backend && ./venv/bin/python scripts/verify_phase2_loop.py

若配置了 LLM_API_BASE / LLM_MODEL 则走 LLM 识别，否则自动演示启发式降级模式。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, get_engine, session_scope  # noqa: E402
from app.llm import default_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

API = "/api/v1"
WQ = f"{API}/wrong-questions"

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
        print(f"  [PASS] {label}" + (f"  ({extra})" if extra else ""))
    else:
        _checks["failed"] += 1
        print(f"  [FAIL] {label}" + (f"  ({extra})" if extra else ""))


def main() -> int:
    print("=" * 74)
    print("阶段二验收：错题诊断 —— 知识点识别 / 薄弱点警报 / 队列优先")
    print("=" * 74)

    engine = get_engine()
    print(f"\n[1] 数据库\n  URL: {engine.url.render_as_string(hide_password=True)}")
    _ensure_schema()
    tables = sorted(Base.metadata.tables)
    print(f"  表: {', '.join(tables)}")
    check("wrong_questions 表已创建", "wrong_questions" in tables, ", ".join(tables))

    mode = "LLM" if default_client.available else "本地启发式（无 Agent 降级模式）"
    print(f"\n[2] 知识点识别模式\n  {mode}")
    print(f"  薄弱点阈值: {settings.weak_point_threshold}")

    username = f"verify2_{uuid.uuid4().hex[:10]}"
    password = "verify-password-123"
    kp = "阿司匹林的作用机制"

    with TestClient(app) as client:
        r = client.post(f"{API}/auth/register", json={"username": username, "password": password})
        check("注册一次性验收用户", r.status_code == 201, f"status={r.status_code}")
        r = client.post(f"{API}/auth/login", json={"username": username, "password": password})
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n[3] 准备两张卡片（用于验证队列优先与知识点关联）")
        weak_card = client.post(
            f"{API}/cards",
            json={
                "front": "阿司匹林的作用机制是什么？",
                "back": "不可逆抑制环氧化酶（COX）",
                "tags": {
                    "subjects": ["药理学"],
                    "chapters": [kp],
                    "point_types": ["机制"],
                },
            },
            headers=headers,
        ).json()
        normal_card = client.post(
            f"{API}/cards",
            json={
                "front": "普通题目",
                "back": "普通答案",
                "tags": {"subjects": ["药剂学"], "chapters": ["无关章节"]},
            },
            headers=headers,
        ).json()
        print(f"  卡片 A id={weak_card['id']}（关联「{kp}」）  卡片 B id={normal_card['id']}（无关）")

        print("\n[4] 验收 1：录入错题并识别知识点")
        r = client.post(
            f"{WQ}/analyze",
            json={"question_text": "阿司匹林的作用机制是什么？"},
            headers=headers,
        )
        analysis = r.json()
        print(f"      识别知识点: {analysis['knowledge_point']}")
        print(f"      来源: {analysis['source']}  需确认: {analysis['requires_confirmation']}")
        print(f"      引导问题: {analysis['socratic_question']}")
        print(f"      问题类型: {analysis['socratic_type_label']}")
        check("analyze 识别出知识点", bool(analysis["knowledge_point"]))
        check("识别结果需用户确认", analysis["requires_confirmation"] is True)
        check("生成了苏格拉底式引导问题", analysis["socratic_question"].endswith(("？", "?")))

        audit = client.get(f"{WQ}", headers=headers).json()
        check("analyze 不落库（等待确认）", audit["total"] == 0, f"total={audit['total']}")

        first = client.post(
            WQ,
            json={"question_text": "阿司匹林的作用机制是什么？", "knowledge_point": kp},
            headers=headers,
        )
        check("录入错题成功 (201)", first.status_code == 201, f"status={first.status_code}")
        first = first.json()
        check("错题使用确认后的知识点", first["knowledge_point"] == kp)

        print("\n[5] 知识点与 cards 表自动关联")
        related = client.get(f"{WQ}/{first['id']}/related-cards", headers=headers).json()
        related_ids = [c["id"] for c in related]
        print(f"      关联卡片: {related_ids}")
        check("关联到「阿司匹林」卡片", weak_card["id"] in related_ids)
        check("未误关联无关卡片", normal_card["id"] not in related_ids)

        print("\n[6] 第 1 次错误：不应触发警报")
        weak = client.get(f"{WQ}/weak-points", headers=headers).json()
        check("尚未触发薄弱点警报", weak["total"] == 0, f"total={weak['total']}")
        check("该错题 is_weak=False", first["is_weak"] is False)

        print("\n[7] 验收 2：同一知识点再错 1 次（累计 2 次）")
        client.post(f"{WQ}/{first['id']}/errors", json={"delta": 1}, headers=headers)
        weak = client.get(f"{WQ}/weak-points", headers=headers).json()
        print(f"      阈值={weak['threshold']}  命中={weak['total']}")
        if weak["items"]:
            p = weak["items"][0]
            print(
                f"      知识点={p['knowledge_point']}  累计错误={p['total_errors']}  "
                f"严重度={p['severity']}  关联卡片={p['related_card_ids']}"
            )
        check("累计错 2 次触发薄弱点警报", weak["total"] == 1)
        check("错误次数统计正确", bool(weak["items"]) and weak["items"][0]["total_errors"] == 2)
        check("关联卡片已进入警报", bool(weak["items"]) and weak_card["id"] in weak["items"][0]["related_card_ids"])

        print("\n[8] 验收 3：薄弱点卡片在复习队列中排在最前")
        due = client.get(f"{API}/review/due", headers=headers).json()
        queue = [item["id"] for item in due["items"]]
        print(f"      队列顺序: {queue}   (薄弱点 {due['weak_card_ids']})")
        check("队列包含两张卡片", len(queue) >= 2, f"count={len(queue)}")
        check("薄弱点卡片排在最前", bool(queue) and queue[0] == weak_card["id"])
        check("响应标记了薄弱卡片", weak_card["id"] in due["weak_card_ids"])
        check("weak_point_count 已上报", due["weak_point_count"] == 1)

        print("\n[9] 苏格拉底式问答（答完才给解析）")
        answer = client.post(
            f"{WQ}/{first['id']}/answer",
            json={"user_answer": "应该是抑制环氧化酶，但我不确定是否可逆"},
            headers=headers,
        ).json()
        check("回答已记录", bool(answer["wrong_question"]["user_answer"]))
        check("last_reviewed_at 已更新", answer["wrong_question"]["last_reviewed_at"] is not None)
        check("返回解析材料",
              answer["explanation"]["has_standard_answer"] is True
              and bool(answer["explanation"]["reference_answers"]))
        print(f"      解析指引: {answer['explanation']['guidance']}")
        check("作答不计为再次做错", answer["wrong_question"]["error_count"] == 2)

        print("\n[10] 偏好性问题（无标准答案）")
        pref = client.post(
            WQ, json={"question_text": "这两种剂型哪个更好？"}, headers=headers
        ).json()
        print(f"      类型={pref['socratic_type_label']}  有答案={pref['has_standard_answer']}")
        pref_answer = client.post(
            f"{WQ}/{pref['id']}/answer",
            json={"user_answer": "我倾向片剂，因为便于服用"},
            headers=headers,
        ).json()
        check("偏好性问题标记为无标准答案", pref["has_standard_answer"] is False)
        check("偏好性问题解析不含标准答案",
              pref_answer["explanation"]["has_standard_answer"] is False)

        print("\n[11] 清理一次性验收数据")
        with session_scope() as db:
            user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if user is not None:
                db.delete(user)

    with session_scope() as db:
        leftover = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    check("一次性用户已删除", leftover is None)

    print("\n" + "=" * 74)
    print(f"结果：{_checks['passed']} 项通过，{_checks['failed']} 项失败")
    print("=" * 74)
    return 0 if _checks["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
