"""
一键 PDF 报告。

对外接口（app 依赖，勿改签名）：
    generate_pdf_report(df, dataset_name="Uploaded Dataset", analyses=None) -> bytes

`analyses` 为 None 或空列表时，只输出数据描述部分；
传入分析记录时，末尾追加「分析结果」章节（问题 / 回答 / 结果表 / 图表）。

回答文本是 Markdown，这里会把它转成真正的排版：标题、粗体、列表、表格，
避免把 "##"、"|"、"**" 原样印到纸上。

报告使用 reportlab 内置的中文字体 STSong-Light（UnicodeCIDFont），无需额外字体文件。
"""

from __future__ import annotations

import io
import re
from typing import Any, Optional

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_CONTENT_WIDTH = 6.6 * inch

_MAX_TABLE_ROWS = 10
_MAX_TABLE_COLS = 8
_MAX_ANSWER_CHARS = 4000          # 回答太长会让报告臃肿，截断但要留提示
_MAX_ANSWER_TABLE_COLS = 7        # 回答里的 Markdown 表格最多渲染几列
_MAX_ANSWER_TABLE_ROWS = 12       # 最多渲染几行
_CORR_MAX_COLS = 9                # 相关矩阵最多 9 列（含字段名列）
_DISCRETE_MAX_UNIQUE = 12         # 唯一值不超过这个数的数值列，按离散分布展示

_FIG_WIDTH = 6.6 * inch
_FIG_MAX_HEIGHT = 4.2 * inch


def _register_font() -> str:
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception:  # noqa: BLE001 - 极端情况下退回西文字体
        return "Helvetica"


_FONT = _register_font()


def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "RTitle",
            parent=base["Title"],
            fontName=_FONT,
            fontSize=20,
            leading=26,
            alignment=TA_CENTER,
            spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "RSubtitle",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=10,
            leading=14,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#666666"),
            spaceAfter=14,
        ),
        "section": ParagraphStyle(
            "RSection",
            parent=base["Heading2"],
            fontName=_FONT,
            fontSize=14,
            leading=20,
            spaceBefore=14,
            spaceAfter=8,
            textColor=colors.HexColor("#1F3864"),
        ),
        "h2": ParagraphStyle(
            "RH2",
            parent=base["Heading3"],
            fontName=_FONT,
            fontSize=11.5,
            leading=16,
            spaceBefore=10,
            spaceAfter=4,
            textColor=colors.HexColor("#1F3864"),
        ),
        "h3": ParagraphStyle(
            "RH3",
            parent=base["Heading4"],
            fontName=_FONT,
            fontSize=10.5,
            leading=15,
            spaceBefore=8,
            spaceAfter=3,
            textColor=colors.HexColor("#2E4A7D"),
        ),
        "body": ParagraphStyle(
            "RBody",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=9.5,
            leading=15,
            spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "RBullet",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=9.5,
            leading=14,
            leftIndent=12,
            bulletIndent=2,
            spaceAfter=2,
        ),
        "small": ParagraphStyle(
            "RSmall",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=8,
            leading=12,
            textColor=colors.HexColor("#555555"),
        ),
    }


def _safe_text(value: Any, limit: int = 1200) -> str:
    """把任意值转成 reportlab 可渲染的纯文本（转义 XML 特殊字符）。"""
    if value is None:
        return ""
    if isinstance(value, float):
        try:
            text = f"{value:.4f}".rstrip("0").rstrip(".")
        except Exception:  # noqa: BLE001
            text = str(value)
    else:
        text = str(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


# 表格单元格用 Paragraph，长文本才能自动换行而不是溢出页面
_CELL_STYLE = ParagraphStyle("RCell", fontName=_FONT, fontSize=7.5, leading=9.5)
_KV_STYLE = ParagraphStyle("RKV", fontName=_FONT, fontSize=9, leading=12.5)
_SMALL_STYLE = ParagraphStyle(
    "RSmallFlat", fontName=_FONT, fontSize=8, leading=12,
    textColor=colors.HexColor("#555555"),
)


def _cell(value: Any, limit: int = 120, style: ParagraphStyle = _CELL_STYLE) -> Paragraph:
    return Paragraph(_safe_text(value, limit), style)


def _cell_md(value: Any, limit: int = 60) -> Paragraph:
    """Markdown 表格里的单元格：先截断原文，再剥掉 ** 和反引号。"""
    text = str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return Paragraph(_inline(text), _CELL_STYLE)


def _table_style() -> TableStyle:
    return TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), _FONT),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA7B4")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FC")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )


