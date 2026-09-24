# BIRD mini-dev 接入方案

目标：把 TK-Boost 这套 populate → retrieve → refine 流程跑在 BIRD mini-dev 上，
拿到与 Spider2 那八轮（A–F、H、J）同口径的三 setting 结果（裸 agent / 臂 A / 臂 B）。

做 BIRD 的**唯一实验动机**：Spider2-SQLite 是 4.5 题一个库，86 个测试实例里 **34 个所在库
在 store 里一条 db 规则都没有**，H 轮全部知识损伤就落在这 34 个上（净 −2），另外 52 个是 +1
（`results_reference_track.md` §16.2）。BIRD mini-dev 是 **45.5 题一个库**，能第一次让
几乎所有测试实例都有同库规则可用。别的都是手段。

---

## 0. 事实核对

### 0.1 数据现状

数据在 **`/home/qchenax/data/bird_mini_dev/minidev/MINIDEV/`**，不在本仓库内。

| 项 | 值 |
| --- | --- |
| `mini_dev_sqlite.json` | **500** 条，字段 `question_id / db_id / question / evidence / SQL / difficulty` |
| 带 gold SQL | **500 / 500**，`SQL` 字段全部非空 |
| `mini_dev_sqlite_gold.sql` | 500 行，每行 `SQL<TAB>db_id`，与 JSON **行序一一对应**，`db_id` 零不匹配 |
| 完全重复的行 | **2 组**（`financial` 库 `question_id` 137 / 138 各出现两次，逐字段相同）→ 去重后 **498** |
| 库数 | **11**，每库 30–66 题（平均 45.5） |
| 难度 | moderate 250 / simple 148 / challenging 102 |
| 有 `evidence` | 498 / 500 |
| **gold 执行结果** | **没有**。BIRD 只给 SQL，不给结果 CSV |

`question_id` 取值范围 5–1533，是原 BIRD dev 的编号，**不连续也不唯一**，不能直接当实例 id，
也不能按它排序切分（会让 train 挤在某几个库）。

### 0.2 代码现状

**已经支持的两处**（不用改）：

- `src/utils/agent_utils.py:195`：`infer_engine` 认 `minidev` 前缀 → `sqlite`
- `src/utils/db_paths.py:132`：minidev 分支解析
  `<repo>/data/minidev/MINIDEV/dev_databases/<db_id>/<db_id>.sqlite`
- `evaluation/evaluate.py:226`：`parse_instance_id_from_output_dir` 用正则
  `^(?P<id>.+)_(\d{8})_(\d{6})$`，实例 id 里带下划线**也能解析**

**缺的四块**：

1. instance loader：`load_instances_from_jsonl`（`sql_agent_runner.py:1262`）读的是 JSONL、
   字段 `instance_id / db / question / external_knowledge`；BIRD 是单个 JSON 数组、字段名全不同
2. gold 三件套：`evaluation/gold/sql/<id>.sql`、`evaluation/gold/exec_result/<id>.csv`、
   `evaluation/gold/spider2lite_eval.jsonl`。后两个 BIRD 都没有
3. store 路径：`tkstore/config.py` 只有 SQLITE / BQ / SF 三条，`_default_index_for_engine('sqlite')`
   会把 BIRD 规则指到 `tkstore/tkstore_sqlite.csv`，和 Spider2 混在一起
4. 评测入口：`evaluate.py` 只有 `evaluate_spider2sql`，它按 `spider2lite_eval.jsonl` 的
   key 列举实例

---

## 1. 数据软链

`db_paths.py` 找的是 `<repo>/data/minidev/`，`.gitignore:79` 已有 `data/minidev/`。

```bash
ln -s /home/qchenax/data/bird_mini_dev/minidev /home/qchenax/TK-Boost/data/minidev
```

**验收**：`resolve_sqlite_db_path("minidev0000", "financial")` 返回
`data/minidev/MINIDEV/dev_databases/financial/financial.sqlite` 的绝对路径，且文件存在。
11 个库全部能解析。

---

## 2. 实例 id 与 train / test 划分

### 2.1 id 方案

去重后按 JSON 原序编号：**`minidev0000` … `minidev0497`**。

理由：必须以 `minidev` 开头（`infer_engine` 靠它判引擎）；必须唯一（runner 用
`<id>_<timestamp>` 命名输出目录，`_has_completed_output` 按 `f"{instance_id}_"` 前缀匹配）；
`question_id` 有重复所以不能用。零填充保证字典序与数值序一致。

同时落一份映射，供追溯与人工核对：

