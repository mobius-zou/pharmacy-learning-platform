"""阶段五索引方案基准（H6）——先量测，再决定是否加生成列/多值索引。

用法（在 backend 下）：
    ./venv/bin/python scripts/bench_exam_match_index.py --cards 20000

脚本会：
1. 在**独立基准库**（``<dbname>_bench``）里建表、灌数据，绝不碰业务库；
2. 依次测量三种访问方式下 ``related_card_ids`` 的耗时与 EXPLAIN：
   - baseline：现有 ``JSON_CONTAINS`` + ``CAST(... AS CHAR) ILIKE`` + 正文 ILIKE
   - generated：生成列 ``chapters_text``（STORED）+ 普通索引，查询改用 LIKE
   - multivalue：多值索引 + ``MEMBER OF``（精确成员匹配）
3. 打印对比表并清理基准库。

结论以实测为准：如果多值索引只在「精确匹配」下有效，而业务需要模糊匹配，
那就应该记录「不引入该 schema 变更」而不强行为之。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def _bench_url() -> str:
    """由当前配置派生基准库地址（独立库名，避免污染业务数据）。"""
    from sqlalchemy.engine import make_url

    from app.config import settings

    # 数据库账号只被授权到 <dbname>_test，因此基准库复用测试库；
    # 跑完会清空全部表，不影响后续测试（conftest 会用迁移重建 schema）。
    url = make_url(settings.database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _prepare_schema(bench_url: str) -> None:
    """用 Alembic 在基准库建表（与生产同一条建表路径）。"""
    url = urllib.parse.makeurl(bench_url) if hasattr(urllib.parse, "makeurl") else None
    del url
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    parsed = make_url(bench_url)
    server = parsed.set(database=None)
    engine = create_engine(server, connect_args={"charset": "utf8mb4"})
    with engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{parsed.database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
        conn.commit()
    engine.dispose()

    # 测试库可能被上一轮 teardown 清表却保留 alembic_version 标记，
    # 那样 `upgrade head` 会空转。先清掉标记与应用表，确保真建表。
    from app import models  # noqa: F401  —— 必须先导入模型，metadata 才有表
    from app.database import Base

    engine = create_engine(parsed, connect_args={"charset": "utf8mb4"})
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()

    env = dict(os.environ)
    env["DATABASE_URL"] = bench_url
    subprocess.run(
        [str(BACKEND_DIR / "venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        check=True,
        capture_output=True,
    )


def _drop_schema(bench_url: str) -> None:
    """清空基准数据（保留库本身，避免影响测试库的可复用性）。"""
    from sqlalchemy import create_engine, text

    from app import models  # noqa: F401  —— 必须先导入模型，metadata 才有表
    from app.database import Base

    engine = create_engine(bench_url, connect_args={"charset": "utf8mb4"})
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()


def _seed(engine, user_id: int, total: int, target_kp: str) -> dict[str, int]:
    """灌数据：一部分卡片在 chapters 标签里带目标知识点，一部分不带。"""
    from sqlalchemy import text

    tagged = max(1, total // 10)
    rows = []
    for index in range(total):
        if index < tagged:
            tags = {"subjects": ["药理学"], "chapters": [target_kp], "point_types": []}
            front = f"卡片 {index}：与该知识点相关的内容"
        else:
            tags = {"subjects": ["药剂学"], "chapters": [f"其他章节{index % 500}"], "point_types": []}
            front = f"卡片 {index}：无关内容"
        rows.append(
            {
                "u": user_id,
                "f": front,
                "b": "答案",
                "t": json.dumps(tags, ensure_ascii=False),
            }
        )
        if len(rows) >= 2000:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO cards (user_id, front, back, tags, created_at, fsrs_state, fsrs_step) "
                        "VALUES (:u, :f, :b, :t, NOW(6), 1, 0)"
                    ),
                    rows,
                )
            rows = []
    if rows:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO cards (user_id, front, back, tags, created_at, fsrs_state, fsrs_step) "
                    "VALUES (:u, :f, :b, :t, NOW(6), 1, 0)"
                ),
                rows,
            )
    return {"total": total, "tagged": tagged}


def _baseline_sql(limit_kp: str) -> tuple[str, dict]:
    """与 related_card_ids_bulk 等价的 SQL（单知识点）。"""
    sql = (
        "SELECT id FROM cards WHERE user_id = :u AND ("
        " front LIKE :p OR back LIKE :p"
        " OR JSON_CONTAINS(tags->'$.chapters', :kpjson)"
        " OR CAST(tags->'$.chapters' AS CHAR) LIKE :p"
        ")"
    )
    return sql, {"u": 1, "p": f"%{limit_kp}%", "kpjson": json.dumps(limit_kp, ensure_ascii=False)}


def _time_query(engine, sql: str, params: dict, repeats: int = 5) -> float:
    from sqlalchemy import text

    samples = []
    with engine.connect() as conn:
        conn.execute(text(sql), params).all()  # 预热
        for _ in range(repeats):
            started = time.perf_counter()
            conn.execute(text(sql), params).all()
            samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def _explain(engine, sql: str, params: dict) -> list[str]:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text("EXPLAIN " + sql), params).all()
    lines = []
    for row in rows:
        # EXPLAIN 列序：id, select_type, table, partitions, type, possible_keys,
        #              key, key_len, ref, rows, filtered, Extra
        values = list(row)
        table = values[2] or "-"
        access_type = values[4] or "-"        # type：ALL / index / range / ref ...
        used_key = values[6] or "NULL"        # 实际使用的索引
        scan_rows = values[9] if values[9] is not None else "-"
        extra = str(values[11] or "")[:60]
        lines.append(
            f"{str(table):<10} type={str(access_type):<8} key={str(used_key):<28} "
            f"rows={str(scan_rows):<8} {extra}"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards", type=int, default=20000)
    args = parser.parse_args()

    target_kp = "阿司匹林的作用机制"
    bench_url = _bench_url()
    print("=" * 78)
    print(f"基准库: {bench_url.split('@')[-1]}")
    print(f"卡片数: {args.cards}（其中约 10% 带目标知识点标签）")
    print("=" * 78)

    _prepare_schema(bench_url)
    os.environ["DATABASE_URL"] = bench_url

    # 配置缓存需重新读取
    from app.config import get_settings

    get_settings.cache_clear()

    from sqlalchemy import create_engine, text

    from app.database import Base

    engine = create_engine(bench_url, connect_args={"charset": "utf8mb4"})
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cards"))
        conn.execute(text("DELETE FROM users"))
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, token_version, created_at) "
                "VALUES ('bench', 'x', 0, NOW(6))"
            )
        )
        user_id = conn.execute(text("SELECT id FROM users WHERE username='bench'")).scalar()

    started = time.perf_counter()
    counts = _seed(engine, user_id, args.cards, target_kp)
    seed_seconds = time.perf_counter() - started
    print(f"灌数据耗时: {seed_seconds:.1f}s，共 {counts['total']} 张（标签命中 {counts['tagged']}）")

    sql, params = _baseline_sql(target_kp)
    params["u"] = user_id

    print("\n--- baseline：JSON_CONTAINS + CAST(...) LIKE + 正文 LIKE ---")
    baseline_ms = _time_query(engine, sql, params)
    for line in _explain(engine, sql, params):
        print("  " + line)
    print(f"  中位耗时: {baseline_ms:.1f} ms")

    # --- 方案 A：生成列 + 普通索引 ---
    print("\n--- 方案 A：生成列 chapters_text(STORED) + 索引，查询用 LIKE ---")
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE cards ADD COLUMN chapters_text VARCHAR(1000) "
                "GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(tags, '$.chapters'))) STORED"
            )
        )
        conn.execute(text("CREATE INDEX ix_cards_chapters_text ON cards (chapters_text(191))"))
    sql_a = (
        "SELECT id FROM cards WHERE user_id = :u AND ("
        " front LIKE :p OR back LIKE :p OR chapters_text LIKE :p)"
    )
    try:
        a_ms = _time_query(engine, sql_a, params)
        for line in _explain(engine, sql_a, params):
            print("  " + line)
        print(f"  中位耗时: {a_ms:.1f} ms  (相对 baseline {baseline_ms / a_ms:.2f}×)")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_cards_chapters_text ON cards"))
            conn.execute(text("ALTER TABLE cards DROP COLUMN chapters_text"))
    a_ms = locals().get("a_ms", float("nan"))

    # --- 方案 B：多值索引 + MEMBER OF（精确成员匹配）---
    print("\n--- 方案 B：多值索引 + MEMBER OF（精确匹配标签）---")
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE cards ADD INDEX ix_cards_chapters_mv "
                "((CAST(tags->'$.chapters' AS CHAR(200) ARRAY)))"
            )
        )
    sql_b = "SELECT id FROM cards WHERE user_id = :u AND :kp MEMBER OF (tags->'$.chapters')"
    try:
        b_ms = _time_query(engine, sql_b, {**params, "kp": target_kp})
        for line in _explain(engine, sql_b, {**params, "kp": target_kp}):
            print("  " + line)
        print(f"  中位耗时: {b_ms:.1f} ms  (相对 baseline {baseline_ms / b_ms:.2f}×)")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_cards_chapters_mv ON cards"))
    b_ms = locals().get("b_ms", float("nan"))

    # --- 方案 C：多值索引 + 保留正文 LIKE（新默认候选）---
    print("\n--- 方案 C：多值索引 + 正文 LIKE（保留文本匹配，去掉 JSON CAST）---")
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE cards ADD INDEX ix_cards_chapters_mv "
                "((CAST(tags->'$.chapters' AS CHAR(200) ARRAY)))"
            )
        )
    sql_c = (
        "SELECT id FROM cards WHERE user_id = :u AND ("
        " front LIKE :p OR back LIKE :p OR :kp MEMBER OF (tags->'$.chapters'))"
    )
    try:
        c_ms = _time_query(engine, sql_c, {**params, "kp": target_kp})
        for line in _explain(engine, sql_c, {**params, "kp": target_kp}):
            print("  " + line)
        print(f"  中位耗时: {c_ms:.1f} ms  (相对 baseline {baseline_ms / c_ms:.2f}×)")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_cards_chapters_mv ON cards"))
    c_ms = locals().get("c_ms", float("nan"))

    print("\n" + "=" * 78)
    print(f"{'方案':<34}{'中位耗时(ms)':>14}{'相对 baseline':>16}")
    print("-" * 78)
    print(f"{'baseline（现状）':<34}{baseline_ms:>14.1f}{'1.00×':>16}")
    print(f"{'A 生成列 + LIKE':<34}{a_ms:>14.1f}{baseline_ms / a_ms:>15.2f}×")
    print(f"{'B 多值索引 + MEMBER OF（精确）':<34}{b_ms:>14.1f}{baseline_ms / b_ms:>15.2f}×")
    print(f"{'C 多值索引 + 正文 LIKE（新默认）':<34}{c_ms:>14.1f}{baseline_ms / c_ms:>15.2f}×")
    print("=" * 78)

    engine.dispose()
    _drop_schema(bench_url)
    print("基准数据已清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
