"""
一键 PDF 报告。

对外接口（app 依赖，勿改签名）：
    generate_pdf_report(df, dataset_name="Uploaded Dataset", analyses=None) -> bytes

`analyses` 为 None 或空列表时，只输出数据描述部分；
传入分析记录时，末尾追加「分析结果」章节（问题 / 回答 / 结果表 / 图表）。

报告使用 reportlab 内置的中文字体 STSong-Light（UnicodeCIDFont），无需额外字体文件。
"""

from __future__ import annotations

import io
from typing import Any, Optional

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_MAX_TABLE_ROWS = 10
_MAX_TABLE_COLS = 8
_MAX_ANSWER_CHARS = 1500
_FIG_WIDTH = 6.6 * inch
_FIG_HEIGHT = 3.7 * inch


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
        "body": ParagraphStyle(
            "RBody",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=9.5,
            leading=15,
            spaceAfter=6,
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


def _cell(value: Any, limit: int = 120, style: ParagraphStyle = _CELL_STYLE) -> Paragraph:
    return Paragraph(_safe_text(value, limit), style)


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


# 各章节
def _section_overview(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("1. 数据集概览", styles["section"]))
    rows, cols = df.shape
    pairs = [
        ("数据行数", rows),
        ("字段数", cols),
        ("完全重复行", int(df.duplicated().sum())),
        ("内存占用", f"{df.memory_usage(deep=True).sum() / 1024 / 1024:.2f} MB"),
    ]
    story.append(_kv_table(pairs))
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
    table = _df_to_table(info, max_rows=40, max_cols=5)
    if table is not None:
        story.append(table)
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
        table = _df_to_table(data, max_rows=25, max_cols=3)
        if table is not None:
            story.append(table)
    story.append(Spacer(1, 6))


def _section_duplicates(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("4. 重复行检查", styles["section"]))
    dup = int(df.duplicated().sum())
    if dup == 0:
        story.append(Paragraph("没有完全重复的行。", styles["body"]))
    else:
        story.append(
            Paragraph(
                f"检测到 <b>{dup}</b> 行完全重复数据，占总行数的 {dup / len(df) * 100:.2f}%。"
                f"建议确认是否为重复录入。",
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
    table = _df_to_table(desc, max_rows=15, max_cols=6)
    if table is not None:
        story.append(table)
    story.append(Spacer(1, 6))


def _section_categorical(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("6. 类别字段分布", styles["section"]))
    categorical = df.select_dtypes(include=["object", "category", "bool"])
    if categorical.empty or categorical.shape[1] == 0:
        story.append(Paragraph("没有类别型字段。", styles["body"]))
        story.append(Spacer(1, 6))
        return

    rows: list[list[Paragraph]] = [[_cell(t) for t in ("字段", "高频取值（前 5）", "唯一值数")]]
    for col in list(categorical.columns)[:12]:
        # 先 dropna 再转字符串，否则缺失值会以 "nan" / "None" 混进高频取值里
        counts = categorical[col].dropna().astype(str).value_counts().head(5)
        if counts.empty:
            continue
        pairs = "，".join(f"{k}({int(v)})" for k, v in counts.items())
        rows.append(
            [_cell(col, 30), _cell(pairs, 400), _cell(int(categorical[col].nunique()))]
        )

    table = Table(rows, colWidths=[1.4 * inch, 3.6 * inch, 0.9 * inch], hAlign="LEFT")
    table.setStyle(_table_style())
    story.append(table)
    story.append(Spacer(1, 6))


def _section_correlation(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("7. 数值字段相关性", styles["section"]))
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        story.append(Paragraph("数值字段不足两个，无法计算相关性。", styles["body"]))
        story.append(Spacer(1, 6))
        return

    corr = numeric.iloc[:, :8].corr(numeric_only=True).round(3).reset_index()
    corr.columns = [str(c) for c in corr.columns]
    table = _df_to_table(corr, max_rows=9, max_cols=9)
    if table is not None:
        story.append(table)
    story.append(Spacer(1, 6))


def _section_quality(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("8. 数据质量与建议", styles["section"]))
    notes: list[str] = []

    high_missing = [c for c in df.columns if df[c].isna().mean() > 0.3]
    if high_missing:
        notes.append(
            "<b>高缺失字段</b>："
            + "、".join(_safe_text(c, 40) for c in high_missing[:8])
            + "（缺失超过 30%，建模前建议剔除或填补）。"
        )

    dup = int(df.duplicated().sum())
    if dup:
        notes.append(f"<b>重复行</b>：{dup} 行完全重复，建议去重后再统计。")

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
            notes.append(
                "<b>强偏态字段</b>："
                + "、".join(_safe_text(c, 40) for c in skewed[:8])
                + "（偏度 > 2，均值可能被极值拉偏，建议同时看中位数）。"
            )

    if not notes:
        notes.append("未发现明显的缺失、重复、常量或极端偏态问题。")

    for note in notes:
        story.append(Paragraph(note, styles["body"]))
    story.append(Spacer(1, 6))


def _section_sample(story: list, df: pd.DataFrame, styles: dict) -> None:
    story.append(Paragraph("9. 数据样例（前 10 行）", styles["section"]))
    table = _df_to_table(df, max_rows=10, max_cols=_MAX_TABLE_COLS)
    if table is not None:
        story.append(table)
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
        answer = _safe_text(item.get("answer") or "", _MAX_ANSWER_CHARS)

        story.append(Paragraph(f"<b>{index}. {question}</b>", styles["body"]))
        if answer:
            story.append(Paragraph(answer, styles["body"]))

        table = _df_to_table(item.get("result_df"))
        if table is not None:
            story.append(Spacer(1, 4))
            story.append(table)

        figure_png = item.get("figure_png")
        if isinstance(figure_png, (bytes, bytearray)) and figure_png:
            try:
                image = Image(io.BytesIO(bytes(figure_png)), width=_FIG_WIDTH, height=_FIG_HEIGHT)
                story.append(Spacer(1, 6))
                story.append(image)
            except Exception:  # noqa: BLE001 - 图片坏了不影响整份报告
                pass

        story.append(Spacer(1, 10))


# 主入口
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
