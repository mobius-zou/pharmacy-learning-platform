"""实验档案 API 集成测试（需要 MySQL，未配置时自动跳过）。

覆盖阶段四两条验收标准：
1. 能创建一份实验记录，含偏差描述和偏差类型
2. 下次创建相似实验时，系统提示历史偏差
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Experiment
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.db]

API = "/api/v1"
EXP = f"{API}/experiments"


def _create(client, headers, **overrides):
    payload = {
        "experiment_type": "合成",
        "drug_name": "阿司匹林",
        "procedure": "乙酰化反应，控温 70℃",
        "expected_result": "收率 > 80%",
    }
    payload.update(overrides)
    resp = client.post(EXP, json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- 验收 1：创建含偏差的实验记录 -------------------------------------------


def test_create_experiment_with_deviation(client, auth_headers) -> None:
    """验收 1：能创建一份实验记录，含偏差描述和偏差类型。"""
    body = _create(
        client,
        auth_headers,
        actual_result="收率仅 42%",
        deviation_description="试剂受潮，乙酰化不完全",
        deviation_type="试剂问题",
        deviation_handling="更换新批号试剂后复测，收率 85%",
    )

    record = body["experiment"]
    assert record["id"] > 0
    assert record["experiment_type"] == "合成"
    assert record["drug_name"] == "阿司匹林"
    assert record["deviation_description"] == "试剂受潮，乙酰化不完全"
    assert record["deviation_type"] == "试剂问题"
    assert record["deviation_handling"] == "更换新批号试剂后复测，收率 85%"
    assert record["created_at"]
    assert "traceback" in body
    assert body["history_hint"]


def test_create_experiment_without_deviation(client, auth_headers) -> None:
    """无偏差实验也应能记录（不参与回溯）。"""
    body = _create(client, auth_headers, drug_name="布洛芬")

    assert body["experiment"]["deviation_type"] is None
    assert body["traceback"]["total"] == 0


def test_reagents_with_lot_numbers_are_preserved(client, auth_headers) -> None:
    """试剂与批号必须完整落库，不能丢字段。"""
    body = _create(
        client,
        auth_headers,
        reagents=[
            {"name": "乙酸酐", "lot": "20240101", "grade": "AR", "supplier": "国药"},
            "水杨酸|20240202",
            {"试剂": "丙酮", "批号": "20240303"},
        ],
    )

    reagents = body["experiment"]["reagents"]
    assert len(reagents) == 3

    by_name = {r["name"]: r for r in reagents}
    assert by_name["乙酸酐"]["lot"] == "20240101"
    assert by_name["乙酸酐"]["grade"] == "AR", "额外字段不应被丢弃"
    assert by_name["乙酸酐"]["supplier"] == "国药"
    assert by_name["水杨酸"]["lot"] == "20240202"
    assert by_name["丙酮"]["lot"] == "20240303", "中文键名应归一化为 lot"


def test_instruments_with_calibration_are_preserved(client, auth_headers) -> None:
    body = _create(
        client,
        auth_headers,
        instruments=[
            {"name": "HPLC", "model": "Agilent 1260", "calibration": "2024-01 已校准"},
            "熔点仪",
        ],
    )

    instruments = body["experiment"]["instruments"]
    hplc = next(i for i in instruments if i["name"] == "HPLC")
    assert hplc["model"] == "Agilent 1260"
    assert hplc["calibration"] == "2024-01 已校准"


def test_drug_structure_is_stored(client, auth_headers) -> None:
    body = _create(client, auth_headers, drug_structure="CC(=O)Oc1ccccc1C(=O)O")

    assert body["experiment"]["drug_structure"] == "CC(=O)Oc1ccccc1C(=O)O"


def test_persistence_through_orm(client, auth_headers, db_session) -> None:
    body = _create(
        client,
        auth_headers,
        deviation_description="控温失灵",
        deviation_type="仪器异常",
    )

    db_session.expire_all()
    row = db_session.execute(
        select(Experiment).where(Experiment.id == body["experiment"]["id"])
    ).scalar_one()

    assert row.deviation_type == "仪器异常"
    assert row.deviation_description == "控温失灵"
    assert row.experiment_type == "合成"
    assert row.has_deviation is True


def test_invalid_experiment_type_rejected(client, auth_headers) -> None:
    resp = client.post(
        EXP, json={"experiment_type": "炼金术", "drug_name": "铅"}, headers=auth_headers
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


def test_invalid_deviation_type_rejected(client, auth_headers) -> None:
    resp = client.post(
        EXP,
        json={"experiment_type": "合成", "drug_name": "阿司匹林", "deviation_type": "心情不好"},
        headers=auth_headers,
    )

    assert resp.status_code == 422


def test_deviation_description_requires_type(client, auth_headers) -> None:
    """有描述无类型会让回溯引擎无法匹配，必须在写入前拒绝。"""
    resp = client.post(
        EXP,
        json={
            "experiment_type": "合成",
            "drug_name": "阿司匹林",
            "deviation_description": "试剂受潮",
        },
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert "偏差类型" in resp.json()["message"]


# --- 验收 2：下次创建相似实验时提示历史偏差 ---------------------------------


def test_second_similar_experiment_prompts_history(client, auth_headers) -> None:
    """验收 2：下次创建相似实验时，系统提示历史偏差。"""
    first = _create(
        client,
        auth_headers,
        drug_name="阿司匹林",
        deviation_description="试剂受潮导致乙酰化不完全",
        deviation_type="试剂问题",
        deviation_handling="更换批号后复测正常",
        reagents=[{"name": "乙酸酐", "lot": "20240101"}],
    )

    # 第二次做相似实验（同类型 + 同偏差类型）
    second = _create(
        client,
        auth_headers,
        drug_name="阿司匹林",
        deviation_description="又是试剂受潮",
        deviation_type="试剂问题",
        reagents=[{"name": "乙酸酐", "lot": "20240101"}],
    )

    traceback = second["traceback"]
    assert traceback["total"] >= 1, "应提示历史偏差"
    assert traceback["matches"][0]["experiment_id"] == first["experiment"]["id"]
    assert traceback["matches"][0]["deviation_type"] == "试剂问题"
    assert "历史处理方式可参考" in traceback["advice"] or traceback["advice"]
    assert second["history_hint"]


def test_template_endpoint_prompts_history(client, auth_headers) -> None:
    """进入相似实验模板时应主动提示历史偏差（验收 2 的主要入口）。"""
    _create(
        client,
        auth_headers,
        deviation_description="试剂受潮",
        deviation_type="试剂问题",
    )
    _create(
        client,
        auth_headers,
        deviation_description="仪器未校准",
        deviation_type="仪器异常",
    )

    resp = client.get(
        f"{EXP}/template/合成?drug_name=阿司匹林", headers=auth_headers
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template"]["experiment_type"] == "合成"
    assert body["template"]["procedure_steps"]
    assert body["template"]["risk_points"]

    history = body["history"]
    assert history["total"] == 2, "同实验类型的两条偏差都应被提示"
    assert history["deviation_type_counts"] == {"试剂问题": 1, "仪器异常": 1}
    assert history["advice"]


def test_template_endpoint_empty_for_new_user(client, auth_headers) -> None:
    resp = client.get(f"{EXP}/template/药理", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["history"]["total"] == 0
    assert "暂无" in body["history"]["advice"]
    assert body["template"]["label"] == "药理实验"


def test_template_unknown_type_rejected(client, auth_headers) -> None:
    resp = client.get(f"{EXP}/template/炼金术", headers=auth_headers)

    assert resp.status_code == 422


def test_warnings_endpoint_filters_by_both_dimensions(client, auth_headers) -> None:
    """匹配条件是「实验类型相同 + 偏差类型相同」。"""
    _create(
        client, auth_headers, experiment_type="合成", deviation_type="试剂问题",
        deviation_description="A",
    )
    _create(
        client, auth_headers, experiment_type="合成", deviation_type="仪器异常",
        deviation_description="B",
    )
    _create(
        client, auth_headers, experiment_type="分析", deviation_type="试剂问题",
        deviation_description="C",
    )

    resp = client.get(
        f"{EXP}/warnings?experiment_type=合成&deviation_type=试剂问题", headers=auth_headers
    )

    assert resp.status_code == 200
    history = resp.json()["history"]
    assert history["total"] == 1, "只有两个维度都相同的才命中"
    assert history["matches"][0]["deviation_type"] == "试剂问题"


def test_warnings_without_deviation_type_returns_whole_type(client, auth_headers) -> None:
    """进入模板尚未发生偏差时，按实验类型召回全部（用于主动提示）。"""
    _create(client, auth_headers, experiment_type="合成", deviation_type="试剂问题",
            deviation_description="A")
    _create(client, auth_headers, experiment_type="合成", deviation_type="仪器异常",
            deviation_description="B")

    resp = client.get(f"{EXP}/warnings?experiment_type=合成", headers=auth_headers)

    assert resp.json()["history"]["total"] == 2


def test_create_excludes_itself_from_traceback(client, auth_headers) -> None:
    """第一条记录不应把自己当成历史偏差。"""
    body = _create(
        client,
        auth_headers,
        deviation_description="首次偏差",
        deviation_type="操作误差",
    )

    assert body["traceback"]["total"] == 0
    assert all(
        m["experiment_id"] != body["experiment"]["id"] for m in body["traceback"]["matches"]
    )


def test_same_drug_ranks_above_different_drug(client, auth_headers) -> None:
    _create(client, auth_headers, drug_name="布洛芬", deviation_description="别的药",
            deviation_type="试剂问题")
    same = _create(client, auth_headers, drug_name="阿司匹林", deviation_description="同药",
                   deviation_type="试剂问题")

    later = _create(client, auth_headers, drug_name="阿司匹林", deviation_description="再来",
                    deviation_type="试剂问题")

    matches = later["traceback"]["matches"]
    assert matches[0]["experiment_id"] == same["experiment"]["id"], "同药物应排最前"
    assert matches[0]["level"] in {"medium", "high"}
    assert any("药物名称相同" in r for r in matches[0]["reasons"])


def test_no_deviation_records_are_not_traced(client, auth_headers) -> None:
    """无偏差的记录不参与回溯。"""
    _create(client, auth_headers, drug_name="布洛芬")  # 无偏差

    body = _create(
        client, auth_headers, deviation_description="有偏差", deviation_type="操作误差"
    )

    assert body["traceback"]["total"] == 0


def test_traceback_endpoint_for_existing_record(client, auth_headers) -> None:
    first = _create(client, auth_headers, deviation_description="先发生",
                    deviation_type="方案设计缺陷")
    second = _create(client, auth_headers, deviation_description="后发生",
                     deviation_type="方案设计缺陷")

    resp = client.get(
        f"{EXP}/{second['experiment']['id']}/traceback", headers=auth_headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["matches"][0]["experiment_id"] == first["experiment"]["id"]
    assert body["matches"][0]["created_at"]


# --- 模板与选项 -------------------------------------------------------------


def test_options_endpoint(client, auth_headers) -> None:
    body = client.get(f"{EXP}/options", headers=auth_headers).json()

    assert body["experiment_types"] == ["合成", "分析", "药理", "药剂"]
    assert body["deviation_types"] == ["操作误差", "试剂问题", "仪器异常", "方案设计缺陷"]


def test_templates_endpoint(client, auth_headers) -> None:
    body = client.get(f"{EXP}/templates", headers=auth_headers).json()

    assert len(body) == 4
    for template in body:
        assert template["label"]
        assert template["procedure_steps"]
        assert template["key_parameters"]
        assert template["risk_points"]


# --- 查询 / 修改 / 删除 -----------------------------------------------------


def test_list_and_filters(client, auth_headers) -> None:
    _create(client, auth_headers, experiment_type="合成", drug_name="阿司匹林",
            deviation_type="试剂问题", deviation_description="A")
    _create(client, auth_headers, experiment_type="分析", drug_name="布洛芬")
    _create(client, auth_headers, experiment_type="合成", drug_name="对乙酰氨基酚",
            deviation_type="仪器异常", deviation_description="B")

    assert client.get(EXP, headers=auth_headers).json()["total"] == 3
    assert client.get(f"{EXP}?experiment_type=合成", headers=auth_headers).json()["total"] == 2
    assert client.get(f"{EXP}?only_deviations=true", headers=auth_headers).json()["total"] == 2
    assert (
        client.get(f"{EXP}?deviation_type=仪器异常", headers=auth_headers).json()["total"] == 1
    )
    assert client.get(f"{EXP}?drug_name=阿司匹林", headers=auth_headers).json()["total"] == 1
    assert client.get(f"{EXP}?keyword=对乙酰氨基酚", headers=auth_headers).json()["total"] == 1


def test_get_update_delete(client, auth_headers) -> None:
    created = _create(client, auth_headers)
    experiment_id = created["experiment"]["id"]

    assert client.get(f"{EXP}/{experiment_id}", headers=auth_headers).status_code == 200

    resp = client.patch(
        f"{EXP}/{experiment_id}",
        json={
            "actual_result": "收率 85%",
            "deviation_description": "补记：试剂受潮",
            "deviation_type": "试剂问题",
            "deviation_handling": "更换批号",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["actual_result"] == "收率 85%"
    assert resp.json()["deviation_type"] == "试剂问题"
    assert resp.json()["drug_name"] == "阿司匹林", "未提供的字段不应被清空"

    assert client.delete(f"{EXP}/{experiment_id}", headers=auth_headers).status_code == 204
    assert client.get(f"{EXP}/{experiment_id}", headers=auth_headers).status_code == 404


def test_update_rejects_invalid_deviation_type(client, auth_headers) -> None:
    created = _create(client, auth_headers)
    experiment_id = created["experiment"]["id"]

    resp = client.patch(
        f"{EXP}/{experiment_id}", json={"deviation_type": "心情不好"}, headers=auth_headers
    )

    assert resp.status_code == 422


def test_unknown_experiment_returns_404(client, auth_headers) -> None:
    assert client.get(f"{EXP}/999999", headers=auth_headers).status_code == 404
    assert client.get(f"{EXP}/999999/traceback", headers=auth_headers).status_code == 404
    assert client.delete(f"{EXP}/999999", headers=auth_headers).status_code == 404


def test_summary_endpoint(client, auth_headers) -> None:
    _create(client, auth_headers, experiment_type="合成", deviation_type="试剂问题",
            deviation_description="A")
    _create(client, auth_headers, experiment_type="合成", deviation_type="试剂问题",
            deviation_description="B")
    _create(client, auth_headers, experiment_type="分析", deviation_type="仪器异常",
            deviation_description="C")
    _create(client, auth_headers, experiment_type="分析")  # 无偏差

    body = client.get(f"{EXP}/summary", headers=auth_headers).json()

    assert body["total"] == 4
    assert body["deviation_total"] == 3
    assert body["by_experiment_type"] == {"合成": 2, "分析": 2}
    assert body["by_deviation_type"] == {"试剂问题": 2, "仪器异常": 1}
    assert body["top_drugs_with_deviations"]
    cross = {(row["experiment_type"], row["deviation_type"]): row["count"] for row in body["cross"]}
    assert cross[("合成", "试剂问题")] == 2


# --- 多用户隔离 -------------------------------------------------------------


def test_experiments_isolated_between_users(client, auth_headers, user_credentials) -> None:
    created = _create(
        client, auth_headers, deviation_description="机密偏差", deviation_type="试剂问题"
    )
    experiment_id = created["experiment"]["id"]

    other = {
        "username": user_credentials["username"] + "_exp",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    # 他人既看不到也改不了我的记录
    assert client.get(EXP, headers=other_headers).json()["total"] == 0
    assert client.get(f"{EXP}/{experiment_id}", headers=other_headers).status_code == 404
    assert (
        client.patch(
            f"{EXP}/{experiment_id}", json={"actual_result": "篡改"}, headers=other_headers
        ).status_code
        == 404
    )
    assert client.delete(f"{EXP}/{experiment_id}", headers=other_headers).status_code == 404

    # 他人新建同类型同偏差类型的实验 —— 也不能进入我的回溯结果
    other_created = _create(
        client,
        other_headers,
        experiment_type="合成",
        deviation_type="试剂问题",
        deviation_description="他人的偏差",
    )

    mine = client.get(
        f"{EXP}/warnings?experiment_type=合成&deviation_type=试剂问题", headers=auth_headers
    ).json()

    my_ids = {match["experiment_id"] for match in mine["history"]["matches"]}
    assert experiment_id in my_ids
    assert other_created["experiment"]["id"] not in my_ids, "绝不能回溯到他人的记录"

    # 反向同理：他人的回溯里只有他自己那条
    theirs = client.get(
        f"{EXP}/warnings?experiment_type=合成&deviation_type=试剂问题", headers=other_headers
    ).json()
    their_ids = {match["experiment_id"] for match in theirs["history"]["matches"]}
    assert their_ids == {other_created["experiment"]["id"]}


def test_unauthorized_access_rejected(client) -> None:
    assert client.get(EXP).status_code == 401
    assert client.post(EXP, json={"experiment_type": "合成", "drug_name": "x"}).status_code == 401
