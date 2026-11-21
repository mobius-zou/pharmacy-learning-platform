"""组 2 回归测试（H4 / H5 / H6）。

对应外部审计报告：

- **H4**：``async def`` 端点里同步调用 OCR（subprocess）与 DB/嵌入
  → 单次 OCR 阻塞事件循环数秒至数十秒。
- **H5**：批量录入错题时每行最多 2 次 LLM，且每行都做一次
  ``known_knowledge_points`` 全表扫描 → 一次导入放大成数百次调用。
- **H6**：``mastery_evidence`` 对每个考点各查一次关联卡片 + 错题聚合
  → O(考点数 × 错题数 × 卡片数)。
"""

from __future__ import annotations

import pytest

import app.api.v1.exam_points as exam_api
import app.ocr as ocr_module
import app.services.wrong_question_service as wq_service
from app.config import settings
from app.models import Card, ExamPoint, WrongQuestion
from app.services import exam_match_service as exam_service
from app.timeutils import utcnow
from tests.conftest import requires_db
from tests.support import count_sql

API = "/api/v1"


# --- H4：OCR 语言包缓存 -----------------------------------------------------


def test_ocr_languages_are_cached(monkeypatch) -> None:
    """枚举语言包会起子进程，必须缓存（原先每次 OCR 都调一次）。"""
    import pytesseract

    ocr_module.reset_ocr_backend()
    backend = ocr_module.get_ocr_backend()
    calls: list[int] = []

    monkeypatch.setattr(type(backend), "available", property(lambda self: True))
    monkeypatch.setattr(
        pytesseract, "get_languages", lambda config=None: calls.append(1) or ["chi_sim", "eng"]
    )

    first = backend.installed_languages()
    second = backend.installed_languages()

    assert first == second == ["chi_sim", "eng"]
    assert len(calls) == 1, f"语言包应只枚举一次，实际 {len(calls)} 次"
    ocr_module.reset_ocr_backend()


# --- H4：OCR / 导入在线程池执行，不阻塞事件循环 -----------------------------


