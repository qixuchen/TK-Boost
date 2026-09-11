# 代码与论文的不一致清单

本文汇总仓库实现与技术报告 `Arming_Data_Agents_with_Tribal_Knowledge_Tech_Report.pdf`
之间的**所有已知偏差**，以及虽与论文无关但会阻碍复现的工程缺陷。

与 [`algorithm_mapping.md`](./algorithm_mapping.md) 的分工：那份文档回答"论文的算法在代码里是哪个函数、
要传什么参数"，是**对照参考**；本文回答"哪里不一致、要不要改"，是**决策记录**。
`algorithm_mapping.md` 末尾那张 7 行的偏差表是本文的简版，两者冲突时以本文为准。

分组依据是**要不要改**：

| 组 | 含义 | 处理方式 |
| --- | --- | --- |
| **A** | 挡住主链路，不改跑不通或跑出来的不是论文方法 | 必须改，见 [`implementation_plan.md`](./implementation_plan.md) |
| **B** | 与论文实现不一致，但不影响主链路能否跑通 | 本轮不改，仅记录；解读实验结果时需要考虑 |
| **C** | 与论文无关的工程缺陷，但会显著增加复现成本 | 顺手修，属于阶段 1 |

---

## A 组 — 必须改

| # | 偏差 | 位置 | 论文依据 |
| --- | --- | --- | --- |
| A1 | Alg 5 没有完整实现体，被拆成两个各缺一半的函数 | `tkboost/__init__.py::sql`（有检索无 agent 回环）、`src/agents/sql_agent_runner.py:427::perform_refinement_and_revision`（有 agent 回环无检索） | Alg 5 |
| A2 | populate 写库用 9 列 header，与 `TKStore.HEADER` 的 10 列不兼容 | `tkstore/harness.py:1410`、`:1558` vs `tkboost/__init__.py:86` | Alg 2 Line 5 `TK-Store.insert(k,a)` |
| A3 | populate 写库路径固定为 BigQuery store，不按 engine 分流 | `tkstore/harness.py` 两处写 `config.MEMORY_INDEX_PATH`，而 `tkstore/config.py:26` 是 `MEMORY_INDEX_PATH = TKSTORE_BQ_PATH` | — |
| A4 | 另一个 populate 入口硬编码丢弃 agent 执行轨迹 | `tkstore/builder.py:389-397` 传 `processed_trace_text=None` | 经验元组 `e = (q, τ, s*)` 中的 `τ` |
| A5 | 没有任何地方强制 train / test 划分 | 全局 | Table 11：Spider2-SQLite 135 全量 / 34 train / 101 test |
| A6 | populate 没有 CLI 入口，只能 Python 调用 | `tkstore/builder.py`、`tkstore/harness.py` | — |

### A2 展开：为什么这条最危险

`TKStore.HEADER`（`tkboost/__init__.py:86`）是 10 列，`db` 在第 3 位：

```python
["mem_id", "instance_id", "db", "scope", "sql_operations",
 "table", "column", "data_type", "nulls", "rule"]
```

而 `run_diff_for_instance` 内嵌的两个写库函数（`tkstore/harness.py:1410`、`:1558`）
用的是 9 列，**缺 `db`**：

```python
["mem_id", "instance_id", "scope", "sql_operations",
 "table", "column", "data_type", "nulls", "rule"]
```

仓库里 5 个已有 CSV（`tkstore_bq.csv`、`tkstore_sf.csv`、`tkstore_sqlite.csv`、
`tkstore_example.csv`、`tmp/quickstart_tkstore_example.csv`）全部是 10 列。两种失败模式：

**新建文件**：写出 9 列 CSV，之后任何 `TKStore` 操作都会在 `_ensure_well_formed()` 抛
`ValueError: Store CSV is not well formed`。这种是**响亮的失败**，反而安全。

**追加到已有 10 列文件**：这种是**静默的数据损坏**。判断是否要补写 header 的条件在
`tkstore/harness.py:1520`：

```python
if not first_line or (expected_header not in first_line
                      and not first_line.startswith('mem_id')):
```