```
data/splits/bird_minidev_index.csv     # instance_id, question_id, db_id, difficulty
```

### 2.2 划分

**随机 25% / 75%，seed=0，去重后在 498 条上切** → **train 124 / test 374**。

实测三个 seed（0 / 1 / 42）的覆盖：

| seed | 有 train 的库 | 测试实例所在库有 train 的比例 | 最小库的 train 数 |
| --- | --- | --- | --- |
| 0 | 11 / 11 | **374 / 374** | 6（`california_schools`） |
| 1 | 11 / 11 | **374 / 374** | **1**（`debit_card_specializing`） |
| 42 | 11 / 11 | **374 / 374** | 6（`financial`） |

**随机划分不用分层也能拿到 100% 的 db 覆盖**，因为每个库都有 ≥30 题。所以「不按库分层」这个
简化没有代价——满覆盖正是 Spider2 给不出来的（那边 59%）。但 seed 会影响**每库 train 的条数**：
seed=1 下 `debit_card_specializing` 只有 1 条 train，那个库基本学不到 db 规则。
**已定 seed=0**（最小库 6 条，难度分布 41 simple / 65 moderate / 18 challenging）。

产物（提交进版本库，和 Spider2 的 splits 一样在 populate 之前定死）：

```
data/splits/bird_minidev_train.txt     # 124 行
data/splits/bird_minidev_test.txt      # 374 行
data/splits/bird_minidev_test_sub.txt  # 125 行，test 的随机 1/3，第一轮用
data/splits/bird_minidev_index.csv
```

生成脚本 `scripts/make_bird_splits.py`，硬编码 seed，确定性可复现。

### 2.3 不需要 no_reference_leak 子集

Spider2 那边需要第三个文件，是因为上游 store `tkstore_sqlite.csv` 在 25 个 test 实例上训练过。
我核对了四份上游 store 的 `db` 列：`tkstore_sqlite.csv` 覆盖的是 `bank_sales_trading` /
`IPL` / `SQLITE_SAKILA` 等 Spider2 的库，**与 BIRD 这 11 个库零交集**，`instance_id` 也全是
`local*` / `bq*` / `sf*`。所以 BIRD 这边不存在参考泄漏，test 就是完整的 374。

---

## 3. gold 产物：必须在 agent 跑之前先算

这是整条链上最容易漏的一步。三个消费方都要 gold，而且 **agent 步就已经要**：

| 消费方 | 代码位置 | 要什么 |
| --- | --- | --- |
| agent 步 | `sql_agent_runner.py:1892` `load_ground_truth` | `gold/sql/<id>.sql` + `gold/exec_result/<id>.csv` |
| populate 的正误闸门 | `populate.py:140` `agent_result_matches_gold` | `gold/exec_result/<id>.csv` + `spider2lite_eval.jsonl` |
| 评测 | `evaluate.py:363` `evaluate_spider2sql` | 同上 |

`load_ground_truth` 读 `evaluation/gold/exec_result/<id>.csv`，runner 再把它写成实例目录里的
`gt_result.csv`；而 `gt_result.csv` 正是第二次 LLM 调用（对照修理）prompt 里的
`GOLD EXECUTION RESULT`（`populate.py:158`）。**没有它，对照修理就没有对照物。**

所以要先跑一个 `scripts/build_bird_gold.py`：

1. 读去重后的 498 条，写 `evaluation/gold_bird/sql/minidev0000.sql` …（直接取 JSON 的 `SQL` 字段）
2. 对每条连上 `dev_databases/<db>/<db>.sqlite` 执行 gold SQL，用
   `src/executors/factory.make_executor('sqlite', path)` 保持和 agent 同一套执行器，
   结果写 `evaluation/gold_bird/exec_result/minidev0000.csv`
3. 写 `evaluation/gold_bird/bird_eval.jsonl`，每行
   `{"instance_id": ..., "condition_cols": [], "ignore_order": true}`

**用独立的 `evaluation/gold_bird/`，不要混进 `evaluation/gold/`。** 虽然 id 前缀不同不会撞车，
但 `evaluate_spider2sql` 是按 `eval_standard_dict` 的全部 key 列举实例的，混在一起会让
Spider2 的聚合分数里凭空多出 498 个实例。`populate_from_output_dir` 和 `evaluate.py`
的 `gold_dir` / `gold_sql_dir` 都是参数，换目录**不用改代码**。

