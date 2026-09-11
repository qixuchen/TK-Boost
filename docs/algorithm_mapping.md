# 论文算法与代码实现对照

本文把技术报告 `Arming_Data_Agents_with_Tribal_Knowledge_Tech_Report.pdf` 中的
Algorithm 2（Populate）、Algorithm 4（Retrieve）、Algorithm 5（Agent Augmentation）
逐条对到仓库里的具体函数，并写清每个函数需要什么输入。

结论先行：**三个算法在代码里都有实现，但分散在两套互不相通的调用链上。**

- `tkboost/` 这套 SDK（`quickstart.py` 走的路径）实现了 populate → retrieve → augment 闭环，
  但不产出 `evaluation/evaluate.py` 需要的目录结构。
- `src/agents/sql_agent_runner.py` 这套评测链路能产出评测结果，但整个文件不 import 任何
  `tkstore` 模块，也就是说它的 refinement 阶段没有用到 tribal knowledge。

---

## 总览

| 论文 | 语义 | 主要实现 | 入口 | 状态 |
| --- | --- | --- | --- | --- |
| Alg 3 `GetCorrectionStatements(e)` | 迭代对比 agent SQL 与 gold SQL，产出 correction statements | `tkstore/harness.py::generate_memory_diff_first_turn` | Python 调用 | 已实现 |
| Alg 2 `Populate(E)` | 把 correction statements 转成知识 + 适用条件并入库 | `tkstore/builder.py::build_knowledge_from_example` 或 `tkstore/harness.py::run_diff_for_instance` | `tkboost.generate()` / Python 调用 | 已实现，两个入口行为不同 |
| Alg 4 `Retrieve(s)` | 按 query features 匹配适用条件 + LLM 过滤 | `tkstore/tagger_index.py::search_index_for_sql` + `_llm_filter_relevant_rules`，封装为 `MemoryRetriever.retrieve` | `MemoryRetriever(csv).retrieve(...)` | 已实现，特征维度少于论文 |
| Alg 5 `Agent Augmented with TK Store` | 逐 CTE 检索知识 → 转成 feedback → 追加到 agent context → agent 自行修正 | 拆成两半：`tkboost/__init__.py::sql`（有检索，无 agent 回环）+ `sql_agent_runner.py::perform_refinement_and_revision`（有 agent 回环，无检索） | `tkboost.sql()` / `--refine-cte` | **没有任何单一入口完整实现** |

论文里蓝色标注的 LLM 子过程（`MakeCorrection`、`GenTKRow`、`FilterKnowledge`、`Feedback`）
在代码里都是 `litellm.completion` 调用，prompt 内联在对应函数里。

---

## Algorithm 2 — Populate(E)

论文伪码：

```
foreach e ∈ E do
    K ← GetCorrectionStatements(e)          # Alg 3
    foreach k ∈ K do
        (k, a) ← GenTKRow(q, D, k)          # 知识 + 适用条件
        TK-Store.insert(k, a)
```

### 逐阶段对应

| 论文步骤 | 代码 | 说明 |
| --- | --- | --- |
| `GetCorrectionStatements(e)` | `tkstore/harness.py::generate_memory_diff_first_turn` | 多轮 LLM 循环，执行候选 SQL 并与 gold 结果比对，直到 `MATCH_OK`；输出含 `CUMULATIVE_EDITS` / `MINIMAL_REQUIRED_EDITS` / `CLEAN_SUMMARY` 的文本 |
| correction → 知识语句 | `tkstore/harness.py::generate_rules_from_diff` | 把 diff 文本转成 `DATABASE_MEMORIES` 和 `GENERIC_MEMORIES` 两段 |
| `GenTKRow`（知识 + 适用条件） | `tkstore/tagger_index.py::generate_tagged_memories_json` | 两轮 LLM：先产结构化 JSON，再用 canonical token 列表补全 `sql_operations` / `search_keywords` |
| `TK-Store.insert` | `tkboost/__init__.py::TKStore.insert_many`（builder 路径）或 `tkstore/tagger_index.py::MemoryIndex.append_tagged`（harness 路径） | 两条路径写库方式不同，见下 |

