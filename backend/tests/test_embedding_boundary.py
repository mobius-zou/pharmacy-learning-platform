"""C2：两套向量空间的边界必须显式且不可越界。

本仓库**刻意保留**两套嵌入：
  · 文献库用离线哈希（稳定、可离线）；
  · 考点匹配优先 sentence-transformers（语义更准）。

代价是「同一文本在两处得到的向量不可比较」。这里把边界钉死，
防止将来有人顺手把 ST 接进文献库、或在考点匹配里误查文献集合。
"""

from __future__ import annotations

import pytest

from app import embeddings
from app.config import settings
from app.services import exam_match_service, kg_service


# --- 常量与命名 -------------------------------------------------------------


def test_space_ids_are_distinct() -> None:
    assert embeddings.SPACE_LITERATURE != embeddings.SPACE_EXAM_MATCH


def test_literature_never_allows_sentence_transformers() -> None:
    """文献空间只允许哈希实现 —— 这是「离线可复现」的前提。"""
    assert embeddings.LITERATURE_ALLOWED_BACKENDS == {embeddings.HASHING_BACKEND_ID}
    assert "sentence-transformers" not in embeddings.LITERATURE_ALLOWED_BACKENDS


def test_exam_match_may_degrade_to_hashing() -> None:
    """考点匹配允许降级，因此两条路径都合法。"""
    assert "sentence-transformers" in embeddings.EXAM_MATCH_ALLOWED_BACKENDS
    assert embeddings.HashingBackend.name in embeddings.EXAM_MATCH_ALLOWED_BACKENDS


# --- 文献集合的固定签名 -----------------------------------------------------


def test_literature_signature_is_stable() -> None:
    signature = kg_service._embedding_signature()

    assert signature["embedding_backend"] == embeddings.HASHING_BACKEND_ID
    assert signature["embedding_dim"] == settings.embedding_dim
    assert signature["embedding_space"] == embeddings.SPACE_LITERATURE
    assert signature["embedding_backend"] in embeddings.LITERATURE_ALLOWED_BACKENDS


def test_literature_signature_ignores_sentence_transformers(monkeypatch) -> None:
    """即使 ST 可用，文献库的签名也不应改变（两套空间彼此独立）。"""
    class _FakeST:
        name = "sentence-transformers"
        model_name = "paraphrase-multilingual-MiniLM-L12-v2"
        dimension = 384
        is_semantic = True

        def encode(self, texts):  # noqa: ANN001
            return [[0.0] * 384 for _ in texts]

    monkeypatch.setattr(embeddings, "resolve_backend", lambda *a, **k: _FakeST())

    assert kg_service._embedding_signature()["embedding_backend"] == embeddings.HASHING_BACKEND_ID


def test_chroma_collection_records_the_space() -> None:
    kg_service.reset_chroma_client()
    metadata = dict(kg_service.literature_collection().metadata or {})

    assert metadata["embedding_space"] == embeddings.SPACE_LITERATURE
    assert metadata["embedding_dim"] == settings.embedding_dim


# --- 文献检索路径不得被 ST 接管 --------------------------------------------


def test_literature_vector_search_does_not_use_resolve_backend(monkeypatch) -> None:
    """文献向量检索必须走离线哈希；一旦它调用 resolve_backend，
    两套空间就会被悄悄合并。"""
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("文献检索不应调用 resolve_backend()")

    monkeypatch.setattr(embeddings, "resolve_backend", boom)
    kg_service.reset_chroma_client()

    collection = kg_service.literature_collection()
    collection.upsert(
        ids=["boundary-1"], documents=["阿司匹林抑制环氧化酶"], metadatas=[{"pmid": "1"}]
    )
    hits = collection.query(query_texts=["阿司匹林"], n_results=1)

    assert hits["ids"][0], "向量检索应可用"
    collection.delete(ids=["boundary-1"])


# --- 维度差异是可观测事实（不是 bug） --------------------------------------


def test_exam_match_dimension_differs_from_literature() -> None:
    """ST 是 384 维、离线哈希是 settings.embedding_dim（默认 256）。

    尺寸不同正是「不可混用」的直接体现；如果哪天相等了，说明有人改了配置，
    需要重新确认边界结论。
    """
    from app.embeddings import HashingBackend

    hashing_dim = len(HashingBackend().encode(["阿司匹林"])[0])
    assert hashing_dim == settings.embedding_dim

    backend = embeddings.resolve_backend()
    if getattr(backend, "is_semantic", False):
        semantic_dim = len(backend.encode(["阿司匹林"])[0])
        assert semantic_dim != hashing_dim, "语义后端维度不应与哈希相同"
    else:
        pytest.skip("当前环境未启用语义后端，无法比较维度")


def test_exam_match_uses_its_own_backend() -> None:
    """考点匹配解析出的后端必须属于考点空间允许集合。"""
    backend = embeddings.resolve_backend()

    assert backend.name in embeddings.EXAM_MATCH_ALLOWED_BACKENDS


# --- 混用会被结构挡住 ------------------------------------------------------


def test_copying_semantic_vector_into_literature_collection_fails() -> None:
    """把 384 维向量塞进 256 维集合，Chroma 必须报错（物理不可混存）。"""
    kg_service.reset_chroma_client()
    collection = kg_service.literature_collection()
    wrong_dim = [0.1] * (settings.embedding_dim + 128)

    with pytest.raises(Exception):
        collection.upsert(ids=["dim-mismatch"], embeddings=[wrong_dim], documents=["x"])


def test_exam_match_does_not_touch_literature_collection() -> None:
    """考点匹配不应读写文献集合。"""
    source = __import__("inspect").getsource(exam_match_service)

    assert "literature_collection" not in source
    assert "draft_collection" not in source