def _df_to_table(
    df: pd.DataFrame,
    max_rows: int = _MAX_TABLE_ROWS,
    max_cols: int = _MAX_TABLE_COLS,
) -> Optional[Table]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None

    subset = df.iloc[:max_rows, :max_cols].copy()
    subset = subset.astype(object).where(pd.notna(subset), "")

    data: list[list[Paragraph]] = [[_cell(c, 40) for c in subset.columns]]
    for row in subset.itertuples(index=False, name=None):
        data.append([_cell(v, 80) for v in row])

    table = Table(data, repeatRows=1, hAlign="LEFT")
    table.setStyle(_table_style())
    return table


def _kv_table(pairs: list[tuple[str, Any]]) -> Table:
    data = [[_cell(k, 40), _cell(v, 200, _KV_STYLE)] for k, v in pairs]
    table = Table(data, colWidths=[1.6 * inch, 4.6 * inch], hAlign="LEFT")
    table.setStyle(_table_style())
    return table


def _keep_together(table: Optional[Table], max_rows: int = 16) -> Any:
    """短表格尽量不跨页，避免上一页只剩一两行；长表格仍允许拆分。"""
    if table is None:
        return None
    rows = len(getattr(table, "_cellvalues", []) or [])
    return KeepTogether([table]) if rows and rows <= max_rows else table


# Markdown -> 排版
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_LIST_RE = re.compile(r"^([-*+]|\d+[.)])\s+")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def _inline(text: str) -> str:
    """处理行内 Markdown：**粗体** 和 `代码`，先转义再替换。"""
    out = _safe_text(text, 600)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _CODE_RE.sub(r"\1", out)
    return out


def _split_md_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _is_md_table(lines: list[str], i: int) -> bool:
    if i + 1 >= len(lines):
        return False
    head, sep = lines[i].strip(), lines[i + 1].strip()
    return head.startswith("|") and bool(_TABLE_SEP_RE.match(sep))


def _md_table_flowable(header: list[str], rows: list[list[str]]) -> Any:
    ncols = min(len(header), _MAX_ANSWER_TABLE_COLS) or 1
    header = header[:ncols]
    body = [r[:ncols] + [""] * max(0, ncols - len(r)) for r in rows[:_MAX_ANSWER_TABLE_ROWS]]

    data = [[_cell_md(c, 30) for c in header]]
    for r in body:
        data.append([_cell_md(c, 60) for c in r])

    table = Table(data, repeatRows=1, hAlign="LEFT",
                  colWidths=[_CONTENT_WIDTH / ncols] * ncols)
    table.setStyle(_table_style())
    parts: list[Any] = [Spacer(1, 3), table]
    if len(rows) > _MAX_ANSWER_TABLE_ROWS:
        parts.append(
            Paragraph(
                f"（表格仅显示前 {_MAX_ANSWER_TABLE_ROWS} 行，原表共 {len(rows)} 行）",
                _SMALL_STYLE,
            )
        )
    parts.append(Spacer(1, 6))
    return KeepTogether(parts)


def _answer_flowables(text: str, styles: dict) -> list[Any]:
    """把回答的 Markdown 转成 reportlab 流式对象。"""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []

    truncated = len(raw) > _MAX_ANSWER_CHARS
    if truncated:
        raw = raw[:_MAX_ANSWER_CHARS]

    lines = raw.split("\n")
    flow: list[Any] = []
    buffer: list[str] = []

    def flush_paragraph() -> None:
        if buffer:
            flow.append(Paragraph(_inline(" ".join(buffer)), styles["body"]))
            buffer.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            i += 1
            continue

        if _is_md_table(lines, i):
            flush_paragraph()
            header = _split_md_row(lines[i])
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_md_row(lines[i]))
                i += 1
            flow.append(_md_table_flowable(header, rows))
            continue

        if stripped.startswith("#"):
            flush_paragraph()
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            flow.append(Paragraph(_inline(title), styles["h3" if level >= 3 else "h2"]))
            i += 1
            continue

        match = _LIST_RE.match(stripped)
        if match:
            flush_paragraph()
            marker = match.group(1)
            item = _LIST_RE.sub("", stripped, count=1)
            flow.append(Paragraph(f"{marker} {_inline(item)}", styles["bullet"]))
            i += 1
            continue

        buffer.append(stripped)
        i += 1

    flush_paragraph()

    if truncated:
        flow.append(Paragraph("（回答较长，以上内容已截断）", styles["small"]))
    return flow


