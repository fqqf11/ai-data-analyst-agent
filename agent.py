"""
Agent 主循环：意图门 → 理解改写 → 决策 → 执行 → 观察 →（循环）→ 最终回答 → 写记忆。

模型自己生成 Python / SQL，由 executor 实际执行，结果回传后继续决策，
直到 done 或达到 max_steps。因此"哪些因素影响评分"这类开放问题也能算出来，
不依赖预先写好的关键词表。

依赖方向（单向，无循环导入）：
    app.py -> agent.py -> executor.py -> tools.py -> data_utils.py
                       -> deepseek.py / prompts.py / memory.py
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

import executor
import prompts
from deepseek import DeepSeekClient, DeepSeekError
from memory import AgentMemory


def _default_max_steps() -> int:
    raw = os.getenv("DEEPSEEK_MAX_STEPS", "")
    try:
        value = int(raw)
        return value if value > 0 else 8
    except (TypeError, ValueError):
        return 8


MAX_STEPS = _default_max_steps()


@dataclass
class RunResult:
    """一次提问的完整产出，交给 app.py 渲染。"""

    answer: str
    tool_used: list[str]
    sql: Optional[str]
    result_df: Optional[pd.DataFrame]
    figure_png: Optional[bytes]
    code_steps: list[dict[str, Any]]
    notes: list[str]


class DataAnalystAgent:
    def __init__(
        self,
        df: pd.DataFrame,
        profile_text: str,
        llm: DeepSeekClient,
        memory: AgentMemory,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.df = df
        self.profile_text = profile_text
        self.llm = llm
        self.memory = memory
        self.max_steps = max(1, int(max_steps or MAX_STEPS))

    # 公共入口
    def ask(self, question: str) -> RunResult:
        if not (question or "").strip():
            return RunResult(
                answer="请输入一个与当前数据集相关的问题。",
                tool_used=[],
                sql=None,
                result_df=None,
                figure_png=None,
                code_steps=[],
                notes=["问题为空，未调用模型。"],
            )

        gate = self._understand(question)

        # 非数据分析请求：直接拒绝，不执行任何工具
        if not gate["is_analysis"]:
            return RunResult(
                answer=gate["refusal"],
                tool_used=[],
                sql=None,
                result_df=None,
                figure_png=None,
                code_steps=[],
                notes=["意图门判定：非数据分析请求，未执行工具。"],
            )

        resolved = gate["resolved"]

        steps: list[dict[str, Any]] = []
        tool_used: list[str] = []
        notes: list[str] = []
        sql_used: Optional[str] = None
        result_df: Optional[pd.DataFrame] = None
        figure_png: Optional[bytes] = None

        for _ in range(self.max_steps):
            action = self._plan(resolved, steps)
            if action is None:
                notes.append("模型返回的动作无法解析（不是有效 JSON），已提前结束。")
                break

            kind = action.get("action")

            if kind == "run_python":
                code = _clean_code(action.get("code") or "")
                res = executor.run_python(code, self.df)
                steps.append(_make_step("python", code, res))
                tool_used.append("python")
                if res.result_df is not None and not res.result_df.empty:
                    result_df = res.result_df
                if res.figure_png:
                    figure_png = res.figure_png

            elif kind == "run_sql":
                code = _clean_code(action.get("code") or "")
                sql_used = code
                res = executor.run_sql(code, self.df)
                steps.append(_make_step("sql", code, res))
                tool_used.append("sql")
                if res.result_df is not None and not res.result_df.empty:
                    result_df = res.result_df

            elif kind == "done":
                break

            else:
                notes.append(f"未知动作 {kind!r}，已忽略。")
                break

        if len(steps) >= self.max_steps:
            notes.append(f"已达到最大步数 {self.max_steps}，转入最终回答。")

        answer = self._final_answer(resolved, steps)
        self._save_memory(question, resolved, sql_used, result_df, tool_used, answer)

        return RunResult(
            answer=answer,
            tool_used=tool_used,
            sql=sql_used,
            result_df=result_df,
            figure_png=figure_png,
            code_steps=steps,
            notes=notes,
        )

    # 内部步骤
    def _understand(self, question: str) -> dict[str, Any]:
        """意图门 + 改写（一次 LLM 调用完成，省成本）。任何异常都默认放行。"""
        try:
            prompt = prompts.INTENT_UNDERSTAND_PROMPT.format(
                profile=self.profile_text,
                memory_context=self.memory.context_for_prompt(question),
                chat_history=self.memory.conversation.recent_history_text(),
                question=question,
            )
        except Exception:  # noqa: BLE001 - 记忆模块异常不应阻断提问
            prompt = f"用户输入：{question}\n只输出 JSON。"

        try:
            raw = self.llm.generate(prompts.SYSTEM_PROMPT, prompt)
        except DeepSeekError:
            # 模型挂了也别误杀用户的问题，直接放行
            return {"is_analysis": True, "resolved": question, "refusal": ""}

        data = _parse_json(raw)
        if not isinstance(data, dict):
            return {"is_analysis": True, "resolved": question, "refusal": ""}

        is_analysis = bool(data.get("is_analysis", True))
        if is_analysis:
            resolved = str(data.get("resolved_question") or question).strip()
            return {"is_analysis": True, "resolved": resolved or question, "refusal": ""}

        refusal = str(
            data.get("refusal")
            or "我只能分析你上传的数据集，请提出与数据相关的问题。"
        ).strip()
        return {"is_analysis": False, "resolved": question, "refusal": refusal}

    def _plan(self, resolved: str, steps: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        prompt = prompts.AGENT_STEP_PROMPT.format(
            profile=self.profile_text,
            question=resolved,
            steps=_format_steps(steps),
        )
        try:
            raw = self.llm.generate(prompts.SYSTEM_PROMPT, prompt)
        except DeepSeekError:
            return {"action": "done"}

        data = _parse_json(raw)
        if not isinstance(data, dict) or data.get("action") not in {"run_python", "run_sql", "done"}:
            return None
        return data

    def _final_answer(self, resolved: str, steps: list[dict[str, Any]]) -> str:
        prompt = prompts.FINAL_ANSWER_PROMPT.format(
            profile=self.profile_text,
            question=resolved,
            tool_results=_format_steps(steps),
        )
        try:
            ans = self.llm.generate(prompts.SYSTEM_PROMPT, prompt)
            return ans or "抱歉，无法生成回答。"
        except DeepSeekError as exc:
            return f"分析过程出错，模型调用失败：{exc}"

    def _save_memory(
        self,
        question: str,
        resolved: str,
        sql: Optional[str],
        result_df: Optional[pd.DataFrame],
        tool_used: list[str],
        answer: str,
    ) -> None:
        try:
            self.memory.remember_turn(question, (answer or "")[:280])
            self.memory.remember_analysis_context(
                question=question,
                resolved_question=resolved,
                sql=sql,
                result_df=result_df,
                tools_used=tool_used,
                chart_type=None,
            )
        except Exception:  # noqa: BLE001 - 记忆写失败不影响本次回答
            pass


# 纯函数辅助（不调用模型）
def _clean_code(code: str) -> str:
    """去掉 Markdown 代码围栏，返回纯代码。"""
    code = (code or "").strip()
    code = re.sub(r"^```[a-zA-Z0-9_+-]*\s*", "", code)
    code = re.sub(r"\s*```$", "", code)
    return code.strip()


def _parse_json(raw: str) -> Optional[Any]:
    """从模型输出里抓第一个 JSON 对象；失败返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```[a-zA-Z0-9_+-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _make_step(action: str, code: str, res: executor.ToolResult) -> dict[str, Any]:
    return {
        "action": action,
        "code": code,
        "ok": res.ok,
        "summary": res.summary,
        "error": res.error,
    }


def _format_steps(steps: list[dict[str, Any]]) -> str:
    if not steps:
        return "(还没有执行任何工具)"
    blocks: list[str] = []
    for i, step in enumerate(steps, 1):
        status = "成功" if step.get("ok") else "失败"
        body = (step.get("summary") or "").strip() or (step.get("error") or "")
        blocks.append(
            f"### Step {i} — {step.get('action')} [{status}]\n"
            f"Code:\n{step.get('code')}\n\n"
            f"Result:\n{body}"
        )
    return "\n\n".join(blocks)