`agent_result_matches_gold` 里 eval 标准的文件名是硬编码的
（`os.path.join(gold_dir, "spider2lite_eval.jsonl")`，`evaluate.py:298`），这一处要参数化，
或者干脆把 BIRD 的标准文件也叫 `spider2lite_eval.jsonl` 放进 `gold_bird/`。**倾向后者**：
零代码改动，代价是文件名难看，加一行注释说明。

**验收**：498 条 gold SQL 全部执行成功（失败的要单独列出来，不能静默跳过）；
`evaluation/gold_bird/exec_result/` 有 498 个 CSV。

---

## 4. instance loader + evidence（已实现）

§1–§3 已落地：软链、`minidev0000`–`minidev0497`、splits、`evaluation/gold_bird/`。
本节实现**没有跑 agent、没有调 LLM**，只让 runner 能把 BIRD 实例装进来，并且 evidence
和 gold CSV 真正进到 prompt / `gt_result.csv`。没有这一步，§5 的 124 条 train 会在
`load_ground_truth` 上读空（路径仍是 `evaluation/gold/`），evidence 也会被静默丢掉。

实测验收命令结束于 `dry-run: checked=124 failed=0`；124 条全部是 sqlite，DB 路径、
gold SQL 和 gold CSV 全部命中，BIRD 的 `expected_output_format` 全部是 `None`。

### 4.1 现状（读代码，不是猜）

| 点 | 代码 | 现在会怎样 |
| --- | --- | --- |
| 唯一入口 | `sql_agent_runner.py:1817` 和 refine 路径 `:1331` 都只调 `load_instances_from_jsonl` | BIRD 是 JSON **数组**，字段名是 `db_id` / `SQL` / `evidence`。用 `--jsonl-path` 指向 `mini_dev_sqlite.json` 会按行 `json.loads` 立刻炸 |
| 实例形状 | `Instance`（`:648`）只要 `instance_id / db / question / external_knowledge` | 和 BIRD 能对上，不必改 dataclass |
| id 算法 | `src/utils/bird.py` 的 `load_minidev_records` | **必须复用**。自己再写一遍去重/编号会和已提交的 splits 错位 |
| evidence | `load_external_knowledge`（`agent_utils.py:72`）把第二参当**文件名**，去 `data/spider2/<id>/<filename>` 读；找不到返回 `None`，不报错 | agent 步 `:1873` 和 refine 步 `:1417` 都走这里。把 evidence 原文塞进 `Instance.external_knowledge` 会当成文件名，**整段 evidence 消失** |
| gold 路径 | `load_ground_truth`（`:735`）写死 `evaluation/gold/sql/` 和 `evaluation/gold/exec_result/`；`:1392`、`:1936` 复制 GT SQL 同样写死 | BIRD gold 在 `evaluation/gold_bird/`。不改的话 `gt_query` / `gt_result` 全是 `None`，实例目录里没有 `gt_result.csv`，populate 的对照修理没有对照物 |
| 列名提示 | `:1894-1905` 用 gold CSV 列名拼 `expected_output_format` 进 agent prompt | §9.1 决策 1：BIRD **关掉**。这一步一起做，否则 train 口径就错了 |

`infer_engine` 和 `resolve_sqlite_db_path` 的 minidev 分支已经能用，loader 不用再碰。

### 4.2 实现内容（最小集）

**A. `src/agents/sql_agent_runner.py`：BIRD loader。**

新增 `load_instances_from_bird_json(path) -> List[Instance]`：

1. 调 `load_minidev_records(Path(path))`（去重 + `minidev%04d`）。
2. 映成 `Instance`：`instance_id` 原样；`db = db_id`；`question` 原样；`external_knowledge = evidence`（空串当成 `None`，与现有 `load_external_knowledge` 对空入参的行为一致）。
3. **不要**把 JSON 里的 `SQL` 放进 `Instance`。agent 不许看见 gold SQL。

抽一个 `load_instances(args)`：`--bird-json` 走上面这条，否则走现在的 JSONL。`main`（`:1817`）和 `run_refinement_on_existing_outputs`（`:1331`）都改调它，否则以后 `--refine-output` 对 BIRD 会再踩一次「Instance not found in JSONL」。

**B. `src/utils/agent_utils.py`：evidence 当内容。**

`load_external_knowledge(instance_id, external_knowledge_file)` 在现有空串检查之后、拼 `data/spider2/...` 之前加：

- `instance_id` 大小写不敏感地以 `minidev` 开头 → **原样返回**入参（strip 后）。
- 其它 id 行为不变：当文件名、找不到 → `None`。