def _image_flowable(png: bytes) -> Optional[Image]:
    """按原图比例缩放，避免图表被拉伸变形。"""
    try:
        reader = ImageReader(io.BytesIO(bytes(png)))
        width, height = reader.getSize()
        if not width or not height:
            return None
        scale = min(_FIG_WIDTH / width, _FIG_MAX_HEIGHT / height)
        return Image(io.BytesIO(bytes(png)), width=width * scale, height=height * scale)
    except Exception:  # noqa: BLE001 - 图片坏了不影响整份报告
        return None


# 各章节
def _section_overview(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("1. 数据集概览", styles["section"]))
    rows, cols = df.shape
    pairs = [
        ("数据行数", rows),
        ("字段数", cols),
        ("完全重复行", int(df.duplicated().sum())),
        ("去重后行数", len(df.drop_duplicates())),
        ("内存占用", f"{df.memory_usage(deep=True).sum() / 1024 / 1024:.2f} MB"),
    ]
    story.append(_kv_table(pairs))
    story.append(
        Paragraph("下列统计均基于未去重的原始数据；去重后的行数见上表。", styles["small"])
    )
    story.append(Spacer(1, 6))


def _section_schema(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("2. 字段与类型", styles["section"]))
    info = pd.DataFrame(
        {
            "字段": df.columns.astype(str),
            "类型": [str(t) for t in df.dtypes],
            "非空值": [int(df[c].notna().sum()) for c in df.columns],
            "缺失值": [int(df[c].isna().sum()) for c in df.columns],
            "唯一值": [int(df[c].nunique(dropna=True)) for c in df.columns],
        }
    )
    story.append(_keep_together(_df_to_table(info, max_rows=40, max_cols=5), max_rows=18))
    story.append(Spacer(1, 6))


def _section_missing(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("3. 缺失值分析", styles["section"]))
    missing = df.isna().sum()
    missing = missing[missing > 0].sort_values(ascending=False)
    if missing.empty:
        story.append(Paragraph("没有检测到缺失值。", styles["body"]))
    else:
        data = pd.DataFrame(
            {
                "字段": missing.index.astype(str),
                "缺失数": missing.values,
                "缺失占比": [f"{v / len(df) * 100:.2f}%" for v in missing.values],
            }
        )
        story.append(_keep_together(_df_to_table(data, max_rows=25, max_cols=3)))
    story.append(Spacer(1, 6))


def _section_duplicates(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("4. 重复行检查", styles["section"]))
    dup = int(df.duplicated().sum())
    if dup == 0:
        story.append(Paragraph("没有完全重复的行。", styles["body"]))
    else:
        kept = len(df.drop_duplicates())
        story.append(
            Paragraph(
                f"检测到 <b>{dup}</b> 行完全重复数据，占总行数的 {dup / len(df) * 100:.2f}%；"
                f"去重后剩余 {kept} 行。建议确认是否为重复录入，并说明后续分析采用哪种口径。",
                styles["body"],
            )
        )
    story.append(Spacer(1, 6))


def _section_numeric(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("5. 数值字段统计", styles["section"]))
    numeric = df.select_dtypes(include="number")
    if numeric.empty or numeric.shape[1] == 0:
        story.append(Paragraph("没有数值型字段。", styles["body"]))
        story.append(Spacer(1, 6))
        return
    desc = numeric.describe().T.reset_index()
    desc.columns = [str(c) for c in desc.columns]
    story.append(_keep_together(_df_to_table(desc, max_rows=15, max_cols=6), max_rows=18))
    story.append(Spacer(1, 6))


