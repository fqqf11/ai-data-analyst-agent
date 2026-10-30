"""
记忆模块：会话历史 + 结构化分析上下文。

agent 用它做两件事实质性的事：
  1. `conversation.recent_history_text()` —— 给意图改写提供对话历史；
  2. `context_for_prompt(question)`        —— 把上一次的分析结论（选了哪些实体、
     跑了什么 SQL、结果列与前几行）带进当前提问，支撑"按地区再看一下"这类追问。

规则：记忆**不自动删除**，只有 app 在"换文件"时显式 `clear()`。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

_MAX_CONTEXTS = 20
_HEAD_ROWS = 5


@dataclass
class Turn:
    question: str
    summary: str
    ts: float = field(default_factory=time.time)


@dataclass
class AnalysisContext:
    question: str
    resolved_question: str
    sql: Optional[str]
    result_columns: list[str]
    result_head: list[list[Any]]
    tools_used: list[str]
    chart_type: Optional[str]
    ts: float = field(default_factory=time.time)


class ConversationMemory:
    """极简对话历史。"""

    def __init__(self, max_turns: int = 20) -> None:
        self.max_turns = max_turns
        self.turns: list[Turn] = []

    def add_turn(self, question: str, summary: str) -> None:
        self.turns.append(Turn(question=question or "", summary=summary or ""))
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]

    def recent_history_text(self, limit: int = 6) -> str:
        if not self.turns:
            return "(还没有历史对话)"
        lines: list[str] = []
        for turn in self.turns[-limit:]:
            q = (turn.question or "").strip().replace("\n", " ")
            a = (turn.summary or "").strip().replace("\n", " ")
            lines.append(f"用户：{q}\n助手：{a}")
        return "\n".join(lines)

    def clear(self) -> None:
        self.turns = []


class AgentMemory:
    def __init__(self) -> None:
        self.conversation = ConversationMemory()
        self.analyses: list[AnalysisContext] = []

    def remember_turn(self, question: str, summary: str) -> None:
        self.conversation.add_turn(question, summary)

    def remember_analysis_context(
        self,
        question: str,
        resolved_question: str,
        sql: Optional[str],
        result_df: Optional[pd.DataFrame],
        tools_used: Optional[list[str]] = None,
        chart_type: Optional[str] = None,
        **_extra: Any,
    ) -> None:
        columns: list[str] = []
        head: list[list[Any]] = []
        if isinstance(result_df, pd.DataFrame) and not result_df.empty:
            columns = [str(c) for c in result_df.columns][:10]
            subset = result_df.iloc[:_HEAD_ROWS, :10]
            head = [[_cell(v) for v in row] for row in subset.itertuples(index=False, name=None)]

        self.analyses.append(
            AnalysisContext(
                question=question or "",
                resolved_question=resolved_question or "",
                sql=sql,
                result_columns=columns,
                result_head=head,
                tools_used=list(tools_used or []),
                chart_type=chart_type,
            )
        )
        if len(self.analyses) > _MAX_CONTEXTS:
            self.analyses = self.analyses[-_MAX_CONTEXTS:]

    def context_for_prompt(self, question: str, limit: int = 3) -> str:
        """返回与当前问题最相关的若干条历史分析上下文（纯文本）。"""
        if not self.analyses:
            return "(还没有任何分析记忆)"

        picked = self._rank(question, limit)
        blocks: list[str] = []
        for ctx in picked:
            lines = [f"- 之前的问题：{ctx.resolved_question or ctx.question}"]
            if ctx.sql:
                lines.append(f"  用过的 SQL：{_one_line(ctx.sql)[:300]}")
            if ctx.result_columns:
                lines.append(f"  结果列：{', '.join(ctx.result_columns)}")
            if ctx.result_head:
                preview = " | ".join(
                    ", ".join(str(c) for c in row) for row in ctx.result_head[: len(ctx.result_head)]
                )
                lines.append(f"  结果前几行：{_one_line(preview)[:400]}")
            if ctx.tools_used:
                lines.append(f"  使用工具：{', '.join(ctx.tools_used)}")
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def clear(self) -> None:
        self.conversation.clear()
        self.analyses = []

    def _rank(self, question: str, limit: int) -> list[AnalysisContext]:
        tokens = _tokens(question)
        if not tokens:
            return self.analyses[-limit:]

        scored: list[tuple[int, float, AnalysisContext]] = []
        for ctx in self.analyses:
            haystack = f"{ctx.question} {ctx.resolved_question} {' '.join(ctx.result_columns)}".lower()
            score = sum(1 for t in tokens if t in haystack)
            scored.append((score, ctx.ts, ctx))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        top = [ctx for score, _, ctx in scored if score > 0][:limit]
        if not top:
            top = [ctx for _, _, ctx in scored[:limit]]
        top.sort(key=lambda c: c.ts)
        return top


def _tokens(text: str) -> list[str]:
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9_]{2,}", text)
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    grams: list[str] = []
    for seg in chinese:
        grams.extend(seg[i : i + 2] for i in range(max(0, len(seg) - 1)))
    return list(dict.fromkeys(words + grams))


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _cell(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    return value
