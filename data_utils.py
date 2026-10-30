"""
数据读取与数据画像。

对外接口（app / agent 依赖，勿改签名）：
    load_file(filename, file_bytes) -> DataFrame
    profile_data(df) -> dict
    profile_to_prompt_text(profile) -> str
"""

from __future__ import annotations

import io
from typing import Any, Optional

import pandas as pd


class DataLoadError(RuntimeError):
    """文件无法读取 / 数据不合法。"""


_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1")
_SUPPORTED_EXT = {"csv", "txt", "tsv", "xlsx", "xls", "xlsm"}
_MAX_PROMPT_COLUMNS = 40


def load_file(filename: str, file_bytes: bytes) -> pd.DataFrame:
    """把上传的 CSV / Excel 读成 DataFrame，并做最基本的数据校验。"""
    name = (filename or "").strip()
    if not file_bytes:
        raise DataLoadError("文件内容为空，请重新上传。")

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _SUPPORTED_EXT:
        raise DataLoadError(f"暂不支持的文件类型：.{ext or '未知'}（支持 CSV / Excel）。")

    if ext in {"xlsx", "xls", "xlsm"}:
        df = _read_excel(name, file_bytes)
    else:
        df = _read_csv(name, file_bytes, ext)

    if df is None or df.empty:
        raise DataLoadError("文件中没有可用数据（0 行或 0 列）。")

    df = _normalize(df)
    return df


def _read_excel(name: str, file_bytes: bytes) -> pd.DataFrame:
    try:
        return pd.read_excel(io.BytesIO(file_bytes))
    except ImportError as exc:  # 缺 openpyxl / xlrd
        raise DataLoadError(f"读取 Excel 需要额外依赖：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise DataLoadError(f"Excel 读取失败：{exc}") from exc


def _read_csv(name: str, file_bytes: bytes, ext: str) -> pd.DataFrame:
    sep_candidates = ["\t", ","] if ext == "tsv" else [None, ",", ";", "\t"]
    last_error: Optional[Exception] = None

    for encoding in _CSV_ENCODINGS:
        try:
            text = file_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
            continue

        for sep in sep_candidates:
            try:
                buffer = io.StringIO(text)
                df = pd.read_csv(
                    buffer,
                    sep=sep,
                    engine="python" if sep is None else "c",
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            if df is not None and not df.empty:
                return df

    raise DataLoadError(f"CSV 读取失败：{last_error}")


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    # 去掉整行全空的行
    df = df.dropna(how="all")
    # 全空的无名列剔除
    keep = [c for c in df.columns if not c.lower().startswith("unnamed:")]
    if keep:
        df = df[keep]
    df.reset_index(drop=True, inplace=True)
    return df


# 数据画像
def profile_data(df: pd.DataFrame) -> dict[str, Any]:
    """生成数据画像：形状、类型、缺失、唯一值、数值/类别摘要、样例。"""
    rows, cols = int(df.shape[0]), int(df.shape[1])

    dtypes = {str(c): str(t) for c, t in df.dtypes.items()}
    missing = {str(c): int(df[c].isna().sum()) for c in df.columns}
    missing_pct = {
        str(c): round(float(df[c].isna().mean() * 100), 2) if rows else 0.0 for c in df.columns
    }
    unique = {str(c): int(df[c].nunique(dropna=True)) for c in df.columns}

    duplicates = int(df.duplicated().sum())

    numeric_cols = [str(c) for c in df.select_dtypes(include="number").columns]
    numeric_summary: dict[str, dict[str, float]] = {}
    for col in numeric_cols[:20]:
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            continue
        numeric_summary[col] = {
            "mean": round(float(series.mean()), 4),
            "std": round(float(series.std()), 4) if len(series) > 1 else 0.0,
            "min": round(float(series.min()), 4),
            "p50": round(float(series.median()), 4),
            "max": round(float(series.max()), 4),
        }

    categorical_cols = [
        str(c) for c in df.select_dtypes(include=["object", "category", "bool"]).columns
    ]
    categorical_summary: dict[str, list[tuple[str, int]]] = {}
    for col in categorical_cols[:20]:
        counts = df[col].astype(str).value_counts(dropna=True).head(5)
        categorical_summary[col] = [(str(k), int(v)) for k, v in counts.items()]

    sample = df.head(5).astype(str).to_dict(orient="records")

    return {
        "rows": rows,
        "columns": cols,
        "dtypes": dtypes,
        "missing": missing,
        "missing_pct": missing_pct,
        "unique": unique,
        "duplicates": duplicates,
        "numeric_columns": numeric_cols,
        "categorical_columns": categorical_cols,
        "numeric_summary": numeric_summary,
        "categorical_summary": categorical_summary,
        "sample": sample,
    }


def profile_to_prompt_text(profile: dict[str, Any]) -> str:
    """把画像压成一段给模型看的纯文本（控制长度，避免烧 token）。"""
    if not isinstance(profile, dict):
        return "(无数据画像)"

    lines: list[str] = []
    lines.append(f"行数: {profile.get('rows', '?')}，列数: {profile.get('columns', '?')}")
    lines.append(f"完全重复行数: {profile.get('duplicates', '?')}")

    dtypes: dict[str, str] = profile.get("dtypes") or {}
    missing: dict[str, int] = profile.get("missing") or {}
    unique: dict[str, int] = profile.get("unique") or {}

    lines.append("字段（名称 | 类型 | 缺失数 | 唯一值数）:")
    for i, (col, dtype) in enumerate(dtypes.items()):
        if i >= _MAX_PROMPT_COLUMNS:
            lines.append(f"...（其余 {len(dtypes) - _MAX_PROMPT_COLUMNS} 列省略）")
            break
        lines.append(f"- {col} | {dtype} | 缺失 {missing.get(col, 0)} | 唯一 {unique.get(col, '?')}")

    numeric_summary: dict[str, dict[str, float]] = profile.get("numeric_summary") or {}
    if numeric_summary:
        lines.append("数值字段摘要:")
        for col, stats in list(numeric_summary.items())[:15]:
            lines.append(
                f"- {col}: mean={stats['mean']}, std={stats['std']}, "
                f"min={stats['min']}, p50={stats['p50']}, max={stats['max']}"
            )

    categorical_summary: dict[str, list[tuple[str, int]]] = profile.get("categorical_summary") or {}
    if categorical_summary:
        lines.append("类别字段高频取值:")
        for col, items in list(categorical_summary.items())[:15]:
            pairs = ", ".join(f"{k}({v})" for k, v in items)
            lines.append(f"- {col}: {pairs}")

    sample = profile.get("sample") or []
    if sample:
        lines.append("前几行样例:")
        for row in sample[:3]:
            lines.append(f"- {row}")

    text = "\n".join(lines)
    return text[:6000] if len(text) > 6000 else text