两个调用点都不改。日志 `:1880` 现在打印 `loaded from {inst.external_knowledge}`，BIRD 下会把整段 evidence 打到终端；改成对 minidev 只打「inline evidence, N chars」，避免 124 条 train 日志被证据刷屏。

**C. gold 目录参数化。**

`load_ground_truth(instance_id, gold_dir=...)` 的 SQL/CSV 根改成 `Path(gold_dir)`，默认仍是 `evaluation/gold`，Spider2 一条 CLI 都不用动。

CLI 加：

- `--bird-json`：与 `--jsonl-path` 互斥（两个都给 → parser error）。
- `--gold-dir`：默认 `evaluation/gold`。只要给了 `--bird-json` 且用户没显式传 `--gold-dir`，默认改成 `evaluation/gold_bird`。

`main` 里复制 `<id>.sql` 的那两处（`:1392`、`:1936`）跟 `load_ground_truth` 用同一个 `gold_dir`。不要把 BIRD gold 拷进 `evaluation/gold/`。

**D. 关掉 BIRD 的列名提示（§9.1 决策 1）。**

`:1894-1905`：`infer_engine` 已经能从 id 认出 sqlite，这里用 **id 前缀** 更稳——`instance_id.lower().startswith("minidev")` 时 `expected_output_format = None`。`load_ground_truth` **仍然要读** `gold_bird/exec_result`（要写 `gt_result.csv`），只是列名不进 agent prompt。

**E. `--dry-run`。**

给 `main` 加 `--dry-run`：走完 loader + split 过滤 + `infer_engine` + `resolve_sqlite_db_path` + `load_ground_truth` + `load_external_knowledge`，打印每条 `instance_id / db / evidence_chars / gold_csv_ok / db_path`，然后 return，不建输出目录、不调 LLM。§5 发 124 条之前用它验一次。没有它，验收只能靠单测，命令行会一发出去就开始烧钱。

### 4.3 明确不改

- `Instance` 字段、populate、evaluate、store 路径。
- 不把 evidence 写成 `data/spider2/minidev####/evidence.md` 再走文件查找——多 498 个文件，和「软链不拷数据」冲突。
- 不在这一步跑 train agent。

### 4.4 TDD 记录（均已完成 Red → Green）

测试放 `tests/test_bird_loader.py`（或并进已有 `tests/test_bird.py`）。每条先写测试再改生产代码。

1. [x] **`load_instances_from_bird_json`**：临时 JSON 含逐字段重复行；验证去重、字段映射和不暴露 `SQL`。
2. [x] **与 splits 对齐**：真实文件加载 498 条，train 124 个 id 全部存在且 db 与 index 对齐。
3. [x] **`load_external_knowledge`**：minidev 返回 evidence 正文；Spider2 仍按文件读取，缺文件仍返回 `None`。
4. [x] **`load_ground_truth(..., gold_dir=)`**：显式目录读到 SQL/CSV；Spider2 默认仍是 `evaluation/gold`。
5. [x] **列名提示**：minidev 返回 `None`；Spider2 原格式不变。
6. [x] **CLI / dry-run**：数据源互斥、gold 默认目录切换、dry-run 不建输出目录且不调用 agent。

### 4.5 验收（2026-09-24 实测通过）

命令（不调 LLM）：

```bash
python -m src.agents.sql_agent_runner \
  --bird-json data/minidev/MINIDEV/mini_dev_sqlite.json \
  --split data/splits/bird_minidev_train.txt \
  --dry-run
```

| 项 | 期望 |
| --- | --- |
| 列出的实例 | 正好 124，id 集合 = `bird_minidev_train.txt` |
| evidence | 有 evidence 的实例 `load_external_knowledge` 返回非空原文；空 evidence 为 `None` |
| engine / db | 124 条 `infer_engine` 都是 `sqlite`；`resolve_sqlite_db_path` 全部命中 |
| gold | 124 条都能读到 `evaluation/gold_bird/exec_result/<id>.csv` |
| 列名提示 | dry-run 打印 `expected_output_format=None` |
| Spider2 | 不传 `--bird-json` 时默认 JSONL / `evaluation/gold` 不变 |

§5 的命令在 dry-run 通过之前不要发。

---

## 5. agent 跑 train 需要什么产物

**回答你的问题 3。** populate 读的是**实例目录**，不是数据库。以下是
`populate_from_output_dir`（`tkstore/populate.py:103-210`）逐行要的东西。

### 5.1 硬需求（缺了就失败或降级）

