"""
只读 SQL 工具：把上传的 DataFrame 注册成 DuckDB 视图 `data`，执行校验过的 SELECT。

对外接口（executor.run_sql 依赖，勿改签名）：
    validate_sql(sql) -> Optional[str]              # None 表示通过，否则返回原因
    run_sql_tool(df, sql) -> SQLResult              # SQLResult(sql, result_df, row_count, error)

只读性靠两道闸：validate_sql 只放行 SELECT / WITH；DuckDB 连接用后即关。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

_MAX_RESULT_ROWS = 20000

_ALLOWED_STATEMENTS = {"select", "with"}

_FORBIDDEN_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "replace", "merge", "grant", "revoke", "attach", "detach", "copy",
    "export", "import", "install", "load", "pragma", "set", "reset",
    "vacuum", "checkpoint", "call", "begin", "commit", "rollback",
}

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", flags=re.DOTALL)


@dataclass
class SQLResult:
    sql: str
    result_df: Optional[pd.DataFrame] = None
    row_count: int = 0
    error: Optional[str] = field(default=None)


def _strip_comments(sql: str) -> str:
    return _COMMENT_RE.sub(" ", sql or "")


def _blank_literals_and_comments(sql: str) -> str:
    """把字符串字面量与注释替换成空格，只留下可做关键字判断的骨架。

    这样 `WHERE note = 'update'`、`WHERE tag = 'a;b'` 这类合法查询不会被误杀，
    而真正的 `DELETE ...`、多语句仍然会被识别。
    """
    out: list[str] = []
    i = 0
    n = len(sql or "")
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # '' 转义
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def validate_sql(sql: str) -> Optional[str]:
    """返回 None 表示是合法的只读查询；否则返回中文原因。"""
    raw = (sql or "").strip()
    if not raw:
        return "SQL 为空"

    # 先抹掉字面量与注释，再判断语句类型 / 多语句 / 关键字
    cleaned = _blank_literals_and_comments(raw).strip()
    if not cleaned:
        return "SQL 为空"

    # 去掉合法的结尾分号后不允许再有第二条语句
    body = cleaned.rstrip(";").strip()
    if ";" in body:
        return "一次只能执行一条 SQL 语句"

    first = re.match(r"[A-Za-z]+", body)
    if not first or first.group(0).lower() not in _ALLOWED_STATEMENTS:
        return "只允许只读查询（SELECT / WITH）"

    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body.lower()))
    bad = sorted(tokens & _FORBIDDEN_KEYWORDS)
    if bad:
        return f"检测到写操作或危险关键字：{', '.join(bad)}"

    return None


def run_sql_tool(df: pd.DataFrame, sql: str) -> SQLResult:
    """在上传数据的 DuckDB 视图 `data` 上执行只读查询。"""
    error = validate_sql(sql)
    if error:
        return SQLResult(sql=sql, result_df=None, row_count=0, error=error)

    if df is None or len(df) == 0:
        return SQLResult(sql=sql, result_df=None, row_count=0, error="数据集为空，无法查询")

    try:
        import duckdb  # 延迟导入：没装 duckdb 时仍然可以 import 本模块
    except ImportError:
        return SQLResult(
            sql=sql,
            result_df=None,
            row_count=0,
            error="缺少依赖 duckdb，请先执行：pip install duckdb",
        )

    con = None
    try:
        con = duckdb.connect(database=":memory:")
        con.register("data", df)
        result = con.execute(_strip_comments(sql)).fetchdf()
    except Exception as exc:  # noqa: BLE001
        return SQLResult(sql=sql, result_df=None, row_count=0, error=f"{type(exc).__name__}: {exc}")
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass

    if result is None:
        return SQLResult(sql=sql, result_df=None, row_count=0, error="查询没有返回结果集")

    row_count = len(result)
    if row_count > _MAX_RESULT_ROWS:
        result = result.head(_MAX_RESULT_ROWS)

    return SQLResult(sql=sql, result_df=result, row_count=row_count, error=None)
