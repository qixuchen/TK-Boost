# 分阶段实现计划

## 目标

按论文 `Arming_Data_Agents_with_Tribal_Knowledge_Tech_Report.pdf` 的描述完成三件事：

1. **TK 生成** —— agent 跑出输出，populate 解析输出产出 TK-Store（Alg 2 / Alg 3）
2. **Augmentation** —— 把 retrieve 和 feedback 接进 agent workflow（Alg 4 / Alg 5）
3. **Pipeline runner** —— 端到端编排，产出 baseline vs augmented 的对比

## 范围与原则

- **数据集**：本轮只做 Spider2-SQLite（135 实例 / 30 个库）。BIRD 暂不做，现状见
  [`deviations.md`](./deviations.md) 附注。
- **Agent**：用仓库自带的 ReAct agent（`src/agents/sql_agent_runner.py`），
  对应论文 6.1.3 的默认设置。不引入 ReFORCE 或外部 agent。
- **对齐优先**：以论文描述为准。[`deviations.md`](./deviations.md) 的 **A 组**必须消除，
  **C 组**顺手修，**B 组**本轮不动但要在解读结果时考虑进去。
- 算法与函数的对照关系见 [`algorithm_mapping.md`](./algorithm_mapping.md)，本文不重复。

## 阶段总览

| 阶段 | 内容 | 消除的偏差 | LLM 成本 | 依赖 |
| --- | --- | --- | --- | --- |
| 1 | 数据与路径基础设施 | C1–C4、C6–C8、A5 | 无 | — |
| 2 | Populate（Alg 2 / 3） | A2、A3、A4、A6 | 中（train 集 agent 跑一遍 + populate） | 阶段 1 |
| 3 | Retrieve + Augment（Alg 4 / 5） | A1、B1、B7 | 无（改代码） | 阶段 2 |
| 4 | Pipeline runner + 评测 | C5 | 高（test 集跑两遍） | 阶段 3 |

阶段 1 完全不花 LLM 调用，且是后面所有阶段跑通的前提，先做。

---

# 阶段 1 — 数据与路径基础设施

## 1.1 库路径解析重构

**用途**：把数据库路径解析从"靠物理目录布局硬凑"改成"查官方映射表"，
并修掉吞异常和动态 exec 两个坑。消除 C1、C2、C3。

关键发现：`~/spider2-localdb/local-map.jsonl` 是 **Spider2 官方的实例→库映射**，
已验证 135 个实例 → 30 个库，映射值加 `.sqlite` 后缀就是共享目录里的确切文件名
（含 `Db-IMDB`、`sqlite-sakila` 这类不规则命名），零缺口、双向一致。
所以**不需要任何归一化匹配启发式**，之前那套小写去下划线的逻辑可以整个扔掉。

**输入**：

| 来源 | 内容 |
| --- | --- |
| 环境变量 `SPIDER2_DB_ROOT` | 共享库目录，默认 `~/spider2-localdb` |
| `$SPIDER2_DB_ROOT/local-map.jsonl` | 官方映射，单行 JSON 对象 `{instance_id: db_basename}` |
| 函数参数 | `instance_id: str`、`db_id: str` |

**输出**：`Optional[str]`，数据库文件绝对路径。保持现有返回类型不变，
因为两个调用方（`src/agents/sql_agent_runner.py:35`、`src/executors/factory.py:5`）都按
`None` 判失败。

**改动内容**：

1. 把 `get_database_path` 从 `src/agents/cte_refiner.py:63` **移到** `src/utils/db_paths.py`，
   让 `cte_refiner` 反向 import。修正倒置的依赖方向（C3）。
2. 删掉 `src/utils/db_paths.py:9-17` 的动态 `spec_from_file_location` 加载（C2）。
3. 解析优先级：

   ```
   1. minidev 分支（instance_id 以 minidev 开头）—— 原样保留，见 deviations 附注
   2. local-map.jsonl 精确查表 → $SPIDER2_DB_ROOT/<db_basename>.sqlite
   3. 回落 data/spider2/<instance_id>/*.sqlite    # 向后兼容
   4. 回落 ./<db_id>.sqlite                        # 保留原有 fallback
   ```

4. 全部失败时**不要静默返回 None**，打印尝试过的每个路径再返回 None（C1）。

**验收**：对全部 135 个实例调用一次，全部解析成功；随便断开一个路径，
错误信息里能看到试过哪些路径。

## 1.2 清理 symlink，切换到共享库目录

**用途**：消除 C4。当前 `data/spider2/` 下 134 个绝对路径 symlink 指向 30 个真实库，
冗余 4.5 倍且不可移植。

