"""
执行层：运行模型生成的 Python / SQL，并把结构化结果交回给 agent。

    executor.run_python(code, df)  -> ToolResult   # pandas / numpy / sklearn / matplotlib
    executor.run_sql(code, df)     -> ToolResult   # 只读 DuckDB SELECT（复用 tools.run_sql_tool）

执行模型的边界：
  * run_python 在进程内 exec，使用白名单 import、受限 builtins（禁用 open / eval / exec /
    compile / input 等）以及单线程超时。因此只适合本人或可信范围内使用：理论上仍可通过
    函数对象的 __globals__ 绕过限制，超时后线程也无法强制终止。
  * 面向不可信用户时，应改为子进程或容器隔离。
  * run_sql 的只读性由 tools.validate_sql 保证（只允许 SELECT / WITH）。
"""

from __future__ import annotations

import builtins
import concurrent.futures
import contextlib
import io
import json
import traceback
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from tools import run_sql_tool

_MAX_DISPLAY_ROWS = 50
_SCALAR_LIMIT = 3000
_PYTHON_TIMEOUT = 25
_SUMMARY_LIMIT = 3000

_ALLOWED_IMPORT_ROOTS = {
    "pandas", "numpy", "sklearn", "statsmodels", "scipy", "matplotlib",
    "math", "statistics", "itertools", "functools", "collections",
    "json", "re", "datetime", "random", "typing", "warnings", "textwrap",
}

_BLOCKED_BUILTINS = {
    "open", "eval", "exec", "compile", "input", "breakpoint", "exit", "quit",
    "help", "globals", "locals", "vars", "memoryview", "__import__",
}

_real_import = builtins.__import__


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """只放行白名单顶层模块，其余导入一律拒绝。"""
    root = (name or "").split(".")[0]
    if root not in _ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"沙箱禁止导入模块: {name}")
    return _real_import(name, globals, locals, fromlist, level)


_SAFE_BUILTINS = {
    k: v
    for k, v in vars(builtins).items()
    if not k.startswith("_") and k not in _BLOCKED_BUILTINS
}
_SAFE_BUILTINS["__import__"] = _safe_import
# 类定义需要它；不放开的话，模型写 "class Foo:" 会直接 NameError
_SAFE_BUILTINS["__build_class__"] = builtins.__build_class__

# 优先使用的中文字体（按平台常见程度排序），避免图表里的中文变成方块
_CJK_FONT_CANDIDATES = (
    "Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC", "Source Han Sans SC",
    "Source Han Sans CN", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
    "PingFang SC", "Heiti SC", "Arial Unicode MS",
)


def _configure_matplotlib_font() -> Optional[str]:
    """给 matplotlib 配置一个系统里存在的中文字体；找不到就保持默认。"""
    try:
        import matplotlib
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in _CJK_FONT_CANDIDATES:
            if name in available:
                existing = list(matplotlib.rcParams.get("font.sans-serif", []))
                matplotlib.rcParams["font.sans-serif"] = [name] + [f for f in existing if f != name]
                matplotlib.rcParams["axes.unicode_minus"] = False
                return name
    except Exception:  # noqa: BLE001 - 字体配置失败不影响执行
        return None
    return None


@dataclass
class ToolResult:
    tool: str
    ok: bool
    summary: str
    result_df: Optional[pd.DataFrame] = None
    figure_png: Optional[bytes] = None
    error: Optional[str] = None
    rows: int = 0