已有 10 列文件的首行确实以 `mem_id` 开头，于是 `not first_line.startswith('mem_id')` 为假，
整个 AND 为假，`not first_line` 也为假 —— 条件不成立，走 `:1529` 的 else 分支，**不重写 header**。
然后在 `:1544-1546` 把 9 个字段追加进 10 列表格：

```python
for i, r in enumerate(rows):
    mem_id = start_idx + i
    writer.writerow([mem_id] + r)   # r 有 8 个字段，+1 = 9
```

结果是整行右移一位错列：`scope` 的值落进 `db` 列、`sql_operations` 的值落进 `scope` 列，以此类推，
`rule` 正文落进 `nulls` 列。不报错，但这些行在检索时全部失效。

### A5 展开：论文未公开具体划分

Table 11 只给了数量（34 / 101），没给实例 ID 列表。所以我们只能自己按固定随机种子划分并记录下来，
**无法复现论文的确切划分**。这一点在报告结论里需要声明。

另外注意 populate 阶段会读 gold SQL 和 gold 执行结果，即**看得到答案**。
在同一个实例上先 populate 再评测等于数据泄漏，划分必须在跑 populate 之前就固定。

---

## B 组 — 记录但不改

### B 组里唯一需要你操作的一条

| # | 偏差 | 位置 | 影响 |
| --- | --- | --- | --- |
| B1 | `use_llm_filtering` 默认 `False`，即默认**跳过** `FilterKnowledge` | `tkboost/__init__.py:121`、`:538`；`tkstore/tagger_index.py:1042` | 论文 Alg 4 最后一步 `K_ret ← FilterKnowledge(s, K_cand)` 是固定步骤，不是可选项 |

这条归在 B 组是因为不用改代码，但**跑实验时必须显式传 `use_llm_filtering=True`**，
否则跑的不是论文方法。已在实现计划里定为默认开启。

### 检索逻辑

| # | 偏差 | 位置 | 说明 |
| --- | --- | --- | --- |
| B2 | `table` / `column` 不参与检索过滤 | `tkstore/tagger_index.py:492::search_index_for_sql` | 论文 Fig. 4 的特征集 `X` 是 4 维（SQL Keywords / Tables / Columns / Data Type）。代码实际只用 3 维：`sql_operations`、`data_type`、`nulls`。`table` 在 `:537` 读出、`:655` 放进结果，从不作为筛选条件；`column` 同理（`:538`、`:656`） |
| B3 | 多出一个论文没有的 `nulls` 维度 | 同上 | 代码用 `nulls` 顶替了论文的 Tables + Columns 两维 |
| B4 | 特征抽取用正则而非 SQL parser | `tkstore/tagger_index.py:395::_infer_data_types_from_sql`、`:472::_detect_null_handling_in_sql` | 论文说用 SQL parser 抽 keywords / tables / columns。`sqlglot` 已在依赖里但此处未使用 |
| B5 | 不查库确定列的真实数据类型 | 同上 | 论文说查库确定 Data Type，代码靠文本推断 |
| B6 | 多一条论文没有的降噪启发式 | `tkstore/tagger_index.py:608-620` | generic scope 且只命中一个常见操作（`select`/`where`/`join`/`from`）时直接丢弃。会压掉一部分本该召回的通用规则 |
| B7 | `generic_only` 默认值三处不一致 | `search_index_for_sql` = `True`、`MemoryRetriever.retrieve`（`:1042`）= `True`、`TKStore.retrieve`（`tkboost/__init__.py:120`）= `False` | 用 `MemoryRetriever` 不改参数只能拿到通用规则，db 专属规则全丢。实现计划里统一显式传 `generic_only=False` + `db=` |

### Populate 与存储

| # | 偏差 | 位置 | 说明 |
| --- | --- | --- | --- |
| B8 | `search_keywords` 被 LLM 生成但从不落盘 | `tkstore/tagger_index.py:31::generate_tagged_memories_json`，字段只出现在 prompt 字符串（`:60`、`:103-105`、`:220`、`:239`、`:244`） | 既不在 `TKStore.HEADER` 也不在 harness 的 header 里。等于每条规则白花一次 LLM 调用的输出。同一轮里对 `sql_operations` 的 canonical token 补全是**有**落盘的 |
| B9 | 缺 agent SQL 时会用 LLM 凭空造一份 draft | `tkstore/builder.py:234::_generate_agent_sql` | 论文的 `e = (q, τ, s*)` 里 agent SQL 是真实 agent 产物。造出来的 draft 与真实 agent 的错误分布不同，学到的规则会偏 |

