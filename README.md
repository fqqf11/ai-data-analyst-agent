# AI Data Analyst Agent

上传 CSV/Excel 后直接提问，模型自己写 Python 和 SQL，结果和代码都能看。

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/streamlit-1.38%2B-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## 为什么做这个

最早的版本是关键词匹配：看问题里有没有"排名""趋势""相关性"，命中哪个就调哪个固定工具。问题是问法一偏就哑火。"哪些因素最影响复购"既不算排名也不算趋势，得先算相关系数，再跑一遍特征重要性，关键词表里根本没有这一项，于是什么都不出来。

改成的做法是让模型直接写代码。它按问题生成 pandas 或 SQL，在本地真的跑一遍，拿到结果再决定要不要继续。省掉了"猜用户会怎么问"这件事。

## 能做什么

- 模型逐轮决定动作：写 Python、跑 SQL，或者收尾。最多 `MAX_STEPS` 轮
- Python 侧能 import pandas、numpy、sklearn、scipy、statsmodels、matplotlib。相关性、回归、特征重要性、聚类、画图都在这个范围里
- SQL 走 DuckDB，表名固定 `data`。只放行 SELECT 和 WITH，写操作会被拦下
- 追问会带上下文。第一轮问"各城市销售额"，第二轮"只看北京呢"，它知道指的是哪个结果
- 每一步生成了什么代码、有没有报错、输出了什么，界面上都在，出错时能看出是哪一步
- 能导 PDF：先是一份数据集本身的描述，后面接着这次会话里每个问题的回答、结果表和图（见下面「报告里有什么」）
- Key 只放在会话里，不写文件，也不进日志

## 跑起来

需要 Python 3.10 以上。我是在 3.13 和 Streamlit 1.66 上测的，更老的版本没试过。

bash
git clone https://github.com/fqqf11/ai-data-analyst-agent.git
cd ai-data-analyst-agent
pip install -r requirements.txt
streamlit run app.py


打开 http://localhost:8501，左边填 Key（在 platform.deepseek.com 建），传文件，然后底下提问。要报告点页面下方的 Generate PDF Report。

第一次装依赖会慢几分钟，主要卡在 scipy 和 scikit-learn 的 wheel 上。国内可以换镜像：

bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple


## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | 界面上填也行，写进 `.env` 只是不用每次填 |
| `DEEPSEEK_MAX_STEPS` | `8` | 一轮提问最多执行几步。3 到 4 步够用，调到 12 以上明显变贵 |

`.env` 在 `.gitignore` 里。

## 代码结构

text
app.py          Streamlit 界面：上传、Key、提问、渲染、导出 PDF
agent.py        主循环：意图判断 → 决策 → 执行 → 回答 → 写记忆
deepseek.py     DeepSeek 客户端，全项目只从这里调模型
executor.py     执行层：run_python 沙箱、run_sql 只读
prompts.py      四个提示词：系统规则、意图判断、决策、最终回答
data_utils.py   读 CSV / Excel，生成数据画像
tools.py        SQL 只读校验 + DuckDB 执行
memory.py       会话历史和分析上下文
report.py       PDF 报告
data/           放数据的地方，程序不强制读写
reports/        生成的图表和报告

依赖单向：`app.py → agent.py → executor.py → tools.py`，`agent.py` 另外用 `deepseek.py`、`prompts.py`、`memory.py`。想改回答风格或规则，基本都在 `prompts.py` 里改。

## 一次提问发生了什么

问题进来先判一次：这是不是针对当前数据集的提问。不是就直接回绝，后面都不走。

确认是分析请求后进循环。模型每轮只返回三种动作之一：`run_python`、`run_sql`、`done`。前两种交给 executor 执行，结果原样塞回下一轮提示词。收到 `done` 后，用同一批结果写最终回答。

mermaid
flowchart LR
    Q[问题] --> G{数据分析请求}
    G -- 否 --> R[拒绝]
    G -- 是 --> P[模型决定动作]
    P -- run_python --> E1[受限沙箱执行]
    P -- run_sql --> E2[DuckDB 只读查询]
    E1 --> P
    E2 --> P
    P -- done --> A[最终回答]


模型不产出数字，只产出代码。数字得从执行结果里来，写回答时也只让引用结果中出现过的值。

## 报告里有什么

点 Generate PDF Report 时生成，内容来自两部分。

前半段是数据集本身的描述，固定九节：概览、字段与类型、缺失值、重复行、数值统计、类别分布、相关性、质量检查、样例数据。这部分跟聊天无关，问多少个问题都一样。

后半段是这次会话的分析记录，标题叫「分析结果」，按提问顺序排列。每问过一次就多一条，包含：

- 当时的问题（原样）
- 模型写出的回答
- 这一步的结果表，前 10 行、最多 8 列
- 这一步画出来的图（如果有）

回答太长会截到 1500 字左右，不然一次深度分析就能凑出几十页。

几点行为要说清楚：

- 报告是点按钮那一刻的快照，不是边聊边写。生成之后再提问，旧 PDF 会作废，得重新点一次
- 被意图门挡掉的闲聊不进报告，那轮没有执行任何工具
- 换数据集会清空会话，报告也跟着失效

## 已知限制

- `run_python` 是进程内 exec。白名单 import、砍掉 `open`/`eval`/`exec` 这些内置函数、25 秒超时，能挡住手滑，挡不住刻意构造的逃逸（比如顺着函数对象的 `__globals__` 摸出去）。自己用或者给可信的人用没问题，真要开放给陌生用户，得先换成子进程或容器
- 数据是 `df.copy()` 一份进沙箱的。几十万行还行，上百万行内存会吃紧，步数多了更明显
- 每次 SQL 都新建 DuckDB 连接、重新注册视图。单次几十毫秒，数据大或者连着问很多次时会有点拖
- 结论对不对，取决于模型代码写得对不对。好在结果表和执行记录都摆在界面上，可以自己核对
- 一轮提问通常 2 到 10 次模型调用，步数调大费用跟着涨

## 碰到过的情况

- 报缺 openai：`pip install openai`
- 模型调用失败：Key 失效、额度用完，或者网络不通
- CSV 读不出来：用 Excel 另存成 UTF-8 的 CSV 再试
- Excel 读不了：多半是没装 openpyxl
- 等太久：把最大步数调到 3 到 4，或者先抽样
- 图表中文变方块：程序会去系统字体里找雅黑和黑体，机器上没中文字体就得自己装
- 关掉终端网页就打不开：服务跟着终端一起退了，重新 `streamlit run app.py`

## 还没做

- `run_python` 挪到子进程或 Docker
- 测试，尤其是沙箱和 SQL 校验这两块
- 接 MySQL 只读账号
- 相同问题的结果缓存，省点调用

## License

MIT，见 [LICENSE](LICENSE)。
