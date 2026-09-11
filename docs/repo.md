# TK-Boost 项目说明

## 一句话概述

TK-Boost 是 UC Berkeley 提出的、面向 NL2SQL（自然语言转 SQL）智能体的外挂式增强框架。它不通过重新训练模型来提升效果，而是从历史中“草稿 SQL 与正确 SQL 的执行差异”学习可复用、可解释的修正规则（tribal knowledge），并在后续生成的 SQL 上检索和应用这些规则。

项目对应论文：[Arming Data Agents with Tribal Knowledge](https://arxiv.org/abs/2602.13521)，许可证为 MIT。

## 要解决的问题

通用 LLM 往往能写出结构合理的 SQL，却不了解某个真实数据库的隐含约定和常见陷阱，例如：

- 字段的业务语义与实际单位；
- 空值、日期、大小写或聚合的处理规则；
- 表之间正确的连接键；
- 特定数据库引擎的 SQL 方言。

TK-Boost 将这些错误中获得的经验保存为可审阅的文本规则，而不是隐藏在模型权重中。README 报告论文实验中，对 Spider2 和 BIRD 的不同 agent 可带来约 `+3.6%` 至 `+16.9%` 的最大准确率增益；这些是论文结果，实际提升取决于数据、模型和积累的规则质量。

## 核心工作流

```text
训练样例
  question + gold SQL +（可选）agent 草稿 SQL / 执行结果
      │
      ├─ 在目标数据库执行并比较 agent 与 gold 的结果
      ├─ 由 LLM 分析差异，提出并验证修复方向
      ├─ 将可泛化的结论提炼为规则并打标签
      ▼
TKStore CSV 知识库
  mem_id / 数据库 / 表 / 列 / SQL 操作 / rule ...
      │
      ├─ 输入新的草稿 SQL（或由问题先生成草稿）
      ├─ 按 SQL 操作、表、列等标签检索相关规则
      └─ 用规则和数据库探针迭代精炼 SQL
      ▼
可执行的 refined SQL + 执行预览 + 使用的规则
```

### 1. 知识生成

`tkboost.generate()` 接受单个 `example.json` 或包含多个样例的目录。每个样例至少描述 `example_id`、`engine`、`question` 和 `gold_sql_path`。

核心编排在 `tkstore/builder.py`；`tkstore/harness.py` 负责通过 LLM 比对执行差异、生成和验证修复思路；`tkstore/tagger_index.py` 将规则打上范围、表、列、数据类型、空值和 SQL 操作等标签。最终规则存入 CSV，而非数据库服务。

### 2. SQL 精炼

`tkboost.sql()` 接收已有 `draft`，或者先根据 `question` 生成一条草稿 SQL。随后它从 TKStore 检索规则并精炼查询：

- 对 SQLite：会解析 CTE，对每个 CTE 和最终查询分别运行多轮数据库探针与精炼；
- 对没有 CTE 的 SQLite SQL：对整条语句进行多轮精炼；
- 对 BigQuery、Snowflake、PostgreSQL 或未提供 SQLite executor 的情形：使用一次 LLM 精炼作为回退路径。

这意味着项目虽然支持多种执行器，但当前最完整的、带数据库探针的修正流程是 SQLite。

### 3. 观察和评估

- `TKStore.visualize()` 在本地启动知识库和 debug trace 的查看界面（默认端口 `8501`）。
- `evaluation/evaluate.py` 用执行结果等方式评估输出；完整 Spider2 复现实验需要按 `evaluation/README.md` 另行准备数据。

## 对外 API

公共接口定义于 `tkboost/__init__.py`：

- `tkboost.init()`：选择 OpenAI / Azure OpenAI，并配置模型和认证环境变量。
- `tkboost.generate()`：从训练样例生成或追加 tribal knowledge 到 CSV 知识库。
- `tkboost.sql()`：检索知识库规则并修正草稿 SQL；可返回执行预览和规则使用情况。
- `TKStore`：CSV 知识库的轻量封装，支持读取、增删改、检索和可视化。
- `SQLAgent`：对内置 ReAct SQL agent 的封装，可由自然语言问题生成可执行的草稿 SQL。
- `SQLiteExecutor`、`PostgresExecutor`、`SnowflakeExecutor`、`BigQueryExecutor`：统一的 SQL 执行器接口，均提供 `execute(sql)`。

TKStore 的固定列为：

```text
mem_id, instance_id, db, scope, sql_operations, table,
column, data_type, nulls, rule
```

## 技术组成

- Python 3 项目，以源码目录和脚本直接运行；当前没有 `pyproject.toml` 或发布包配置。
- LLM 调用：LiteLLM，支持 OpenAI 与 Azure OpenAI。
- SQL 处理：sqlglot、DuckDB。
- 数据库连接：SQLite、PostgreSQL（psycopg）、Snowflake、BigQuery。
- 数据处理：pandas、datasets、tqdm。
- 可视化：项目自带的本地 HTTP dashboard。

完整依赖见 `requirements.txt`。

## 目录地图

```text
TK-Boost/
├── tkboost/                 # 面向使用者的 SDK、TKStore 与本地 dashboard
├── tkstore/                 # 知识库构建、规则标注/检索、样例及默认路径配置
├── src/
│   ├── agents/              # ReAct SQL agent、CTE refiner、prompt
│   ├── executors/           # 多数据库执行器和执行器工厂
│   └── utils/               # SQL/CTE 解析、路径和认证等工具
├── data/                    # Spider2 元数据及 BigQuery/Snowflake schema 上下文
├── evaluation/              # Spider2 评估脚本和说明
├── scripts/                 # schema 上下文生成等辅助脚本
├── docs/                    # API 文档
├── quickstart.py            # 端到端示例入口
└── demo.ipynb               # Notebook 演示
```

值得优先阅读的文件：

- `README.md`：项目目标、安装、Quick Start、实验结果和运行前提。
- `quickstart.py`：最小端到端用例，串联知识生成和 SQL 精炼。
- `tkboost/__init__.py`：公共 API 的真实行为和参数。
- `tkstore/builder.py`、`tkstore/harness.py`、`tkstore/tagger_index.py`：知识生成与规则检索主链路。
- `src/agents/cte_refiner.py`：SQLite 的多轮 CTE 级精炼机制。
- `src/agents/sql_agent_runner.py`：内置 ReAct NL2SQL agent。
- `src/executors/factory.py`：执行器选择与创建。
- `docs/API_SUMMARY.md`、`evaluation/README.md`：接口及评测细节。

## 运行方式

这是一个本地 Python 库/脚本项目，没有 Docker 或常驻服务的部署配置。通常在仓库根目录执行：

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt

# 需先按照 README 下载 Baseball.sqlite 到 tkstore/example/
export OPENAI_API_KEY="..."
python quickstart.py
```

也可改用 Azure OpenAI，所需环境变量为：

```text
AZURE_API_KEY
AZURE_API_BASE
AZURE_API_VERSION
```

默认模型由代码决定：OpenAI 为 `gpt-5`，Azure 为 `azure/gpt-5`；这与部分文档中出现的较旧模型示例并不完全一致，接入时应以 `tkboost.init()` 的实际参数为准。

## 外部数据与凭证

- Quick Start 依赖 Spider2 的 `Baseball.sqlite`，该数据库不随仓库分发，需要按 README 下载。
- 完整 Spider2/BIRD 数据集和部分评测资源也需要自行准备。
- Snowflake 与 BigQuery 需要用户提供凭证 JSON；PostgreSQL 可使用 DSN 或凭证。
- `.env`、数据库文件、credential/key/password 类文件均被 `.gitignore` 排除，不应提交密钥或本地数据。

## 当前边界与使用注意

1. 知识库存储为 CSV，易于查看和编辑，但未提供并发控制、服务化、版本治理或大规模检索能力。
2. 知识生成和精炼均会调用 LLM；Quick Start 预计约需 5–10 分钟，也会产生模型调用成本。
3. “引擎无关”主要体现在执行器与规则检索层；最深入的逐 CTE 探针精炼目前只针对 SQLite。
4. 项目使用的是相对源码导入和本地路径约定，建议从仓库根目录运行。
5. 规则质量依赖训练样例中的 gold SQL、执行差异以及 LLM 的归纳结果；规则应通过 debug trace、可视化和实际评测持续审查。