**输入**：1.1 完成后的解析器；现有 `data/spider2/` 目录树。

**输出**：

- 删除 134 个 symlink
- 删除 `data/spider2/local007/Baseball.sqlite` —— 已验证共享目录里有同名文件，
  这 30MB 是纯重复
- `data/spider2/<instance_id>/` 只保留 metadata（`DDL.csv`、各表 JSON 等）
- `data/instance_db_mapping.csv` 保留但降级为参考，不再作为解析依据
  （它把 BigQuery / Snowflake 实例也混在一起，且不区分文件名大小写）

**验收**：symlink 数为 0；1.1 的 135 实例解析仍然全部成功。

## 1.3 固定 train / test 划分

**用途**：消除 A5。populate 阶段会读 gold SQL 和 gold 执行结果，
**看得到答案**，所以划分必须在跑 populate 之前固定，否则同实例先 populate 再评测是数据泄漏。

**输入**：`data/spider2-lite.jsonl` 里的 135 个 SQLite 实例 ID；论文 Table 11 的数量
（34 train / 101 test）。

**输出**：两个纯文本文件，每行一个 instance ID。

```
data/splits/spider2_sqlite_train.txt   # 34 行
data/splits/spider2_sqlite_test.txt    # 101 行
```

外加一个 `data/splits/README.md` 记录生成方式（排序后固定种子采样，写明种子值）。

**注意两点**，都要写进最终报告：

- 论文只给了数量，**没给实例 ID 列表**，所以我们复现不了它的确切划分。
- 34 个 train 实例分布在 30 个库上，意味着大部分库只有 0–1 个 train 实例。
  db 专属规则要靠"train 和 test 共享同一个库"才能命中，这个划分下命中率天生受限，
  预期主要收益来自 generic 规则。不做按库分层，因为论文没提，分层会引入新的不可比性。

**验收**：两个文件无交集、并集等于 135、行数分别是 34 和 101。

## 1.4 修文档与评测脚本容错

**用途**：消除 C5、C6、C7。这些是纯粹的时间黑洞，上一轮排查成本几乎全在这里。

**输入/ 输出**：

| 项 | 改动 |
| --- | --- |
| C5 | `evaluation/evaluate.py:497` 的 `item['score']` 改用 `.get('score', 0)`；`:521` 起在 `df_rows` 为空表时提前报错并说明可能原因（`--result_dir` 尾部斜杠、实例 ID 解析失败、预测 CSV 为空） |
| C6 | 删掉 `src/agents/sql_agent_runner.py:725` 那句过期的 `reduced max_turns` 注释 |
| C7 | `evaluation/README.md:20-23` 改成 gold SQL 在 `evaluation/gold/sql/`；换掉虚构的 outputs 目录示例 |

**验收**：拿一个空 `--result_dir` 跑 `evaluate.py`，得到可读的错误说明而不是 `KeyError: 'score'`。

---

# 阶段 2 — Populate（Alg 2 / Alg 3）

## 2.1 生成 train 集 agent 输出

**用途**：produce 论文经验元组 `e = (q, τ, s*)` 里的 `τ`（agent 执行轨迹）和 agent SQL。
populate 是从**真实 agent 的错误**里学规则，所以这一步不能跳过、也不能用 LLM 造假 draft 替代。

**输入**：`data/splits/spider2_sqlite_train.txt`（34 个实例）。

**输出**：34 个实例目录，每个含

```
execution_query.sql      # agent 最终 SQL
execution_result.csv     # 它的执行结果
processed_trace.txt      # 轨迹 τ
messages.json            # 完整对话
gt_result.csv            # gold 执行结果
```

**命令**（现有 CLI，无需改动）：

```bash
python -m src.agents.sql_agent_runner \
  --run-all-from-file data/splits/spider2_sqlite_train.txt \
  --jsonl-path data/spider2-lite.jsonl \
  --out-base outputs/train \
  --verbose
```

注意**不加** `--refine-cte`。这一步要的是 agent 未经修正的原始产物，
带 refine 的输出属于阶段 3。

**验收**：34 个目录都有非空 `execution_query.sql` 和 `processed_trace.txt`。
`execution_result.csv` 允许为空（agent SQL 执行失败也是有效的学习素材，
恰好是 correction 信号最强的样本）。

## 2.2 新入口 `populate_from_output_dir`

**用途**：Alg 2 的正确入口。现有两个入口各有硬伤 —— `run_diff_for_instance` 的输入适配是对的
但写库用 9 列 header 且固定写 BQ store（A2、A3）；`build_knowledge_from_example` 的写库是对的
但硬编码丢弃轨迹（A4）。这个新函数取两者的正确部分，**不修改现有两个入口**（低风险路线）。

