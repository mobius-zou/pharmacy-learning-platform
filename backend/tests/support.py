"""测试辅助：SQL 计数等共享工具。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event


@contextmanager
def count_sql(engine) -> Iterator[dict[str, int]]:
    """统计上下文内执行的 SQL 语句条数。"""
    counter = {"count": 0, "select": 0, "insert": 0}

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter["count"] += 1
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if head == "SELECT":
            counter["select"] += 1
        elif head in {"INSERT", "UPDATE", "DELETE"}:
            counter["insert"] += 1

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
