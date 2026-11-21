"""模型公共定义。"""

from __future__ import annotations

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql

# 所有表统一 utf8mb4 / InnoDB
MYSQL_TABLE_ARGS = {
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
    "mysql_engine": "InnoDB",
}

# MySQL 的 DATETIME 默认只有「秒」精度，且会四舍五入（.887825 会变成下一秒）。
# 这会让落库后的 fsrs_due 与调度计算结果不一致，也会让 due/last_review 的
# 精确比较失效，因此显式使用 DATETIME(6) 保留微秒。
UTCDateTime = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