| 产物 | 谁写 | populate 拿它干什么 |
| --- | --- | --- |
| `execution_query.sql` | `:1924` | agent 的最终 SQL。**同时是「这条跑完了」的判据**（`_completed_dir` 要求它存在且非空），也是对照修理 prompt 里的 `AGENT FULL SQL` |
| `execution_result.csv` | `:1925` | 两个用途：正误闸门 `agent_result_matches_gold`；prompt 里的 `AGENT EXECUTION RESULT` |
| `gt_result.csv` | `:1951` | prompt 里的 `GOLD EXECUTION RESULT`。**来自 `gold_bird/exec_result/`，所以 §3 必须先做** |

### 5.2 软需求（有则更好）

| 产物 | 谁写 | 用途 |
| --- | --- | --- |
| `processed_trace.txt` | `:1930` | prompt 里的可选 `TRACE` 段（`populate.py:161`） |
| `messages.json` | `:1926` | 只在 `execution_result.csv` 为空时用：`_last_sql_error_in_messages` 从里面捞最后一条 `SQL_ERROR:`，让模型能区分「查询报错」和「结果集真的为空」 |

### 5.3 runner 顺带写的、populate 不读

`gt_query.sql`、`gt_result.json`、`execution_result.json`、`<id>.sql`（gold SQL 的副本）。
留着无害，便于人工核对。

### 5.4 所以 train 那一跑该怎么发

**只跑 agent，不要带 `--refine-cte`。** populate 学的是「裸 agent 错在哪」，refiner 会把
`execution_query.sql` 之后的产物搅进来（虽然 `execution_query.sql` 本身在精修之前就写盘了，
`:1924` 早于 `:1972`，但多花钱且无用）。

```bash
source .env && python -u -m src.agents.sql_agent_runner \
  --bird-json data/minidev/MINIDEV/mini_dev_sqlite.json \
  --split data/splits/bird_minidev_train.txt \
  --out-base outputs/bird_train \
  --model gpt-4.1 \
  2>&1 | tee outputs/bird_train.log
```

跑完每个实例目录里应该有 5.1 + 5.2 那五个文件。

**关键预期：train 的有效产出只有「答错的那些」。** populate 第一步就是正误闸门，
答对的直接 `skipped="correct"`、不调 LLM、零规则。实测（`compare_pandas_table` +
`gold_bird`）**82 对 / 42 错**，比早先按 60% 估的「约 50 条能学」略少失败、略多跳过。

---

## 6. populate BIRD store（适配已实现；全量抽规则尚未跑）

Alg 2/3、正误闸门、对照修理、写 CSV **不重写**。`populate_from_output_dir` /
`populate_split`（`tkstore/populate.py`）原样用。不能拿默认 CLI 对着
`mini_dev_sqlite.json` 开跑：实例表读不进，SQLite 路径也解析不到 BIRD 的库文件。

Train agent 已跑完（2026-09-24）。`agent_result_matches_gold` + `evaluation/gold_bird`
实测 **82 条判对**（`skipped="correct"`）、**42 条判错**可抽规则。124 个目录齐全，
gold SQL 也齐全。闸门用的是我们的比较器，会把 `minidev0007` 那种「按题答对、gold 表形态不同」
的例子也送进 populate。

### 6.1 产物路径

沿用 `implementation_plan.md` §2.2：**自己 populate 的 store 写 `artifacts/`，
绝不追加 `tkstore/tkstore_*.csv`。**

```
data/bird_minidev.jsonl                       # 旁路 JSONL，见 §6.3 A
artifacts/tkstore_bird_minidev.csv            # 我们的 BIRD store
evaluation/gold_bird/sql/*.sql                # gold SQL 从这里读，不进 JSONL
evaluation/gold_bird/exec_result/*.csv
evaluation/gold_bird/spider2lite_eval.jsonl
outputs/bird_train/                           # populate 的输入
outputs/bird_train/populate_report.json       # populate_split 自动写
outputs/bird_test_agent/
outputs/bird_<round>_refonly/
outputs/bird_<round>_tk/
outputs/bird_<round>_comparison.csv
```

`tkstore/config.py` 的 `TKSTORE_BIRD_PATH` **不是前置条件**。store 始终 `--store`
显式传入。若加配置项，不要让 `_default_index_for_engine('sqlite')` 指到它，否则和
Spider2 撞车。

### 6.2 现状（读代码）