### 入口 A：`build_knowledge_from_example`

```python
tkstore/builder.py:297
def build_knowledge_from_example(
    example_json_path: str,
    store: Optional[str] = None,
    index_path: Optional[str] = None,   # legacy alias
    tkstore_obj: Optional[Any] = None,
    executor: Optional[Any] = None,
    model: str = "azure/o4-mini",
    draft_sql_model: Optional[str] = None,
    max_turns: int = 6,
    verbose: bool = True,
    hint: Optional[str] = None,
    debug: bool = False,
    sim_gate: bool = False,
) -> Dict[str, Any]
```

**必填输入**：一个 `example.json` 路径。字段校验在 `tkstore/examples.py:6`：

```
必填：example_id, engine, question, gold_sql_path
可选：db_name, db_path, agent_sql_path, gold_result_path, agent_result_path,
      db_info_path, external_evidence_path, credential_path
```

行为说明：

- 相对路径以 `example.json` 所在目录为基准解析。
- SQLite 场景下，`db_path` 与 `executor` 至少给一个。
- 缺 `agent_sql_path` 时会先用 LLM 生成一份 draft SQL（`_generate_agent_sql`）。
- 缺 result CSV 时会在数据库上实际执行 SQL 补出来（`_execute_sql_to_csv`）。
- `store` 为空时按 engine 落到 `tkstore/config.py` 的 `TKSTORE_SQLITE_PATH` /
  `TKSTORE_BQ_PATH` / `TKSTORE_SF_PATH`（`_default_index_for_engine`，`builder.py:124`）。
- 写入是 append 语义，store 已存在则追加。

批量版本 `build_knowledge_from_examples_dir(examples_root, ...)`（`builder.py:470`）
要求目录下每个样例各自带一个 `example.json`。

对外封装是 `tkboost.generate()`（`tkboost/__init__.py:318`），二者传 `example_json` 或
`examples_dir` 其中之一。

### 入口 B：`run_diff_for_instance`

```python
tkstore/harness.py:1067
def run_diff_for_instance(
    instance_id: str,
    outputs_dir: str,
    jsonl_path: Optional[str] = None,
    db_path: Optional[str] = None,
    max_turns: int = 6,
    model: str = "azure/o4-mini",
    debug: bool = False,
    hint: Optional[str] = None,
    sim_gate: bool = False,
) -> None
```

**必填输入**：一个 `outputs_dir` 目录，也就是 `sql_agent_runner.py` 的产物目录。
目录内必须存在：

```
execution_query.sql              # agent 生成的 SQL
execution_result.csv             # agent SQL 的执行结果
gt_result.csv                    # gold 结果
gt_query.sql 或 {instance_id}.sql # gold SQL，二选一
processed_trace.txt              # 可选，agent 推理轨迹
```

`jsonl_path` 用于补 `question` 和 `external_knowledge`，通常传
`data/spider2-lite.jsonl`。不传时回落到 `tkstore/config.py::JSONL_DEFAULT`。

这个入口是对接 Spider2 评测链路的那一个，因为它直接消费 runner 的输出目录。

### 与论文的差异

**引擎路径。** `run_diff_for_instance` 写库时固定使用 `config.MEMORY_INDEX_PATH`
（`harness.py:1409`、`harness.py:1557`），而 `tkstore/config.py:26` 里
`MEMORY_INDEX_PATH = TKSTORE_BQ_PATH`。所以即使 populate 的是 SQLite 实例，
规则也会写进 `tkstore/tkstore_bq.csv`，不会进 `tkstore_sqlite.csv`。
入口 A 反而有正确的按引擎分流逻辑。