### Augmentation

| # | 偏差 | 位置 | 说明 |
| --- | --- | --- | --- |
| B10 | `run_refiner` 是 25 轮探库 agent 循环，论文 `Feedback` 是单次 LLM 调用 | `src/agents/cte_refiner.py:286::run_refiner`，`max_turns=25` 在 `sql_agent_runner.py:416`、`:471`、`:563` 三处传入 | 论文 Alg 5 的 `Feedback(q, c_i, K)` 是把知识转成自然语言修正指令的一次调用。代码里它会连库做 PRAGMA、抽样、DISTINCT / NULL 检查，**做的比论文多**。会让增强侧结果偏好，baseline / augmented 的差值里混入了探库带来的增益 |
| B11 | 每个 CTE 的修订有次数上限，且失败静默跳过 | `src/agents/sql_agent_runner.py:503` 的 `for attempt in range(1, 6)` | 论文外层 `while is_final = False ∨ f ≠ ∅` 没有迭代上限。代码是"最多 5 次尝试拿到 1 个能执行的新 `<solution>`"，`:535` 一旦成功就 break；5 次都失败就带着**未修改的 `final_sql`** 进入下一个 CTE，论文没有这条放弃路径。注意这 5 次预算还会被空响应、纯 `<sql>` 探针、执行报错消耗掉 |
| B12 | `run_refiner` 的两个知识相关参数是死参数 | `src/agents/cte_refiner.py:286` 签名与 `:736` CLI，函数体内零引用 | `use_all_rules` 和 `tribalknowledge_generic_only` 是预留未实现的钩子。看签名会误以为知识注入已生效 |
| B13 | `is_final` 用 `<solution>` 标签检测代替布尔量 | `detect_solution` | 功能等价，仅记录 |

### 三层循环的轮数（澄清）

容易混淆，单独列清：

| 循环 | 上限 | 位置 | 语义 |
| --- | --- | --- | --- |
| 外层 ReAct | 25 | `sql_agent_runner.py:306`、`:318` | agent 自由思考 / 探库直到吐 `<solution>`。这是运行日志里 `TURN n/25` 的那个 |
| refiner 自身 | 25 | `sql_agent_runner.py:416`、`:471`、`:563` | refiner 的探库循环，见 B10 |
| 每 CTE 修订 | 5 | `sql_agent_runner.py:503` | 拿到 1 个可执行新解的尝试预算，见 B11 |

---

## C 组 — 工程缺陷（与论文无关，但阻碍复现）

| # | 缺陷 | 位置 | 影响 |
| --- | --- | --- | --- |
| C1 | 库路径解析吞掉所有异常 | `src/utils/db_paths.py:16-17` 的 `except Exception: return None` | `get_database_path` 主动抛的 `FileNotFoundError` 带着"找了哪些路径"的信息，全被吞掉，只剩一句没有信息量的 `Could not resolve SQLite DB`。排查成本全在这里 |
| C2 | 用动态 exec + 相对路径加载模块 | `src/utils/db_paths.py:11` 的 `spec_from_file_location("cte_refiner", "src/agents/cte_refiner.py")` | 相对路径意味着 cwd 必须是仓库根目录；而且每解析一次库路径就 `exec_module` 重跑整个 `cte_refiner.py` |
| C3 | 依赖方向倒置 | `src/utils/db_paths.py` → `src/agents/cte_refiner.py` | 库路径解析和 CTE refiner 毫无逻辑关系，`get_database_path` 放在 refiner 里本身就不合理，应该反向 |
| C4 | 数据库靠 134 个 symlink 硬凑一题一目录 | `data/spider2/<instance_id>/` | 见下方展开 |
| C5 | 评测脚本对空预测结果无容错 | `evaluation/evaluate.py:497` 直接 `item['score']`，`:521` 起直接 `df_rows["score"]` | 没有实例匹配上时 `df_rows` 是无列空表，直接 `KeyError: 'score'`，而真正的原因（`--result_dir` 尾部斜杠导致实例 ID 解析失败、或 agent SQL 执行失败导致 CSV 为空）完全看不出来 |
| C6 | 过期注释 | `src/agents/sql_agent_runner.py:725` 写 `Run refinement with reduced max_turns` | 下面传的还是 25，没有 reduce |
| C7 | `evaluation/README.md` 路径写错 | `evaluation/README.md:20-23` | 说 gold SQL 在 `exec_result/*.sql`，实际在 `evaluation/gold/sql/`（256 个文件）。另外示例里的 outputs 目录名是虚构的 |
| C8 | populate 要求 gold SQL 在输出目录里，但 runner 不保证产出 | `tkstore/harness.py:1067::run_diff_for_instance` 要求 `gt_query.sql` 或 `{instance_id}.sql` | 实测 5 个输出目录里 **2 个没有** gold SQL（`local002`、`local007`）。权威来源是 `evaluation/gold/sql/{id}.sql`，populate 应该直接读那里 |