| 点 | 代码 | 现在会怎样 |
| --- | --- | --- |
| 实例表 | `_load_instance_record`（`populate.py:48`）逐行 JSONL，找 `instance_id`，再用 `question` / `db` / `external_knowledge` | BIRD 是 JSON 数组，字段是 `db_id` / `evidence`，没有 `minidev####`。默认 `--jsonl-path data/spider2-lite.jsonl` 里也没有这些 id → `KeyError` |
| Gold SQL | `Path(gold_sql_dir) / f"{instance_id}.sql"`（`:144`） | 与 JSONL 无关。`--gold-sql-dir evaluation/gold_bird/sql` 即可 |
| 正误闸门 | `agent_result_matches_gold(..., gold_dir)`（`:140`） | `--gold-dir evaluation/gold_bird` 即可（目录里已有 `exec_result/` 和 `spider2lite_eval.jsonl`） |
| Gold 结果进 prompt | 实例目录的 `gt_result.csv`（`:158`） | agent 步已从 `gold_bird` 拷好，不用再适配 |
| Evidence | `load_external_knowledge(instance_id, record["external_knowledge"])`（`:149`） | `minidev` 前缀已按**正文**返回。JSONL 里这一格必须是 evidence 原文，不能是文件名 |
| SQLite 路径 | `populate_split` 调 `resolve_sqlite_db_path(instance_id)`（`:301`），**不传** `db_id` | minidev 分支要求「id 以 `minidev` 开头 **且** 有 `db_id`」，否则返回 `None`。CSV 非空时对照修理还能走文件；空结果重跑 SQL、以及 store 的 `db` 列会空 |

### 6.3 两处小适配（已落地）

**A. 旁路 JSONL，不改 `_load_instance_record` 的解析方式。**

生成 `data/bird_minidev.jsonl`（gitignore 与否自定；建议提交，和 splits 一样是 populate 前置）。
用已有的 `load_minidev_records`，保证 id 与 `bird_minidev_index.csv` 一致。每行：

| JSONL 字段 | 来源 | 注意 |
| --- | --- | --- |
| `instance_id` | `minidev%04d` | 禁止另编一套 |
| `db` | BIRD `db_id` | 字段名必须是 `db`，populate / store 都读这个 |
| `question` | `question` | |
| `external_knowledge` | `evidence` **正文** | 空串写成 `null` 或省略。不要写文件名，不要在 `data/spider2/` 下造 md |

**不要**写入官方 `SQL` 字段。Gold SQL 只从 `evaluation/gold_bird/sql/<id>.sql` 引入。

**B. 解析 DB 路径时补上库名。**

不是新 CLI。`resolve_sqlite_db_path(instance_id, db_id=None)` 已有第二参。
在 `populate_from_output_dir` 里 `db_name = record.get("db")` 之后，若调用方没给
`db_path_or_cred`，则 `resolve_sqlite_db_path(instance_id, db_name)`。
`populate_split` 可以不再自己解析路径，交给这一处。Spider2 的 `local*` 有 map，
多传 `db` 也不应打坏现有路径。

### 6.4 适配后的命令（适配已完成；抽规则需 LLM，先不跑）

```bash
source .env && python -m tkstore.populate \
  --outputs-base outputs/bird_train \
  --split-file data/splits/bird_minidev_train.txt \
  --jsonl-path data/bird_minidev.jsonl \
  --gold-dir evaluation/gold_bird \
  --gold-sql-dir evaluation/gold_bird/sql \
  --store artifacts/tkstore_bird_minidev.csv \
  --model gpt-4.1 \
  --verbose
```

默认会 wipe 再写 `--store`（没有 `--no-rebuild`）。约 42 条 × 对照修理 / 抽规则 / 打标
三次 LLM。

### 6.5 验收

- `populate_report.json` 里 `skipped="correct"` 条数 = 82（与闸门实测一致）；有规则的实例来自那 42 条失败
- `git status` 里 `tkstore/` 四个 CSV 一个字节没动
- `artifacts/tkstore_bird_minidev.csv` 10 列；`scope` 只有 `db` 和 `generic`；`db` 列是 BIRD 的 11 个库名而不是 `all`
- 抽规则用的 evidence 非空（有 evidence 的失败实例）；`load_external_knowledge` 没有因为找文件失败而整段丢掉

### 6.6 明确不改

- 不把 BIRD gold 拷进 `evaluation/gold/`
- 不改闸门比较器（第一轮仍是 `compare_pandas_table`）
- 不为格式不一致的 gold（如 `minidev0007`）单独开白名单；记下即可
- 本步不跑 test agent / 两臂

---

## 7. 评测适配

`evaluate_spider2sql` 换个 `--gold_dir` 就能跑，不用新写入口。但**比较器要定**。

### 7.1 现有比较器的实际行为