**位置**：新增 `tkstore/populate.py`。

**签名**：

```python
def populate_from_output_dir(
    output_dir: str,                       # agent 输出目录，见 2.1
    instance_id: Optional[str] = None,     # 缺省从目录名解析（去掉 _时间戳 后缀）
    engine: Optional[str] = None,          # 缺省 infer_engine(instance_id)
    jsonl_path: Optional[str] = None,      # 取 question / external_knowledge / db
    gold_sql_dir: str = "evaluation/gold/sql",
    store: Optional[str] = None,           # 建议显式传 artifacts/ 下的路径，见下方说明
    db_name: Optional[str] = None,         # 缺省取 jsonl 的 db 字段
    model: Optional[str] = None,
    max_turns: int = 6,
    verbose: bool = True,
) -> Dict[str, Any]
```

**输入**（逐项来源，这是这个函数的核心价值）：

| 论文符号 | 内容 | 来源 |
| --- | --- | --- |
| `q` | NL 问题 | `jsonl_path` 里该 instance 的 `question` |
| `τ` | agent 执行轨迹 | `output_dir/processed_trace.txt`，**必须传下去** |
| agent SQL | agent 最终 SQL | `output_dir/execution_query.sql` |
| agent 结果 | 它的执行结果 | `output_dir/execution_result.csv` |
| `s*` | gold SQL | **`gold_sql_dir/{instance_id}.sql`** |
| gold 结果 | gold 执行结果 | `output_dir/gt_result.csv`，缺失时回落 `evaluation/gold/exec_result/` |
| `D` | 数据库 | 1.1 的解析器 |
| 外部知识 | evidence | jsonl 的 `external_knowledge` |

gold SQL 走 `gold_sql_dir` 而不是输出目录，是因为 runner **不保证**产出它 ——
实测 5 个输出目录里 2 个（`local002`、`local007`）既没有 `gt_query.sql` 也没有
`{instance_id}.sql`（C8）。权威来源是 `evaluation/gold/sql/`，那里有 256 个文件。

**处理流程**（三个 LLM 阶段直接复用现有函数，它们本身没问题）：

```
1. generate_memory_diff_first_turn   tkstore/harness.py:54     → diff 文本   [Alg 3]
     传入 processed_trace_text=<τ>，这是与 builder 路径的关键区别
2. generate_rules_from_diff          tkstore/harness.py:967    → 规则文本
3. generate_tagged_memories_json     tagger_index.py:31        → 结构化 JSON  [GenTKRow]
4. _persist_via_tkstore              tkstore/builder.py:19     → 写库        [TK-Store.insert]
```

第 4 步是关键替换。**不用** `run_diff_for_instance` 内嵌的 `_append_memories_index`，
改走 `_persist_via_tkstore` → `TKStore.insert_many` → `TKStore.insert`，
后者按 10 列 `HEADER` 写行并自行分配 `mem_id`，同时消除 A2 和 A3。

**输出**：

- **副作用**：向 `store` 指向的 CSV 追加若干行，10 列格式，`db` 列填 `db_name`
- **返回值**：

  ```python
  {
      "instance_id": str,
      "engine": str,
      "store": str,              # 实际写入的 CSV 路径
      "db": str,
      "rule_count": int,
      "inserted": List[TKStoreEntry],
      "diff_text": str,          # 便于人工审阅
      "tagged": dict,
  }
  ```

**store 写到哪里**：**不要**追加进上游已跟踪的 `tkstore/tkstore_*.csv`。那四个 CSV 是上游随仓库
发布的示例数据，往里追加会把我们的规则和它混在一起，而且每跑一次 populate 就产生一次大 diff。

本轮的约定是写到仓库根下的 `artifacts/`，该目录已在 `.gitignore` 里：

```
artifacts/tkstore_sqlite.csv      # 本轮 Spider2-SQLite 的 TK-Store
```

首次写入时 `artifacts/` 不存在，`populate_from_output_dir` 需要自己 `mkdir -p`。
文件不存在时按 `TKStore.HEADER` 写表头，这条路径由 `TKStore.insert` 天然覆盖。

即便如此，A3（写库路径固定为 BQ store）仍然要修 —— 缺省行为必须按 engine 正确分流，
不能依赖调用方每次都记得传 `--store`。

**验收**：

