#!/usr/bin/env python
"""阶段三验收脚本：文献工作台闭环。

覆盖三条验收标准：
1. 输入一个 PubMed ID，能返回 PICO-D 结构化摘要
2. 结构化结果需人工确认后才能写入数据库
3. 确认后的知识点可一键转为复习卡片

并演示：三元组抽取（LLM 优先 / 规则降级）、知识图谱构建与导出、人工校对、向量检索。

默认使用**真实 PubMed**；网络不可用时自动退回内置样本并明确标注。
脚本只创建一个一次性用户，结束时删除用户及其草稿，不留业务数据。

用法：
    cd backend && ./venv/bin/python scripts/verify_phase3_loop.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.database import Base, get_engine, session_scope  # noqa: E402
from app.llm import default_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Card, KnowledgeGraph, User  # noqa: E402
from app.services import kg_service  # noqa: E402
from app.services import literature_service as lit  # noqa: E402
from tests.test_literature_service import SAMPLE_SEARCH, SAMPLE_XML, FakePubMed  # noqa: E402

API = "/api/v1"
LIT = f"{API}/literature"
PMID = "33301246"

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


def _purge_leftovers() -> None:
    """开跑前清理上次**中途失败**残留的验收用户。

    清理逻辑写在正常路径末尾，一旦中途抛错（网络抖动、断言失败）就不会执行，
    会在业务库里留下一次性用户。这里让脚本「自愈」：每次开跑先扫掉旧残留。
    """
    with session_scope() as db:
        stale = list(
            db.execute(select(User).where(User.username.like("verify3\\_%"))).scalars()
        )
        for user in stale:
            for draft in kg_service.list_drafts(user.id, status=None, limit=500):
                kg_service.delete_draft(draft.draft_id, user.id)
            db.delete(user)
    if stale:
        print(f"  已清理上次残留的验收用户 {len(stale)} 个")


def main() -> int:
    print("=" * 76)
    print("阶段三验收：文献工作台 —— PICO-D 抽取 / 人工校对 / 知识图谱 / 转卡片")
    print("=" * 76)

    engine = get_engine()
    print(f"\n[1] 数据库与向量库")
    print(f"  MySQL: {engine.url.render_as_string(hide_password=True)}")
    print(f"  ChromaDB 目录: {kg_service.settings.chroma_dir}")
    print(f"  嵌入维度: {kg_service.settings.embedding_dim}（离线确定性，跨进程稳定）")
    _ensure_schema()
    _purge_leftovers()
    tables = sorted(Base.metadata.tables)
    print(f"  表: {', '.join(tables)}")
    check("knowledge_graph 表已创建", "knowledge_graph" in tables)

    # PubMed 连通性
    print(f"\n[2] PubMed 连通性")
    pubmed = lit.PubMedClient(timeout=25)
    online = True
    try:
        article = pubmed.fetch(PMID)
        print(f"  在线抓取成功：{article.title[:70]}")
    except lit.PubMedError as exc:
        online = False
        fallback = FakePubMed()
        article = fallback.fetch(PMID)
        print(f"  网络不可用（{exc}），改用内置样本：{article.title[:70]}")
    check("取得文献元数据", bool(article.pmid and article.title))

    llm_mode = "LLM" if default_client.available else "本地规则（无 Agent 降级模式）"
    print(f"  抽取模式: PICO-D={llm_mode}")
    print(f"  LLMGraphTransformer: {'可用' if default_client.available else '未配置 LLM，三元组走规则抽取'}")

    username = f"verify3_{uuid.uuid4().hex[:10]}"
    password = "verify-password-123"
    draft_ids: list[str] = []

    with TestClient(app) as client:
        if not online:
            # 让路由也使用离线样本，保证脚本在无网络时依然完整可跑
            import app.api.v1.literature as lit_router

            lit_router._pubmed = pubmed = FakePubMed()  # noqa: SLF001

        r = client.post(f"{API}/auth/register", json={"username": username, "password": password})
        check("注册一次性验收用户", r.status_code == 201, f"status={r.status_code}")
        r = client.post(f"{API}/auth/login", json={"username": username, "password": password})
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        with session_scope() as db:
            before = db.execute(select(func.count()).select_from(KnowledgeGraph)).scalar_one()

        print(f"\n[3] 验收 1：输入 PMID={PMID}，返回 PICO-D 结构化摘要")
        r = client.post(f"{LIT}/extract", json={"pmid": PMID}, headers=headers)
        check("抽取成功 (200)", r.status_code == 200, f"status={r.status_code}")
        draft = r.json()
        draft_ids.append(draft["draft_id"])

        pico = draft["pico_d"]
        labels = {
            "population": "P 人群",
            "intervention": "I 干预",
            "comparator": "C 对照",
            "outcome": "O 结局",
            "design": "D 设计",
        }
        for key, label in labels.items():
            value = pico[key]["value"] or "（未能识别，需人工补充）"
            print(f"      {label}: {value}   [置信度 {pico[key]['confidence']:.2f}]")
        check("PICO-D 五个字段齐备", all(k in pico for k in labels))
        check("研究设计识别正确", bool(pico["design"]["value"]))
        check("至少 3 个字段抽取到内容",
              sum(1 for k in labels if pico[k]["value"]) >= 3,
              f"{sum(1 for k in labels if pico[k]['value'])}/5")
        check("标注了抽取来源", draft["extraction_source"] in {"heuristic", "llm"},
              draft["extraction_source"])
        print(f"      三元组 {len(draft['triples'])} 条，知识点建议 {len(draft['knowledge_points'])} 个")
        for t in draft["triples"][:5]:
            print(f"        · {t['entity']} --{t['relation']}--> {t['target']}")
        check("抽取到实体关系", len(draft["triples"]) > 0)
        check("给出知识点建议", len(draft["knowledge_points"]) > 0)

        print("\n[4] 验收 2：结构化结果必须人工确认后才入库")
        with session_scope() as db:
            after_extract = db.execute(select(func.count()).select_from(KnowledgeGraph)).scalar_one()
        check("抽取后 MySQL 未新增任何三元组", after_extract == before,
              f"{before} -> {after_extract}")

        graph_before = client.get(f"{LIT}/graph", headers=headers).json()
        check("知识图谱仍为空", graph_before["stats"]["node_count"] == 0)

        # 人工校对：修正干预措施
        corrected = dict(draft["pico_d"])
        corrected["intervention"] = {
            "value": "BNT162b2 mRNA 疫苗（人工核对）",
            "confidence": 1.0,
            "evidence": "人工阅读原文后填写",
        }
        r = client.patch(
            f"{LIT}/reviews/{draft['draft_id']}",
            json={"pico_d": corrected, "note": "已核对原文"},
            headers=headers,
        )
        check("人工校对已保存", r.status_code == 200 and r.json()["note"] == "已核对原文")
        with session_scope() as db:
            after_patch = db.execute(select(func.count()).select_from(KnowledgeGraph)).scalar_one()
        check("校对保存后仍未入库", after_patch == before)

        r = client.post(f"{LIT}/reviews/{draft['draft_id']}/confirm", json={}, headers=headers)
        check("确认入库成功 (200)", r.status_code == 200, f"status={r.status_code}")
        confirmed = r.json()
        print(f"      新增 {confirmed['added']} 条，跳过重复 {confirmed['skipped']} 条")
        with session_scope() as db:
            after_confirm = db.execute(select(func.count()).select_from(KnowledgeGraph)).scalar_one()
        check("确认后三元组已入库", after_confirm > before,
              f"{before} -> {after_confirm}")

        # 幂等性：再确认一次不应重复写入
        second = client.post(f"{LIT}/extract", json={"pmid": PMID}, headers=headers).json()
        draft_ids.append(second["draft_id"])
        repeat = client.post(
            f"{LIT}/reviews/{second['draft_id']}/confirm", json={}, headers=headers
        ).json()
        with session_scope() as db:
            after_repeat = db.execute(select(func.count()).select_from(KnowledgeGraph)).scalar_one()
        check("重复确认不产生重复数据",
              repeat["added"] == 0 and after_repeat == after_confirm,
              f"新增={repeat['added']}")

        print("\n[5] 知识图谱（NetworkX）")
        graph = client.get(f"{LIT}/graph", headers=headers).json()
        stats = graph["stats"]
        print(f"      节点 {stats['node_count']}  关系 {stats['edge_count']}  "
              f"连通分量 {stats['component_count']}  密度 {stats['density']}")
        if stats["top_nodes"]:
            top = "、".join(f"{n['node']}({n['degree']})" for n in stats["top_nodes"][:4])
            print(f"      核心实体: {top}")
        check("图谱含节点与边", stats["node_count"] > 0 and stats["edge_count"] > 0)
        check("统计信息完整",
              {"node_count", "edge_count", "density", "component_count"} <= set(stats))
        for fmt in ("json", "graphml", "gexf"):
            r = client.get(f"{LIT}/graph/export?fmt={fmt}", headers=headers)
            check(f"导出 {fmt} 成功", r.status_code == 200 and len(r.content) > 0,
                  f"{len(r.content)} bytes")

        print("\n[6] 验收 3：知识点一键转为复习卡片")
        points = [k["knowledge_point"] for k in draft["knowledge_points"][:2]]
        r = client.post(
            f"{LIT}/reviews/{draft['draft_id']}/to-cards",
            json={"knowledge_points": points, "tags": {"subjects": ["药理学"]}},
            headers=headers,
        )
        check("转卡片成功 (200)", r.status_code == 200, f"status={r.status_code}")
        cards = r.json()
        print(f"      生成 {cards['created']} 张卡片：{cards['card_ids']}")
        check("生成了复习卡片", cards["created"] == len(points))
        if cards["card_ids"]:
            card = client.get(f"{API}/cards/{cards['card_ids'][0]}", headers=headers).json()
            print(f"      示例卡片正面: {card['front']}")
            check("知识点写入章节标签", points[0] in card["tags"]["chapters"])
            check("卡片是新卡待复习", card["is_new"] is True)
            due = client.get(f"{API}/review/due", headers=headers).json()
            check("卡片进入复习队列",
                  cards["card_ids"][0] in [i["id"] for i in due["items"]])

        print("\n[7] 向量检索（ChromaDB + 离线嵌入）")
        r = client.get(f"{LIT}/vector-search?q=BNT162b2 vaccine efficacy&limit=3", headers=headers)
        check("向量检索可用", r.status_code == 200 and len(r.json()["items"]) > 0,
              f"{len(r.json().get('items', []))} 条命中")

        print("\n[8] 清理一次性验收数据")
        for draft_id in draft_ids:
            kg_service.delete_draft(draft_id, 0) or None
        with session_scope() as db:
            user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
            user_id = user.id if user else None
            if user is not None:
                db.delete(user)
        # 草稿在 ChromaDB 中，需要显式清理
        if user_id is not None:
            for draft in kg_service.list_drafts(user_id, status=None, limit=200):
                kg_service.delete_draft(draft.draft_id, user_id)

    with session_scope() as db:
        leftover_user = db.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
    check("一次性用户已删除", leftover_user is None)

    print("\n" + "=" * 76)
    print(f"结果：{_checks['passed']} 项通过，{_checks['failed']} 项失败")
    print("=" * 76)
    return 0 if _checks["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