# Python 工具
def run_python(code: str, df: pd.DataFrame, timeout: int = _PYTHON_TIMEOUT) -> ToolResult:
    result = ToolResult(tool="python", ok=True, summary="")

    if not code or not code.strip():
        result.ok = False
        result.error = "空代码"
        result.summary = result.error
        return result

    plt = None
    try:
        import matplotlib

        try:
            matplotlib.use("Agg", force=False)
        except Exception:  # noqa: BLE001
            pass
        import matplotlib.pyplot as plt_mod

        plt = plt_mod
        _configure_matplotlib_font()
    except Exception:  # noqa: BLE001 - 没装 matplotlib 时只是不能画图
        plt = None

    try:
        import numpy as np
    except Exception as exc:  # pragma: no cover - numpy 是硬依赖
        result.ok = False
        result.error = f"缺少依赖 numpy: {exc}"
        result.summary = result.error
        return result

    ns: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "__name__": "__agent_python__",
        "df": df.copy(),
        "pd": pd,
        "np": np,
    }
    if plt is not None:
        ns["plt"] = plt
        ns["matplotlib"] = plt

    stdout_buf = io.StringIO()
    holder: dict[str, Any] = {}

    def _run() -> None:
        try:
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stdout_buf):
                exec(compile(code, "<agent_python>", "exec"), ns)
            holder["ok"] = True
        except Exception as exc:  # noqa: BLE001
            holder["ok"] = False
            holder["error"] = f"{type(exc).__name__}: {exc}"
            holder["traceback"] = traceback.format_exc()

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_run)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            holder["ok"] = False
            holder["error"] = f"Python 执行超时（>{timeout}s），已中断。"
    finally:
        pool.shutdown(wait=False)

    out_text = stdout_buf.getvalue()

    if not holder.get("ok"):
        result.ok = False
        result.error = holder.get("error") or "执行失败"
        tb = holder.get("traceback", "")
        merged = (out_text + "\n" + tb).strip()
        result.summary = _truncate(merged, _SUMMARY_LIMIT) or result.error
        return result

    parts: list[str] = []
    if out_text.strip():
        parts.append(_truncate(out_text.strip(), _SCALAR_LIMIT))

    if "result" in ns:
        try:
            repr_text, df_out = _serialize(ns["result"])
            if repr_text:
                parts.append("result =\n" + repr_text if isinstance(df_out, pd.DataFrame) else "result = " + repr_text)
            if df_out is not None and not df_out.empty:
                result.result_df = df_out
                result.rows = len(df_out)
        except Exception as exc:  # noqa: BLE001
            parts.append(f"(无法序列化 result: {exc})")

    result.figure_png = _capture_figure()
    if result.figure_png:
        parts.append("[已生成图表]")

    result.summary = "\n\n".join(parts).strip() or "(执行成功，无输出/无 result)"
    return result


# SQL 工具（只读）
def run_sql(code: str, df: pd.DataFrame) -> ToolResult:
    result = ToolResult(tool="sql", ok=True, summary="")

    if not code or not code.strip():
        result.ok = False
        result.error = "空 SQL"
        result.summary = result.error
        return result

    res = run_sql_tool(df, code)  # tools.SQLResult(sql, result_df, row_count, error)

    if res.error:
        result.ok = False
        result.error = res.error
        result.summary = f"SQL 错误: {res.error}"
        return result

    result.result_df = res.result_df
    result.rows = int(res.row_count or 0)

    if res.result_df is None or res.result_df.empty:
        result.summary = "SQL 返回 0 行。"
    else:
        result.summary = (
            f"SQL 返回 {result.rows} 行，前 {_MAX_DISPLAY_ROWS} 行：\n"
            + res.result_df.head(_MAX_DISPLAY_ROWS).to_string(index=False)
        )
    return result


# 辅助函数
def _serialize(value: Any) -> tuple[str, Optional[pd.DataFrame]]:
    if isinstance(value, pd.DataFrame):
        display = value.head(_MAX_DISPLAY_ROWS)
        # 行索引若是默认 RangeIndex 就丢掉；若是分组键 / 特征名等有意义索引则保留，
        # 否则模型只能看到一串没有名字的数字（例如相关系数表）。
        default_index = isinstance(display.index, pd.RangeIndex) and display.index.name is None
        return display.to_string(index=not default_index), display.copy()
    if isinstance(value, pd.Series):
        head = value.head(_MAX_DISPLAY_ROWS)
        return head.to_string(), head.to_frame()
    if isinstance(value, (dict, list, tuple, set)):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return _truncate(text, _SCALAR_LIMIT), None
    return str(value), None


def _capture_figure() -> Optional[bytes]:
    try:
        import matplotlib.pyplot as plt

        nums = plt.get_fignums()
        if not nums:
            return None
        fig = plt.figure(nums[-1])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        buf.seek(0)
        data = buf.getvalue()
        plt.close("all")
        return data
    except Exception:  # noqa: BLE001
        return None


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]