### C4 展开：官方映射表让 symlink 完全没必要

当前布局的问题：135 个实例目录里塞了 134 个 symlink + 1 个真实文件，
指向 `~/spider2-localdb/` 下的 **30** 个真实库（平均 4.5 题一个库，冗余 4.5 倍）。
symlink 是我用启发式名字匹配（小写、去 `_` 和 `-`）一次性脚本生成的，
逻辑没进代码库，新增实例要重跑；且 symlink 存的是绝对路径，换机器全废。

但 `~/spider2-localdb/local-map.jsonl` 就是 **Spider2 官方的实例→库映射**，
单行 JSON 对象 `{instance_id: db_basename}`。已验证：

- 135 个条目 → 30 个唯一库名，与 `data/spider2/` 的 135 个目录完全一致
- 每个映射值加 `.sqlite` 后缀都精确对应共享目录里的真实文件，**0 个缺失**
- 共享目录里没有任何未被引用的孤立库
- 不规则命名（`Db-IMDB`、`sqlite-sakila`、`E_commerce`）都被精确覆盖

也就是说这是一次**精确查表**，那套归一化匹配启发式可以整个扔掉。
另外 `data/spider2/local007/Baseball.sqlite` 这个唯一的真实文件，
共享目录里有同名文件，是纯重复的 30MB。

顺带一提 `data/instance_db_mapping.csv` 不适合当解析依据：它把 BigQuery 和 Snowflake
实例混在一起，且库名与实际文件名的大小写、分隔符都不一致 —— 这正是当初需要启发式匹配的原因。

---

## 附注

### BIRD / minidev 现状

本轮范围是 Spider2，BIRD 暂不做。记录一下现状备查：DB 路径解析和 engine 推断**已经有支持**，
分布在三处 —— `src/agents/cte_refiner.py:65-75`（解析
`data/minidev/MINIDEV/dev_databases/{db_id}/{db_id}.sqlite`）、
`src/utils/agent_utils.py:189`（`minidev` 前缀 → sqlite）、
`tkstore/harness.py:1163`、`:1188-1196`（populate 路径同样认）。

缺的是三块：instance loader（BIRD 是单个 JSON 数组，字段
`question_id/db_id/question/evidence/SQL/difficulty`）、gold 加载（BIRD gold 在
`mini_dev_sqlite_gold.sql`，每行 `SQL<TAB>db_id`，行序对应 JSON 顺序，没有预算好的执行结果）、
以及 `evaluation/evaluate.py` 里对应的 BIRD 评测函数（现在只有 `evaluate_spider2sql`）。
另外 `tkstore/config.py` 没有 BIRD 的 store 路径，BIRD 规则会落进 `tkstore_sqlite.csv` 和 Spider2 混在一起。

规模也对不上：手头的 minidev 副本是 **500 题 / 11 个库**，而论文 Table 11 的 BIRD 是
**283 题（72 train / 211 test）**。minidev 不是论文那个子集，数字不能直接对标。

BIRD 有一点反而比 Spider2 有利：平均 45 题一个库，db 专属规则的复用率天然很高，
而 Spider2-SQLite 是 4.5 题一个库。以后要看 db-scoped 规则的效果，BIRD 是更好的设置。