**db 列。** 入口 A 经 `TKStore.insert` 写入完整 10 列（含 `db`）；
入口 B 直接拼 CSV 行，header 里没有 `db` 概念上的一致保证，检索时按 `db` 过滤会受影响。

**训练集划分。** 论文 Table 11 明确划了 train / test（Spider2 SQLite 是 34 / 101）。
代码里没有任何地方强制这件事。注意 populate 阶段会读 `gt_query.sql` 和 `gt_result.csv`，
即**看得到 gold 答案**；在同一个实例上先 populate 再评测等于数据泄漏。

---

## Algorithm 4 — Retrieve(s)

论文伪码：

```
c ← parse s and obtain query features
K_cand ← ∅
foreach row r in TK-Store do
    if ⋀_{x∈X} (r[x] = *) ∨ (r[x] ∩ c[x] ≠ ∅) then
        K_cand ← K_cand ∪ {r[knowledge]}
K_ret ← FilterKnowledge(s, K_cand)     # LLM 上下文感知过滤
return K_ret
```

论文 Fig. 4 给出的特征集 `X` 是：**SQL Keywords、Tables、Columns、Data Type**。

### 对应代码

| 论文步骤 | 代码 |
| --- | --- |
| 解析 query features | `tagger_index.py::_infer_data_types_from_sql`（`:395`）+ `_detect_null_handling_in_sql`（`:472`），正则匹配，不用 sqlglot |
| 逐行匹配适用条件 | `tagger_index.py::search_index_for_sql`（`:492`） |
| `FilterKnowledge` | `tagger_index.py::_llm_filter_relevant_rules`（`:753`），分块调 `_process_rule_chunk` |
| 统一入口 | `tagger_index.py::MemoryRetriever.retrieve`（`:1038`） |

### 输入规格

```python
tkstore/tagger_index.py:1038
MemoryRetriever(index_csv_path: str).retrieve(
    sql_text: str,                       # 待检索的 SQL 或单个 CTE
    generic_only: bool = True,           # True 只返回 scope='generic'
    use_llm_filtering: bool = False,     # 对应论文 FilterKnowledge
    llm_model: str = "azure/gpt-4.1",
    llm_only: bool = False,              # 跳过代码层过滤，全部交给 LLM 选
    db: Optional[str] = None,            # 数据库名，用于保留 db 专属规则
    instance_id: Optional[str] = None,
) -> List[Dict[str, Any]]
```

`index_csv_path` 指向一个 tkstore CSV。返回的每条规则是 dict，含
`mem_id / instance_id / scope / sql_operations / table / column / data_type / nulls / rule / matches`。

**`generic_only` 的默认值需要留意**，不同层级不一致：

| 调用点 | 默认值 |
| --- | --- |
| `search_index_for_sql` | `True` |
| `MemoryRetriever.retrieve` | `True` |
| `TKStore.retrieve`（`tkboost/__init__.py:120`） | `False` |
| `tkboost.sql()` 内部调用 | 显式传 `False` |

也就是说直接用 `MemoryRetriever` 且不改参数，只会拿到通用规则，
数据库专属规则全部被丢掉。要复现论文行为需要 `generic_only=False` 并传 `db`。

### 与论文的差异

**特征维度不一致。** 代码实际参与过滤的只有三项：`sql_operations`、`data_type`、`nulls`
（`search_index_for_sql` 内的三段 AND 判断）。`table` 和 `column` 虽然从 CSV 读出来
（`:537`、`:538`）并塞进返回结果（`:655`、`:656`），但**从未作为筛选条件使用**。
论文的 `X` 包含 Tables 和 Columns，代码用 `nulls` 替换了这两项。

**特征抽取方式。** 论文说用 SQL parser 抽 keywords / tables / columns，并查库确定列的数据类型。
代码用的是正则关键字匹配加类型推断，没有连库查 schema。

**降噪启发式。** 代码额外加了一条论文没有的规则：generic scope 且只命中一个常见操作
（`select` / `where` / `join` / `from`）时直接丢弃（`:608`-`:620`），用于压噪声。