`compare_pandas_table`（`evaluate.py:80-127`）把两张表转置，每个元素是一**列**：

| 维度 | 行为 |
| --- | --- |
| 列顺序 | **永远忽略**。对每个 gold 列去 pred 的列里找一个匹配的 |
| 列名 | 完全不比 |
| 行顺序 | `ignore_order=True` 时忽略，做法是**每列各自独立排序**再比 |
| 行数 | 必须一致（`vectors_match` 里的 `len(v1) != len(v2)` 比的是行数） |
| 数值 | 绝对容差 **0.01** |
| pred 多出的列 | 不惩罚（只遍历 gold 列） |

**缺陷**：`ignore_order=True` 的按列独立排序会**丢掉行的对应关系**，两列各自排序相等
但行配对错乱的情况会被判对。这是虚高的来源。

### 7.2 BIRD 官方 EX

读的是本地副本 `/home/qchenax/data/text_to_sql_benchmarks/text_to_sql_agents/MAC-SQL/evaluation/evaluation_bird_ex.py`
（E-SQL / TA-SQL / RSL-SQL 几份副本实现一致）：

```python
predicted_res = cursor.fetchall()      # 行 tuple 的 list
ground_truth_res = cursor.fetchall()
res = 0
# todo: this should permute column order!      ← 官方源码原有的注释
if set(predicted_res) == set(ground_truth_res):
    res = 1
```

| 维度 | 行为 |
| --- | --- |
| 行顺序 | 忽略（set） |
| 行重复度 | **也被忽略**（set 去重，这是官方的已知宽松处） |
| 列顺序 | **不忽略**，tuple 按位置比 |
| 列名 | 不比 |
| 数值 | **无容差**，精确相等 |

mini-dev 还新增了 soft F1（`E-SQL/evaluation/evaluation_f1.py`）：先 set 去重，再按
**去重后 list 的位置**配对 gold 行与 pred 行，行内用 `pred_val in ground_truth_row` 判断，
所以行内列顺序被忽略。但那个行配对用的是 Python set 的迭代顺序，本身没有意义，指标是脆的。

### 7.3 两边的能力是互补的

「行列重排后比较」这件事分布在两边：**列重排容忍我们有、BIRD 官方没有**；
**行集合相等 BIRD 官方有、我们只是按列独立排序的近似**。

### 7.4 两个工程约束

1. **BIRD EX 要原生 tuple。** 它比的是 sqlite `fetchall()` 的结果。我们的
   `execution_result.csv` 是 runner 写出的 CSV，pandas 读回来类型会变（int/float 推断、
   NULL→NaN）。精确复刻 BIRD EX 必须**重新执行 predicted SQL**，而现在的评测不碰数据库。
2. **和 §9 决策 1 耦合。** 关掉列名提示后 agent 的输出列序不受约束：在我们的比较器下无所谓
   （列序被忽略），在 BIRD EX 下会直接扣分。

### 7.5 `ignore_order` 怎么填（已定）

BIRD 不提供这个标注。**按 gold SQL 里有无 `ORDER BY` 自动判定**，由
`scripts/build_bird_gold.py` 生成 `bird_eval.jsonl`：有 `ORDER BY` → `ignore_order: false`，
否则 `true`。`condition_cols` 一律 `[]`。

理由：比全填 `true` 更严格，能避免 §7.1 那个「按列独立排序丢行对应」的缺陷被最大化；
语义上也与 Spider2 那份逐实例标准的意图一致。

实现细节：判定要**忽略子查询和窗口函数里的 `ORDER BY`**——只有最外层
（或最后一个 `SELECT` 之后）的 `ORDER BY` 才决定输出行序。`OVER (... ORDER BY ...)`
和 CTE 内部的排序都不算。用 `src/utils/agent_utils.py` 里现成的注释/字符串剥离逻辑先清洗，
再判最外层。

---

## 8. 顺序与成本

```
1. 软链数据                              §1     分钟级
2. 生成 id 映射与 train/test 划分         §2     分钟级
3. 生成 gold SQL / 执行结果 / eval 标准    §3     分钟级（498 条本地 SQLite）
4. 适配 instance loader + evidence        §4     不调 LLM；含 gold_dir 与关掉列名提示
5. 跑 train 的 agent                     §5     **已完成**：124/124，闸门 82 对 / 42 错
6. populate 出 BIRD store                 §6     旁路 JSONL + db 路径已适配；抽规则约 42×3 LLM
7. 跑 test 的 agent（共享产出）                  见下
8. 两臂精修 + 评测 + compare_arms
```