1. 拿一个 train 实例跑通，`TKStore(store).rows()` 能正常读出（不抛 `_ensure_well_formed`）
2. 新增行的 `db` 列不为空
3. 规则进 `artifacts/tkstore_sqlite.csv`；上游的 `tkstore/tkstore_*.csv` 四个文件
   `git status` 里保持干净，一个字节都没动
4. `MemoryRetriever(store).retrieve(sql, generic_only=False, db=<db>)` 能召回到 db 专属规则

## 2.3 批量 populate CLI

**用途**：消除 A6。同时强制只在 train 集上 populate。

**输入**：

```bash
python -m tkstore.populate \
  --outputs-base outputs/train \
  --split-file data/splits/spider2_sqlite_train.txt \
  --jsonl-path data/spider2-lite.jsonl \
  --store tkstore/tkstore_sqlite.csv \
  --verbose
```

**输出**：填充好的 `tkstore/tkstore_sqlite.csv`；一份 `outputs/train/populate_report.json`
记录每个实例产出多少条规则、失败原因。

**必须内置的护栏**：如果 `--outputs-base` 下出现了不在 `--split-file` 里的实例，
直接报错退出，而不是静默 populate 进去。这是防泄漏的最后一道闸。

**验收**：`tkstore_sqlite.csv` 行数 > 0；`instance_id` 列的取值集合是 train 集的子集。

---

# 阶段 3 — Retrieve + Augment（Alg 4 / Alg 5）

## 3.1 把检索接进 agent workflow

**用途**：消除 A1。这是整个计划的核心一步。

现状是 Alg 5 被拆成两半：`tkboost.sql()` 有逐 CTE 检索但没有 agent 回环；
`perform_refinement_and_revision` 有完整 agent 回环（feedback 追加进 `messages`，
让原 agent 重出 `<solution>`，对应 Alg 5 Line 13 的 `C_{t+1} ← concat(C_t, s_t, R_t, f)`）
但完全没有检索。选后者作骨架，把前者的检索移植进去。

**关键便利**：两边最终都调 `run_refiner`，知识的落点是同一个字符串参数 `cte_goal`，
在 `src/agents/cte_refiner.py:335` 拼进 prompt。所以只需要在传参前把规则拼进 `goal`，
不用改 refiner。

**位置**：`src/agents/sql_agent_runner.py:427::perform_refinement_and_revision`。

**输入**（新增参数）：

```python
tkstore_path: Optional[str] = None,      # tkstore CSV；None 则完全退化为当前 baseline 行为
use_llm_filtering: bool = True,          # 对应论文 FilterKnowledge，默认开，见下
```

**改动**：

1. 函数开头按 `tkstore_path` 构造一个 `MemoryRetriever`（构造一次，逐 CTE 复用）
2. 在逐 CTE 循环里，`goal = extract_goal_from_cte_body(...)`（`:450`）之后插入检索，
   调用方式照搬 `tkboost/__init__.py:585` 的 `_rules_block_for`：

   ```python
   retriever.retrieve(
       sql_text=cte_sql,
       generic_only=False,        # 必须 False，否则 db 专属规则全丢（B7）
       db=inst.db,
       use_llm_filtering=True,    # 论文 Alg 4 最后一步是固定步骤（B1）
   )
   ```

3. 拼接格式沿用 `tkboost/__init__.py:615` 已有的写法，保持两条路径一致：

   ```python
   cte_goal = f"{goal}\n\nUse these tribal knowledge rules as guidance:\n\n{rules_block}"
   ```

4. 检索为空时保持 `goal` 原样，不要塞空的知识段落进 prompt

**两个默认值必须显式设成这样**，否则跑的不是论文方法：
`generic_only=False`（B7，三处默认值不一致）、`use_llm_filtering=True`
（B1，默认 `False` 会跳过 `FilterKnowledge`）。

**输出**：

- 每个 CTE 的 refiner prompt 里带上检索到的规则
- 落盘一份 `<out_dir>/retrieved_rules.json`，记录每个 CTE 检索到哪些 `mem_id`。
  这个用于事后归因，判断增益到底来自哪条规则，别省。
- 最终 SQL 仍由现有 `_choose_and_mark_final_artifacts`（`:62`）提升为
  `execution_result_final.csv`，评测侧不用改

**验收**：

1. 不传 `--tkstore` 时，输出与阶段 3 之前逐字节一致（保证 baseline 可比）
2. 传 `--tkstore` 时，`retrieved_rules.json` 非空，且 refiner trace 里能看到规则文本
3. 至少有一个实例的 `execution_query_after_<cte>.sql` 与 `execution_query.sql` 不同，
   证明回环真的生效了

## 3.2 CLI 参数贯通