---

## Algorithm 5 — Agent Augmented with TK Store

论文伪码要点：

```
while is_final = False ∨ f ≠ ∅ do
    (s_t, is_final) ← A(C_t)                       # agent 生成 SQL
    if is_final ∧ i < number_of_CTEs(s_t) then
        f ← GetTKStoreFeedback(s_t, i, D)          # 只处理第 i 个 CTE
        i ← i + 1
    R_t ← ExecSQL(s_t, D)
    C_{t+1} ← concat(C_t, s_t, R_t, f)             # feedback 回灌 agent context
return s_t

Procedure GetTKStoreFeedback(s, i, D):
    K ← TK-Store.Retrieve(c_i)                     # Alg 4
    return Feedback(q, c_i, K)                     # LLM 转成 NL 修正指令
```

三个要素：**逐 CTE**、**知识检索**、**feedback 回灌让 agent 自己改**。
代码里这三个要素被拆到了两个函数,各缺一块。

### 半实现 A：`tkboost.sql()`

```python
tkboost/__init__.py:530
def sql(
    question: Optional[str] = None,      # 与 draft 二选一
    draft: Optional[str] = None,         # 已有的 agent draft SQL
    executor: Optional[Executor] = None, # 多轮 DB 探针只对 SQLiteExecutor 启用
    store: Optional[Union[str, TKStore]] = None,   # 必填
    model: Optional[str] = None,
    db_name: Optional[str] = None,       # 影响 db 专属规则检索
    db_info: Optional[str] = None,
    use_llm_filtering: bool = False,
    max_turns: int = 10,
    min_probes: int = 3,
    verbose: bool = False,
) -> Dict[str, Any]
```

**有**：逐 CTE 循环（`parse_ctes_from_sql`）、逐 CTE 检索（`_rules_block_for`，`:585`）、
把规则拼进 `cte_goal` 传给 refiner（`:615`）。

**缺**：没有 agent 回环。它拿 `run_refiner` 返回的 `suggested_fix_sql` 直接替换 CTE，
而不是把 feedback 送回原 agent 让其重写 `<solution>`。

返回 dict 含 `draft_sql / refined_sql / rule_count / rules_used / execution / verdicts`。

注意非 SQLite executor 会退化成单次 LLM 精炼（`:729` 之后的 fallback 分支），
没有逐 CTE，也没有数据库探针。

### 半实现 B：`perform_refinement_and_revision()`

```python
src/agents/sql_agent_runner.py:427
def perform_refinement_and_revision(
    inst: Instance,
    final_sql: str,
    predicted_cte_hint: Optional[str],
    engine: str,
    db_path_or_cred: Optional[str],
    messages: List[dict],                # agent 的完整对话历史
    out_dir: Path,
    model: str,
    verbose: bool,
    tribalknowledge_generic_only: bool = True,
    external_knowledge: str = None,
    schema_context: str = None,
) -> Tuple[str, Optional[dict]]
```

**有**：逐 CTE 循环；完整的 agent 回环——把 refiner 的 issues 拼成 feedback 追加到
`messages`（`:486`-`:501`），让原 agent 重新输出 `<solution>`，执行后写
`execution_result_after_{cte_name}.csv`，最终由 `_choose_and_mark_final_artifacts`
（`:62`）提升为 `execution_result_final.csv`。这一段最贴近 Alg 5 Line 13。

**缺**：完全没有知识检索。传给 refiner 的 `cte_goal` 只是
`extract_goal_from_cte_body(cte_body, cte_name)`（`:450`），即 CTE 注释，
不含任何 tribal knowledge。整个 `sql_agent_runner.py` 不 import `tkstore`。

### refiner 的注入点

两边最终都调 `src/agents/cte_refiner.py::run_refiner`（`:286`），
知识的落点是同一个字符串参数 `cte_goal`，在 `:335` 拼进 prompt：