def _section_categorical(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("6. 类别与离散字段分布", styles["section"]))

    categorical = list(df.select_dtypes(include=["object", "category", "bool"]).columns)
    discrete = [
        c
        for c in df.select_dtypes(include="number").columns
        if df[c].nunique(dropna=True) <= _DISCRETE_MAX_UNIQUE
    ]
    # 离散数值列（例如评分、等级）本质是类别，不该被当成连续变量忽略掉
    targets = list(dict.fromkeys(categorical + discrete))

    if not targets:
        story.append(Paragraph("没有类别型或离散型字段。", styles["body"]))
        story.append(Spacer(1, 6))
        return

    rows: list[list[Paragraph]] = [[_cell(t) for t in ("字段", "高频取值（前 5）", "唯一值数")]]
    for col in targets[:14]:
        counts = df[col].dropna().astype(str).value_counts().head(5)
        if counts.empty:
            continue
        pairs = "，".join(f"{k}({int(v)})" for k, v in counts.items())
        rows.append([_cell(col, 30), _cell(pairs, 400), _cell(int(df[col].nunique()))])

    table = Table(rows, colWidths=[1.4 * inch, 3.6 * inch, 0.9 * inch], hAlign="LEFT")
    table.setStyle(_table_style())
    story.append(_keep_together(table, max_rows=16))
    story.append(
        Paragraph("其中唯一值较少的数值列（如评分、等级）按离散分布处理。", styles["small"])
    )
    story.append(Spacer(1, 6))


def _pick_corr_columns(numeric: pd.DataFrame, limit: int) -> tuple[list[str], list[str]]:
    """按与其他字段的整体相关性挑列，避免直接截前 8 列把关键字段漏掉。"""
    corr = numeric.corr(numeric_only=True).abs()
    centrality = corr.sum() - 1.0  # 减掉自身那 1
    ranked = [c for c in centrality.sort_values(ascending=False).index]
    keep = [c for c in numeric.columns if c in ranked[:limit]]
    omitted = [c for c in numeric.columns if c not in keep]
    return keep, omitted


def _section_correlation(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("7. 数值字段相关性", styles["section"]))
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        story.append(Paragraph("数值字段不足两个，无法计算相关性。", styles["body"]))
        story.append(Spacer(1, 6))
        return

    keep, omitted = _pick_corr_columns(numeric, _CORR_MAX_COLS - 1)
    matrix = numeric[keep].corr(numeric_only=True).round(3)
    data = pd.DataFrame({"字段": [str(c) for c in matrix.index]})
    for col in matrix.columns:
        data[str(col)] = matrix[col].to_numpy()
    story.append(
        _keep_together(
            _df_to_table(data, max_rows=len(keep) + 1, max_cols=len(keep) + 1),
            max_rows=12,
        )
    )
    if omitted:
        story.append(
            Paragraph(
                "受版面限制未显示：" + "、".join(_safe_text(c, 40) for c in omitted)
                + "。这些字段与其他字段的相关系数可在对话的分析结果里查看。",
                styles["small"],
            )
        )
    story.append(Spacer(1, 6))


def _section_quality(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("8. 数据质量与建议", styles["section"]))
    notes: list[str] = []

    dup = int(df.duplicated().sum())
    if dup:
        notes.append(
            f"<b>重复行</b>：{dup} 行完全重复（占 {dup / len(df) * 100:.2f}%），"
            f"去重后 {len(df.drop_duplicates())} 行。分析前应明确是否去重，否则统计量会被重复样本放大。"
        )

    high_missing = [c for c in df.columns if df[c].isna().mean() > 0.3]
    if high_missing:
        notes.append(
            "<b>高缺失字段</b>："
            + "、".join(_safe_text(c, 40) for c in high_missing[:8])
            + "（缺失超过 30%，建模前建议剔除或填补）。"
        )

    const_cols = [c for c in df.columns if df[c].nunique(dropna=True) <= 1]
    if const_cols:
        notes.append(
            "<b>常量字段</b>："
            + "、".join(_safe_text(c, 40) for c in const_cols[:8])
            + "（只有一个取值，对分析无贡献）。"
        )

    numeric = df.select_dtypes(include="number")
    if not numeric.empty:
        skew = numeric.skew(numeric_only=True).abs()
        skewed = [c for c in skew.index if pd.notna(skew[c]) and skew[c] > 2]
        if skewed:
            detail = []
            for col in skewed[:6]:
                median = numeric[col].median()
                mean = numeric[col].mean()
                detail.append(f"{_safe_text(col, 30)}（中位数 {median:.4g}，均值 {mean:.4g}）")
            notes.append(
                "<b>强偏态字段</b>：" + "、".join(detail)
                + "。偏度大于 2，均值容易被极值拉偏，描述这类字段时应同时给中位数。"
            )

    if not notes:
        notes.append("未发现明显的缺失、重复、常量或极端偏态问题。")

    for note in notes:
        story.append(Paragraph(note, styles["body"]))
    story.append(Spacer(1, 6))


