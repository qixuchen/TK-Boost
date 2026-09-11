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
| A7 | SQLite 路径上 `db` 列被静默写成 `all`，库专属规则退化成全局规则 | `tkstore/harness.py:1233` 只在 snowflake/bq 时传 `db_name`，`:1395` 又硬编码 `db_name=None`；`tkstore/builder.py:39` 的 `ir.get("db", db_name or "all")` 因此必然落到 `"all"` | Fig. 4 的 db 维度 / Alg 4 的 `r[x]∩c[x]≠∅` |

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

### A5 展开：论文未公开划分，但 train 集可从随仓库发布的 store 复原

Table 11 只给了数量（34 / 101），正文没给实例 ID 列表。**但上游随仓库发布的
`tkstore/tkstore_sqlite.csv` 事实上泄露了 train 集**：它的 `instance_id` 列有 32 个不同的
local 实例，而论文 Table 13 明确写着 SQLite 的迭代统计是在 `n=34` 个 training queries 上做的。
另外两个 store 也对得上（Snowflake 26 个 vs 论文 train 29 个）。差的那两个实例最可能是
产出了 0 条规则。

所以正确的做法是**采用这 32 个实例当 train**，而不是自己随机划分。这比 seed=0 的随机划分离
论文近得多。相应地 test 是剩下的 103 个（论文 101）。

我们验证过这个 store 确实是在 Spider2 上跑出来的（详见下方"store 溯源验证"），
所以这个复原是可信的。

另外注意 populate 阶段会读 gold SQL 和 gold 执行结果，即**看得到答案**。
在同一个实例上先 populate 再评测等于数据泄漏，划分必须在跑 populate 之前就固定。
如果阶段 3 直接使用上游的 store，那么它涉及的这 32 个实例**必须**从 test 里排除。

### A7 展开：为什么这条会让阶段 3 的验收假通过

链条上有三个环节共同导致 `db` 信息丢失：

1. `harness.py:1233` 传 `db_name=db_id if (engine == "snowflake" or engine == "bq" or engine == "bigquery") else None`
   —— SQLite 走 `None` 分支。
2. `harness.py:1395` 调 tagger 时又硬编码了一次 `db_name=None`。
3. tagger 的 prompt 规定 `index_rows` 的字段是 `scope / sql_operations / table / column /
   data_type / nulls / applies_when / rule`，**没有 per-row 的 `db` 字段**。

所以 `builder.py:39` 的 `str(ir.get("db", db_name or "all") or "all")` 必然落到默认值，
`db_name=None` 就等于全部写成 `db="all"`。

危险之处在检索侧的这条判定（`tagger_index.py:568`）：

```python
db_matches = (row_db == 'all' or row_db == db.lower())
```