```python
user_payload += "\n\n[CTE_GOAL]\n" + cte_goal.strip()
```

`run_refiner` 自身**不做检索**——它不 import `MemoryRetriever`，不读 tkstore CSV。
签名里的 `tribalknowledge_generic_only` 和 `use_all_rules` 两个参数
只出现在签名行和文件末尾的 CLI 调用行，函数体内零引用，是预留但未实现的钩子。

---

## TK-Store 表结构

CSV 列定义在 `tkboost/__init__.py:86`（`TKStore.HEADER`）：

| 列 | 对应论文 | 说明 |
| --- | --- | --- |
| `mem_id` | Id | 行号，`delete` 后会重排 |
| `instance_id` | — | 来源样例 |
| `db` | — | 数据库名或 `all` |
| `scope` | — | `db` / `generic` / `question`（后者不参与检索） |
| `sql_operations` | SQL Keywords | 分号分隔，`all` 表示通配 |
| `table` | Tables | **当前不参与检索过滤** |
| `column` | Columns | **当前不参与检索过滤** |
| `data_type` | Data Type | `str/int/numeric/date/bool/all/unspecified` |
| `nulls` | 论文无此项 | `Yes/No/all` |
| `rule` | Knowledge | 知识语句正文 |

论文的通配符 `*` 在代码里写作字符串 `all`。

---

## 已知偏差汇总

> **这张表是简版。** 完整清单（按"要不要改"分 A / B / C 三组，共 27 条）见
> [`deviations.md`](./deviations.md)，两者冲突时以那份为准。
> 实现计划见 [`implementation_plan.md`](./implementation_plan.md)。

| # | 偏差 | 位置 | 影响 |
| --- | --- | --- | --- |
| 1 | Alg 5 没有完整实现体 | 两个半实现 | 从评测链路出发时,augmentation 阶段不用知识 |
| 2 | `table` / `column` 不参与检索过滤 | `tagger_index.py:585`-`:647` | 检索精度低于论文的 4 维匹配 |
| 3 | `run_diff_for_instance` 固定写 BQ CSV | `harness.py:1409` + `config.py:26` | SQLite populate 的规则进错文件 |
| 4 | `run_refiner` 两个知识参数是死参数 | `cte_refiner.py:286` | 看签名会误以为钩子已生效 |
| 5 | 无 train/test 划分强制 | 全局 | populate 读 gold,同实例复用会泄漏 |
| 6 | 特征抽取不查 schema | `tagger_index.py:395` | 列的真实数据类型靠正则猜 |
| 7 | 无 CLI 入口 | `builder.py` / `harness.py` | populate 只能 Python 调用 |

---

## 打通评测链路所需的改动

> 这里是最小改动草案。分阶段的完整方案（含每步的用途 / 输入 / 输出、train-test 划分、
> 成本估算）见 [`implementation_plan.md`](./implementation_plan.md)。

好消息是两个半实现已经共用 `cte_goal` 这个字符串接口,所以缺口不大：

1. **补 retrieve 到 augment 的连接**：给 `perform_refinement_and_revision` 加一个
   store 路径参数,在逐 CTE 循环里调
   `MemoryRetriever.retrieve(sql_text=cte_sql, generic_only=False, db=inst.db)`,
   把结果拼进 `goal`。逻辑可直接照搬 `tkboost/__init__.py:585` 的 `_rules_block_for`。
2. **补 CLI 参数**：`sql_agent_runner.py` 加 `--tkstore <csv>`,在 `main()` 里传下去。
3. **补 populate 驱动**：写一个脚本对训练集实例循环调 `run_diff_for_instance`,
   并显式维护 train / test 实例列表。同时决定是否修正偏差 #3。

评测侧不需要改动。`evaluation/evaluate.py` 已经区分
`execution_result.csv`（`score`,baseline）和 `execution_result_final.csv`
（`score_final`,增强后),槽位是现成的。