def _section_sample(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph(f"9. 数据样例（前 10 行，前 {_MAX_TABLE_COLS} 列）", styles["section"]))
    story.append(_keep_together(_df_to_table(df, max_rows=10, max_cols=_MAX_TABLE_COLS)))
    omitted = [str(c) for c in df.columns[_MAX_TABLE_COLS:]]
    if omitted:
        story.append(
            Paragraph(
                "另有 " + str(len(omitted)) + " 列未显示："
                + "、".join(_safe_text(c, 30) for c in omitted[:12])
                + ("…" if len(omitted) > 12 else "") + "。",
                styles["small"],
            )
        )
    story.append(Spacer(1, 6))


def _section_analyses(story: list, analyses: list[dict], styles: dict) -> None:
    story.append(Paragraph("分析结果", styles["section"]))
    story.append(
        Paragraph("以下为本次会话中基于该数据集完成的问答与分析。", styles["small"])
    )
    story.append(Spacer(1, 6))

    for index, item in enumerate(analyses, 1):
        if not isinstance(item, dict):
            continue
        question = _safe_text(item.get("question") or f"分析 {index}", 300)

        story.append(Paragraph(f"{index}. {question}", styles["h3"]))
        story.extend(_answer_flowables(item.get("answer") or "", styles))

        result_df = item.get("result_df")
        table = _df_to_table(result_df)
        if table is not None:
            story.append(Paragraph("结果表：", styles["small"]))
            story.append(table)
            if isinstance(result_df, pd.DataFrame):
                extra_cols = len(result_df.columns) - _MAX_TABLE_COLS
                extra_rows = len(result_df) - _MAX_TABLE_ROWS
                if extra_cols > 0 or extra_rows > 0:
                    story.append(
                        Paragraph(
                            f"（仅显示前 {_MAX_TABLE_ROWS} 行、前 {_MAX_TABLE_COLS} 列；"
                            f"完整结果共 {len(result_df)} 行 × {len(result_df.columns)} 列）",
                            styles["small"],
                        )
                    )

        figure_png = item.get("figure_png")
        if isinstance(figure_png, (bytes, bytearray)) and figure_png:
            image = _image_flowable(bytes(figure_png))
            if image is not None:
                story.append(Spacer(1, 6))
                story.append(image)

        story.append(Spacer(1, 10))


def generate_pdf_report(
    df: pd.DataFrame,
    dataset_name: str = "Uploaded Dataset",
    analyses: Optional[list[dict]] = None,
) -> bytes:
    """生成 PDF 报告并返回字节流。"""
    analyses = analyses or []
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("没有可导出的数据。")

    styles = _build_styles()
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title=f"数据分析报告 - {dataset_name}",
        author="AI Data Analyst Agent",
    )

    story: list[Any] = [
        Paragraph("数据集分析报告", styles["title"]),
        Paragraph(f"数据集：{_safe_text(dataset_name, 120)}", styles["subtitle"]),
    ]

    _section_overview(story, df, styles)
    _section_schema(story, df, styles)
    _section_missing(story, df, styles)
    _section_duplicates(story, df, styles)
    _section_numeric(story, df, styles)
    _section_categorical(story, df, styles)
    _section_correlation(story, df, styles)
    _section_quality(story, df, styles)
    _section_sample(story, df, styles)

    if analyses:
        _section_analyses(story, analyses, styles)

    doc.build(story)
    return buffer.getvalue()