`db="all"` 且 `scope="db"` 的行**对任何数据库都命中**。于是"能召回到 db 专属规则"这条验收
确实会通过，但通过的原因是通配而不是库匹配，34 个 train 实例学到的库专属知识实际退化成了
全局规则。阶段 2.2 必须显式传 jsonl 的 `db` 字段（实测 135/135 个 local 实例都有该字段），
并把验收改成"`db` 列不等于 `all`"才有区分力。

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
| C5 | 评测脚本对空预测结果无容错 | `evaluation/evaluate.py:497` 直接 `item['score']`，`:521` 起直接 `df_rows["score"]` | 没有实例匹配上时 `df_rows` 是无列空表，直接 `KeyError: 'score'`，而真正的原因（`--result_dir` 尾部斜杠导致实例 ID 解析失败、或 agent SQL 执行失败导致 CSV 为空）完全看不出来。**已于阶段 1.4 修复** |
| C13 | `evaluate.py` 有一个死 import，导致脚本在缺该包的环境里完全无法启动 | `evaluation/evaluate.py:24`（原）`import duckdb` | `duckdb` 全文只出现在这一行，从未被使用，是从上游 Spider2 的 `evaluate.py` 继承来的。但它是模块级 import，所以没装 duckdb 的环境里 `evaluate.py --help` 都跑不起来。**已于阶段 1.4 删除**；`requirements.txt` 里的 `duckdb>=0.9.0` 也因此不再是评测的必需依赖 |
| C6 | 过期注释 | `src/agents/sql_agent_runner.py:725` 写 `Run refinement with reduced max_turns` | 下面传的还是 25，没有 reduce |
| C7 | `evaluation/README.md` 路径写错 | `evaluation/README.md:20-23` | 说 gold SQL 在 `exec_result/*.sql`，实际在 `evaluation/gold/sql/`（256 个文件）。另外示例里的 outputs 目录名是虚构的 |
| C8 | populate 要求 gold SQL 在输出目录里，但 runner 不保证产出 | `tkstore/harness.py:1067::run_diff_for_instance` 要求 `gt_query.sql` 或 `{instance_id}.sql` | 实测 5 个输出目录里 **2 个没有** gold SQL（`local002`、`local007`）。权威来源是 `evaluation/gold/sql/{id}.sql`，populate 应该直接读那里 |
| C9 | 一共有 **4 条**互相重复的数据库路径解析逻辑，其中 harness 那条是坏的 | `src/utils/db_paths.py`、`src/agents/cte_refiner.py:63`、`tkstore/harness.py:1186-1204`、`src/agents/sql_agent_runner.py:596` | 见下方展开 |
| C10 | 仓库零测试基础设施 | 全局 | 没有任何测试文件、没有 conftest、`pytest` 不在 `requirements.txt`。任何重构都没有安全网 |
| C11 | `data/instance_db_mapping.csv` 是死数据 | 全局 | 代码里零引用。它把 BigQuery / Snowflake 实例混在一起，且库名与真实文件名的大小写、分隔符不一致，不适合当解析依据 |
| C15 | `_persist_via_tkstore` 只给 `sql_operations` 做了 list→分号转换，`table`/`column`/`data_type` 是裸 `str()` | `tkstore/builder.py:41-44` | tagger 的 prompt 只规定 `sql_operations` 是数组，但上游 store 里 `table` 有 9 行、`data_type` 有 6 行是分号分隔的多值。如果 tagger 把这些字段输出成 JSON 数组，`str(["customers","orders"])` 会写出 `"['customers', 'orders']"` 这种 Python list repr，检索时 `row['table']` 永远匹配不上任何真实表名。阶段 2.2 需要加防护 |
| C14 | 检索的 `instance_id` 过滤是彻底失效的 no-op | `tkstore/tagger_index.py:532` 的 `instance_id = row.get('instance_id')` 在循环里覆盖了同名形参 | 之后 `:578` 的判定 `row_instance != instance_id.lower()` 恒为假，因为 `instance_id` 已经是当前行自己的值。实测构造两条不同 `instance_id` 的规则、只请求其中一条，两条都被召回。论文 Alg 4 不按 `instance_id` 过滤，所以不挡主链路，但任何依赖这个参数的调用都会静默拿到全量结果 |
| C12 | external knowledge 文件缺失时静默返回 `None` | `src/utils/agent_utils.py:84-91::load_external_knowledge` | 声明的文件不存在时直接 `return None`，不打印任何警告。13 个 local 实例声明了 external knowledge（`local003` 需要 `RFM.md` 等），文件一直不在 `data/spider2/<id>/` 下，于是这 13 个实例的输入长期不完整而无人发现。文件已于阶段 1.2 补齐，但**缺失时无告警这一点仍未修**，下次换机器同样会静默退化 |

### C9 展开：harness 那条 fallback 从来没命中过

`tkstore/harness.py:1199` 拼接的是：

```python
example_folder = os.path.join(data_base_folder, str(instance_id))
```

`config.DATA_BASE_FOLDER` 是 `'data'`，所以拼出来是 `data/local007`，
而真实布局是 `data/spider2/local007` —— 少了一层。这条 fallback 一直是死的，
只有调用方显式传 `db_path` 时 populate 才能拿到数据库。

阶段 2 的 populate 正好走这条路，所以这是个埋着的雷，已决定在阶段 1.1 一并统一到新解析器。

四条路径的失败契约也不一致，绕成一个圈：`get_database_path` 抛 `FileNotFoundError`
→ `db_paths.resolve_sqlite_db_path` 用 `except Exception` 吞掉返回 `None`
→ `src/executors/factory.py:29-30` 又把 `None` 转回 `FileNotFoundError`。
异常里携带的"试过哪些路径"信息在中间那一步全部丢失（即 C1）。

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

### gold SQL 可得性约束（不是仓库缺陷，是数据集限制）

这一条不属于 A/B/C 任何一组 —— 它不是代码问题，而是公开数据本身给不了论文所需的输入。
但它是阶段 2 最硬的约束，必须记录。

论文 Algorithm 3 的第 5 行是 `(δ,s) ← MakeCorrection(q, D, s, s★, R, R★)`，
`s★`（gold SQL）是显式输入；第 3 行 `R★ ← ExecSQL(D, s★)` 还要执行 `s★` 才能得到 `R★`。
也就是说论文的 populate 需要全部 train 实例的 gold SQL。

而公开的 spider2-lite 只发布了一部分 gold SQL：

