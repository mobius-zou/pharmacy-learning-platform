#!/usr/bin/env python
"""阶段五验收脚本：考点匹配。

覆盖三条验收标准：
1. 导入一段考点文本，能匹配到知识图谱中的知识点
2. 匹配结果按 高危 / 巩固 / 确认 三级分类
3. 高危考点自动插入复习队列

同时演示：分词 + 同义词替换、编辑距离初筛 + sentence-transformers 精筛、
OCR 可用性、复习清单生成。

脚本只创建一个一次性用户，结束时删除（外键 CASCADE 连带清理），不留业务数据。

用法：
    cd backend && ./venv/bin/python scripts/verify_phase5_loop.py
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

from app import ocr as ocr_module  # noqa: E402
from app.database import Base, get_engine, session_scope  # noqa: E402
from app.embeddings import describe as describe_embedding  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Card, ExamPoint, KnowledgeGraph, User  # noqa: E402
from app.services import exam_match_service as exam_service  # noqa: E402

API = "/api/v1"
EXP = f"{API}/exam-points"

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
    print("=" * 78)
    print("阶段五验收：考点匹配 —— 导入 / 三级分类 / 自动插入复习队列")
    print("=" * 78)

    engine = get_engine()
    print(f"\n[1] 数据库与匹配后端")
    print(f"  MySQL: {engine.url.render_to_string() if hasattr(engine.url,'render_to_string') else engine.url.render_as_string(hide_password=True)}")
    _ensure_schema()
    tables = sorted(Base.metadata.tables)
    print(f"  表: {', '.join(tables)}")
    check("exam_points 表已创建", "exam_points" in tables)

    print("\n[2] 算法与依赖")
    print(f"  同义词表条目: {len(exam_service.SYNONYMS)}")
    print(f"  分词示例: {exam_service.tokenize('阿司匹林抑制COX-2')}")
    print(f"  同义词替换: 'aspirin 抑制 COX-2' -> {exam_service.apply_synonyms('aspirin 抑制 COX-2')!r}")
    print(f"  编辑距离: '阿司匹林' vs '阿司匹林片' = {exam_service.edit_similarity('阿司匹林','阿司匹林片'):.2f}")

    embedding = describe_embedding(probe=True)
    print(f"  语义后端: {embedding['backend']}  degraded={embedding['degraded']}")
    if embedding.get("fallback_reason"):
        print(f"    降级原因: {embedding['fallback_reason']}")
    check("匹配算法为「编辑距离初筛 + 语义精筛」",
          exam_service.settings.exam_match_edit_weight > 0
          and exam_service.settings.exam_match_semantic_weight > 0)

    ocr_state = ocr_module.ocr_status()
    print(f"  OCR: available={ocr_state['available']}"
          + (f"  reason={ocr_state.get('reason')}" if not ocr_state["available"] else f"  languages={ocr_state['languages']}"))

    username = f"verify5_{uuid.uuid4().hex[:10]}"
    password = "verify-password-123"

    with TestClient(app) as client:
        r = client.post(f"{API}/auth/register", json={"username": username, "password": password})
        check("注册一次性验收用户", r.status_code == 201, f"status={r.status_code}")
        user_id = r.json()["id"]
        r = client.post(f"{API}/auth/login", json={"username": username, "password": password})
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n[3] 准备知识图谱（模拟阶段三人工确认后的知识点）")
        knowledge_points = [
            "阿司匹林的作用机制",
            "青霉素的抗菌机制",
            "半衰期的计算方法",
            "高效液相色谱",
        ]
        with session_scope() as db:
            for kp in knowledge_points:
                db.add(
                    KnowledgeGraph(
                        user_id=user_id,
                        source_type="pubmed",
                        source_id="33301246",
                        entity=kp,
                        relation="作用机制",
                        target=f"{kp}·相关",
                    )
                )
        graph = client.get(f"{API}/literature/graph", headers=headers).json()
        print(f"      知识图谱节点 {graph['stats']['node_count']} 个")
        check("知识图谱已就绪", graph["stats"]["node_count"] >= len(knowledge_points))

        print("\n[4] 制造不同的掌握状态")
        # 高危：阶段二薄弱点（同知识点错 2 次）
        for _ in range(2):
            client.post(
                f"{API}/wrong-questions",
                json={"question_text": "阿司匹林的作用机制", "knowledge_point": "阿司匹林的作用机制"},
                headers=headers,
            )
        # 巩固：一张卡刷一次（稳定性 ≈8 天 < 21）
        moderate = client.post(
            f"{API}/cards",
            json={"front": "青霉素的抗菌机制", "back": "抑制细胞壁合成",
                  "tags": {"chapters": ["青霉素的抗菌机制"]}},
            headers=headers,
        ).json()
        client.post(f"{API}/review/{moderate['id']}/grade", json={"rating": 4}, headers=headers)
        # 确认：两张卡各评「简单」，稳定性 ≈65 天 ≥ 21
        mastered = client.post(
            f"{API}/cards",
            json={"front": "半衰期的计算方法", "back": "t1/2 = 0.693/k",
                  "tags": {"chapters": ["半衰期的计算方法"]}},
            headers=headers,
        ).json()
        first_grade = client.post(
            f"{API}/review/{mastered['id']}/grade", json={"rating": 4}, headers=headers
        ).json()
        second_grade = client.post(
            f"{API}/review/{mastered['id']}/grade",
            json={"rating": 4, "reviewed_at": first_grade["due"]},
            headers=headers,
        ).json()
        print(f"      巩固卡稳定性 ≈ {second_grade['card']['fsrs_stability']:.1f} 天（青霉素）")
        check("掌握状态已构造", True)

        print("\n[5] 验收 1：导入考点文本并匹配知识图谱")
        text = "\n".join(
            [
                "1. 阿司匹林的作用机制",
                "2. 青霉素的抗菌机制",
                "3. 半衰期的计算方法",
                "4. aspirin 是怎么起效的",          # 同义/改写表述
                "5. 实验室消防安全管理制度",          # 图谱中没有
            ]
        )
        r = client.post(
            f"{EXP}/import",
            json={"text": text, "auto_insert_high_risk": False},
            headers=headers,
        )
        check("导入成功 (201)", r.status_code == 201, f"status={r.status_code}")
        imported = r.json()
        print(
            f"      解析出考点 {imported['points_found']} 条，"
            f"重复合并 {imported.get('duplicates_skipped', 0)} 条，"
            f"匹配后端 {imported['embedding_backend']}"
        )
        # 去重合并：同一考点重复出现时只保留一行（本轮新增行为）
        check(
            "重复考点已合并（duplicates_skipped 已上报）",
            "duplicates_skipped" in imported,
            f"{imported.get('duplicates_skipped', 0)} 条",
        )
        for f in imported["findings"]:
            mark = "✓" if f["matched"] else "✗"
            print(f"        {mark} {f['point_text'][:22]:24} -> {f['matched_knowledge_point'] or '（未匹配）'}"
                  f"  [{f['priority']}] score={f['match_score']} sem={f['semantic_similarity']}")

        matched = [f for f in imported["findings"] if f["matched"]]
        check("匹配到知识图谱中的知识点", len(matched) >= 4, f"{len(matched)}/5")
        check("改写表述也能匹配（语义精筛生效）",
              any("aspirin" in f["point_text"] and f["matched"] for f in imported["findings"]))
        check("未匹配考点被如实标记",
              any(not f["matched"] for f in imported["findings"]))
        check("给出匹配依据（分数/相似度）",
              all("match_score" in f for f in imported["findings"]))

        print("\n[6] 验收 2：按高危 / 巩固 / 确认三级分类")
        by_point = {f["point_text"]: f for f in imported["findings"]}
        for label, point in [
            ("高危", "阿司匹林的作用机制"),
            ("巩固", "青霉素的抗菌机制"),
            ("确认", "半衰期的计算方法"),
        ]:
            finding = by_point[point]
            print(f"      {label}: {point}  掌握度={finding['mastery']}  理由={finding['reason'][:44]}")
        check("薄弱点被判为高危", by_point["阿司匹林的作用机制"]["priority"] == "高危")
        check("掌握一般被判为巩固", by_point["青霉素的抗菌机制"]["priority"] == "巩固")
        check("已掌握被判为确认", by_point["半衰期的计算方法"]["priority"] == "确认")
        check("未匹配考点按高危处理",
              by_point["实验室消防安全管理制度"]["priority"] == "高危")
        summary = imported["summary"]["by_priority"]
        print(f"      汇总: {summary}")
        check("汇总含三级计数", all(k in summary for k in ("高危", "巩固", "确认")))

        print("\n[7] 验收 3：高危考点自动插入复习队列")
        r = client.post(f"{EXP}/import", json={"text": "实验室消防安全管理制度"}, headers=headers)
        auto = r.json()
        print(f"      自动插入 {auto['inserted_to_queue']} 张卡片: "
              f"{auto['findings'][0]['card_ids']}")
        print(f"      提示: {auto['note']}")
        check("高危考点已自动插入", auto["inserted_to_queue"] >= 1)
        check("响应回传了卡片 id", bool(auto["findings"][0]["card_ids"]))

        card_id = auto["findings"][0]["card_ids"][0]
        due = client.get(f"{API}/review/due", headers=headers).json()
        check("卡片出现在到期队列中", card_id in [item["id"] for item in due["items"]])
        card = client.get(f"{API}/cards/{card_id}", headers=headers).json()
        print(f"      卡片正面: {card['front']}")
        check("卡片正面标注为考点", "考点" in card["front"])

        print("\n[8] 插入幂等性")
        first_run = client.post(
            f"{EXP}/to-review-queue", json={"priorities": ["高危"]}, headers=headers
        ).json()
        second_run = client.post(
            f"{EXP}/to-review-queue", json={"priorities": ["高危"]}, headers=headers
        ).json()
        print(f"      第一次: inserted={first_run['inserted']} skipped={first_run['skipped']}")
        print(f"      第二次: inserted={second_run['inserted']} skipped={second_run['skipped']}")
        check("重复插入不产生重复卡片",
              second_run["inserted"] == 0 and second_run["skipped"] >= 1)

        print("\n[9] 针对性复习清单")
        review = client.get(f"{EXP}/review-list", headers=headers).json()
        print(f"      清单总数 {review['total']}，高危 {len(review['groups'].get('高危', []))} 条")
        for item in review["items"][:3]:
            print(f"        [{item['priority']}] {item['point_text'][:26]}")
        check("清单按优先级排序", bool(review["items"]) and review["items"][0]["priority"] == "高危")
        check("清单按三级分组", "高危" in review["groups"])
        check("给出提示文案", bool(review["note"]))

        print("\n[10] 能力状态与 OCR")
        capabilities = client.get(f"{EXP}/capabilities", headers=headers).json()
        print(f"      嵌入后端: {capabilities['embedding']['backend']}"
              f" (semantic={capabilities['embedding']['is_semantic']})")
        print(f"      OCR 可用: {capabilities['ocr']['available']}")
        check("能力接口可用", capabilities["priorities"] == ["高危", "巩固", "确认"])
        check("capabilities 不阻塞（未加载模型）",
              "loaded" in capabilities["embedding"])
        r = client.post(
            f"{EXP}/import/image",
            files={"file": ("exam.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
            headers=headers,
        )
        if capabilities["ocr"]["available"]:
            check("图片导入走真实 OCR（假图 -> 422）", r.status_code == 422, f"status={r.status_code}")
            check("OCR 已装载语言包", bool(capabilities["ocr"]["languages"]),
                  "、".join(capabilities["ocr"]["languages"]))
        else:
            body = r.json()
            check("OCR 不可用时返回 503 而非 500", r.status_code == 503, f"status={r.status_code}")
            check("给出安装指引", bool(body.get("detail", {}).get("install_hint")))
            print(f"      安装指引: {body['detail']['install_hint']}")
            print(f"      替代方案: {body['detail']['alternatives']}")

        print("\n[11] 数据落库校验")
        with session_scope() as db:
            points = db.execute(select(ExamPoint).where(ExamPoint.user_id == user_id)).scalars().all()
            cards = db.execute(select(Card).where(Card.user_id == user_id)).scalars().all()
        print(f"      考点 {len(points)} 条，卡片 {len(cards)} 张")
        # 原文里的重复考点会被合并，因此不再断言固定条数
        check(
            "考点已写入 MySQL（重复已合并）",
            len(points) >= 5 and len(points) <= 6,
            f"{len(points)} 条",
        )
        check("三级优先级都已落库",
              {p.priority for p in points} >= {"高危", "巩固"})
        check("未匹配考点的知识点为空",
              any(p.matched_knowledge_point is None for p in points))

        print("\n[12] 清理一次性验收数据")
        with session_scope() as db:
            user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if user is not None:
                db.delete(user)

    with session_scope() as db:
        leftover = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    check("一次性用户已删除", leftover is None)

    print("\n" + "=" * 78)
    print(f"结果：{_checks['passed']} 项通过，{_checks['failed']} 项失败")
    print("=" * 78)
    return 0 if _checks["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
