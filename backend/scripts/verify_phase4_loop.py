#!/usr/bin/env python
"""阶段四验收脚本：实验档案 + 偏差回溯引擎。

覆盖两条验收标准：
1. 能创建一份实验记录，含偏差描述和偏差类型
2. 下次创建相似实验时，系统提示历史偏差

同时演示：模板主动提示、规则匹配的两个必要条件、相关度排序、试剂批号/仪器校准持久化、
偏差统计。**全部为规则引擎 + MySQL 查询，不涉及任何 AI 调用。**

脚本只创建一个一次性用户，结束时删除（外键 CASCADE 连带清理），不留业务数据。

用法：
    cd backend && ./venv/bin/python scripts/verify_phase4_loop.py
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

from app.database import Base, get_engine, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Experiment, User  # noqa: E402

API = "/api/v1"
EXP = f"{API}/experiments"

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
    print("阶段四验收：实验档案 —— 药学 ELN + 偏差回溯规则引擎（无 AI）")
    print("=" * 78)

    engine = get_engine()
    print(f"\n[1] 数据库\n  {engine.url.render_as_string(hide_password=True)}")
    _ensure_schema()
    tables = sorted(Base.metadata.tables)
    print(f"  表: {', '.join(tables)}")
    check("experiments 表已创建", "experiments" in tables)

    # 确认本模块不依赖任何 AI
    import app.services.experiment_service as exp_service

    module_source = Path(exp_service.__file__).read_text(encoding="utf-8")
    ai_markers = ("langchain", "openai", "LLMClient", "chromadb", "import llm")
    found = [m for m in ai_markers if m in module_source]
    check("偏差引擎不依赖 AI（纯规则）", not found, f"命中: {found}" if found else "无 AI 依赖")
    print(f"  实验类型: {'、'.join(exp_service.EXPERIMENT_TYPES)}")
    print(f"  偏差类型: {'、'.join(exp_service.DEVIATION_TYPES)}")

    username = f"verify4_{uuid.uuid4().hex[:10]}"
    password = "verify-password-123"

    with TestClient(app) as client:
        r = client.post(f"{API}/auth/register", json={"username": username, "password": password})
        check("注册一次性验收用户", r.status_code == 201, f"status={r.status_code}")
        user_id = r.json()["id"]
        r = client.post(f"{API}/auth/login", json={"username": username, "password": password})
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n[2] 验收 1：创建含偏差描述与偏差类型的实验记录")
        r = client.post(
            EXP,
            json={
                "experiment_type": "合成",
                "drug_name": "阿司匹林",
                "drug_structure": "CC(=O)Oc1ccccc1C(=O)O",
                "reagents": [
                    {"name": "水杨酸", "lot": "20240101", "grade": "AR"},
                    {"name": "乙酸酐", "lot": "20240202"},
                ],
                "instruments": [
                    {"name": "磁力搅拌器", "model": "RCT basic"},
                    {"name": "HPLC", "model": "Agilent 1260", "calibration": "2024-01 已校准"},
                ],
                "procedure": "水杨酸与乙酸酐在浓硫酸催化下 70℃ 反应 30 min，冰水淬灭后重结晶。",
                "expected_result": "白色针状结晶，收率 > 80%，熔点 135-136℃",
                "actual_result": "淡黄色固体，收率 42%，熔点 128-131℃",
                "deviation_description": "试剂受潮导致乙酰化不完全，产物发黄、收率偏低",
                "deviation_type": "试剂问题",
                "deviation_handling": "更换新批号乙酸酐并严格干燥，复测收率 85%",
            },
            headers=headers,
        )
        check("创建成功 (201)", r.status_code == 201, f"status={r.status_code}")
        first = r.json()
        record = first["experiment"]
        print(f"      记录 id={record['id']}  类型={record['experiment_type']}  药物={record['drug_name']}")
        print(f"      偏差类型: {record['deviation_type']}")
        print(f"      偏差描述: {record['deviation_description']}")
        print(f"      处理方式: {record['deviation_handling']}")
        check("偏差描述已保存", bool(record["deviation_description"]))
        check("偏差类型已保存", record["deviation_type"] == "试剂问题")
        check("处理方式已保存", bool(record["deviation_handling"]))
        check("试剂批号已保存",
              record["reagents"][0]["lot"] == "20240101" and record["reagents"][0]["grade"] == "AR")
        check("仪器校准信息已保存",
              any(i.get("calibration") for i in record["instruments"]))
        check("首次记录无历史可回溯", first["traceback"]["total"] == 0)

        print("\n[3] 再记录一条「不同偏差类型」的实验（用于验证必要条件）")
        other = client.post(
            EXP,
            json={
                "experiment_type": "合成",
                "drug_name": "阿司匹林",
                "actual_result": "熔点偏低",
                "deviation_description": "熔点仪未校准",
                "deviation_type": "仪器异常",
            },
            headers=headers,
        ).json()
        check("第二条记录创建成功", other["experiment"]["id"] > 0)
        check("偏差类型不同则不匹配",
              all(m["deviation_type"] == "仪器异常" for m in other["traceback"]["matches"]),
              f"命中 {other['traceback']['total']} 条")

        print("\n[4] 验收 2：下次创建相似实验时提示历史偏差")
        second = client.post(
            EXP,
            json={
                "experiment_type": "合成",
                "drug_name": "阿司匹林",
                "reagents": [{"name": "乙酸酐", "lot": "20240303"}],
                "actual_result": "收率又只有 55%",
                "deviation_description": "乙酸酐再次受潮",
                "deviation_type": "试剂问题",
            },
            headers=headers,
        ).json()

        traceback = second["traceback"]
        print(f"      命中历史偏差 {traceback['total']} 条（扫描 {traceback['total_scanned']} 条）")
        for match in traceback["matches"][:3]:
            print(f"        · [{match['level']}] 相关度 {match['relevance']} "
                  f"药物={match['drug_name']} 偏差={match['deviation_type']}")
            print(f"          命中理由: {'；'.join(match['reasons'])}")
        print(f"      系统提示: {second['history_hint']}")

        check("提示了历史偏差", traceback["total"] >= 1)
        check("命中的是首条「试剂问题」记录",
              traceback["matches"][0]["experiment_id"] == record["id"])
        check("未误匹配「仪器异常」记录",
              all(m["deviation_type"] == "试剂问题" for m in traceback["matches"]))
        check("给出了命中理由", bool(traceback["matches"][0]["reasons"]))
        check("相关度等级已评定", traceback["matches"][0]["level"] in {"low", "medium", "high"})
        check("给出了提示文案", bool(second["history_hint"]))

        print("\n[5] 进入相似实验模板时的主动提示")
        r = client.get(f"{EXP}/template/合成?drug_name=阿司匹林&reagent=乙酸酐", headers=headers)
        check("模板接口可用 (200)", r.status_code == 200, f"status={r.status_code}")
        warned = r.json()
        template = warned["template"]
        print(f"      模板: {template['label']}")
        print(f"      关键参数: {'、'.join(template['key_parameters'][:5])}")
        print(f"      风险点: {'；'.join(template['risk_points'][:2])}")
        print(f"      历史偏差分布: {warned['history']['deviation_type_counts']}")
        print(f"      主动提示: {warned['history']['advice']}")
        check("返回了模板骨架", bool(template["procedure_steps"] and template["key_parameters"]))
        check("模板含风险点", bool(template["risk_points"]))
        check("主动提示了历史偏差", warned["history"]["total"] >= 1)
        check("给出了偏差类型分布", bool(warned["history"]["deviation_type_counts"]))
        check("advice 文案非空", bool(warned["history"]["advice"]))

        print("\n[6] 规则匹配的两个必要条件（实验类型 + 偏差类型）")
        r = client.get(
            f"{EXP}/warnings?experiment_type=合成&deviation_type=试剂问题", headers=headers
        )
        both = r.json()["history"]["total"]
        r = client.get(f"{EXP}/warnings?experiment_type=分析&deviation_type=试剂问题", headers=headers)
        wrong_type = r.json()["history"]["total"]
        r = client.get(f"{EXP}/warnings?experiment_type=合成&deviation_type=操作误差", headers=headers)
        wrong_dev = r.json()["history"]["total"]
        print(f"      合成+试剂问题={both}  分析+试剂问题={wrong_type}  合成+操作误差={wrong_dev}")
        check("两个维度都相同才命中", both >= 1)
        check("实验类型不同 → 不命中", wrong_type == 0)
        check("偏差类型不同 → 不命中", wrong_dev == 0)

        print("\n[7] 偏差统计")
        summary = client.get(f"{EXP}/summary", headers=headers).json()
        print(f"      实验总数 {summary['total']}，其中有偏差 {summary['deviation_total']}")
        print(f"      按实验类型: {summary['by_experiment_type']}")
        print(f"      按偏差类型: {summary['by_deviation_type']}")
        check("统计总数正确", summary["total"] == 3)
        check("偏差计数正确", summary["deviation_total"] == 3)
        check("按偏差类型分布正确", summary["by_deviation_type"].get("试剂问题") == 2)

        print("\n[8] 无偏差实验不参与回溯")
        clean = client.post(
            EXP,
            json={
                "experiment_type": "合成",
                "drug_name": "对乙酰氨基酚",
                "actual_result": "收率 88%，符合预期",
            },
            headers=headers,
        ).json()
        check("无偏差记录创建成功", clean["experiment"]["deviation_type"] is None)
        check("无偏差记录不产生回溯", clean["traceback"]["total"] == 0)

        print("\n[9] 数据落库校验")
        with session_scope() as db:
            rows = db.execute(
                select(Experiment).where(Experiment.user_id == user_id)
            ).scalars().all()
            has_deviation = [r for r in rows if r.has_deviation]
        check("记录已写入 MySQL", len(rows) == 4, f"{len(rows)} 条")
        check("偏差记录数正确", len(has_deviation) == 3, f"{len(has_deviation)} 条")

        print("\n[10] 清理一次性验收数据")
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
