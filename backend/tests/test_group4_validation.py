"""组 4 回归测试（M10/M11/M12/M14/L1/L4/L7/L17/L18）。

对应审计报告 P2/P3 中的「输入校验 / 配置健壮性 / 规范」类问题。
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.config import Settings, validate_settings
from app.limits import MAX_CONTENT_LENGTH, MAX_SHORT_TEXT_LENGTH
from app.ratelimit import SlidingWindowLimiter, reset_register_limiter
from app.services.review_service import format_interval
from tests.conftest import requires_db

API = "/api/v1"


# --- M10：缺失的 max_length ------------------------------------------------


def test_login_request_has_length_limits() -> None:
    from app.schemas.auth import LoginRequest

    fields = LoginRequest.model_fields
    assert fields["username"].metadata, "username 应有 max_length"
    assert fields["password"].metadata

    with pytest.raises(Exception):
        LoginRequest(username="a" * 100, password="x")
    with pytest.raises(Exception):
        LoginRequest(username="u", password="x" * 200)


def test_wrong_question_fields_have_limits() -> None:
    from app.schemas.wrong_question import (
        WrongQuestionCreateRequest,
        IncrementErrorRequest,
    )

    long_kp = "知" * (MAX_SHORT_TEXT_LENGTH + 1)
    with pytest.raises(Exception):
        WrongQuestionCreateRequest(question_text="题", knowledge_point=long_kp)
    with pytest.raises(Exception):
        WrongQuestionCreateRequest(
            question_text="题", socratic_question="问" * (MAX_CONTENT_LENGTH + 1)
        )
    with pytest.raises(Exception):
        IncrementErrorRequest(delta=10_000)


def test_triple_length_matches_column_width() -> None:
    from app.schemas.literature import TripleIn

    with pytest.raises(Exception):
        TripleIn(entity="e" * 201, relation="r", target="t")
    assert TripleIn(entity="e" * 200, relation="r", target="t").entity == "e" * 200


def test_tag_limits_reject_too_many_or_too_long() -> None:
    from app.tags import MAX_TAG_LENGTH, MAX_TAGS_PER_DIMENSION, TagValidationError, normalize_tags

    too_many = [f"章节{i}" for i in range(MAX_TAGS_PER_DIMENSION + 1)]
    with pytest.raises(TagValidationError):
        normalize_tags({"chapters": too_many})

    with pytest.raises(TagValidationError):
        normalize_tags({"chapters": ["长" * (MAX_TAG_LENGTH + 1)]})

    # 正常数量仍被接受
    assert len(normalize_tags({"chapters": ["第一章"]})["chapters"]) == 1


def test_exam_max_points_matches_service_cap() -> None:
    from app.schemas.exam_point import ExamPointImportRequest

    with pytest.raises(Exception):
        ExamPointImportRequest(text="考点", max_points=201)
    assert ExamPointImportRequest(text="考点", max_points=200).max_points == 200


# --- M11：注册限流 ---------------------------------------------------------


def test_register_rate_limiter_has_own_budget() -> None:
    from app.ratelimit import get_register_limiter, reset_register_limiter
    from app.config import settings

    limiter = get_register_limiter()
    assert limiter.max_attempts == settings.register_max_per_window
    reset_register_limiter(None)


@requires_db
@pytest.mark.db
def test_register_is_rate_limited(client) -> None:
    """脚本批量刷号会触发大量 bcrypt 哈希，属 CPU 放大攻击。"""
    reset_register_limiter(SlidingWindowLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60))

    statuses = [
        client.post(
            f"{API}/auth/register", json={"username": f"bulk_user_{i}", "password": "secret123"}
        ).status_code
        for i in range(4)
    ]

    assert statuses[:2] == [201, 201], statuses
    assert statuses[2] == 429, f"第 3 次注册应被限流，实际 {statuses}"


# --- M12：配置自检 ---------------------------------------------------------


def test_validate_settings_flags_bad_values() -> None:
    base = Settings()
    base.jwt_algorithm = "none"
    base.jwt_expire_minutes = 0
    base.login_max_attempts = 0
    base.max_multipart_bytes = 1
    base.max_request_body_bytes = 1024

    problems = validate_settings(base)

    assert any("JWT_ALGORITHM" in p for p in problems)
    assert any("JWT_EXPIRE_MINUTES" in p for p in problems)
    assert any("LOGIN_MAX_ATTEMPTS" in p for p in problems)
    assert any("MAX_MULTIPART_BYTES" in p for p in problems)


def test_validate_settings_passes_for_default_config() -> None:
    assert validate_settings(Settings()) == []


# --- M14：向量集合的嵌入签名 -----------------------------------------------


def test_collection_records_embedding_signature() -> None:
    from app.services import kg_service

    kg_service.reset_chroma_client()
    collection = kg_service.literature_collection()
    metadata = dict(collection.metadata or {})

    assert metadata.get("embedding_backend") == "offline-hashing"
    assert metadata.get("embedding_dim") == kg_service.settings.embedding_dim
    assert metadata.get("hnsw:space") == "cosine"


def test_signature_decision_prefers_keeping_legacy_data() -> None:
    """历史集合（无签名）= 一直是同一套嵌入 → 补签名、保留数据，不能清空。"""
    from app.services import kg_service

    signature = kg_service._embedding_signature()

    assert kg_service._signature_action({}, signature) == kg_service.SIGNATURE_ADOPT
    assert (
        kg_service._signature_action({"hnsw:space": "cosine"}, signature)
        == kg_service.SIGNATURE_ADOPT
    )


def test_signature_decision_rebuilds_on_real_mismatch() -> None:
    from app.services import kg_service

    signature = kg_service._embedding_signature()
    other = {"embedding_backend": "sentence-transformers", "embedding_dim": 384}

    assert kg_service._signature_action(other, signature) == kg_service.SIGNATURE_REBUILD
    assert kg_service._signature_action(signature, signature) == kg_service.SIGNATURE_KEEP


def test_collection_records_signature_and_survives_reopen() -> None:
    """回归：自定义嵌入函数未注册时，重开集合会「找不到嵌入函数」。"""
    from app.services import kg_service

    kg_service.reset_chroma_client()
    collection = kg_service.literature_collection()
    collection.upsert(ids=["reopen-1"], documents=["青霉素"], metadatas=[{"pmid": "9"}])

    kg_service.reset_chroma_client()
    reopened = kg_service.literature_collection()

    assert reopened.count() >= 1, "重开客户端后集合应仍可读（嵌入函数必须已注册）"
    assert dict(reopened.metadata or {}).get("embedding_backend") == "offline-hashing"
    reopened.delete(ids=["reopen-1"])


# --- L1 / L18：静态规范 ----------------------------------------------------


def _app_sources():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app"
    return list(root.rglob("*.py"))


def test_no_bare_assert_used_for_business_validation() -> None:
    """assert 在 python -O 下会被移除，不能用来做业务校验。"""
    offenders: list[str] = []
    for path in _app_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s+assert\s+", line) and "noqa" not in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert offenders == [], f"仍有 assert 做校验：{offenders}"


def test_no_legacy_query_api() -> None:
    """SQLAlchemy 2.x 应统一用 select()/delete()，不要混用 1.x 的 db.query。"""
    offenders: list[str] = []
    for path in _app_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "db.query(" in line:
                offenders.append(f"{path.name}:{lineno}")

    assert offenders == [], f"仍在用 db.query：{offenders}"


# --- L4：format_interval 的 None -------------------------------------------


def test_format_interval_accepts_none() -> None:
    assert format_interval(None) == "-"
    assert format_interval(0) == "0 分钟" or "分钟" in format_interval(0)


# --- L17：PATCH 可以显式清空可空字段 --------------------------------------


@requires_db
@pytest.mark.db
def test_patch_can_clear_nullable_field(client, auth_headers) -> None:
    """回归：旧实现 `if value is not None` 让「清空字段」永远做不到。"""
    resp = client.post(
        f"{API}/experiments",
        json={"experiment_type": "合成", "drug_name": "阿司匹林", "procedure": "称量后溶解"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["experiment"]
    assert created["procedure"] == "称量后溶解"

    patched = client.patch(
        f"{API}/experiments/{created['id']}",
        json={"procedure": None},
        headers=auth_headers,
    )

    assert patched.status_code == 200
    assert patched.json()["procedure"] is None, "显式传 null 应清空该字段"


@requires_db
@pytest.mark.db
def test_patch_keeps_untouched_fields(client, auth_headers) -> None:
    resp = client.post(
        f"{API}/experiments",
        json={"experiment_type": "合成", "drug_name": "阿司匹林", "procedure": "称量后溶解"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["experiment"]

    # PATCH 直接返回实验对象（不是 {experiment: ...} 包装）
    patched = client.patch(
        f"{API}/experiments/{created['id']}", json={"drug_name": "青霉素"}, headers=auth_headers
    ).json()

    assert patched["drug_name"] == "青霉素"
    assert patched["procedure"] == "称量后溶解", "未传的字段不应被清空"