**成本必须先缩规模。** 按 Spider2 实测线性外推（86 实例：agent 约 2 小时、
臂 A 1.58 小时、臂 B 3.09 小时）：

| 规模 | agent | 两臂 | 合计 |
| --- | --- | --- | --- |
| test 全量 374 | ~9 h | ~20 h | **~29 h** |
| **test 随机 1/3 ≈ 125（第一轮）** | ~3 h | ~7 h | **~10 h** |

第一轮用 **test 的随机 1/3（125 条）**，规模与 Spider2 的 86 同量级、结论能并排读，
先确认 db 规则在「45 题一个库」下是否终于转正，再决定是否扩到 374。
`bird_minidev_test.txt` 仍按全量 374 定死，子样本是从 test 里再抽一层，**不改 train**。

臂的配置直接沿用 J 轮那套（当前默认已是 `--cross-db-generic never`）：

```bash
--refiner-turns 5 --refiner-min-probes 3 \
--adopt-refiner-sql --validate-fix-in-context --verdict-attempts 3
```

臂 B 额外带 `--tkstore artifacts/tkstore_bird_minidev.csv --filter-model gpt-4.1 --context-filter`。
**这次不用显式传 `--cross-db-generic`**：BIRD 每个库都有同库规则，`never` 不会像 Spider2
那样把大半实例的规则清零。

---

## 9. 决策记录

### 9.1 已定

| # | 决策 | 取值 | 影响 |
| --- | --- | --- | --- |
| 1 | agent 是否收到 gold 列名提示 | **关掉** | 与 BIRD 官方设置一致；**与我们 Spider2 那八轮不再同口径**，要登记进 `deviations.md` |
| 2 | split seed | **0** | 最小库 `california_schools` 有 6 条 train |
| 3 | train 规模 | 先 **25%（124 条）** | 跑完 agent 步数答对率再决定是否加大 |
| 4 | 第一轮 test 规模 | test 里**随机 1/3 ≈ 125 条**（不分层） | 约 3 小时 agent + 7 小时两臂；后续再全量 374 |
| 5 | 比较器 | **第一轮只用 `compare_pandas_table`** | 零代码改动；容忍列重排，正好配合决策 1；与我们八轮的比较逻辑一致 |
| 6 | `ignore_order` | **按 gold SQL 有无最外层 `ORDER BY` 自动判定** | 见 §7.5 |

**决策 1 的实现**：放在 §4，不另开一步。`sql_agent_runner.py:1894-1905` 由 gold CSV 列名生成
`expected_output_format`。实例 id 以 `minidev` 开头时置 `None`，不进 agent prompt。
注意 `load_ground_truth` 仍然要读 gold CSV（`gt_result.csv` 靠它），只是不再把列名转给 agent。

**决策 4 的实现**：子样本单独落一个文件，不改已提交的 train/test：

```
data/splits/bird_minidev_test_sub.txt      # 125 行，seed 固定，是 test 的子集
```

**决策 5 的已知代价**：绝对分数**与 BIRD 榜单不可比**，只能用于臂 A / 臂 B 的差值归因，
以及与我们自己 Spider2 各轮的并排。要对外引用绝对分数时再补 BIRD 官方 EX
（重新执行 predicted SQL、`set(rows)` 相等），那时要连带记住它**不容忍列重排**，
与决策 1 叠加会扣掉一批本来正确的答案。这一条也要写进 `deviations.md`。

---

## 10. 总验收

- 11 个库的 `.sqlite` 全部能由 `resolve_sqlite_db_path` 解析
- `data/splits/bird_minidev_{train,test}.txt` 共 498 行、无交集、已提交
- `evaluation/gold_bird/` 下 498 个 gold SQL + 498 个执行结果
- train 的 124 个实例目录各有 `execution_query.sql`（非空）、`execution_result.csv`、
  `gt_result.csv`、`processed_trace.txt`、`messages.json`
- `populate_report.json` 里 `skipped="correct"` 的条数与评测算出的 train 答对数一致
  （这是正误闸门没写错的交叉校验）
- `artifacts/tkstore_bird_minidev.csv` 的 `db` 列是 BIRD 库名；`tkstore/*.csv` 未被修改
- 臂 A 有 0 个 `retrieved_rules.json`，臂 B 每个实例都有；`compare_arms.py` 输出无 unpaired 警告
- **BIRD 特有的那条**：`compare_arms.py` 的 `n_db_rules` 分组里，
  「无 db 规则」那一组应该接近 0 个实例——如果不是，说明 store 或库名对不上，要先查