@requires_db
@pytest.mark.db
def test_image_import_runs_ocr_off_event_loop(client, auth_headers, monkeypatch) -> None:
    """OCR 与导入必须经 ``run_in_threadpool``，否则会卡死整个 worker。"""
    calls: list[str] = []
    real_run = exam_api.run_in_threadpool

    async def recording_run(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(getattr(func, "__name__", str(func)))
        return await real_run(func, *args, **kwargs)

    monkeypatch.setattr(exam_api, "run_in_threadpool", recording_run)
    # 让 OCR 直接返回一段文本，避免依赖真实 tesseract
    def fake_extract(raw, languages="chi_sim+eng"):  # noqa: ANN001
        return ocr_module.OCRResult(
            ok=True, text="阿司匹林的作用机制", backend="fake", languages="chi_sim"
        )

    monkeypatch.setattr(ocr_module, "extract_text_from_image", fake_extract)

    resp = client.post(
        f"{API}/exam-points/import/image",
        files={"file": ("exam.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert "fake_extract" in calls, f"OCR 未经线程池：{calls}"
    assert "_import_text" in calls, f"导入未经线程池：{calls}"


@requires_db
@pytest.mark.db
def test_file_import_runs_off_event_loop(client, auth_headers, monkeypatch) -> None:
    calls: list[str] = []
    real_run = exam_api.run_in_threadpool

    async def recording_run(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(getattr(func, "__name__", str(func)))
        return await real_run(func, *args, **kwargs)

    monkeypatch.setattr(exam_api, "run_in_threadpool", recording_run)

    resp = client.post(
        f"{API}/exam-points/import/file",
        files={"file": ("a.txt", "考点一\n考点二".encode(), "text/plain")},
        headers=auth_headers,
    )

    assert resp.status_code == 201
    assert "_import_text" in calls


# --- H5：批量录入不再放大 LLM 调用 ------------------------------------------


@requires_db
@pytest.mark.db
def test_bulk_wrong_questions_uses_heuristics_by_default(db_session, user_id) -> None:
    """批量导入默认走本地启发式，不逐条调用 LLM。"""
    text = "\n".join(f"阿司匹林的作用机制 第{i}题" for i in range(10))

    records, errors = wq_service.create_wrong_questions_bulk(db_session, user_id, text)

    assert len(records) == 10
    assert errors == []
    # 启发式识别出的知识点不会是空
    assert all(record.knowledge_point for record in records)


@requires_db
@pytest.mark.db
def test_bulk_wrong_questions_computes_known_points_once(db_session, user_id, monkeypatch) -> None:
    """已知知识点只取一次 —— 原先每行一次全表扫描。"""
    calls: list[int] = []
    real = wq_service.known_knowledge_points

    def counting(db, uid):  # noqa: ANN001
        calls.append(1)
        return real(db, uid)

    monkeypatch.setattr(wq_service, "known_knowledge_points", counting)
    text = "\n".join(f"题目{i}" for i in range(20))

    wq_service.create_wrong_questions_bulk(db_session, user_id, text)

    # create_wrong_question 内部仍会调用（每次收到 known_points 就不查），
    # 但批量入口只应算一次
    assert len(calls) == 1, f"known_knowledge_points 应只调用一次，实际 {len(calls)} 次"


@requires_db
@pytest.mark.db
def test_bulk_wrong_questions_respects_line_cap(db_session, user_id, monkeypatch) -> None:
    """超过行数上限要截断并明确告知，而不是生成数万行。"""
    monkeypatch.setattr(settings, "wrong_question_bulk_max_lines", 5)
    text = "\n".join(f"题目{i}" for i in range(20))

    records, errors = wq_service.create_wrong_questions_bulk(db_session, user_id, text)

    assert len(records) == 5
    assert any("最多导入 5 条" in message for message in errors)


@requires_db
@pytest.mark.db
def test_bulk_explicit_knowledge_point_needs_no_identification(db_session, user_id) -> None:
    records, _errors = wq_service.create_wrong_questions_bulk(
        db_session, user_id, "题目一 | 指定知识点\n题目二 | 指定知识点"
    )

    assert [record.knowledge_point for record in records] == ["指定知识点", "指定知识点"]


# --- H6：掌握度证据批量预取 -------------------------------------------------


def _seed_points(db_session, user_id: int, count: int) -> list[str]:
    """造 count 个知识点：各有 1 张卡 + 1 条错题。"""
    names: list[str] = []
    for index in range(count):
        point = f"考点知识点{index}"
        names.append(point)
        db_session.add(
            WrongQuestion(
                user_id=user_id,
                question_text=f"{point} 题目",
                knowledge_point=point,
                error_count=1,
                created_at=utcnow(),
            )
        )
        db_session.add(
            Card(
                user_id=user_id,
                front=f"{point} 内容",
                back="答案",
                tags={"subjects": [], "chapters": [point], "point_types": []},
                created_at=utcnow(),
            )
        )
    db_session.flush()
    return names


@requires_db
@pytest.mark.db
def test_mastery_context_query_count_is_constant(db_session, db_engine, user_id) -> None:
    """查询数不随知识点数量增长（原先每个知识点约 3 条查询）。"""
    small = _seed_points(db_session, user_id, 2)
    with count_sql(db_engine) as small_counter:
        exam_service.build_mastery_context(db_session, user_id, small)

    large = _seed_points(db_session, user_id, 6)
    with count_sql(db_engine) as large_counter:
        exam_service.build_mastery_context(db_session, user_id, large)

    assert large_counter["count"] == small_counter["count"], (
        f"知识点从 {len(small)} 增到 {len(large)} 时查询数应不变，"
        f"实际 {small_counter['count']} -> {large_counter['count']}"
    )
    assert small_counter["count"] <= 5, f"预取应只有几条固定查询，实际 {small_counter['count']}"


@requires_db
@pytest.mark.db
def test_mastery_context_matches_single_point_lookup(db_session, user_id) -> None:
    """批量上下文的结果必须与单点查询一致（重构不得改变语义）。"""
    names = _seed_points(db_session, user_id, 3)

    context = exam_service.build_mastery_context(db_session, user_id, names)

    for point in names:
        single = exam_service.mastery_evidence(db_session, user_id, point)
        bulk = context.evidence(point)
        assert bulk.card_count == single.card_count
        assert bulk.new_card_count == single.new_card_count
        assert bulk.total_errors == single.total_errors
        assert bulk.avg_stability == single.avg_stability
        assert bulk.weak_point_hit == single.weak_point_hit


@requires_db
@pytest.mark.db
def test_mastery_context_unknown_point_is_empty(db_session, user_id) -> None:
    context = exam_service.build_mastery_context(db_session, user_id, ["不存在的知识点"])

    evidence = context.evidence("不存在的知识点")

    assert evidence.card_count == 0
    assert evidence.total_errors == 0
    assert evidence.weak_point_hit is False


@requires_db
@pytest.mark.db
def test_import_exam_points_query_count_is_constant(client, auth_headers, db_session, db_engine, user_id) -> None:
    """导入考点时，查询数不应随考点数量线性增长。"""
    _seed_points(db_session, user_id, 2)

    with count_sql(db_engine) as small:
        client.post(
            f"{API}/exam-points/import",
            json={"text": "考点知识点0\n考点知识点1", "auto_insert_high_risk": False},
            headers=auth_headers,
        )

    _seed_points(db_session, user_id, 5)
    with count_sql(db_engine) as large:
        client.post(
            f"{API}/exam-points/import",
            json={
                "text": "\n".join(f"考点知识点{i}" for i in range(6)),
                "auto_insert_high_risk": False,
            },
            headers=auth_headers,
        )

    assert large["select"] == small["select"], (
        f"考点数增加后 **读查询** 数应保持恒定（写入按条数是必要的），"
        f"实际 SELECT {small['select']} -> {large['select']}"
    )
