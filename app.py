"""
Streamlit 入口：上传数据 → 填 DeepSeek Key → 提问 → 渲染回答/表/图 → 导出 PDF。

本文件只做"界面 + 编排"，不实现分析逻辑：
    DataAnalystAgent(...).ask(question) -> RunResult
数据读取、画像、记忆、报告全部走现有接口。
"""

from __future__ import annotations

import hashlib
import os

import streamlit as st
from dotenv import load_dotenv

from agent import DataAnalystAgent
from data_utils import DataLoadError, load_file, profile_data, profile_to_prompt_text
from deepseek import DeepSeekClient, DeepSeekError
from memory import AgentMemory
from report import generate_pdf_report

load_dotenv()

st.set_page_config(page_title="AI Data Analyst Agent", layout="wide")
st.title("AI Data Analyst Agent")
st.caption("上传数据集 → 填写 DeepSeek API Key → 用自然语言提问，Agent 会自己写代码/SQL 算出真实结果。")


def _drop_pdf() -> None:
    """丢掉已生成的 PDF（换文件 / 换 Key / 清空对话时）。"""
    if "pdf_report" in st.session_state:
        del st.session_state["pdf_report"]


def _wide() -> dict:
    """整宽布局的兼容写法：新版 Streamlit 用 width="stretch"，旧版用 use_container_width。"""
    try:
        major, minor = (int(p) for p in st.__version__.split(".")[:2])
        if (major, minor) >= (1, 49):
            return {"width": "stretch"}
    except Exception:  # noqa: BLE001
        pass
    return {"use_container_width": True}

# 侧边栏：模型设置
with st.sidebar:
    st.header("⚙️ DeepSeek 设置")
    api_key = st.text_input(
        "DeepSeek API Key",
        type="password",
        value=os.getenv("DEEPSEEK_API_KEY", ""),
        help="Key 只保存在本次浏览器会话中，不会写入日志或代码。",
    )
    model = st.text_input("模型", value="deepseek-chat")
    max_steps = st.number_input("最大分析步数", min_value=1, max_value=20, value=8, step=1)

    st.divider()
    if st.button("🧹 清空对话与记忆", **_wide()):
        st.session_state.chat = []
        st.session_state.memory = AgentMemory()
        _drop_pdf()
        st.rerun()

if not api_key:
    st.warning("请先在左侧填写 DeepSeek API Key 后再提问。")

# 会话状态
st.session_state.setdefault("memory", AgentMemory())
st.session_state.setdefault("chat", [])
st.session_state.setdefault("df", None)
st.session_state.setdefault("profile", None)
st.session_state.setdefault("uploaded_name", "")
st.session_state.setdefault("uploaded_sig", None)
st.session_state.setdefault("llm", None)
st.session_state.setdefault("llm_key", None)

# 密钥或模型变化时重建客户端
_llm_signature = (api_key or "", model or "")
if st.session_state.get("llm_key") != _llm_signature:
    if api_key:
        try:
            st.session_state.llm = DeepSeekClient(api_key, model=model or "deepseek-chat")
        except DeepSeekError as exc:
            st.session_state.llm = None
            st.error(str(exc))
    else:
        st.session_state.llm = None
    st.session_state.llm_key = _llm_signature
    _drop_pdf()

llm = st.session_state.get("llm")

# 上传数据
uploaded = st.file_uploader("上传数据集（CSV / Excel）", type=["csv", "txt", "tsv", "xlsx", "xls"])

if uploaded is not None:
    raw = uploaded.read()
    # 用内容指纹判断是否换了文件，避免每次重跑页面都重新解析大文件
    signature = hashlib.md5(raw).hexdigest()
    if st.session_state.get("uploaded_sig") != signature:
        try:
            df = load_file(uploaded.name, raw)
            profile = profile_data(df)
            st.session_state.df = df
            st.session_state.profile = profile
            st.session_state.uploaded_name = uploaded.name
            st.session_state.uploaded_sig = signature
            # 换文件才重置：聊天历史 + 记忆 + 旧报告
            st.session_state.chat = []
            st.session_state.memory = AgentMemory()
            _drop_pdf()
        except DataLoadError as exc:
            st.error(str(exc))
            st.session_state.df = None
            st.session_state.profile = None
            st.session_state.uploaded_sig = None

df = st.session_state.get("df")
profile = st.session_state.get("profile")

