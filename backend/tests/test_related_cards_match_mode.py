"""锁定「卡片↔知识点关联」三种匹配口径的真实行为。

为什么需要这组测试：`fallback` 很容易被误读成「全量并集」。
它不是 —— 只要某个知识点有标签命中，正文提到该知识点的卡片就不会被关联。
把这条语义用测试钉死，将来有人改动匹配逻辑时会立刻发现行为变了。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import settings
from app.models import Card
from app.services import wrong_question_service as wq
from app.timeutils import utcnow
from tests.conftest import requires_db

KP = "阿司匹林的作用机制"


def _add_card(db_session, user_id: int, front: str, back: str, chapters: list[str]) -> int:
    card = Card(
        user_id=user_id,
        front=front,
        back=back,
        tags={"subjects": [], "chapters": chapters, "point_types": []},
        created_at=utcnow(),
    )
    db_session.add(card)
    db_session.flush()
    return card.id


@pytest.fixture
def two_cards(db_session, user_id):
    """一张打了标签，一张只在正文提到同一个知识点。"""
    tagged = _add_card(db_session, user_id, "无关正文", "无关答案", [KP])
    text_only = _add_card(db_session, user_id, f"{KP} 的临床要点", "答案", ["其他章节"])
    return tagged, text_only


def _related(db_session, user_id: int) -> set[int]:
    return wq.related_card_ids(db_session, user_id, [KP])


# --- 三种口径 ---------------------------------------------------------------


@requires_db
@pytest.mark.db
def test_fallback_mode_omits_text_only_card(db_session, user_id, two_cards, monkeypatch) -> None:
    """**核心语义**：有标签命中时，fallback 不再扫描正文。

    这正是「fallback 不是全量并集」的可执行证据。
    """
    tagged, text_only = two_cards
    monkeypatch.setattr(settings, "related_cards_match_mode", "fallback")

    matched = _related(db_session, user_id)

    assert tagged in matched
    assert text_only not in matched, "有标签命中时不应再把正文命中的卡片算进来"


@requires_db
@pytest.mark.db
def test_fallback_mode_falls_back_when_no_tag_hit(db_session, user_id, monkeypatch) -> None:
    """标签完全没命中时才回落正文扫描 —— 覆盖率由这条保证。"""
    text_only = _add_card(db_session, user_id, f"{KP} 的临床要点", "答案", ["其他章节"])
    monkeypatch.setattr(settings, "related_cards_match_mode", "fallback")

    matched = _related(db_session, user_id)

    assert matched == {text_only}


@requires_db
@pytest.mark.db
def test_never_mode_is_strictly_tag_based(db_session, user_id, two_cards, monkeypatch) -> None:
    tagged, text_only = two_cards
    monkeypatch.setattr(settings, "related_cards_match_mode", "never")

    matched = _related(db_session, user_id)

    assert matched == {tagged}


@requires_db
@pytest.mark.db
def test_always_mode_restores_legacy_union(db_session, user_id, two_cards, monkeypatch) -> None:
    """`always` 才是旧的全量并集（更慢、含假阳性）。"""
    tagged, text_only = two_cards
    monkeypatch.setattr(settings, "related_cards_match_mode", "always")

    matched = _related(db_session, user_id)

    assert matched == {tagged, text_only}


# --- 归属与边界 -------------------------------------------------------------


@requires_db
@pytest.mark.db
def test_bulk_attribution_respects_mode(db_session, user_id, two_cards, monkeypatch) -> None:
    """批量归属与单点查询必须同口径。"""
    tagged, text_only = two_cards
    monkeypatch.setattr(settings, "related_cards_match_mode", "fallback")

    mapping = wq.related_card_ids_bulk(db_session, user_id, [KP])

    assert mapping[KP] == {tagged}


@requires_db
@pytest.mark.db
def test_one_point_falls_back_without_affecting_another(db_session, user_id, monkeypatch) -> None:
    """回落是**按知识点**判断的：A 有标签命中不回落，B 没有命中才回落。"""
    other_kp = "青霉素的抗菌机制"
    a_tagged = _add_card(db_session, user_id, "无关", "无关", [KP])
    a_text = _add_card(db_session, user_id, f"{KP} 正文", "答案", ["其他章节"])
    b_text = _add_card(db_session, user_id, f"{other_kp} 正文", "答案", ["其他章节"])
    monkeypatch.setattr(settings, "related_cards_match_mode", "fallback")

    mapping = wq.related_card_ids_bulk(db_session, user_id, [KP, other_kp])

    assert mapping[KP] == {a_tagged}, "A 有标签命中，不应回落"
    assert mapping[other_kp] == {b_text}, "B 无标签命中，应回落"
    assert a_text not in mapping[KP]


@requires_db
@pytest.mark.db
def test_invalid_mode_falls_back_to_default(db_session, user_id, two_cards, monkeypatch) -> None:
    """配置写错时退回 fallback，而不是抛异常或静默变成 always。"""
    tagged, _text_only = two_cards
    monkeypatch.setattr(settings, "related_cards_match_mode", "nonsense")

    assert _related(db_session, user_id) == {tagged}


@requires_db
@pytest.mark.db
def test_mode_is_read_per_call(db_session, user_id, two_cards, monkeypatch) -> None:
    """配置变更无需重启即可生效（服务在调用时读取，而非导入时固化）。"""
    tagged, text_only = two_cards

    monkeypatch.setattr(settings, "related_cards_match_mode", "never")
    assert _related(db_session, user_id) == {tagged}

    monkeypatch.setattr(settings, "related_cards_match_mode", "always")
    assert _related(db_session, user_id) == {tagged, text_only}


# --- 索引确实存在（H6 的物理前提） -----------------------------------------


@requires_db
@pytest.mark.db
def test_multivalued_index_exists() -> None:
    """`never`/`fallback` 的精确路径依赖这个索引；索引丢了就会退化成全表扫描。"""
    from app.database import get_engine

    with get_engine().connect() as conn:
        rows = conn.execute(text("SHOW INDEX FROM cards")).all()

    names = {row[2] for row in rows}
    assert "ix_cards_chapters_mv" in names, "多值索引缺失，精确匹配会退化为全表扫描"