| 类型 | 实例数 | 有 gold SQL | 覆盖率 | 有 gold 结果 |
| --- | --- | --- | --- | --- |
| bq | 189 | 134 | 70.9% | 180（95.2%）|
| sf_bq | 180 | 76 | 42.2% | 180 |
| **local（SQLite）** | **135** | **24** | **17.8%** | **135（100%）**|
| ga | 25 | 14 | 56.0% | 25 |
| sf | 18 | 8 | 44.4% | 18 |
| 合计 | 547 | 256 | 46.8% | 538 |

上游 `Spider2` 仓库（核对时是 2026-08-12 的提交）全盘搜索后确认没有别处的 local gold SQL，
`spider2-lite.jsonl` 本身也只有 `instance_id / question / db / external_knowledge` 四个字段。
SQLite 子集覆盖率最低这一点符合防刷榜的惯例：gold **执行结果**全量公开（用于评测），
gold **SQL** 只公开一部分。

两个直接推论：

1. **评测完全不受影响。** `evaluate.py` 的比对基准是 `evaluation/gold/exec_result/*.csv`，
   local 实例 135/135 齐全。gold SQL 只在 populate 里当 `s★` 用。
2. **论文作者用了非公开的 gold SQL。** 从 store 复原的 32 个 train 实例里，只有 7 个
   （`local004`、`local039`、`local075`、`local099`、`local163`、`local197`、`local301`）
   有公开 gold SQL。剩下 25 个没有，作者却产出了规则。

因此我们无法在全部 train 实例上复现忠实的 Alg 3。本轮采取的对策见实现计划的阶段 2：
阶段 3 直接使用上游 store 作为忠实复现的输入，我们自己的 populate 在那 7 个有 gold SQL 的
实例上验证实现正确性，并与上游同实例的规则逐条对比。

也不建议改用 BigQuery 子集换取更高的 gold SQL 覆盖率：
`src/executors/bigquery_credential.json` 不存在（需要 GCP 项目与计费），BQ 查询按扫描量真实
收费而 agent 每个实例最多跑 25 轮探针，gold 结果只有 95.2%，且阶段 1 的整套基础设施
（`db_paths`、135 实例映射、splits）都是 SQLite 专用的。

### store 溯源验证：上游 store 确实是在 Spider2 上跑出来的

因为阶段 3 计划直接使用 `tkstore/tkstore_sqlite.csv`，先验证了它的来源。结论是压倒性的：

| 检验 | 结果 |
| --- | --- |
| 32 个 `instance_id` 是否都是真实 spider2-lite local 实例 | 32/32 |
| `db` 列与 jsonl 的 `db` 字段一致（忽略大小写） | 63/66 条 `scope='db'` 行一致，3 条是 tagger 写错库名 |
| `table` 列的具体表名存在于真实 SQLite schema | 69.6%；未命中的是 `customer_months`、`country_averages` 这类 **CTE 名**，符合论文的 per-CTE 设计 |
| 规则正文提到**自己那个库**的真实表名 vs 随机另一个库 | 0.70 vs 0.09 个/条，**偏向 7.7 倍** |
| 规则内容与该实例 `question` 文本的对应性 | 抽 3 个全部精确对应（见下）|
| 引擎特异性 | 规则大量引用 SQLite 专有的 `strftime`、`julianday`、整数除法语义 |
| 实例数 vs 论文 | SQLite 32 vs Table 13 的 `n=34`；Snowflake 26 vs Table 11 的 29 |

内容对应性是最硬的一条：

- `local004` 问 "average payment per order 和 customer lifespan in weeks，除以七" →
  规则是 AOV 的 SQLite 整数除法与 `CAST ... AS INTEGER` 输出类型
- `local007` 问 "debut 与 final game 日期之间的年、月、日差值" →
  规则是 `strftime` 分量提取优于 `julianday/365`、以及 `year + month/12 + day/365` 的一致性
- `local075` 问 "加入购物车但未购买的次数" →
  规则是用 `EXISTS`/`NOT EXISTS` 而非硬编码常量区分 abandoned 与 completed

另有一条直接痕迹：`local004` 的规则正文写着 "matched gold behavior better than..."，
说明生成时确实做过与 gold 的比对。

**结构兼容性**（决定我们能否与它逐条对比）：列与 `TKStore.HEADER` 逐字节相同，
`TKStore(path).rows()` 能直接读出 118 行；`scope` 只有 `db`(66) / `generic`(52)，
**没有一条 `question` 行**；`sql_operations` 为分号连接的小写词；`mem_id` 是连续的 0..117；
`rule` 内无换行。这些都与 `_persist_via_tkstore` 的输出形态一致。

需要注意上游 store 自身也有漂移：`data_type` 出现了 `id`、`id;numeric`、`date;id` 等
**超出 tagger prompt 规定取值集**（`str/int/numeric/date/bool/all/unspecified`）的值，共 6 行。
这说明 tagger 会偏离自己的规范，我们复现时也会，对比时不必因此判定不一致。

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