if df is not None and profile:
    with st.expander("📋 Dataset overview", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Rows", profile.get("rows", 0))
        c2.metric("Columns", profile.get("columns", 0))
        c3.metric("Duplicates", profile.get("duplicates", 0))
        total_missing = sum(int(v) for v in (profile.get("missing") or {}).values())
        c4.metric("Missing cells", total_missing)

        st.write("**字段类型与缺失**")
        st.dataframe(
            {
                "Column": list((profile.get("dtypes") or {}).keys()),
                "dtype": list((profile.get("dtypes") or {}).values()),
                "Missing": [profile.get("missing", {}).get(c, 0) for c in (profile.get("dtypes") or {})],
                "Missing %": [
                    profile.get("missing_pct", {}).get(c, 0.0) for c in (profile.get("dtypes") or {})
                ],
                "Unique": [profile.get("unique", {}).get(c, 0) for c in (profile.get("dtypes") or {})],
            },
            **_wide(),
        )
        st.dataframe(df.head(50), **_wide())

# 渲染历史对话
def _render_steps(steps: list[dict]) -> None:
    """渲染一步到多步的执行详情（历史轮次和当轮都用它，避免两处逻辑各改一遍）。"""
    if not steps:
        return
    with st.expander("🔍 分析详情"):
        for i, step in enumerate(steps, 1):
            st.write(f"**Step {i} — {step.get('action')}**")
            st.code(
                step.get("code", ""),
                language="python" if step.get("action") == "python" else "sql",
            )
            if step.get("error"):
                st.error(step["error"])
            elif step.get("summary"):
                st.text(step["summary"][:1500])


def _render_turn(turn: dict) -> None:
    with st.chat_message(turn.get("role", "assistant")):
        st.markdown(turn.get("content", ""))
        turn_df = turn.get("df")
        if turn_df is not None and not turn_df.empty:
            st.dataframe(turn_df, **_wide())
        figure = turn.get("figure")
        if figure:
            st.image(figure, **_wide())
        _render_steps(turn.get("steps") or [])


for _turn in st.session_state.chat:
    _render_turn(_turn)

# 提问
question = st.chat_input("例如：哪些因素对红酒评分有影响？")

if question:
    current_df = st.session_state.get("df")
    current_profile = st.session_state.get("profile")

    if current_df is None:
        st.warning("请先上传数据集。")
    elif llm is None:
        st.warning("请先在左侧填写 DeepSeek API Key。")
    else:
        st.session_state.chat.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("正在分析…（理解问题 → 写代码 → 执行 → 汇总）"):
                try:
                    agent = DataAnalystAgent(
                        df=current_df,
                        profile_text=profile_to_prompt_text(current_profile),
                        llm=llm,
                        memory=st.session_state.memory,
                        max_steps=int(max_steps),
                    )
                    result = agent.ask(question)

                    st.markdown(result.answer)
                    if result.result_df is not None and not result.result_df.empty:
                        st.dataframe(result.result_df, **_wide())
                    if result.figure_png:
                        st.image(result.figure_png, **_wide())

                    _render_steps(result.code_steps)

                    compact_steps = [
                        {
                            "action": s.get("action"),
                            "code": s.get("code"),
                            "error": s.get("error"),
                            "summary": s.get("summary"),
                        }
                        for s in result.code_steps
                    ]

                    st.session_state.chat.append(
                        {
                            "role": "assistant",
                            "question": question,
                            "content": result.answer,
                            "df": result.result_df,
                            "figure": result.figure_png,
                            "steps": compact_steps,
                            # 被意图门挡下的轮次没有执行任何工具，不写进报告
                            "analysis": bool(result.code_steps),
                        }
                    )
                    # 有新分析后旧 PDF 已过期，避免用户下载到上一轮的报告
                    _drop_pdf()
                except Exception as exc:  # noqa: BLE001 - 界面不能因为一次失败就崩
                    message = f"分析失败：{exc}"
                    st.error(message)
                    st.session_state.chat.append(
                        {
                            "role": "assistant",
                            "question": question,
                            "content": message,
                            "df": None,
                            "figure": None,
                            "steps": [],
                        }
                    )

# 报告导出
def _collect_analyses() -> list[dict]:
    """把聊天里"有问题 + 有回答"的分析轮次整理成 report 需要的结构。"""
    collected: list[dict] = []
    for turn in st.session_state.chat:
        if turn.get("role") != "assistant":
            continue
        # 意图门拒绝的轮次没有执行工具，不算分析结果
        if turn.get("analysis") is False:
            continue
        answer = turn.get("content") or ""
        question_text = turn.get("question") or ""
        if not answer.strip():
            continue
        collected.append(
            {
                "question": question_text,
                "answer": answer,
                "result_df": turn.get("df"),
                "figure_png": turn.get("figure"),
            }
        )
    return collected


if df is not None:
    st.divider()
    st.subheader("📄 导出报告")
    left, right = st.columns([1, 2])
    with left:
        if st.button("Generate PDF Report", type="primary", **_wide()):
            with st.spinner("正在生成 PDF…"):
                try:
                    st.session_state["pdf_report"] = generate_pdf_report(
                        st.session_state.df,
                        dataset_name=st.session_state.get("uploaded_name") or "Uploaded Dataset",
                        analyses=_collect_analyses(),
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"PDF 生成失败：{exc}")

    if "pdf_report" in st.session_state:
        right.download_button(
            "⬇️ 下载 PDF",
            data=st.session_state["pdf_report"],
            file_name="AI_Data_Analyst_Report.pdf",
            mime="application/pdf",
            **_wide(),
        )