**用途**：让阶段 4 能从命令行控制知识注入。

**输入 / 输出**：`src/agents/sql_agent_runner.py::main()`（`:813`）新增

| 参数 | 含义 |
| --- | --- |
| `--tkstore <csv>` | tkstore CSV 路径，不传则不注入知识 |
| `--no-llm-filtering` | 关掉 `FilterKnowledge`，仅用于消融实验 |

在 `main()` 里透传给 `perform_refinement_and_revision`。
现有的 `--tribalknowledge-all-scopes` 保持原样不动 —— 它最终传给 `run_refiner` 的那个
同名参数是死参数（B12），改它没有意义，也不在本轮范围。

**验收**：`--help` 能看到新参数；传一个不存在的路径时报清晰错误而不是静默跳过。

---

# 阶段 4 — Pipeline runner 与评测

## 4.1 端到端编排脚本

**用途**：一条命令跑完 baseline 与 augmented 两侧，产出可对比的结果。

**位置**：新增 `scripts/run_pipeline.py`（或 Makefile 目标）。

**输入**：

| 参数 | 含义 |
| --- | --- |
| `--train-split` / `--test-split` | 阶段 1.3 的两个划分文件 |
| `--store` | tkstore CSV |
| `--stage` | `populate` / `baseline` / `augmented` / `evaluate` / `all` |
| `--model` | LLM 模型 |

**流程**：

```
populate    : 2.1 train 集 agent 输出 → 2.3 批量 populate → store
baseline    : test 集跑 agent，不传 --tkstore          → outputs/test_baseline/
augmented   : test 集跑 agent，传 --tkstore + --refine-cte → outputs/test_augmented/
evaluate    : 对两个目录分别跑 evaluate.py，汇总对比
```

**输出**：`outputs/pipeline_<timestamp>/` 下含两侧的实例目录、两份评测结果、
一份汇总 JSON。

**验收**：`--stage all` 在 3 个实例的小集合上跑通，且中断后可从任一 stage 续跑。

## 4.2 结果对比

**用途**：产出论文 Fig. 6 那种 baseline vs augmented 的准确率对比。

**输入**：两个 `evaluate.py` 的输出。

**输出**：一张表，至少含

| 列 | 含义 |
| --- | --- |
| `instance_id` | 实例 |
| `score_baseline` | 未注入知识 |
| `score_augmented` | 注入知识 |
| `delta` | `+1` 修好 / `0` 无变化 / `-1` 改坏 |
| `rules_used` | 该实例命中的 `mem_id` 列表 |

`rules_used` 是关键 —— 论文强调 TK 是**可审阅**的。有了它才能回答
"哪条规则真的起了作用"、"改坏的那些是哪条规则导致的"。

**注意**：`evaluate.py` 本身已经区分 `score`（`execution_result.csv`，baseline）和
`score_final`（`execution_result_final.csv`，增强后），槽位是现成的。
但因为我们 baseline 和 augmented 跑在**两个独立目录**，用的是两侧的 `score` 列做对比，
不要混用同一目录内的 `score` / `score_final`（那个对比的是"refine 前 vs refine 后"，
不是"有知识 vs 无知识"，两者不是一回事）。

---

## 规模与成本

| 阶段 | agent 运行次数 | 说明 |
| --- | --- | --- |
| 2.1 | 34 | train 集，外层 ReAct 最多 25 轮 |
| 2.2/2.3 | 0 | 每实例约 3–4 次 LLM 调用（diff 循环最多 6 轮） |
| 4 baseline | 101 | test 集 |
| 4 augmented | 101 | test 集，额外含 refiner 的 25 轮探库循环 |

合计约 236 次 agent 运行。augmented 一侧因为带 refiner 探库，单实例成本明显高于 baseline。
建议先在 3–5 个实例上把整条链路跑通，再放开跑全量。

## 本轮不做

- [`deviations.md`](./deviations.md) **B 组**全部 —— 检索只用 3 维特征、正则抽特征、
  refiner 是 25 轮探库循环而非单次 `Feedback`、每 CTE 修订 5 次上限等
- BIRD / minidev 适配
- ReFORCE agent（论文 6.1.3 用它证明通用性，不是主结果）
- 论文 6.4 的各项消融

其中 **B10**（refiner 做的比论文多）和 **B11**（修订次数上限）会影响 augmented 一侧的绝对数值，
解读结果时必须一并说明：baseline 与 augmented 的差值里混入了 refiner 探库带来的增益，
不能全部归因于 tribal knowledge。要干净地分离，需要一个"有 refiner 无知识"的第三组对照，
那属于后续消融。
