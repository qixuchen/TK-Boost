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

## 产物目录约定

| 路径 | 内容 | 是否进版本库 |
| --- | --- | --- |
| `data/splits/` | train / test 划分文件 | **进**，复现必需 |
| `artifacts/` | populate 生成的 TK-Store CSV | 不进，已 ignore |
| `outputs/` | agent 运行产物、trace、评测中间结果 | 不进，已 ignore |
| `tkstore/tkstore_*.csv` | 上游随仓库发布的示例 store | 上游已跟踪，**本轮不改动** |

上游那四个 store CSV 一个字节都不要动。我们的规则写进 `artifacts/`，
这样 `git status` 能一直保持干净，也不会把实验产物和上游示例数据混在一起。

## 阶段总览

| 阶段 | 内容 | 消除的偏差 | LLM 成本 | 依赖 |
| --- | --- | --- | --- | --- |
| 1 | 数据与路径基础设施 | A5、C1–C4、C6–C11 | 无 | — |
| 2 | Populate（Alg 2 / 3） | A2、A3、A4、A6 | 中（train 集 agent 跑一遍 + populate） | 阶段 1 |
| 3 | Retrieve + Augment（Alg 4 / 5） | A1、B1、B7 | 无（改代码） | 阶段 2 |
| 4 | Pipeline runner + 评测 | C5 | 高（test 集跑两遍） | 阶段 3 |

阶段 1 完全不花 LLM 调用，且是后面所有阶段跑通的前提，先做。

---

# 阶段 1 — 数据与路径基础设施

## 1.0 测试脚手架

**用途**：仓库目前零测试基础设施（C10），TDD 无从开始，所以这是第一步。

**输入**：无。

**输出**：

```
tests/__init__.py            # 空文件，让 tests 成为包
tests/conftest.py            # 共享 fixture
requirements-dev.txt         # pytest>=8.0
```

`pytest` 放 `requirements-dev.txt` 而不是 `requirements.txt`，避免污染运行时依赖。

**约定的测试命令**（后续每个 TDD 循环都用它，只换文件名）：

```bash
python -m pytest tests/test_db_paths.py -v
```

**验收**：`python -m pytest tests/ -v` 能跑起来（哪怕 0 个测试）。

## 1.1 库路径解析重构

**用途**：把数据库路径解析从"靠物理目录布局硬凑"改成"查官方映射表"，
统一四条重复实现，并修掉吞异常和动态 exec。消除 C1、C2、C3、C9、C11。

关键发现：`local-map.jsonl` 是 **Spider2 官方的实例→库映射**，
已验证 135 个实例 → 30 个库，映射值加 `.sqlite` 后缀就是共享目录里的确切文件名
（含 `Db-IMDB`、`sqlite-sakila` 这类不规则命名），零缺口、双向一致。
所以**不需要任何归一化匹配启发式**，之前那套小写去下划线的逻辑可以整个扔掉。

### 已确定的设计决策

| 决策 | 选择 |
| --- | --- |
| 失败契约 | 抛专用异常 `DbPathNotFound`（携带尝试过的路径列表），另留一个返回 `Optional` 的薄封装给现有调用方 |
| 映射表位置 | 拷一份进仓库并提交，优先用它；`$SPIDER2_DB_ROOT` 里的作为回落 |
| `SPIDER2_DB_ROOT` 默认值 | **无默认值**。未设置时跳过依赖它的步骤，并在错误信息里提示去设置 |
| harness 那份坏逻辑 | 在本节一并统一 |
| 返回路径形式 | 一律绝对路径 |
| 根目录锚定 | 锚定仓库根（`Path(__file__)` 推导），不依赖 cwd |
| 文件校验强度 | 只检查存在性，不打开验证是否合法 SQLite（那是 executor 的职责） |
| `db_id` 与映射冲突 | 映射表优先；映射表查不到该 instance 时才拿 `db_id` 当文件名试 |

### 公开 API

```python
# src/utils/db_paths.py

class DbPathNotFound(Exception):
    """携带诊断信息，取代原先被吞掉的 FileNotFoundError。"""
    instance_id: str
    db_id: Optional[str]
    attempted: List[str]        # 按顺序试过的每个绝对路径
    hints: List[str]            # 如 "SPIDER2_DB_ROOT is not set"

def get_database_path(
    instance_id: str,
    db_id: Optional[str] = None,
    *,
    db_root: Optional[str] = None,      # 便于测试注入，默认读 SPIDER2_DB_ROOT
    repo_root: Optional[str] = None,    # 便于测试注入，默认从 __file__ 推导
) -> str:
    """成功返回绝对路径；失败抛 DbPathNotFound。"""

def resolve_sqlite_db_path(
    instance_id: str,
    db_id: Optional[str] = None,
) -> Optional[str]:
    """向后兼容薄封装：捕获 DbPathNotFound，打印诊断后返回 None。"""
```

`db_root` / `repo_root` 是**关键字参数且可注入**，这样测试可以用 `tmp_path` 造假目录，
不必 monkeypatch 模块内部变量。

### 解析优先级

```
1. minidev 分支（instance_id 以 minidev 开头）
     <repo_root>/data/minidev/MINIDEV/dev_databases/<db_id>/<db_id>.sqlite
2. 映射表查 instance_id → basename，两个来源按序：
     a. <repo_root>/data/spider2_local_map.json      # 仓库内，已提交
     b. <db_root>/local-map.jsonl                    # 共享目录，回落
   命中后 → <db_root>/<basename>.sqlite
3. 映射表查不到该 instance 且给了 db_id → <db_root>/<db_id>.sqlite
4. 回落 <repo_root>/data/spider2/<instance_id>/*.sqlite      # 向后兼容
5. 回落 <repo_root>/<db_id>.sqlite                           # 保留原有 fallback
```

**`SPIDER2_DB_ROOT` 未设置时不要立即硬失败** —— 步骤 2b / 3 需要它，跳过即可；
步骤 1、4、5 不需要它，仍应照常尝试。只有全部失败时才抛异常，
并在 `hints` 里带上 `SPIDER2_DB_ROOT is not set`。这条容易写错，单独列一个测试。

### 要改的文件

| 文件 | 改动 |
| --- | --- |
| `src/utils/db_paths.py` | 重写。承载 `DbPathNotFound`、`get_database_path`、`resolve_sqlite_db_path`。删掉 `:9-17` 的动态 `spec_from_file_location`（C2） |
| `src/agents/cte_refiner.py` | 删掉 `:63-88` 的 `get_database_path`，改为 `from src.utils.db_paths import get_database_path`。修正倒置的依赖方向（C3） |
| `src/agents/sql_agent_runner.py` | 删掉 `:596-597` 那个纯转发的 `resolve_db_path_for_sqlite`，两个调用点（`:720`、`:918`）直接调 `resolve_sqlite_db_path` |
| `tkstore/harness.py` | 把 `:1186-1204` 内联的 sqlite 解析抽成 `_resolve_db_path_or_cred(instance_id, db_id, engine)` 并改调新解析器，修掉少一层目录的 bug（C9） |
| `data/spider2_local_map.json` | 新增，从 `local-map.jsonl` 拷入，**要提交** |

抽出 `_resolve_db_path_or_cred` 是为了可测 —— 原逻辑埋在 `run_diff_for_instance`
的 jsonl 循环里，不抽出来就只能靠跑 LLM 才能覆盖。

### 测试清单（TDD 的 Red 列表）

放在 `tests/test_db_paths.py`。fixture 用 `tmp_path` 造一个假 `db_root`
（几个空 `.sqlite` 文件 + 一个假映射）和假 `repo_root`，全部通过关键字参数注入。

| # | 行为 |
| --- | --- |
| 1 | 映射表命中 → 返回 `<db_root>/<basename>.sqlite` 的绝对路径 |
| 2 | 不规则命名能解析（`Db-IMDB`、`sqlite-sakila`），作为对旧启发式的回归保护 |
| 3 | 仓库内映射优先于共享目录映射（两者对同一 instance 给出不同 basename 时） |
| 4 | 仓库内映射缺失时回落到共享目录映射 |
| 5 | 映射查不到 instance 但给了 `db_id` → `<db_root>/<db_id>.sqlite` |
| 6 | 映射与 `db_id` 冲突时，映射优先 |
| 7 | 回落到 `<repo_root>/data/spider2/<instance_id>/*.sqlite` |
| 8 | minidev 分支解析 `dev_databases/<db_id>/<db_id>.sqlite` |
| 9 | 每个成功分支返回的都是绝对路径 |
| 10 | cwd 无关：`monkeypatch.chdir(tmp_path)` 后仍能解析 |
| 11 | 全部失败时抛 `DbPathNotFound`，且 `attempted` 非空、按尝试顺序排列 |
| 12 | `SPIDER2_DB_ROOT` 未设置时不崩，仍尝试步骤 1/4/5；失败时 `hints` 含未设置提示 |
| 13 | `resolve_sqlite_db_path` 薄封装在失败时返回 `None` 而不是抛异常 |
| 14 | `harness._resolve_db_path_or_cred` 对 sqlite 实例返回与解析器一致的路径（C9 回归） |
| 15 | `harness._resolve_db_path_or_cred` 对 bq / snowflake 实例仍返回凭证路径 |

**验收**：上面 15 条全绿；额外跑一次**真实环境**的冒烟检查 ——
对全部 135 个实例调用 `get_database_path`，全部解析成功，且返回路径都真实存在。
这条冒烟检查依赖本机数据，不进 `tests/`（否则别人 clone 下来必失败），
放 `scripts/` 下当一次性校验脚本。

## 1.2 清理 symlink，切换到共享库目录

**用途**：消除 C4。当前 `data/spider2/` 下 134 个绝对路径 symlink 指向 30 个真实库，
冗余 4.5 倍且不可移植。

**输入**：1.1 完成后的解析器；现有 `data/spider2/` 目录树。

**输出**：

- 删除 134 个 symlink
- 删除 `data/spider2/local007/Baseball.sqlite` —— MD5 与共享目录里的同名文件一致，
  是纯重复的 30MB
- **保留** 135 个 `data/spider2/<instance_id>/` 目录。它们不是只放数据库的：
  `src/utils/agent_utils.py:84::load_external_knowledge` 从
  `data/spider2/<instance_id>/<filename>` 读 external knowledge
- 从 `~/Spider2/spider2-lite/resource/documents/` 拷入 13 个 external knowledge 文件
  （见下）
- 把 `SPIDER2_DB_ROOT` 写进 `.env`，并让解析器在环境变量缺失时回落读它
- `data/instance_db_mapping.csv` 保留但降级为参考，不再作为解析依据（C11）。
  已确认代码里零引用，所以降级是零成本的

**关于 external knowledge**：原计划以为实例目录里存着 `DDL.csv` 和各表 JSON，
实际上 `find data/spider2 -type f ! -name '*.sqlite'` 返回 **0** —— 135 个目录里
除了那个 symlink 什么都没有。而 `data/spider2-lite.jsonl` 里有 **13 个** local 实例
声明了 `external_knowledge` 文件（`local003` → `RFM.md`，`local009`/`local010` →
`haversine_formula.md` 等）。`load_external_knowledge` 在文件不存在时静默返回
`None`（C12），所以这 13 个实例的输入长期不完整而无人发现。文件必须补齐，
因为 external knowledge 是论文设定里 agent 输入的一部分。

**删掉 symlink 后 `SPIDER2_DB_ROOT` 就是必需的**，而 runner
（`python -m src.agents.sql_agent_runner`）不像 `tkboost.init()` 那样读 `.env`，
只读 shell 环境变量。忘设的后果是跑到一半才发现全部实例解析失败。
所以解析器改为：环境变量优先，其次读仓库根的 `.env`。

**验收**：

1. symlink 数为 0，真实 `.sqlite` 数为 0
2. 不导出环境变量、只靠 `.env`，135 个实例仍全部解析成功
3. 解析来源全部是共享目录映射，**0 个**走 `data/spider2` 回落
   （symlink 还在时这条无法验证，容易被掩盖）
4. 13 个 external knowledge 文件都能被 `load_external_knowledge` 真实读出

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

生成命令（`seed=0` 已记录在 `data/splits/README.md` 里）：

```bash
python scripts/make_splits.py --seed 0 --train-size 34
```

纯函数放 `src/utils/splits.py`（`make_split` / `write_split` / `load_split`），
因为阶段 2.3 的 populate CLI 和阶段 4 的 runner 都要读划分文件，
`load_split` 属于代码库而不只是脚本。

> **本节的 seed=0 随机划分已被 2.0 取代。** 后来发现上游随仓库发布的
> `tkstore/tkstore_sqlite.csv` 泄露了论文的 train 集（32 个 `instance_id`，
> 对应 Table 13 的 `n=34`），采用它比随机划分离论文近得多。下面关于随机划分的
> 记录保留备查，`data/splits/` 的两个文件会按 2.0 重新生成，
> `scripts/make_splits.py` 要增加"从 store 复原 train 集"的模式。
> 本节里 `src/utils/splits.py` 的纯函数（`make_split` / `write_split` / `load_split`）
> 仍然有用，`load_split` 是阶段 2.3 和阶段 4 的依赖。

**注意两点**，都要写进最终报告：

- 论文正文只给了数量，没给实例 ID 列表。**但 train 集可从上游 store 复原**，见 2.0。
- 不做按库分层，因为论文没提，分层会引入新的不可比性。

**db 覆盖率比预期好得多。** 实际生成后测得：21/30 个库至少有一个 train 实例，
18 个库同时出现在两侧，**77/101（76%）** 的 test 实例所属库有 train 数据。
原先我担心"34 个 train 摊到 30 个库、大部分库只有 0–1 个"导致 db 专属规则几乎无法命中，
这个判断是错的 —— 实例在库上的分布很不均匀，有好几个库各带 7–9 个实例，
随机抽 34 个大概率落在这些大库里，而它们同时也装着大部分 test 实例。
所以 76% 是 db 专属规则能触及的上限，剩下 24 个实例只能靠 generic 规则。

**验收**：

1. train 34 行、test 101 行
2. 两者无交集
3. 并集等于 jsonl 里全部 135 个 `local*` 实例
4. 两侧每个实例的数据库都能解析成功
5. 重跑 `make_splits.py` 后文件 MD5 不变（确定性）

## 1.4 修文档与评测脚本容错

**用途**：消除 C5、C6、C7。这些是纯粹的时间黑洞，上一轮排查成本几乎全在这里。

**输入/ 输出**：

| 项 | 改动 |
| --- | --- |
| C5 | `evaluation/evaluate.py` 新增三个可测的纯函数：`build_eval_dataframe`（用 `pd.DataFrame(rows, columns=EVAL_COLUMNS)` 保证空输入也带 score 列）、`normalize_result_dir`（去掉尾部斜杠）、`require_predictions`（零匹配时抛 `NoPredictionsFound` 并列出可能原因）。入口处捕获它和 `FileNotFoundError`，转成 `error: ...` + 退出码 1 |
| C6 | 删掉 `src/agents/sql_agent_runner.py` 那句过期的 `reduced max_turns` 注释（因 1.1 删了 4 行，实际在 `:722` 而非 `:725`） |
| C7 | `evaluation/README.md` 改成 gold SQL 在 `evaluation/gold/sql/`；修正 `spider2lite_eval.jsonl` 的 cp 源路径（上游在 `evaluation_suite/gold/` 下，不在 `evaluation_suite/` 下）和目标路径（要进 `evaluation/gold/`，不是 `evaluation/`）；换掉虚构的 outputs 目录示例并说明目录命名要求 |
| C13（新发现） | 删掉 `evaluation/evaluate.py:24` 的 `import duckdb` |

**C13 的严重性**：`duckdb` 在 `evaluate.py` 全文只出现在那一行 import，从未被使用，是从上游 Spider2 继承来的。但它是模块级 import，所以在没装 duckdb 的环境里 `evaluate.py --help` 都会 `ModuleNotFoundError`。这一项排在 C5 前面，因为不删掉它，C5 的验收根本没法跑。

**验收**（全部已通过）：

1. 空 `--result_dir` → `error: No evaluable instances found under ...` + 三条可能原因 + 退出码 1，不是 `KeyError: 'score'`。
2. 不存在的 `--result_dir` → `error: Result path not found: ...` + 退出码 1，不是 traceback。
3. 真实实例目录**带与不带尾部斜杠，聚合结果与 `evals.csv` 路径完全一致**——尾斜杠正是上一轮踩到 `KeyError` 的实际触发条件。
4. README 里声明的每个路径都实测存在：`evaluation/gold/sql/`（256 个文件）、`evaluation/gold/exec_result/`（2040 个）、`evaluation/gold/spider2lite_eval.jsonl`。

**顺带的结构改动**：新增空的 `evaluation/__init__.py`，让 `evaluation.evaluate` 可以被测试导入。`evaluate.py` 没有相对导入，所以 `python evaluation/evaluate.py` 的用法不受影响。

---

# 阶段 2 — Populate（Alg 2 / Alg 3）

## 2.0 路线：双轨制（因 gold SQL 不可得）

这一节是阶段 2/3 的总纲，先读它再读后面的分节。

公开的 spider2-lite 只发布 24/135 个 local 实例的 gold SQL（详见 deviations 的
"gold SQL 可得性约束"），而论文 Alg 3 的 `MakeCorrection(q,D,s,s★,R,R★)` 需要 `s★`。
论文作者用了非公开的 gold SQL：从上游 store 复原的 32 个 train 实例里只有 7 个有公开 gold SQL。
所以我们**无法在全部 train 实例上复现忠实的 Alg 3**。

对策是把"复现论文结果"和"验证我们的 populate 实现"拆成两条独立的轨道：

| | 轨道 B：我们自己的实现（主）| 轨道 A：上游 store（参照）|
| --- | --- | --- |
| 目的 | 端到端复现整套方法并测准确率提升 | 提供论文真实知识的参照点 |
| TK-Store 来源 | 我们 populate 产出的 `artifacts/tkstore_sqlite.csv` | **上游 `tkstore/tkstore_sqlite.csv`**（已验证是 Spider2 真实产物，只读）|
| 覆盖实例 | 24 个 train 实例，**全部有 gold SQL，可走忠实 Alg 3** | 论文 32 个 train 实例学到的 118 条规则 |
| 评测范围 | 全部 **111** 个 test | 仅 **86** 个未污染子集 |
| 成功标准 | test 上 augmented 显著优于 baseline | 与轨道 B 在同一 86 子集上的对照 |

轨道 B 是主轨：24 个 train 实例全有 gold SQL，所以整条 Alg 2/3 链路都能忠实跑通，
产出的 store 是我们自己的端到端成果。轨道 A 提供一个"论文真实知识能做到多少"的参照。

**额外的实现保真度证据**：那 7 个既在我们 train 又被上游 store 训练过的实例
（`local004`、`local039`、`local075`、`local099`、`local163`、`local197`、`local301`），
可以把我们产出的规则与上游同 `instance_id` 的行并排对比。这个对比是 populate 质量检查，
不涉及评测，所以不受子集问题影响。不要求逐字相同（两次 LLM 调用不可能一致），
要求 `scope` 分类一致、`sql_operations` 有实质重叠、规则指向同一类错误。
写进 `artifacts/populate_comparison.md`。

**划分按 gold SQL 可得性决定**（已实施）：有 gold SQL 的 24 个当 train，其余 111 个当 test。
理由是 Alg 3 需要 `s★`，而 gold 结果 135/135 齐全所以评测不受影响。

这条规则的收益是轨道 B 大幅升级：train 的 24 个**全部**有 gold SQL，
所以我们自己的 populate 能在每一个 train 实例上跑完整忠实的 Alg 3，
而不是只在 7 个上验证。我们的 store 因此成为正当的主产物。

代价是与上游 store 有交叉污染：它训练过的 32 个实例里 7 个落在我们 train、
25 个落在我们 test。所以额外生成第三个文件
`data/splits/spider2_sqlite_test_no_reference_leak.txt`（86 个），
**上游 store 只在这个子集上评测**，我们自己的 store 在全部 111 个上评测。
报告里必须声明两者的评测范围不同，不能直接比绝对数值。

**轨道 B 的对比方法**：对那 7 个实例，把我们产出的规则与上游 store 里**同 `instance_id`** 的行
并排放。不要求逐字相同（两次 LLM 调用不可能一致），要求的是：`scope` 分类一致、
`sql_operations` 有实质重叠、规则指向的是同一类错误。这份对比写进
`artifacts/populate_comparison.md`，是实现保真度的唯一证据。

## 2.1 生成 train 集 agent 输出

**用途**：produce 论文经验元组 `e = (q, τ, s*)` 里的 `τ`（agent 执行轨迹）和 agent SQL。
populate 是从**真实 agent 的错误**里学规则，所以这一步不能跳过、也不能用 LLM 造假 draft 替代。

**输入**：`data/splits/spider2_sqlite_train.txt`（24 个实例，全部有 gold SQL，见 2.0）。

注意 Alg 3 第 1 行是 `s ← incorrect SQL in τ` —— **populate 只能从 agent 做错的实例里学**。
agent 做对的实例没有 correction 可提取，不产出规则。这也解释了上游 store 为什么是 32 个实例
而论文 Table 13 说 `n=34`：少数实例产出 0 条规则。

**实测产出率远低于早先预估**。头 7 个 train 实例跑完后
`evaluate.py --result_dir outputs/train` 给出 5/7 = 0.71，只有 `local003`（答非所问，
返回 min/max 汇总而不是 RFM 分桶）和 `local019`（SQL 未能执行）是错的。
早先基于 batch_5 的「约 25% 正确率、24 个里约 18 个能进 populate」已作废：
按 0.71 外推，24 个里只有约 7 个能进 populate，我们的 store 会明显小于上游的 118 条。
n=7 波动很大，但方向上要留意——按 gold SQL 可得性挑出的这 24 个可能系统性偏简单。
最终数字等 24 个跑完再确认。

**输出**：每个实例一个目录，含

```
execution_query.sql      # agent 最终 SQL
execution_result.csv     # 它的执行结果
processed_trace.txt      # 轨迹 τ
messages.json            # 完整对话
gt_result.csv            # gold 执行结果
```

**命令**（现有 CLI，无需改动）：

```bash
set -a; source .env; set +a

python -m src.agents.sql_agent_runner \
  --jsonl-path data/spider2-lite.jsonl \
  $(grep -v '^#' data/splits/spider2_sqlite_train.txt | sed 's/^/--instance-id /') \
  --out-base outputs/train \
  --model azure/gpt-4.1 \
  --verbose
```

三个容易踩的点：

`--run-all-from-file` 是 `store_true` 开关，**不接路径**（`sql_agent_runner.py:834` 无条件读
整个 `--jsonl-path`）。能限定实例的只有可重复的 `--instance-id`，所以要把划分文件展开。

`set -a` 不能省。`.env` 里 `SPIDER2_DB_ROOT` 那行没有 `export` 前缀，光 `source .env`
只会变成 shell 局部变量，传不进子进程。

`--model` 必须显式给。runner 不读 `.env` 的 `TKBOOST_MODEL`（那个只被 `tkboost.init()` 读），
它的默认值是 `azure/gpt-4.1`，经 `AZURE_TO_OPENAI_MODEL` 映射成 `gpt-4.1`。三处默认值不一致，
写出来避免歧义。

注意**不加** `--refine-cte`。这一步要的是 agent 未经修正的原始产物，
带 refine 的输出属于阶段 3。

**断点续跑是现成的**：`has_completed_output` 会跳过 `--out-base` 下已有非空
`execution_query.sql` 的实例，中断后重跑同一条命令不会重复花钱。

**验收**：每个目录都有非空 `execution_query.sql` 和 `processed_trace.txt`。
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
    gold_dir: str = "evaluation/gold",     # 正误闸门要的 exec_result/ 与 eval jsonl
    store: Optional[str] = None,           # 建议显式传 artifacts/ 下的路径，见下方说明
    db_name: Optional[str] = None,         # 缺省取 jsonl 的 db 字段
    db_path_or_cred: Optional[str] = None, # 缺省由 1.1 的解析器给出
    model: Optional[str] = None,
    max_turns: int = 6,
    verbose: bool = True,
) -> Dict[str, Any]
```

`gold_dir` 和 `db_path_or_cred` 是相对初版签名的两处增补：前者是正误闸门的必需输入，
后者对应 `run_diff_for_instance` 已有的 `db_path`，同时让测试不依赖开发机上的数据库。

**重跑语义**归 2.3：单实例函数只追加，清空重建由批量 CLI 在全量运行前做。

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

**已确定的设计决策**：

| 项 | 决定 | 理由 |
| --- | --- | --- |
| 正误闸门位置 | **放在 `populate_from_output_dir` 内部**：先比对 `R` 与 `R★`，判定正确就直接返回 `rule_count=0` 且 `skipped="correct"`，**一次 LLM 都不调** | Alg 3 第 1 行要求 `s` 是 incorrect SQL。放在函数内意味着单实例调用也安全，不依赖调用方先跑 evaluate；2.3 的批量 CLI 因此不需要重复这段逻辑 |
| 正误判定方式 | **复用 `evaluation/evaluate.py` 的 `agent_result_matches_gold`**（内部走 `compare_multi_pandas_table`） | 与最终评测同一口径，支持多个 gold 变体（`local019` 有 `_a`/`_b`）和 `ignore_order`。自己写一份 CSV 等价比较会与评测口径漂移 |
| `db_name` | **必须显式传 jsonl 的 `db` 字段**，不能沿用现有入口的 `None` | 见 deviations A7。不传就等于所有行 `db="all"`，库专属规则退化成全局规则，且阶段 3 验收会假通过 |
| `clean_summary` 行 | **不写进 TK-Store** | 两条检索路径都显式跳过 `scope='question'` 和 `sql_operations='NA'`，是永远召回不到的死行；上游参考库里这类行也是 0 条。实现上不用改 `_persist_via_tkstore`——它写这行有 `if clean_summary:` 守卫，传 `""` 即可跳过，同时真实的 clean_summary 照常喂给 tagger |
| 重跑语义 | **全量运行前清空 store 重建** | `TKStore.insert` 不去重，追加会产生重复规则并放大阶段 3 的召回 |
| agent SQL 执行失败的实例 | **重跑 agent SQL 取回真实报错**喂给 diff，`messages.json` 里最后一条 `SQL_ERROR:` 作兜底 | 只传空字符串的话 LLM 分不清是语法错还是空结果集，而这类样本恰好 correction 信号最强。原计划写的「从 `messages.json` 提取」**实测不成立**：`run_agent` 只在循环内的探针失败时才把 `SQL_ERROR:` 追加进 messages（`sql_agent_runner.py:380`），**最终 SQL 的执行失败只 print 到 stdout**（`:393`），既不进 messages 也不进 trace。`local019` 就是这种情形，messages.json 里一条 `SQL_ERROR:` 都没有。重跑是本地 SQLite、确定性、零成本，且拿到的是精确报错——实测取回 `SQL_ERROR: no such column: winner_id` |
| 模型 | 与 agent 同一个模型，默认取 `.env` 的 `TKBOOST_MODEL` | 现状三处默认值不一致：runner 的 `--model` 是 `azure/gpt-4.1`，populate 相关 helper 是 `azure/o4-mini`，`.env` 另有 `TKBOOST_MODEL`。读 `.env` 可复用 `src/utils/db_paths.py` 里已有的 `_read_dotenv_value` |
| 文本解析 helper | 直接从 `builder.py` 导入 `_extract_clean_summary` 和 `_extract_memories_from_rules` | 零改动、零风险；不动 `run_diff_for_instance` 里那份内联副本 |

**处理流程**（LLM 阶段直接复用现有函数，它们本身没问题）：

第 0 步之前先过正误闸门（见上表）。`agent_result_matches_gold(pred_csv, instance_id, gold_dir)`
的语义已定并有测试覆盖：预测文件缺失或为 0 字节算**错**（这是 correction 信号最强的样本，
不能抛异常）；gold 结果缺失或该实例不在 `spider2lite_eval.jsonl` 里则**抛异常**，
因为无法判定的实例不该悄悄当成错的喂给 Alg 3。

实际是 6 步，不是 4 步 —— 中间两步纯文本解析容易漏，但少了它们 tagger 拿不到必填入参：

```
0. format_csv_as_table + _is_csv_like   harness.py:1137-1164  → 把结果 CSV 转 markdown 表
     并让 SQL_ERROR: 前缀的内容原样透传，不要跳过这层
1. generate_memory_diff_first_turn      harness.py:55         → diff 文本   [Alg 3]
     传入 processed_trace_text=<τ>，这是与 builder 路径的关键区别
2. generate_rules_from_diff             harness.py:968        → 规则文本
     注意 tkstore/rules.py 只是个单行 re-export shim，实现只有 harness 这一份
3. _extract_clean_summary(diff)         builder.py:160        → clean_summary
4. _extract_memories_from_rules(rules)  builder.py:165        → database/generic memories
     ↑ 这两步是 tagger 的必填入参 database_memories / generic_memories 的唯一来源
5. generate_tagged_memories_json        tagger_index.py:31    → 结构化 JSON  [GenTKRow]
     必须传 db_name=<jsonl 的 db>，见上表
6. _persist_via_tkstore                 builder.py:19         → 写库        [TK-Store.insert]
     传 clean_summary="" 以跳过那条不可检索的 question 行
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
      "skipped": Optional[str],  # "correct" 表示正误闸门判定 agent 做对了，未调 LLM
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

**不需要**自己 `mkdir -p`：`TKStore._ensure_well_formed` 已经做了
`p.parent.mkdir(parents=True, exist_ok=True)`，文件不存在时自动写 10 列表头，
列数不匹配时抛 `ValueError`。

A3 在新入口是**天然规避**的，不需要额外工作：`_default_index_for_engine('sqlite')` 返回的
就是 `TKSTORE_SQLITE_PATH`，本身是对的。A3 的病灶是 `tkstore/config.py:26` 的
`MEMORY_INDEX_PATH = TKSTORE_BQ_PATH`，只有走 `_append_memories_index` / `add_memory`
的老路径才会中招。新入口的唯一要求是**不要碰 `config.MEMORY_INDEX_PATH`**。

**验收**：

1. 新增行的 `db` 列等于该实例 jsonl 里的 `db`，**不是 `all`**（这是 A7 的直接验收；
   原先写的"`TKStore(store).rows()` 不抛异常"在新入口下恒真，没有区分力，已废弃）
2. store 是 10 列，`scope` 只出现 `db` 和 `generic`，`scope='question'` 的行数为 **0**
3. 规则进 `artifacts/tkstore_sqlite.csv`；上游的 `tkstore/tkstore_*.csv` 四个文件
   `git status` 里保持干净，一个字节都没动
4. `MemoryRetriever(store).retrieve(sql, generic_only=False, db=<db>)` 能召回到 db 专属规则，
   且换一个**不相干的 `db=` 值时召回不到**这些 db 专属行 —— 只做前半段的话，
   `db="all"` 的退化情形会假通过
5. 对 `evaluate.py` 判为正确的实例（当前 7 个里的 `local004`、`local017`、`local039`、
   `local058`、`local066`）调用后返回 `rule_count=0`、`skipped="correct"`，store 行数不变

## 2.3 批量 populate CLI

**用途**：消除 A6。同时强制只在 train 集上 populate。

**输入**：

```bash
python -m tkstore.populate \
  --outputs-base outputs/train \
  --split-file data/splits/spider2_sqlite_train.txt \
  --jsonl-path data/spider2-lite.jsonl \
  --store artifacts/tkstore_sqlite.csv \
  --verbose
```

**输出**：填充好的 `artifacts/tkstore_sqlite.csv`；一份 `outputs/train/populate_report.json`
记录每个实例产出多少条规则、失败原因。两者都不进版本库。

**必须内置的护栏**：如果 `--outputs-base` 下出现了不在 `--split-file` 里的实例，
直接报错退出（退出码 1），**先于**清空 store，而不是静默 populate 进去。这是防泄漏的最后一道闸。
空目录也按目录名计入，所以一次中断留下的 test 实例目录同样会挡住。

train 集里还没有非空 `execution_query.sql` 的 id 记为 `skipped: "missing_output"`，
不调 LLM、不算失败。默认每次全量运行前删除 `--store` 再重建；`--no-rebuild` 留给调试。
模型缺省读 `.env` 的 `TKBOOST_MODEL`。

**验收**：`artifacts/tkstore_sqlite.csv` 行数 > 0；`instance_id` 列的取值集合是 train 集的子集。
当前 7 个产物里只有 `local003` 和 `local019` 会写入规则，所以验收要等这批（或后续补跑）真的调 LLM 之后才能在磁盘上看到行数 > 0；函数与 CLI 的行为已由 `tests/test_populate_split.py` 覆盖。

---

# 阶段 3 — Retrieve + Augment（Alg 4 / Alg 5）

**本轮的 store 输入是上游的 `tkstore/tkstore_sqlite.csv`**，不是我们 populate 的产物，
理由见 2.0 的双轨制。这个文件只读不写，`git status` 里必须保持干净。

它是可以直接用的：列与 `TKStore.HEADER` 逐字节相同，`TKStore(path).rows()` 能读出 118 行；
66 条 `scope='db'` 行里 63 条的 `db` 值与 jsonl 的 `db` 字段一致，所以检索时传
`db=<jsonl 的 db>` 能正常命中库专属规则。

**前提**：它只能在 `data/splits/spider2_sqlite_test_no_reference_leak.txt`（86 个）上评测，
因为它训练过的 32 个实例里有 25 个落在我们的 test 里。我们自己的 store 走全部 111 个。

**规则覆盖实测**（决定了能期待多少增益）：66 条 `scope='db'` 行覆盖 22 个库，
但 86 个免泄漏 test 实例里只有 52 个的库在其中，另外 34 个
（`BowlingLeague`、`EntertainmentAgency`、`complex_oracle`、`electronic_sales`、`f1`、
`imdb_movies`、`log`、`oracle_sql`、`school_scheduling` 共 9 个库）只能吃 generic 规则。

`generic_only=False`（B7）是真的要紧。先前拿 local003 单个 CTE 实测时，20 条候选**全是
generic**，`generic_only` 的两个取值结果相同，据此一度判断 B7 影响不大 —— **这个判断是错的**。
扩到 4 个实例 24 个片段后，db 专属规则在 8 个片段里进入了候选
（`local004::customer_summary` 2 条、`local017::top2_sets` 1 条、`local066` 多个片段 1–2 条），
`generic_only=True` 会把它们全丢掉。local003 那次只是恰好该 CTE 的 3 条 db 规则被
`data_type` / `nulls` 维度先滤掉了。

## 3.0 前置修复：CTE 循环的陈旧快照

**用途**：这不是工程洁癖，是 Alg 5 的保真问题，必须在 3.1 之前修。

`perform_refinement_and_revision` 的 CTE 循环里，主 agent 采纳新解后有一行

```python
ctes, remainder_sql = parse_ctes_from_sql(final_sql)   # sql_agent_runner.py:535
```

注释写的是 "Refresh CTE bodies from adopted solution"，但 `for idx_cte, c in enumerate(ctes)`
的迭代器在循环开始时就绑定到了原 list 对象上，这里只是**重新绑定名字**，迭代器毫无感知。
于是第 2 个 CTE 之后拿到的都是修订前的旧 CTE 体，传给 refiner 的
`previous_ctes_text = ctes[:idx_cte]` 也是旧的。更别扭的是循环**之后**的 final SELECT
那段读的是重绑定后的新值 —— 同一个函数里循环用旧的、final SELECT 用新的，半生效。

论文里 `number_of_CTEs(s_t)` 和 `c_i` 都取自**当前**的 `s_t`，每轮从最新 SQL 重取。
所以这是对 Alg 5 的偏离，不只是工程 bug。接上检索后它还会让 `retrieved_rules.json`
里记的「规则是为这段 SQL 检索的」对不上真正在跑的 SQL，直接毁掉阶段 4.2 的 `rules_used` 归因。

**改法**：把 `for idx_cte, c in enumerate(ctes)` 换成显式下标的 `while` 循环，
每轮从最新的 `ctes` 取 `ctes[idx]`。注意新解的 CTE 个数可能变化，循环条件要用
`idx < len(ctes)` 实时求值。

**验收**：Red 用一个 stub 掉 `refiner_run` 与 `llm_completion` 的测试 —— 第 1 个 CTE 被
"修好"后返回一个 CTE 体全变了的新解，断言第 2 轮 refiner 收到的 `cte_text` 是**新**的体。
修之前该断言必须失败。

**已完成**（偏差记为 C16）。`tests/test_refinement_cte_refresh.py` 5 个测试，改前 2 红 3 绿。
产品改动 6 行：`for idx_cte, c in enumerate(ctes)` → `while idx_cte < len(ctes)` + `c = ctes[idx_cte]`
+ 循环末尾 `idx_cte += 1`。

实际缺陷比原先描述的窄：`previous_ctes`（`ctes[:idx_cte]`）和循环之后的 final SELECT 段
读的都是重绑定后的**新**值，陈旧的只有循环变量 `c` 本身和迭代轮数。那两条断言写成了
characterization 测试留作重构护栏。

**顺带发现并修掉了 C17**，它直接决定阶段 3 的有效样本量。`parse_ctes_from_sql` 有两处
会让 CTE 列表静默丢失或截断：定位 `WITH` 用的是裸词 `\bwith\b`，会命中前导注释里的英文
"with"（`local066` 因此解析出 **0 个 CTE**，尽管 SQL 里有 10 个 `AS (`）；remainder 起点用
右括号之后 20 字符内有无 `SELECT` 判断，下一个 CTE 名字短的时候会把它自己的 `SELECT`
算进去从而截断。解析成 0 个 CTE 时逐 CTE 循环整个跳过，Alg 5 的 per-CTE 检索一次都不发生。

改法：`WITH` 改用跳过注释的 `_find_sql_keyword`；remainder 边界改成"跳过空白与注释后
是否为逗号"——逗号说明还有 CTE，否则就是 remainder。`tests/test_parse_ctes.py` 14 个测试，
分两个循环各自见过 Red（3 红 / 2 红）。修后手上 12 个真实 SQL 的 `parsed == AS( 计数`
全部相等，`local066` 从 0 个恢复成 10 个。

即便如此，3.1 的 `retrieved_rules.json` 仍要记下 `n_ctes` —— 这个解析器是 best-effort 的，
真解析成 0 个 CTE 时那个实例其实是退化成单次整句精修，不记下来会把它的 0 增益误算进结论。

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
filter_model: str = "gpt-4.1",           # FilterKnowledge 用的模型，必须显式传，见下
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
       llm_model=filter_model,    # 必须显式传，默认值会静默退化，见下
       # 绝对不要传 instance_id：search_index_for_sql:532 用同名局部变量覆盖了形参（C14），
       # 传了等于没过滤，还会让人误以为做了实例级过滤
   )
   ```

3. 拼接格式沿用 `tkboost/__init__.py:615` 已有的写法，保持两条路径一致：

   ```python
   cte_goal = f"{goal}\n\nUse these tribal knowledge rules as guidance:\n\n{rules_block}"
   ```

4. **同样要在 final SELECT 那一段注入**（`:554-570`）。原计划漏了这个点：
   现在传的是不含任何知识的固定串 `f"Final SELECT using {len(ctes)} CTE(s)"`，
   而 `tkboost.sql()` 这一侧对 remainder 也是调 `_rules_block_for` 的
   （`tkboost/__init__.py:659`）。**更要紧的是 draft SQL 不含 `WITH` 时 `ctes` 为空、
   CTE 循环根本不执行，整条 SQL 全落在这个分支** —— 只改循环的话，所有无 CTE 的实例
   会一条规则都注入不到。检索的 `sql_text` 用 `rebuild_sql_from_ctes(ctes, remainder_sql)`
   的完整 SQL，与 `tkboost/__init__.py:659` 一致。
5. 检索为空时保持 `goal` 原样，不要塞空的知识段落进 prompt

**三个参数必须显式设成这样**，否则跑的不是论文方法：

- `generic_only=False`（B7，三处默认值不一致）
- `use_llm_filtering=True`（B1，默认 `False` 会跳过 `FilterKnowledge`）
- `llm_model` 显式传一个 **OpenAI 口径**的模型名（决策：`gpt-4.1`，与 agent / populate 一致）

第三条是个**静默失效陷阱**，单独说清。`tkstore/tagger_index.py` 是三个模块里唯一
没有 `AZURE_TO_OPENAI_MODEL` 映射的（`sql_agent_runner.py:40` 和 `cte_refiner.py:21` 都有），
它直接 `litellm.completion(model=model)`；而 `MemoryRetriever.retrieve` 的默认
`llm_model="azure/gpt-4.1"`（`tagger_index.py:1043`）。我们的 `.env` 只有 `OPENAI_API_KEY`
和 `TKBOOST_MODEL="gpt-4.1"`，没有 `AZURE_*`。这个调用会抛异常，然后被
`_llm_filter_relevant_rules` 自己的兜底吞掉、**原样返回全部候选规则**
（`tagger_index.py:745-750` 和 `:1016-1022`），只在 stdout 打一行 `[ERROR in LLM filtering]`。
结果是"开了 FilterKnowledge"和"没开"看起来都正常，实际上 Alg 4 的最后一步整步没跑，
prompt 里塞的是 20 条未筛选规则。

**输出**：

- 每个 CTE 的 refiner prompt 里带上检索到的规则
- 落盘一份 `<out_dir>/retrieved_rules.json`，用于事后归因，别省。schema 要**区分
  code filter 候选与 LLM 选中**，否则分不清规则是在 Alg 4 哪一步丢的：

  ```json
  {"tkstore": {"path": "tkstore/tkstore_sqlite.csv", "sha1": "3f75f169…", "n_rules": 118},
   "n_ctes": 2, "filter_model": "gpt-4.1", "use_llm_filtering": true,
   "retrievals": [{"stage": "cte", "name": "customer_months", "sql_sha1": "…",
                   "candidates": ["24", "26"], "selected": ["26"]}]}
  ```

  `n_ctes` 用于统计有多少实例因 C17 退化成 0 个 CTE，见 3.0 末尾。
  `sql_sha1` 是被检索的那段 SQL 的指纹，用于确认规则是针对**当前**而非陈旧的 CTE 检索的。

  `tkstore` 这一组是**进 4.1 之前补的**（`_store_provenance`）。3.3 冒烟时报告里只有
  `filter_model` 和 `use_llm_filtering`，**不记用了哪份 store**，只能靠 `mem_id` 最大值
  反推（冒烟出现 98，故知是 118 条的上游 store 而非我们那份 8 条的）。4.1 的两臂只差 store
  这一个变量，某一臂传错就无从事后区分，所以记路径 + 文件 sha1 + 行数：两臂即使 store 同名
  也能靠 sha1 分开。文件读不出时三字段为 null 而不是丢掉整份报告，因为 `_knowledge_options`
  已在 CLI 边界拦过不存在的路径。

  原计划里还有个 `filter_ok` 字段，**去掉了**：`_llm_filter_relevant_rules` 在自己内部
  吞掉异常并返回全量候选，我们在外面拿不到真假，写一个可能撒谎的字段比不写更糟。
  改成靠下面的显式守卫在**调用前**拦住已知的静默退化。
- 最终 SQL 仍由现有 `_choose_and_mark_final_artifacts`（`:62`）提升为
  `execution_result_final.csv`，评测侧不用改

**验收**：

1. 不传 `--tkstore` 时，输出与阶段 3 之前逐字节一致（保证 baseline 可比）
2. 传 `--tkstore` 时，`retrieved_rules.json` 非空，且 refiner trace 里能看到规则文本
3. 传一个在 `AZURE_TO_OPENAI_MODEL` 里没有对应项的 `azure/...` 模型名时**直接报错**，
   而不是跑完之后才发现 FilterKnowledge 没生效
4. 无 CTE 的输入也能拿到规则：用一条不含 `WITH` 的 SQL 走单元测试，断言
   final SELECT 分支的 `cte_goal` 里含规则文本
5. 至少有一个实例的 `execution_query_after_<cte>.sql` 与 `execution_query.sql` 不同，
   证明回环真的生效了

**原来的验收 3 是错的，已改成上面这条。** 原文写的是"至少有一条记录满足
`len(selected) < len(candidates)`，用来抓静默退化"。实测推翻了它：拿 `local003` 的 CTE
对真实上游 store 跑一次真调用，20 条候选**全部被选中**（`selected == candidates`），
且没有任何错误输出，说明过滤确实成功执行了，只是 `_process_rule_chunk` 的 prompt 明确写着
"It's better to include a rule that might be relevant than to exclude one"，天然偏向全选。
所以"没收窄"不能推出"没生效"，这条断言会给出假警报。

**FilterKnowledge 实测（4 个实例 / 24 个片段 / 34 次调用）**：`local004`、`local017`、
`local039`、`local066`，用它们真实的 agent SQL 逐 CTE 加 final SELECT 跑完两级漏斗。

| 指标 | 结果 |
| --- | --- |
| 候选合计 → 选中合计 | 290 → 243（收窄 16%） |
| 发生收窄的片段 | 17 / 24 |
| 每片段候选数范围 | 1 – 36 |
| FilterKnowledge 调用数 | 34（约 8.5 次/实例） |

**结论：保留 `use_llm_filtering=True`。** 它确实在做筛选，不是 pass-through。
7 个"一条没淘汰"的片段几乎都是候选本来就极少的（1 / 4 / 5 / 7 条），无可筛。
收窄最明显的是候选多的片段：`qualifying_cities` 5→1、`set_frequency` 17→10、
`customer_summary` 23→15、`local066` 的 final SELECT 27→19。

**这推翻了先前基于 `local003` 单点得出的"基本不起筛选作用"。** 那一次是 20→20，
属于异常样本而非常态。教训是不要用一个片段的观察去推断整个检索层的行为。

顺带两条：final SELECT 片段的候选数总是全实例最多的（整条 SQL 匹配到的操作维度最多），
所以它也是最需要过滤的地方；`local066` 能逐 CTE 检索 10 次是 3.0 修掉 C17 的直接收益，
修之前它是 0 次。

## 3.2 CLI 参数贯通

**用途**：让阶段 4 能从命令行控制知识注入。

**输入 / 输出**：`src/agents/sql_agent_runner.py::main()`（`:813`）新增

| 参数 | 含义 |
| --- | --- |
| `--tkstore <csv>` | tkstore CSV 路径，不传则不注入知识 |
| `--no-llm-filtering` | 关掉 `FilterKnowledge`，仅用于消融实验 |
| `--filter-model <name>` | FilterKnowledge 的模型，默认 `gpt-4.1` |

**两个调用点都要透传**，不只 `main()`：`:1002`（正常跑）和
`:747::run_refinement_on_existing_outputs`（`--refine-output` 续跑）。漏掉后者会让
复跑时知识静默丢失，而这条路径恰好是最容易被用来省钱重跑的。

**`--tkstore` 必须校验前置条件，不能静默无效**。检索只发生在
`perform_refinement_and_revision` 里，而它只在 `if args.refine_cte and final_sql`
（`:1000`）时被调用。所以：

- 传了 `--tkstore` 但没传 `--refine-cte` → 直接报错退出，别静默跑成 baseline
- `--tkstore` 指向不存在的路径 → 报错退出，别静默跳过

现有的 `--tribalknowledge-all-scopes` 保持原样不动（决策）—— 它最终传给 `run_refiner`
的那个同名参数是死参数（B12），改它没有意义；更不要把它接到新的 `generic_only` 上，
那会让人误以为过去带这个 flag 跑的实验是有效的。

**验收**：`--help` 能看到新参数；`--tkstore` 缺 `--refine-cte` 或路径不存在时报清晰错误。

**已完成。** `tests/test_cli_knowledge_args.py` 11 个测试，写完先跑全红（`_build_parser`
尚不存在），实现后全绿。为了让 CLI 可测，把 parser 从 `main()` 里抽成 `_build_parser()`，
校验与 kwargs 组装抽成纯函数 `_knowledge_options(args)`，两个调用点都用 `**_knowledge_options(args)`
透传。

`--refine-output` 也被接受为合法的 refinement stage（它无条件精修，不需要 `--refine-cte`）。
两条拒绝路径实测退出码均为 2，消息分别是
`--tkstore has no effect without --refine-cte or --refine-output, because retrieval only runs during refinement`
和 `--tkstore path does not exist: <path>`。

**覆盖缺口（明确记下）**：`--refine-output` 续跑路径的透传有测试（stub 掉
`perform_refinement_and_revision` 后断言收到了 `tkstore_path`）；**正常跑那条路径的透传没有自动化测试**，
因为它要先跑完整个外层 ReAct 循环，成本不合适。那处只靠 `_knowledge_options` 的单元测试
加人工核对调用点。3.3 冒烟时 `retrieved_rules.json` 是否出现，就是这条路径的实际验证。

## 3.3 链路冒烟（放开全量之前）

**用途**：花小钱验证两个注入点、FilterKnowledge 真的生效、以及 agent 回环真的改了 SQL。

**实例**（决策：5 个，刻意覆盖 db 规则的三档密度，都来自 86 个免泄漏子集）：

| 实例 | 库 | db 规则数 | 覆盖意图 |
| --- | --- | --- | --- |
| `local074` | `bank_sales_trading` | 12 | db 规则最密，最可能命中库专属知识 |
| `local025` | `IPL` | 6 | 中等密度 |
| `local070` | `city_legislation` | 1 | 稀疏，验证只有 1 条时不崩 |
| `local310` | `f1` | 0 | 无 db 规则，只走 generic |
| `local269` | `oracle_sql` | 0 | 同上，另一个库 |

**有 CTE / 无 CTE 这个维度没法事前选**——取决于 agent 当场吐什么。所以：跑完先检查这 5 个
里有没有产出不含 `WITH` 的 SQL；如果没有，无 CTE 那条路径就靠 3.1 验收第 4 条的单元测试
兜住，不额外花钱去凑一个真实实例。

**验收**：3.1 的 5 条验收在这 5 个实例上全过。

## 3.3 结果

5 个实例全部跑完（`outputs/smoke_augmented/`，约 45 分钟）。

| 实例 | 库 | n_ctes | 检索片段 | 候选→选中 | 选中的 db 规则 | `after_*.sql` | 最终 SQL 有变化 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `local025` | IPL | 5 | 6 | 90 → 68 | 6 | 5 | 是 |
| `local070` | city_legislation | 5 | 6 | 74 → 57 | 0 | 3 | 是 |
| `local074` | bank_sales_trading | **0** | 1 | 36 → 31 | 6 | 0 | **否** |
| `local269` | oracle_sql | **0** | 1 | 21 → 16 | 0 | 0 | **否** |
| `local310` | f1 | 5 | 6 | 101 → 70 | 0 | 2 | 是 |
| 合计 | | | 20 | 322 → 242 | | 10 | 3/5 |

**通过的验收**：`retrieved_rules.json` 全部生成（这同时是 3.2 里那条没有自动化测试的
正常跑路径的实际验证）；20 个片段里 15 个发生收窄，与先前 4 实例漏斗研究一致；
每个片段的 `sql_sha1` 互不相同，说明规则是针对各自那段 SQL 检索的；
3 个实例产出了 10 个 `execution_query_after_*.sql` 且与原始 SQL 不同，agent 回环有效。

**3.0 修复在生产里的直接证据**：`local025` 的原始 SQL 有 6 个 CTE，精修 `over_runs` 时
agent 的新解删掉了 `over_with_bowler`，列表变成 5。修复前迭代器绑定原始 6 元素列表，
会去精修一个已不存在的 CTE；修复后循环重新读取，`retrieved_rules.json` 里正好是 5 次
CTE 检索且没有 `over_with_bowler`。

### 三个必须记下的问题

**一、验收 #2 按原文无法满足，已作废。** 原文要求"refiner trace 里能看到规则文本"。
但 `cte_refiner` 的 `trace.add_section` 只记 USER QUERY / LLM THINKING / SQL QUERY /
SQL RESULT / VERDICT，**从不记录自己的 prompt**，而 `[CTE_GOAL]` 才是知识的唯一落点。
实测把 `local074` 选中的 31 条规则原文逐条去 trace 里找，命中 0 条 —— 这不代表注入失败，
只代表 trace 记不下来。注入本身由 3.1 的单元测试覆盖。记为 B15。

**二、C18：解析器还有两种语法会返回 0 个 CTE，冒烟里命中 2/5。**
`local269` 是 `WITH RECURSIVE packaging_expansion AS (`，解析器把 `RECURSIVE` 当 CTE 名
读掉；`local074` 是 `WITH\nmonths(month) AS (`，CTE 带列名列表。两者都在"读完名字后硬性
期待 `AS`"这一步 break。与 3.0 修的 C17 同类但成因不同。

**三、B14：final SELECT 的 verdict 算出来就丢掉，从不回灌 agent。**
逐 CTE 那段有完整的 feedback → agent 重出 `<solution>` → 落盘回环；final SELECT 这段
只调 `refiner_run` 写 `refiner_final_select.json`，没有 `messages.append`，没有修订循环。

**二 + 三叠加的后果是本轮最重要的发现**：任何解析出 0 个 CTE 的实例，增强对最终 SQL 的
影响必然为零 —— 知识检索到了（`local074` 36→31、`local269` 21→16）、注入 refiner 了、
verdict 也产出了，但没有任何通路能改动输出。这两个实例的 `execution_query_final.sql`
与 `execution_query.sql` 逐字节相同。也就是说，如果直接放开全量，
**这类实例会以"增益 0"的身份计入结果，而原因是工程缺陷而非知识无效**。

冒烟集里占 40%。放开 86 个实例前必须先量化真实占比，否则 baseline vs augmented 的
差值会被系统性稀释。

### 三个问题的处置（均已修，TDD）

| 编号 | 修法 | 回归覆盖 |
| --- | --- | --- |
| C18 | `WITH` 后跳过可选 `RECURSIVE`；CTE 名后遇 `(` 用 `_skip_balanced_parens` 跳过列名列表再期待 `AS` | `tests/test_parse_ctes.py::TestCteHeaderSyntax`（6 例） |
| B15 | `[CTE_GOAL]` 拼好处补 `trace.add_section("CTE GOAL", ...)` | `tests/test_refiner_trace_goal.py`（3 例） |
| B14 | 抽出 `_feedback_text` / `_revise_from_feedback`，逐 CTE 与 final SELECT 共用回灌逻辑；final-select 产物在 `_choose_and_mark_final_artifacts` 里提到最高优先级 | `tests/test_final_select_feedback.py`（8 例） |

B14 里那条优先级调整是必须的：`_choose_and_mark_final_artifacts` 原本让调用方传的
`last_cte_name` 压过 mtime，final-select 的修订会被静默丢弃，等于白做。

C18 修完后 `local074` 由 0 个 CTE 变 6 个（`months, customers, month_grid, txn_sums,
joined, final`），`local269` 由 0 个变 3 个（`packaging_expansion, leaf_expansion,
leaf_totals`）。

全量测试 169 passed。

### 修复后的端到端复验（`local074`，`outputs/smoke_verify/`）

挑之前"增强必然无效"的 `local074` 重跑一遍：

- **C18**：`n_ctes` 由 0 变 4（`txn_monthly, cust_calendar, txn_activity, final`），检索片段由
  1 个变 5 个，5 个片段全部发生收窄（24→20、7→6、24→17、13→5、35→29）。
- **B15**：`refiner_txn_monthly_trace.txt` 里有 `=== CTE GOAL ===` 段落，且该片段选中的
  20 条规则**原文全部可在 trace 中定位**，归因链路完整可审计。
- **B14**：这一轮 final SELECT 的 verdict 是 `ok`，回灌路径没被触发（生产侧未验证，
  逻辑由 8 个单元测试覆盖）。但产物优先级已生效：`execution_query_final.sql` 取自
  `execution_query_after_txn_activity.sql`，**与原始 SQL 不同**（修复前逐字节相同）。

也就是说这个实例从"结构上不可能产生增益"变成了真的被修订。

### 修复后 5 实例整体重跑（`outputs/smoke_augmented_v2/`，约 50 分钟）

| 实例 | n_ctes 前→后 | 片段 前→后 | 候选→选中（后） | `after_*` 前→后 | 最终 SQL 有变 前→后 | finalSel verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `local025` | 5 → 3 | 6 → 4 | 73 → 60 | 5 → 2 | True → True | ok |
| `local070` | 5 → **0** | 6 → 1 | 9 → 4 | 3 → 1 | True → True | **issues（已回灌）** |
| `local074` | **0 → 5** | 1 → 6 | 91 → 67 | 0 → 1 | **False → True** | ok |
| `local269` | **0 → 3** | 1 → 4 | 44 → 32 | 0 → 1 | **False → True** | **issues（已回灌）** |
| `local310` | 5 → 5 | 6 → 6 | 104 → 75 | 2 → 3 | True → True | ok |
| 合计 | | 20 → 21 | 321 → 238 | | **3/5 → 5/5** | |

**核心结果：最终 SQL 发生变化的实例由 3/5 变成 5/5。** 之前那两个"结构上不可能产生增益"
的实例现在都被真正修订了。整体过滤收窄率 26%（321 → 238），17/21 个片段发生收窄。

**B14 的生产侧这次被验证了。** `local070` 与 `local269` 的 final SELECT verdict 是
`issues`，回灌确实触发并落了 `execution_query_after_final_select.sql`。其中 `local070`
这轮 agent 产出的是纯 `UNION ALL`、**完全不含 `WITH`** 的查询（已核对全文，0 个 CTE 是
正确解析而非新 bug），修复前它必然空转 —— 这正是 B14 要救的场景。

**B15 可审计性 5/5 通过**：每个实例首个片段选中的规则原文在对应 trace 里 100% 可定位
（5/5、4/4、1/1、1/1、13/13），`=== CTE GOAL ===` 段落全部存在。

### 正确性评测（补做，`evaluation/evaluate.py --mode exec_result`）

3.3 原本只验了链路与结构，没有比对执行结果。补跑评测后：

| 实例 | 修复前 base → final | 修复后 base → final |
| --- | --- | --- |
| `local025` | ✗ → ✗ | ✗ → ✗ |
| `local070` | ✗ → ✗ | ✗ → ✗ |
| `local074` | **✓ → ✓** | **✓ → ✓** |
| `local269` | ✗ → ✗ | ✗ → **✓** |
| `local310` | ✗ → ✗ | ✗ → **✓** |
| 合计 | 1/5 → 1/5 | 1/5 → **3/5** |

`base` 是精修前的 `execution_result.csv`，`final` 是精修后的 `execution_result_final.csv`。
两轮都**没有出现 ✓ → ✗ 的回退**。

**修复前精修的净收益是 0**：3 个实例的 SQL 被改动，但一个都没改对。修复后变成 +2。

**但 +2 不能都记在修复上，逐实例归因如下**：

- `local269` 与修复有合理因果链：修复前 0 个 CTE、增益结构上不可能；修复后拿到 3 个 CTE
  的逐段精修，且 final SELECT verdict 为 `issues` 并成功回灌，然后变对。
- `local310` **与三个修复都无关**：两轮都是 5 个 CTE（C18 不影响它），final SELECT verdict
  为 `ok`（B14 未触发）。它修复前 ✗→✗、修复后 ✗→✓，只能归因于 run-to-run 波动。
- `local074` 虽然是 C18 修复效果最显著的实例（0→5 个 CTE），但它**两轮 base 就已经是对的**，
  分数没变。前文强调它"从不可能产生增益变成真的被修订"在链路上成立，在分数上无体现。

**更重要的是这组数字不能当作知识增益。** 外层 ReAct agent 全程看不到 tribal knowledge
（知识只进 `refiner_run` 的 `cte_goal`），所以 `base` 确实是无知识产出、`final` 是带知识
精修后产出。但 refiner 同时在探库，`base → final` 的差值里**混着探库增益与知识增益**，
正是先前讨论过的两臂设计的归因缺陷。要单独归因给知识，仍需第三臂（带 refiner 不带知识）。

n=5，且下面这条波动观察成立，因此 1/5 → 3/5 不具统计意义，只能作为"链路没跑坏、且方向
不为负"的信号。

**一个要带进阶段 4 的观察：run-to-run 波动很大。** 同一实例两次运行的 CTE 结构可以完全
不同（`local025` 5→3、`local070` 5→0），候选规则数随之从 74 掉到 9。这说明
baseline vs augmented 的**单次对比噪声很高**，差值小于波动幅度时不可解读。阶段 4 需要
先确定重复次数或改用配对设计。

---

# 阶段 4 — Pipeline runner 与评测

## 4.0 给 runner 补 `--split`

**用途**：让 `sql_agent_runner` 能按划分文件跑，并支持分批。

**为什么现在才做**：阶段 1.3 已经写了"阶段 4 的 runner 要读划分文件"
（见 1.3 末尾的注），但那时只建了 `src/utils/splits.py::load_split` 这个纯函数，
**从没接到 CLI 上**。实际清点 `_build_parser`（`sql_agent_runner.py:963`）只有两个入口：

| 现有参数 | 问题 |
| --- | --- |
| `--instance-id`（可重复） | 能用，但 86 个实例要展开成 86 个参数 |
| `--run-all-from-file` | 读 jsonl 全部 **547** 个实例，含 bq / sf，用不了 |

所以 4.1 的第一条命令目前无法直接写出来，必须先补这个参数。

**位置**：`src/agents/sql_agent_runner.py::_build_parser` 与 `main()` 的实例筛选段
（`:1017-1028`）。

**参数**：

| 参数 | 含义 |
| --- | --- |
| `--split PATH` | 划分文件路径，用现成的 `load_split`（会跳过 `#` 注释行） |
| `--split-offset N` | 从第 N 个开始，默认 0 |
| `--split-limit N` | 最多取 N 个，默认全部 |

后两个是给 4.1 分批用的：单臂 86 个实例约 14 小时，切成小批便于分段推进。
按划分文件加偏移量比手工拼 ID 列表可靠。（写这条时 `--refine-output` 的续跑还是整目录
全有或全无，分批是唯一的止损手段；C19 修掉之后两条路径都是实例级续跑，
分批的作用降级成控制单条命令的墙钟时间，见 4.1 的"断点续跑与分批"。）

**行为**：

- 与 `--instance-id`、`--run-all-from-file` 三者互斥，同时传要报错而不是静默择一
- 划分文件里的 ID 若不在 jsonl 中，沿用现有的 `missing` 报错路径（`:1028`）
- 文件不存在时给可读错误，不要 traceback

**验收**（TDD）：

1. `--split` 只跑文件里的实例，且 `#` 注释行被跳过
2. `--split-offset` / `--split-limit` 切出的子集正确，边界（offset 超出长度）不崩
3. 与 `--instance-id` 或 `--run-all-from-file` 同时传时报错
4. 文件不存在时是可读错误信息

**已完成**。实现落在 `_requested_instance_ids`（`sql_agent_runner.py:990`），
`main()` 只负责把它的异常转成 `p.error`。测试见 `tests/test_cli_split_selection.py`（17 条）。

实现时定的两条边界，比验收条目更严：

- **offset 超出长度直接报错**，不是返回空集。分批跑时静默跑 0 个实例，会让人以为那一批已经跑完。
- **`--split-offset` / `--split-limit` 不配 `--split` 时报错**，与 3.2 里 `--tkstore` 的守卫同风格。
  `--split-limit` 超出剩余量不报错，因为最后一批本来就短。

顺带把 `main()` 的筛选段改成按划分文件的顺序输出实例（原来是 `set` 成员判断，
输出顺序随 jsonl 而定），这样分批的边界和日志顺序对得上。

实机验证：真实的 86 id 划分文件切成 30/30/26 三批，拼接后与全量逐位相等且无重叠；
四条错误路径（文件不存在、与 `--instance-id` 互斥、批参数缺 `--split`、offset 越界）
都是单行可读信息。

## 4.1 端到端编排脚本

**用途**：产出可对比的三个准确率数字，且知识增益能单独归因。

**位置**：新增 `scripts/run_pipeline.py`（或 Makefile 目标）。

### 架构决策：一次 agent 运行，两次精修

原计划是两侧各跑一次完整 agent，baseline 定义为"无 refiner 无知识"的裸 agent。
**已改为共享同一次 agent 产出**，理由如下。

`execution_query.sql` 在 `sql_agent_runner.py:1136` 写盘，而精修在 `:1184` 才开始，
知识只在 `perform_refinement_and_revision` 内部注入。所以**任何一次运行的
`execution_query.sql` 都是"无知识无 refiner"的裸 agent 产出**，裸 agent 这个数字是免费的，
不需要单独跑一臂。

而 `--refine-output`（`:1013` 独立分发，不需要 `--refine-cte`）会
`shutil.copytree` 整个目录再原地精修（`:788`），所以可以对同一份 agent 产出精修两次。
两臂的 `execution_query.sql` 逐字节相同，**知识增益的对比是严格配对的**。

这一点很关键：主要对比已从"裸 agent vs 增强"改成
**"有 refiner 无知识" vs "有 refiner 有知识"**，因为只有后者两臂之间只差知识这一个变量。
而这个对比恰恰是对 agent 波动最敏感的——两臂都建立在起始 SQL 之上。3.3 实测同一实例
两次运行的 CTE 结构能从 5 个变 0 个、5 个变 3 个，所以若各跑一次 agent，差值里会混进
"这次 agent 恰好写得不一样"。共享产出把这个噪声源彻底消除。

### 三条命令

```bash
# 共享的 agent 产出（86 个免泄漏实例）——依赖 4.0 的 --split
python -m src.agents.sql_agent_runner \
  --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
  --out-base outputs/test_agent

# 臂 A：有 refiner 无知识
python -m src.agents.sql_agent_runner --refine-output outputs/test_agent \
  --refine-output-dir outputs/test_refonly

# 臂 B：有 refiner 有知识
python -m src.agents.sql_agent_runner --refine-output outputs/test_agent \
  --refine-output-dir outputs/test_tk --tkstore tkstore/tkstore_sqlite.csv
```

### 断点续跑与分批

两条路径现在都是**实例级续跑**，中断后重跑同一条命令即可接上。哨兵统一是
`REFINEMENT_MARKER`（每个实例目录下的 `refinement_complete.marker`）。

| 路径 | 判据 | 位置 |
| --- | --- | --- |
| agent 产出 | `execution_query.sql` 非空 | `_has_completed_output` |
| agent + `--refine-cte` | 上面**再加**实例级哨兵 | 同上，`require_refinement=True` |
| `--refine-output` | 实例级哨兵 | `run_refinement_on_existing_outputs` 循环开头 |

**修之前的两个坑**（都已修，见 `deviations.md` C19 / C20）：

- `--refine-output` 是整目录全有或全无：目标目录缺目录级 marker 时，非交互模式直接
  `shutil.rmtree` 重来。86 个实例跑到第 80 个挂掉，前 79 个全丢。
- 带 `--refine-cte` 的正常路径是**假续跑**：`execution_query.sql` 在精修**之前**写盘，
  所以精修阶段中断的实例会被当成已完成永久跳过，产出一个未精修的实例且不报警。

**`--split` 对精化路径依然无效**：`main()` 在 early return 处就进了
`run_refinement_on_existing_outputs`，那里按目录里的实例目录遍历，不看 `--split`。
这一条没改——有了实例级续跑之后不需要改了。

**分批方案（已定）**：在 agent 阶段用 `--split-offset` / `--split-limit` 产出多个小目录，
再逐目录精修两臂。分批的作用从"控制中断损失"降级成"控制单条命令的墙钟时间"，
因为损失现在最多是一个实例（约 10 分钟）。

```bash
# agent 阶段切三批（30/30/26），已验证拼接后与全量逐位相等且无重叠
for off in 0 30 60; do
  python -m src.agents.sql_agent_runner \
    --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
    --split-offset $off --split-limit 30 \
    --out-base outputs/test_agent_b$off
done
# 之后对 outputs/test_agent_b{0,30,60} 各跑臂 A / 臂 B，评估时把三批的 evals.csv 合并
```

另有一个评估侧的破坏性行为要知道：同一 instance_id 出现多个时间戳目录时（中断重跑会留下
半成品），`evaluation/evaluate.py:424-448` 会优选"最新且有 `execution_result.csv`"的那个，
并 `shutil.rmtree` **删掉其余的**。因为两臂是逐实例复制出来的副本，删一臂不影响另一臂，
但共享产出目录上跑评估会真的删目录。

**顺带去掉的交互提示**：目标目录已存在时原先会问 `Overwrite? (y/n)`，答 `y` 就
`rmtree`。现在一律续跑，要重来请自己 `rm -rf`。误按一个 `y` 就毁掉整臂的风险不值得保留。
另外目录级 marker 现在**只在零失败时才写**——原先带着失败实例也照写，会把重试永久挡住。

三个数字的来源：

| 数字 | 取自 | 含义 |
| --- | --- | --- |
| 裸 agent | 共享产出的 `score`（两臂目录里相同） | 无 Alg 5 |
| 臂 A | `outputs/test_refonly` 的 `score_final` | 有 refiner 无知识 |
| 臂 B | `outputs/test_tk` 的 `score_final` | 有 refiner 有知识 |

**知识增益 = 臂 B − 臂 A**（严格配对）。**论文 Fig. 6 口径 = 裸 agent vs 臂 B**。

### 决策记录

| 决策 | 结论 |
| --- | --- |
| 架构 | 共享一次 agent 产出，`--refine-output` 精修两次 |
| 臂数 | 三个数字（裸 agent / 臂 A / 臂 B），主要对比是 B − A |
| store | 先用仓库自带的上游 `tkstore/tkstore_sqlite.csv`，我们自己 populate 的 store 之后再跑 |
| 样本与重复 | 86 × 1 次 |
| `--refine-output` 是否改成读真实 `messages.json` | **不改**，保持现状 |
| 全量 evaluate | 未经确认不得自行运行 |

### 解读时必须写清的三件事

**一、两臂的改写 agent 都缺少原始探库历史。** `--refine-output` 不读 `messages.json`
（该文件在每个输出目录里都有，`:1138` 写的），而是重建一份三条消息的最小上下文
（`:887-898`）。注意这不是编造：前两条调的是**和正常路径完全相同的
`get_system_prompt` / `build_user_message`**（对比 `:440-443`），只把
`train_context_file` 与 `expected_output_format` 硬编码成 `None`——只要我们不传这两个 flag，
前两条消息与正常路径逐字节相同。

真正缺的只有中间那段：15 轮 `<think>` / `<sql>` / 查询结果被一条"这是你的解"顶替。
正常路径下 `messages` 是主 agent 那次活的对话（实测 `local310` 有 30 条消息约 2 万字符），
改写时它还记得 `race_id` 不是年份、必须 join `races` 才能按年聚合。

影响比听起来轻：`_revise_from_feedback` 会执行 agent 在改写中吐出的 `<sql>` 探针并把结果
喂回去，所以它**可以现场重新探库**。代价是那 5 次修订预算由探针和出解共用（见 B11），
它得花掉本来用于出解的次数去重新发现已知的事。

所以**两臂的绝对值可能都比正常路径略低**。B − A 的差值不受影响（两臂同等受损），但
"裸 agent vs 臂 B"这个论文口径会对臂 B 偏保守——实际部署走正常路径会更好。

**二、3.3 的冒烟数据不可与阶段 4 直接比较。** 那 5 个实例是走**正常路径**跑的
（`outputs/smoke_augmented_v2/`），改写 agent 有完整历史，与臂 B 不是同一条代码路径。

**三、~~续跑是按目录全有或全无的~~**（已失效，两个坑都已修，见上面的"断点续跑与分批"
以及 `deviations.md` C19 / C20）。

**前置**：第一条命令依赖 **4.0** 的 `--split`；分批跑还要 `--split-offset` / `--split-limit`。

**验收**：三条命令在 3 个实例的小集合上跑通；两臂目录里的 `execution_query.sql` 逐字节相同。

### 已完成

按仓库惯例拆成两块：编排逻辑在 `src/utils/pipeline.py`（可测），
CLI 外壳在 `scripts/run_pipeline.py`（与 `make_splits.py` 同构）。
测试见 `tests/test_pipeline.py`（24 条）。

```bash
# 看命令不执行
python scripts/run_pipeline.py --dry-run \
  --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
  --out-prefix outputs/test --tkstore tkstore/tkstore_sqlite.csv

# 分三批真跑
python scripts/run_pipeline.py --batch-size 30 \
  --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
  --out-prefix outputs/test --tkstore tkstore/tkstore_sqlite.csv
```

目录名由 `--out-prefix` 派生：`_agent` / `_refonly` / `_tk`，分批时插 `_b<offset>`。
实测 86 个 id 按 30 一批切成 `offset/limit` = `0/30`、`30/30`、`60/26`。

三条实现上的取舍：

- **分批参数只给 agent 那一步**。`--refine-output` 按目录遍历、不看 `--split`，
  给臂传 `--split` 会暗示一个不存在的过滤。
- **`--tkstore` / `--filter-model` / `--no-llm-filtering` 只给臂 B**，臂 A 一个都不带——
  这正是两臂唯一的差别。agent 那一步连 `--refine-cte` 都不带，它的
  `execution_query.sql` 就是裸 agent 数字。
- **某一步非零退出就停下整批**。带着半成品的 agent 目录继续精修会静默缩小样本量，
  那看起来像结果而不像错误。

**`verify_shared_agent_output` 是这一节的核心断言**：每批跑完逐实例比对
agent 目录与两个臂目录的 `execution_query.sql` 字节。不相等就意味着
`delta_knowledge` 不是配对比较，脚本报错退出而不是继续算数。
精修的改写落在 `execution_query_after_*.sql`，不动原始文件，所以这个不变量应当恒成立。

**验收执行情况**：`--dry-run` 与分批的命令形态已核对（见上）。三条 argv 被 runner 接受、
续跑生效、配对校验会跑，是用**零 LLM 调用**的方式验的——预置好产物让三步都走"已完成"
分支，整条管道 exit 0。反向也验了：把某个实例的起始 SQL 篡改一个字节，
脚本报 `does not share the agent's starting SQL` 并 exit 1。
四条参数守卫（store 不存在、`--batch-size` 与 `--split-limit` 互斥、`--batch-size 0`、
划分文件不存在）都是单行可读信息。

**尚未执行**：真跑 3 个实例的小集合（要真实 agent 与 refiner 调用，约 1 小时）。
精修本身的正确性已由 3.3 冒烟覆盖，这条待确认后再跑。

## 4.2 结果对比

**用途**：产出论文 Fig. 6 那种 baseline vs augmented 的准确率对比。

**输入**：两个 `evaluate.py` 的输出。

**输出**：一张表，至少含

| 列 | 含义 |
| --- | --- |
| `instance_id` | 实例 |
| `score_bare` | 裸 agent，取共享产出的 `score` |
| `score_arm_a` | 有 refiner 无知识，取 `outputs/test_refonly` 的 `score_final` |
| `score_arm_b` | 有 refiner 有知识，取 `outputs/test_tk` 的 `score_final` |
| `delta_knowledge` | 臂 B − 臂 A，`+1` 知识修好 / `0` 无变化 / `-1` 知识改坏 |
| `delta_paper` | 臂 B − 裸 agent，对齐论文 Fig. 6 |
| `rules_used` | 该实例的 `mem_id` 列表，定义见下 |
| `n_db_rules` | 该实例的库在 store 里有多少条 db 作用域规则，用于下面的分组 |

### `rules_used` 的定义

`rules_used` 是关键 —— 论文强调 TK 是**可审阅**的。有了它才能回答
"哪条规则真的起了作用"、"改坏的那些是哪条规则导致的"。

但**没有真值可用**：refiner 的 verdict 从不引用规则 ID，所以无法确知某条规则是否被采纳。
**决策：定义为"判 `issues` 且改写被采纳的那些片段所选中的规则"**，
即从 `retrieved_rules.json` 的 `selected` 取，条件是该片段的
`refiner_<name>.json` 状态为 `issues` 且存在对应的 `execution_query_after_<name>.sql`。

这是**可能影响过输出的上界**，不是"确实起了作用"。3.3 实测这个上界远小于检索总量：
21 个片段里只有 8 个判 `issues`，**全局去重**后 52 条选中的规则里只有 29 条（56%）
落在这个上界内，其余在 refiner 判 `ok` 时就被静默吸收了。

口径要写清，两个数都对但差一倍：**全局去重**（5 个实例合起来出现过的 distinct `mem_id`）
是 52 → 29（56%）；**逐实例去重再求和**是 118 → 72（61%）。上面那句用的是前者。
`src/utils/compare.py::rules_used` 按实例返回，所以聚合时用哪个口径要显式说明。

备选方案是让 refiner 在 verdict 里显式引用规则 ID（改 prompt 与 schema），归因最硬，
但那是新工作且依赖 LLM 如实报告，本轮不做。

**取数注意**：`evaluate.py` 已区分 `score`（`execution_result.csv`）和
`score_final`（`execution_result_final.csv`），槽位是现成的。共享产出架构下取数很自然——
两臂目录里的 `score` 都是同一份裸 agent 产出（应逐字节相同，可作为一致性校验），
各自的 `score_final` 才是本臂结果。

### 必须按"有无 db 规则可用"分组报告

**总体一个数字会掩盖结论。** 检索的硬闸门只有库名：`generic` 规则对所有实例可用，
`db` 作用域规则只在库名相等时才通过（`tagger_index.py:552-572`）。按这个闸门清点上游
store 对 86 个实例的可达性：

| 度量 | 值 |
| --- | --- |
| generic 规则（对全部 86 个实例可用） | 52 条 |
| db 作用域规则 | 66 条，分布在 22 个库 |
| 86 个实例中库有 db 规则可用的 | **52 个（60%）** |
| 每实例可用 db 规则数的分布 | 0 条：34 个实例；1–6 条：42 个；**12 条：10 个**（全在 `bank_sales_trading`）|

分布极不均：34 个实例一条 db 规则都吃不到，而 10 个实例独占 12 条。
所以总体增益会被少数库主导。**决策：4.2 的结果表按 db 规则可用性分两组报**
（0 条 vs ≥1 条），否则总体增益接近 0 时无法区分"知识没用"和"大部分实例本来就没知识可用"。

参考：换成我们自己 populate 的 store 时，闸门是 train 与 test 的库重叠。
train 24 个实例覆盖 16 个库，test 111 个覆盖 28 个、共享 14 个，
**65/111（59%）** 的 test 实例其库在 train 里出现过；对 86 子集是 49/86（57%）。
量级与上游 store 的 60% 相当，所以上面的分组口径两条轨道通用。

表这一层的重叠远薄于库这一层，但**不影响检索能否命中**，因为 `table`/`column` 不是筛选
条件（B2）。共享库里 train 的 gold SQL 触及面很窄，例如 `f1` 有 29 张表而 train 只碰了 4 张、
`bank_sales_trading` 19 张碰 5 张（`IPL` 是例外，8 张碰 5 张）。实测已补进 B2。

### 已完成

同 4.1 的拆法：取数与聚合在 `src/utils/compare.py`，CLI 在 `scripts/compare_arms.py`。
测试见 `tests/test_compare.py`（24 条）。

```bash
# 先分别评两臂（不要评共享的 agent 目录，见下）
python evaluation/evaluate.py --mode exec_result --result_dir outputs/test_refonly --gold_dir evaluation/gold
python evaluation/evaluate.py --mode exec_result --result_dir outputs/test_tk --gold_dir evaluation/gold

python scripts/compare_arms.py --arm-a outputs/test_refonly --arm-b outputs/test_tk \
  --tkstore tkstore/tkstore_sqlite.csv --out outputs/comparison.csv
```

**只需要两份 `evals.csv`，不评共享的 agent 目录。** `score_bare` 从两臂的 `score` 列取
（那一列算的是 `execution_result.csv`，即复制过来的裸 agent 结果）。这样做有两个好处：
共享目录保持原样不被 `evaluate.py` 的删重目录行为碰到，且两臂各给一份裸分，
互相就是一次一致性交叉校验。

**裸分不一致的行会被排除出所有汇总组**，并单独计数（`Row.paired`、`GroupSummary.unpaired`）。
两臂本应精修同一条起始 SQL，不一致说明配对已经破了，那一行的 `delta_knowledge`
量的不是知识。只发警告却照样计入汇总，会让表头数字在一行警告背后失真。
正常情况下 4.1 的 `verify_shared_agent_output` 会先硬失败，这里是第二道防线。

`+1` / `-1` 分开计数而不是只报净差：净差为 0 可能是"什么都没发生"，
也可能是"修好一个、改坏一个"，两者的结论完全不同。

**目录名与批次**：用了 `--batch-size` 时目录带 `_b<offset>` 后缀，evaluate 与 compare
要按批分别跑，或先把批目录合并。

**验证**：`rules_used` 拿 3.3 的真实产物核过，精确复现上面记的全局去重 52 → 29（56%）
与片段 21 → 8。CLI 用冒烟产物搭出的两臂跑通，覆盖 `+1` / `-1` / 无变化 / 裸分不一致
四种情况，以及四条错误路径（两臂缺 `evals.csv`、store 不存在、两臂交集为空退出码 1）。

**一个反直觉但正确的现象**：`local269` 的 `n_db_rules=0` 却有 13 条 `rules_used`。
那些全是 generic 规则——它们对所有实例可用，不受库名闸门限制。
所以分组的含义是"有没有**库专属**知识可用"，不是"有没有知识可用"。

---

## 规模与成本

| 阶段 | agent 运行次数 | 说明 |
| --- | --- | --- |
| 2.1 | 24（已跑 8） | 我们的 train 集 = 有 gold SQL 的 24 个，外层 ReAct 最多 25 轮。还差 16 个 |
| 2.2/2.3 | 0 | 每实例约 3–4 次 LLM 调用（diff 循环最多 6 轮） |
| 3.3 冒烟 | 5 | 走正常路径，只跑带知识一侧 |
| 4 共享 agent 产出 | 86 | 免泄漏子集，只跑外层 ReAct，约 3–4 分钟/实例，合计约 5 小时 |
| 4 臂 A | 0 | 复用上面的产出，只跑精修 |
| 4 臂 B | 0 | 同上，额外含检索与 FilterKnowledge |

精修一侧按 3.3 实测约 10 分钟/实例（含 refiner 每 CTE 的 25 轮探库），
单臂 86 个约 14 小时，两臂约 28 小时。共享 agent 产出省掉了第二次 86 个实例的 agent 运行。

**检索本身的 LLM 开销实测**：每个 CTE 约 20 条 code filter 候选，按 `CHUNK_SIZE=15` 切
就是 2 次 `FilterKnowledge` 调用。按每实例 3 个 CTE 加 1 个 final SELECT 算，
**光检索每实例约 8 次调用**，86 个实例约 690 次 —— 这还没算 refiner 每个 CTE 的 25 轮探库。
augmented 一侧单实例成本明显高于 baseline。

先按 3.3 的 5 个实例把链路跑通，再放开全量。

## 本轮不做

- [`deviations.md`](./deviations.md) **B 组**全部 —— 检索只用 3 维特征、正则抽特征、
  refiner 是 25 轮探库循环而非单次 `Feedback`、每 CTE 修订 5 次上限等
  （唯一例外是 3.0 那条陈旧快照，它是 Alg 5 的保真问题，本轮修）
- 让 refiner 在 verdict 里显式引用规则 ID（`rules_used` 用上界代替，见 4.2）
- 把 `--refine-output` 改成加载真实 `messages.json`（决策：保持现状，见 4.1 解读第一条）
- 我们自己 populate 的 store 那条轨道（先跑上游 store，之后再补）
- BIRD / minidev 适配
- ReFORCE agent（论文 6.1.3 用它证明通用性，不是主结果）
- 论文 6.4 的各项消融

其中 **B10**（refiner 做的比论文多）和 **B11**（修订次数上限）会影响带 refiner 两臂的绝对数值。
但归因问题已由 4.1 的臂设计解决：**臂 A 与臂 B 都带 refiner，差值里不再混入探库增益**，
`delta_knowledge` 可以直接归因给 tribal knowledge。需要注意的是 `delta_paper`
（裸 agent vs 臂 B）仍然混着两者，那个数字只用于与论文 Fig. 6 对齐，不用于归因。

B10 另有一层后果值得记住：知识只进 refiner 的 `cte_goal`，出来的是它的 verdict，
所以**知识是透过 refiner 的判决间接到达 agent 的**。refiner 判 `ok` 时那批规则彻底消失
（3.3 实测 21 个片段里 13 个如此）。这不算偏离论文——Alg 5 的循环条件 `f ≠ ∅` 本身就允许
feedback 为空——但它意味着"检索到 N 条规则"与"agent 收到 N 条规则"是两件差很远的事，
这也正是 `rules_used` 只能给出上界的原因。

---

# 阶段 5 — 把候选 SQL 一并交给 agent（`f` 里带 SQL）

**尚未实现。** 本阶段的设计依据是阶段 4 五轮实验的结论，完整数据见
[`results_reference_track.md`](./results_reference_track.md)。

## 5.0 为什么要做

五轮实验把问题定位到了一处：**知识信息没有到达真正改写 SQL 的那一步。**

| 轮次 | 修复由谁应用 | 知识净 |
| --- | --- | --- |
| A–D | agent 重写（忠于论文 Alg 5） | **−6 / −5 / −1 / −3** |
| E | 直接采纳 refiner 的 SQL（复现上游） | **+3** |

refiner 的 verdict 一直带着 `suggested_fix_sql`（完整的修正片段）并落盘在
`refiner_<name>.json`，但 `sql_agent_runner.py` 里这个字段名出现 **0 次**。默认路径只把散文式
的 `suggested_fix` 转给主 agent，而**那个 agent 从未见过任何规则**。实测这条链损耗极大：
主 agent 的改写与 refiner 建议的相似度中位数只有 **0.23–0.47**，78–83% 的片段低于 0.5 ——
主 agent 基本在自己另写一版。

但 E 轮那条捷径有它自己的病：**refiner 的建议有一半跑不通**（108/211 被执行校验拦下），
其中 **33 条（49%）是"输出契约被破坏"** —— refiner 只看到一个 CTE，不知道下游消费者
期待什么列，改了输出 schema 就把整条查询打挂。典型如 `local018`：refiner 把
`category_counts` 的输出从 `(collision_year, total_incidents, speeding_incidents)`
改成 `(collision_year, pcf_violation_category, n)`，而下游 `percentage_shares`
还在引用 `speeding_incidents`。

**这类失败在 Alg 5 里结构上不可能发生**，因为论文把 agent 留在环里：第 7 行
`(s_t, is_final) ← A(C_TK_t)` 规定 SQL 永远由 agent 产出，第 12–13 行
`R_t ← ExecSQL(s_t)` 与 `concat(..., R_t, f)` 把执行结果回灌上下文，第 5 行
`while is_final = False ∨ f ≠ ∅` 保证还有反馈就继续。agent 是唯一有全局视野的组件。
上游 `tkboost.sql()` 把这三件事全拿掉了 —— 它执行了 `refined_sql` 但只把失败写进返回值的
`execution.ok = False`，照样返回那条跑不通的 SQL。

## 5.1 设计

**只改 `_feedback_text` 的渲染**，把 `suggested_fix_sql` 作为候选 SQL 加进 `f`。
其余机制一个不动：仍由 agent 产出完整 `<solution>`、仍执行校验、失败仍把 `SQL_ERROR`
追加进 messages 重试。即论文第 7、12、13 行的闭环完整保留。

```
[Refiner feedback for CTE <name>]
Issues:
- ...（不变）

Suggested fix (reference):
...（不变，散文）

Candidate SQL from the refiner (reference, not validated):     ← 新增
WITH ...

Tests / checks to satisfy:
- ...（不变）

Instruction: Revise ONLY the CTE named '<name>' ...（不变）
You may reuse or adapt the candidate SQL above, but you are responsible for
keeping the rest of the query consistent with it -- downstream CTEs and the
final SELECT must still reference columns that actually exist.   ← 新增
```

三个要点：

- **标注"未经校验"**：它确实有一半跑不通，要让 agent 保持怀疑而非盲抄。
- **明确把全局一致性的责任写给 agent**：这是整个设计的核心。refiner 看不见下游，
  agent 手里有完整的解、看得见。
- **不动其余任何机制**，以便与 A–E 轮严格可比。

### 它取的是两者各自成立的那一半

| | 知识信息是否完整传到改 SQL 的那一步 | 谁负责全局一致性 |
| --- | --- | --- |
| A–D 轮（论文路径） | ❌ 只传散文 | agent ✅ |
| E 轮 / 上游 | ✅ 完整 SQL | **没有人** ← 33 条契约破坏的根源 |
| **本阶段** | ✅ 完整 SQL | agent ✅ |

### 仍然忠于论文

Alg 5 只规定 `f ← Feedback(q, c_i, K)`，**没有规定 `f` 的内容形式**。论文正文说 `f` 是
natural language feedback，而一段带 SQL 的反馈仍是自然语言反馈 —— 如同人类 code review
既写评语也贴 diff。关键在于第 7 行仍是 `A(C_TK_t)` 产出 `s_t`，agent 仍是唯一作者与整合者。

有一处旁证：refiner 自己的 `tests` 字段里出现过
`WITH fixed AS (/* use suggested_fix_sql upstream CTEs here */)` ——
**它写测试时就假设下游能看到 `suggested_fix_sql`**，说明 prompt 的设计意图里它本该被传下去。

## 5.2 可证伪的预测

- 臂 A 应回到 **+1**（与 A–D 轮相同，agent 仍在环里整合）
- 知识净值应**不差于 E 轮的 +3**

三种失败模式及其后果：

| 失败模式 | 退化到 | 为什么最坏不差于 E 轮 |
| --- | --- | --- |
| agent 盲抄候选 SQL 不改下游 | E 轮行为 | 执行校验拦住且**能重试**（最多 5 次，B11）；上游拦不住也不重试 |
| agent 完全忽略候选 SQL | A–D 轮行为 | 理论下界 −5 |
| prompt 变长挤压注意力 | 不确定 | 候选 SQL 常几百至上千字符；25 轮配置下风险更大，5 轮配置下较小 |

## 5.3 验收

- 单测：`_feedback_text` 在 `suggested_fix_sql` 非空时渲染候选 SQL 段与责任声明；
  为空时输出与现在逐字节一致（保证 A–E 轮可复现）
- 单测：新增段落不改变 `issues` / `suggested_fix` / `tests` 的现有渲染
- 默认关闭（新增标志），A–E 轮的复现命令不受影响
- `retrieved_rules.json` 记录该标志，作为区分两臂目录的凭据
- 跑法：复用同一份裸 agent 产出与 5 轮探库配置，两臂同开该标志

## 5.4 这一阶段的意义

E 轮的结论是"README 报告的增益依赖一个**偏离论文**的实现细节"。如果本阶段的预测成立，
结论会变成更有建设性的一句：**论文的架构是对的，上游的实现只是把知识传丢了；
补上传递、保留 agent 整合，两者兼得。** 后者对论文是正面的，也更可能是作者的本意。

## 5.5 实测结果（F 轮）：预测不成立

已实现为 `--include-candidate-sql`，跑在同一份裸 agent 产出与 5 轮探库配置上。

| 轮次 | 修复由谁应用 | 裸 | 臂 A | 臂 B | refiner 净 | 知识净 |
| --- | --- | --- | --- | --- | --- | --- |
| B | agent 重写（只传散文） | 36 | 37 | 32 | +1 | −5 |
| E | refiner 的 SQL（上游） | 36 | 34 | 37 | −2 | **+3** |
| **F** | agent 重写 + 候选 SQL | 36 | **35** | 35 | −1 | **+0** |

**5.2 那两个预测都没成立**：臂 A 没有回到 +1（是 −1），知识净没有 ≥ +3（是 0）。
F 轮的 3 修好 3 改坏全在噪声内（±1/54），唯一能确定的是**它没有复现 E 轮的 +3**。

### 但诊断排除了"agent 忽略候选 SQL"

| | 主 agent 改写与 refiner 建议的相似度 |
| --- | --- |
| B 轮（只传散文） | 中位数 **0.23**，>0.9 的 1 个 |
| F 轮 臂 B | 中位数 **0.45**，>0.9 的 6 个 |
| F 轮 臂 A | 中位数 **0.48**，>0.9 的 11 个 |

**候选 SQL 确实传到了并被部分采信**（相似度翻倍、完全照抄的从 1 涨到 6–11 个），
三种失败模式一个都没完全命中 —— 既非盲抄（该接近 1.0）也非忽略（该还在 0.23）。
问题是"部分采信"没有转化成分数。

顺带一个反常现象：臂 A 的相似度（0.48）**高于**臂 B（0.45），采纳数也略多（74 vs 70），
而有知识时 refiner 判 `issues` 的比例反而降了（25% → 22%）—— 与前几轮"知识让 refiner
更爱挑问题"方向相反。

### 由此得到的结论

**5.4 那句话不能用了。** 现有证据更支持：E 轮那个 +3 **不是**来自"知识信息传递得更完整"，
因为 F 轮把信息传过去了却没拿到增益。**传递不是关键变量。**

E 轮与 F 轮真正的差别在于 **agent 的任务形态**：F 轮的指令仍是"改写 CTE X、输出完整
`<solution>`"，agent 要从头组装整条查询，候选 SQL 只是旁边的参考；E 轮则是 refiner 的
SQL 直接成为结果。这个观察引出阶段 6。

---

# 阶段 6 — 让 refiner 的自检对准整条 SQL（G 轮）

**尚未实现。** 以 **E 轮**（`--adopt-refiner-sql`，目前唯一有正增益的配置）为基线。
**明确定位为"在上游实现基础上的改进"，不是论文复现** —— Alg 5 第 7 行规定 SQL 由 agent
产出，而这里主 agent 只写初版、之后纯粹是执行器。报告时归入"超出论文的设计探索"。
依据是 E 轮已证明偏离论文的上游路径反而有正增益，而忠于论文的 A–D 轮没有。

## 6.0 问题的精确定位

E 轮拦下 107 条建议，**其中 33 条（可归因的 49%）是"输出契约被破坏"**：refiner 改掉了
CTE 原本产出的列，下游还在引用。典型 `local018`：输出从
`(collision_year, total_incidents, speeding_incidents)` 改成
`(collision_year, pcf_violation_category, n)`，而 `percentage_shares` 还在读
`speeding_incidents`。

根因有两层，**第二层是本阶段的关键发现**：

**一、refiner 看不见下游。** `sql_agent_runner.py:693-696` 的 `prev_blocks` 取
`ctes[:idx_cte]`，下标严格小于当前位置。所以 CTE 阶段 refiner 只看得见自己和前面的 CTE，
**看不见后续 CTE 与 final SELECT**。它一生中只在最后一个片段才见过全貌。

**二、refiner 已有自检重试循环，但检错了对象。** `cte_refiner.py:570-590`：它打算给出
verdict 前会执行一次 `suggested_fix_sql`，跑不通就把报错追进**自己的对话**、清空
`verdict_data`、`continue` 重新出 verdict。机制完整、同对话续跑、成本低。

问题在第 571 行：

```python
_execute_with_timeout(conn, cursor, suggested_sql, fetch_all=False)
#                                   ↑ 只是 WITH category_counts AS (...) 这一段
```

**这一段孤立执行完全正确** —— 语法对、列都在。于是自检通过、verdict 产出；到我们这边替换
进整条 SQL 才炸。**refiner 检片段、我们检整体，中间这道缝就是那 33 条的全部来源。**

> ### ⚠️ 上面这个前提已被实测证伪，见 6.8
>
> "refiner 已有自检重试循环，只是检错了对象"**是错的**。那个循环挂在"循环内收到 verdict"
> 的路径上，而实测 **708/708 个片段都走兜底路径**（轮数耗尽后的强制出 verdict），
> 该路径完全绕过自检。所以那套自检**从未执行过一次** —— 在上游也一样。
> 阶段 6 的改动因此在现状下不产生任何效果，两次 `local018` 实测的校验器调用次数都是 0。

## 6.1 与 E 轮的差异，只有两处

| | E 轮（现状） | G 轮 |
| --- | --- | --- |
| refiner 的 payload | `[PREVIOUS_CTES]`、`[CTE]`、`[CTE_GOAL]`… | **多一段 `[DOWNSTREAM]` + 一句约束指令** |
| refiner 自检执行的对象 | 只有 `suggested_fix_sql` 片段本身 | **替换回整条 SQL 之后的完整查询** |
| 自检失败后的重试 | 已存在（`need_rev` 分支） | **不动，直接复用** |
| 主 agent 的角色 | 只写初版，之后纯执行器 | 同 |
| `_adopt_refiner_sql` 的执行校验 | 保留 | 保留（成为第二道防线） |
| 其余一切 | | 不动 |

**没有新增重试循环。** 这是本方案相对早期草案的关键简化：那个循环已经存在，只需把它自检的
对象换对。也因此**不需要**让 `run_refiner` 暴露对话历史。

### payload 的 before / after（`local018` / `category_counts` 实例）

新增只有两处，插在 `[CTE]` 之前以保持"上游／下游／目标片段"的顺序：

```
[USER_QUERY]      ...
[PREVIOUS_CTES]   已有（此例为空，category_counts 是第一个 CTE）
[DOWNSTREAM]      ★新增 —— ctes[idx+1:] 原文 + remainder_sql
[CTE]             已有
[CTE_GOAL]        已有（含注入的知识）
[CTE_NAME] / [Harness tip] / [MANDATORY_PROBES] / Instructions   已有
★ Your rewrite replaces only [CTE]. Everything in [DOWNSTREAM] keeps reading this
  CTE's output, so your rewrite must preserve the output column names it references.
```

实测该例 payload 从 1069 → 1665 字符（**+596**）。`speeding_incidents`、
`total_incidents`、`collision_year` 三个列名从此直接出现在 refiner 眼前。

**不加 `[FULL_QUERY]`。** 它与 `[PREVIOUS_CTES]` + `[CTE]` + `[DOWNSTREAM]` 完全重复，
实测会让新增量从 418 涨到 1175 字符（多 64%）。F 轮已暴露"上下文变长挤压注意力"的风险
（那轮 +1376 字符、结果 +0），所以精简不只是省钱。

**重试时不重复任何 SQL。** 续用同一对话，只追加约 150 字符的报错 turn，并指回
`[DOWNSTREAM]`。两个改动共用同一段上下文，报错不需自带解释材料。

## 6.2 实现方式：把校验器传下去，不要把重组逻辑搬过去

`previous_ctes` 是**每段各带一个 `WITH`** 的展示格式（实测确认），拼起来不是合法 SQL，
所以 refiner 无法自行重组整条查询。而 runner 手里有 `ctes` 与 `remainder_sql`。

因此给 `run_refiner` 加一个可选参数：

```python
validate_fix_sql: Optional[Callable[[str], Optional[str]]] = None
#   入参：refiner 的 suggested_fix_sql
#   返回：None 表示整条 SQL 跑得通；否则返回报错字符串
```

runner 传入闭包，内容就是 `_adopt_refiner_sql` 现在那套：解析建议 → 取
`fixed_ctes[0].body` → 替换 `candidate[idx_cte]` → `rebuild_sql_from_ctes` → 执行 →
返回报错。`cte_refiner.py:571` 改成优先调 `validate_fix_sql`，未传时退回现有的孤立检查。

这样**重组与校验逻辑只存在一处**（runner），refiner 完全不必知道 CTE 如何装配。
`run_refiner` 的调用方只有 runner、`tkboost/__init__.py` 和它自己的 `main()`，
新增可选参数对后两者零影响。

## 6.3 三点风险与对策

**报错措辞要指向原因。** 闭包返回的报错经 `need_rev` 进对话，必须让 refiner 明白是下游坏了
而非自己的片段坏了。拼成 `substituted into the full query, which then failed: <error>`
并提示去看 `[DOWNSTREAM]`。

**重试预算与 `max_turns` 共用。** `need_rev` 走 `continue`，消耗同一个探库轮数预算（5 轮）。
自检变严后可能出现"轮数耗尽仍无可用建议"，最终落到 `no_verdict` 兜底
（`status: "issues"`、`issues: ["no_verdict"]`）—— 而它在我们的 runner 里**会触发改写**。
前六轮 `no_verdict` 一直是 0，**本轮必须重新监控**；必要时把 `--refiner-turns` 提到 8。

**自检的 DB 开销上升。** 从"执行一个片段"变成"执行整条查询"，更容易撞 120 秒超时
（E 轮已有 2 例）。

## 6.4 验收

- 测试钉住：传入 `validate_fix_sql` 时，自检通过与否由**它的返回值**决定，而非孤立片段
- 测试钉住：**不传时行为与现在逐字节一致**（A–F 六轮必须保持可复现，这条已吃过一次亏）
- 测试钉住：`downstream` 为空时（最后一个 CTE、或无 CTE 的实例）`[DOWNSTREAM]` 不出现
- 默认关闭（新标志），两臂同开，与 `--include-candidate-sql` 互斥
- 产物记录：标志、`no_verdict` 片段数、自检重试次数

## 6.5 预期与可证伪点

臂 A 应回到 **36–37**（E 轮是 34，因为坏 SQL 被放弃；现在能在 refiner 侧修好），
知识净 **≥ +3**。三种结局各有明确读法：

| 结局 | 读法 |
| --- | --- |
| 契约破坏大幅减少且知识净 ≥ +3 | 方案成立，结论转为"**知识有用，但 refiner 需要足够上下文才能正确应用它**" |
| 契约破坏减少但知识净仍 ≈ 0 | 指向 **E 轮那个 +3 本身不稳定**（仅 3.5%，略高于 ±2% 噪声线）。应先重跑 E 轮验稳定性，而非继续加设计 |
| `no_verdict` 明显上升 | 自检变严挤爆轮数预算，需调 `--refiner-turns` |

## 6.6 成本

两臂都要重跑（机制变了），约 **2.5–3 小时/臂**，合计 5–6 小时。不新增 LLM 调用 ——
只是每次自检的 DB 执行更重，payload 每片段多约 600 字符。

## 6.7 若成立，结论是什么

前六轮的叙事是"README 的增益依赖一个偏离论文的实现"。若 G 轮成立，会变成更本质的一句：

> **知识是有用的，但需要 refiner 有足够的上下文才能正确应用它。** 论文把
> `Feedback(q, c_i, K)` 的输入限定为问题、单个 CTE 和知识，这个信息量不足以让它在不破坏
> 全局一致性的前提下改写子查询 —— 论文这里给的信息比实现所需要的更少，而这不是实现的疏忽。

这比"传递方式"更本质，也能独立成立。

## 6.8 实测发现：refiner 从未在循环内出过 verdict

**这一节推翻了 6.0 的前提，并且是七轮实验里最重要的一个实现层发现。**

`local018` 实跑两次（一次 `max_turns=5`、一次 8），`validate_fix_sql` 都是 **0 次调用**。
追查 E 轮全部 trace 后确认：

| 度量 | 值 |
| --- | --- |
| 两臂片段总数 | 708 |
| **走兜底路径（轮数耗尽后强制出 verdict）** | **708（100%）** |
| 其中循环内一次合法 verdict 都没出过 | 708（100%） |
| 走兜底那些片段的探库次数中位数 | **2** |

**不是探库把轮数耗光了**，而是模型从不主动交结论。以 `local018` / `category_counts` 为例，
5 轮全是探库：`sqlite_master` → `PRAGMA table_info(collisions)` →
`SELECT * FROM collisions LIMIT 5` → `DISTINCT pcf_violation_category` → `COUNT(*) ... IS NULL`。

### 为什么

`cte_refiner.py:622-634` 的接收闸门有两个条件，任一成立就把 verdict 打回：

```python
if has_verdict:
    if (sql_executed < min_required_sql            # 条件 A：探库不足 3 次
        or (cte_name_hint and not harness_executed)):  # 条件 B：没跑过 CTE 自身
        ...
        continue
```

`local018` 探了 5 次，**条件 A 通过**。卡住的是**条件 B**：`harness_executed` 只在某条探库
SQL 里出现 `FROM <当前CTE名>` 时才置真（`:579`、`:611`），而那 5 条查询全在探基表
`collisions`，**一条都没碰 `category_counts`**。

而这道要求模型很难自发满足：系统提示里那句 `[Harness tip]` 措辞是
`You may compile-test the CTE by ...` —— **是"可以"不是"必须"**，位置在 payload 的 95% 处。
相比之下 `[MANDATORY_PROBES]` 那三条标着 mandatory，模型老老实实做了，正是上面第 2–5 轮。
**它做了被明确要求的，没做只是"可以"的。**

### 兜底路径做什么

四步，**一次 SQL 都不跑**：追加 `"TIME LIMIT REACHED... Make a decision NOW"` → 调一次
LLM → 抠 `<verdict_json>` → `json.loads` 返回。不执行 `suggested_fix_sql`、不执行 `tests`、
不重试。所以那 107 次 `does not run` 是在 runner 替换时才第一次被发现的。

模型对强制指令**高度服从**：1404 个 verdict 里 1401（99.8%）给出合法 JSON，
仅 3 个解析失败，0 个漏标签。这说明"带着错误再要一次"很可能奏效 —— 阶段 7 的基础。

### 三个后果

1. **上游完全一样。** `tkboost/__init__.py:574` 导入的就是同一个 `run_refiner`，
   CTE 阶段传的也是 `max_turns=5`、`min_required_sql=3`（`:641-642`、`:687-688`）。
   所以 **README 报的 +3.6%～+16.9% 是在"每个 verdict 都被催出来、且 `suggested_fix_sql`
   未经任何验证"的状态下测出的。**
2. **`run_refiner` 的整套自检是死代码** —— `tests` 非空检查、LEFT JOIN + CASE 空值检查、
   `suggested_fix_sql` 编译检查，七轮里一次都没执行。
3. **refiner 不在做"局部修正"。** 实测 85 条 CTE 阶段建议：与原 CTE body 相似度中位数
   **0.48**、**55% 基本重写**、**53% 连 CTE 名字都改了**、**62% 返回多个 CTE**。
   而上游 `fixed_ctes[0]` 只取第一个、丢掉其余 —— `local018` 那份
   `annual_counts → ranked_2021 → target_category → category_shares` 四段联动方案
   被截断后逻辑就断了。**它写的不是"修好的那个 CTE"，是一套新解法。**

---

# 阶段 7 — verdict 的校验—重试环（H 轮）

**已实现，未跑实验。** 建立在 6.8 的发现之上：既然 verdict 全部来自兜底路径、
且从未被验证过，就在兜底路径之后补上校验与重试。

开关是 `--verdict-attempts N`（默认 1 = 关，即 E 轮行为），要求 `--validate-fix-in-context`，
两臂同传。落地位置：`cte_refiner.run_refiner` 的兜底路径之后（`max_verdict_attempts` 参数）、
`sql_agent_runner`（CTE 阶段与 final SELECT 阶段各一个校验闭包）、
`pipeline.plan_steps`、`scripts/run_pipeline.py`（跑 agent 步之前就校验参数组合）。
测试 `tests/test_verdict_retry.py`（33 例），全套 397 例通过。

## 7.0 核心

refiner 交出 `verdict_json` 后**不直接采信**：把 `suggested_fix_sql` 的第一个 CTE 替换回
整条 SQL 执行一遍。跑不通就带着具体错误让它重来，最多 **3 次**；仍不行则放弃该片段、
保留原 SQL、进下一个。

对应 Alg 5 第 12–13 行（`R_t ← ExecSQL(s_t)`、错误进上下文），只是接收反馈的是 refiner
而非主 agent。

## 7.1 判据只有一条：整条 SQL 能否执行

**结构问题不作为拒收理由，只用来丰富错误信息。**

```
拿到 verdict_json
  ├─ 解析 suggested_fix_sql，取第一个 CTE 的 body
  ├─ 替换进原位置 → rebuild_sql_from_ctes → 执行
  ├─ 跑得通 → 采纳（即使名字不匹配、即使返回了多个 CTE）
  └─ 跑不通 → 拼错误信息 → 重试
```

依据是 E 轮臂 B 那 85 条建议的交叉验证：

| 结构检查（单 CTE + 名字匹配） | 执行 | 条数 |
| --- | --- | --- |
| 不通过 | 失败 | 41（48%） |
| **不通过** | **通过** | **17（20%）** ← 放行，见下 |
| 通过 | 通过 | 16（19%） |
| **通过** | **失败** | **11（13%）** ← 只有执行校验能发现 |

**决策：那 17 条结构不合但能跑的放行。** 若按结构直接拒收会损失它们，而它们截断后仍执行
通过。同时那 11 条结构合规却执行失败的说明**光有结构检查不够**，执行校验是必需的。

## 7.2 错误信息的拼法

**总是包含**执行报错：

> Substituted into the full query it failed: `no such column: speeding_incidents`.
> `[DOWNSTREAM]` still reads that column.

**若返回了多个 CTE**（实测 62%）追加：

> You returned 4 CTEs, but **only the first is used and the rest are discarded** — your
> design is broken by that truncation. Express the entire fix inside a single CTE.

**若名字不匹配**（实测 53%）追加：

> Your suggested_fix_sql defines CTE `annual_counts`, but it must be the CTE named
> `category_counts`. Return exactly one CTE under that name.

## 7.3 重试机制

**位置**：紧接兜底路径之后，**续用同一对话**（refiner 刚探过库，schema 在上下文里）。

**探库 budget 重置**：重置 `turn` 计数器（给 5 轮新预算），但 **`sql_executed` 不清零** ——
清零会让"至少 3 次探库"那道闸门重新生效、强迫它再探 3 次；不清零则它**可以**继续探库、
但不被强迫。

```
for attempt in 1..3:
    跑轮次循环（最多 max_turns 轮）→ 出 verdict（实测总是走兜底）
    校验（替换后执行整条 SQL）
    若通过 → 结束
    否则 → 错误追加进对话，重置 turn 计数，继续
3 次都失败 → 返回 None，保留原 SQL
```

**一个必然后果**：`harness_executed` 不清零仍是 `False`，条件 B 会继续挡住循环内出 verdict，
所以**每次重试仍会走满轮数再走兜底**。因此每次重试的成本是**一整个探库周期**（约 6 次
LLM 调用），而非单次调用。

## 7.4 与前面各轮的关系

**A–F 不修改**（已决策）。H 轮的臂 A 与前六轮的 37/34/35 不可直接比 —— refiner 的产出质量
变了。H 轮自己的臂 A vs 臂 B 仍严格配对，那才是知识增益的归因依据。

**阶段 6 的代码在这里被用上**：`[DOWNSTREAM]` 让 refiner 事前看见下游、段说明让它理解各段
含义、`validate_fix_sql` 闭包正好是这里要用的校验器。阶段 6 唯一白做的部分是把校验挂在了
循环内那条从不执行的路上 —— 挪到兜底路径之后，那段代码就活了。

## 7.5 验收

- ✅ **校验器实际被调用**（阶段 6 两次实测都是 0 次，这是最基本的活性检查）。
  构造同型场景（模型从不主动交结论、建议改了 CTE 名）实测：校验器 3 次调用、
  拒收信息回传 2 次、`verdict_attempts=3`、`verdict_validated=False`
- ✅ 每片段的重试次数与结果记进 `refiner_<name>.json`：
  `verdict_attempts` / `verdict_validated` / `verdict_validation_errors`；
  `retrieved_rules.json` 记本次配置
- ✅ 默认关闭（`--verdict-attempts 1`），两臂同传（`plan_steps` 只发给两臂不发给 agent 步）
- ✅ 关闭时行为与 E 轮一致：无校验器时不加任何审计字段、不多调一次 LLM
  （`TestWithoutAValidatorNothingChanges`）。兜底 prompt 提为模块常量 `FORCED_VERDICT_PROMPT`
  时逐字节未动，266 行缩进经内容级比对确认只改了缩进
- ✅ 循环内那次校验拒收也计入审计。实测发现重试会让 refiner 去跑编译自检，
  从而解锁 §6.8 里那道 `harness_executed` 闸门，于是循环内的自检第一次真正执行并拦下一条
  建议 —— 起初只记了兜底那次，导致这次拒收不可见、trace 也没留原因。
  现在两处校验共用同一个记录入口（`record_validation`），既不漏也不重复计数
- ⬜ 监控 `forced_verdict_no_json_tags` 与 `forced_verdict_json_parse_error`
  （实测 1404 个 verdict 里 3 个解析失败；重试次数增加后可能上升）

实测行为核验见 `results_reference_track.md` §15（4 个实例，三条路径各命中一次）。

## 7.8 跑 H 轮的指令

两臂都要重跑（refiner 产出质量变了），**agent 步直接复用 E 轮的 `outputs/adopt5_agent`** ——
那里 86 个实例的 `execution_query.sql` 都在。这么做省掉约 2 小时，更重要的是让 H 轮与 E 轮
**逐实例共享同一份起始 SQL**，于是 E vs H 也成为配对比较，而不只是臂 A vs 臂 B。
`--refine-output` 只读源目录（先 sync 到目标再精修），不会改动 `adopt5_agent`。

因此不走 `run_pipeline.py`（它的 agent 步会从头再跑一遍），直接发两条臂命令：

```bash
# 臂 A：有 refiner、无知识
source .env && python -u -m src.agents.sql_agent_runner \
  --refine-output outputs/adopt5_agent \
  --refine-output-dir outputs/h_retry_refonly \
  --model gpt-4.1 \
  --refiner-turns 5 --refiner-min-probes 3 \
  --adopt-refiner-sql --validate-fix-in-context --verdict-attempts 3 \
  2>&1 | tee outputs/h_retry_refonly.log

# 臂 B：有 refiner、有知识
source .env && python -u -m src.agents.sql_agent_runner \
  --refine-output outputs/adopt5_agent \
  --refine-output-dir outputs/h_retry_tk \
  --tkstore tkstore/tkstore_sqlite.csv \
  --model gpt-4.1 --filter-model gpt-4.1 \
  --refiner-turns 5 --refiner-min-probes 3 \
  --adopt-refiner-sql --validate-fix-in-context --verdict-attempts 3 \
  2>&1 | tee outputs/h_retry_tk.log
```

两条除 `--tkstore` / `--filter-model` 外逐字相同，这是配对的前提。可并行（输出目录不同、
互不写同一文件），也可串行。中断后重跑同一条命令即按实例续跑。

评测与对比：

```bash
python evaluation/evaluate.py --result_dir outputs/h_retry_refonly --gold_dir evaluation/gold
python evaluation/evaluate.py --result_dir outputs/h_retry_tk      --gold_dir evaluation/gold
python scripts/compare_arms.py \
  --arm-a outputs/h_retry_refonly --arm-b outputs/h_retry_tk \
  --tkstore tkstore/tkstore_sqlite.csv --out outputs/h_retry_comparison.csv
```

### 成本（按 E 轮同配置实测重估）

`results_reference_track.md` §9 那个"9 小时/臂"是 **25 轮**时代的数，不适用。E 轮
（5 轮 / 3 探针）实测：臂 A **1.5 小时**、臂 B **2.3 小时**，合计 3.8 小时。H 轮只在重试
触发时加成本（4 实例探针里 3/17 个片段触发，每次约 6 次调用），预计**臂 A 约 2 小时、
臂 B 约 3 小时**，合计 4–6 小时；并行则墙上时间取较长的那条。

**实际**：臂 A 1.58 小时、臂 B 3.09 小时，与预估一致。结果见
`results_reference_track.md` §16 —— 知识净 +3 → **−1**，且损伤全部集中在没有 db 规则的
34 个实例上（净 −2）。下面两个阶段针对这个结果。

---

# 阶段 8 — 关掉"别库 generic 规则"的注入（I 轮）

**代码已落地，86 实例的 I 轮尚未跑。** `--cross-db-generic` 默认 `never`；J 轮全量故意
传了 `always`，与 H 轮同一套候选。四实例探针（`never` + 新 FilterKnowledge）见
`results_reference_track.md` §17。针对 §16.2：H 轮全部的知识损伤集中在那 34 个"所在库在
store 里一条规则都没有"的实例上，它们能收到的 100% 是从别库挖出来的 generic 规则。

## 8.0 现状判据

`tkstore/tagger_index.py:566-572` 的保留条件是"**scope 是 generic** 或 **库名匹配**"：

```python
if db is not None:
    is_generic = (scope_lower == 'generic')
    db_matches = (row_db == 'all' or row_db == db.lower())
    if not is_generic and not db_matches:
        continue
```

`is_generic` 一为真就短路，`db_matches` 算了也不用。

## 8.1 "在各字段上过滤"实际只有一个字段能起作用

原始设想是"让 generic 规则也在各字段上过滤"。实测 store 里 52 条 generic 规则的取值：

| 字段 | generic 52 条 | 加过滤能排除吗 |
| --- | --- | --- |
| `table` | **全 52 条是 `all`** | 不能，`all` 按定义匹配一切 |
| `column` | **全 52 条是 `all`** | 不能 |
| `nulls` | 49 条 `all`，3 条 `yes` | 几乎不能 |
| `data_type` | 34 条 `all`，18 条具体值 | **现有判据已在过滤** |
| `sql_operations` | 平均 3.8 个操作 | **现有判据已在过滤** |
| `db` | 全 52 条具体库名，**0 条 `all`** | **只有这个能排除** |

所以本阶段等价于"按库名过滤 generic 规则"。B2 那条偏离（`table`/`column` 不参与过滤）
只对 66 条 db 规则有意义 —— 它们里 56/53 条带具体表名列名，那是**另一个**改动。

## 8.2 两种切法，效果对 −2 相同，对另外 52 个实例不同

| | 做法 | 34 个无 db 规则的实例 | 另外 52 个实例 |
| --- | --- | --- | --- |
| **全局版** | 删掉 `is_generic` 短路 | 可见规则归零，臂 B ≡ 臂 A | generic 也被删，只剩同库规则 |
| **条件版** | 仅当该实例没有任何同库规则时关闭 generic 注入 | 同上 | **不变**，与 H 轮逐实例可比 |

全局版改动后每实例可见规则数（实测）：

| 可见规则数 | 实例数 |
| --- | --- |
| **0 条** | **34** |
| 2–6 条 | 32 |
| 10–19 条 | 20 |

归零的 34 个实例所在库为 `complex_oracle`、`log`、`oracle_sql`、`f1`、`imdb_movies`、
`electronic_sales`、`BowlingLeague`、`EntertainmentAgency`、`school_scheduling` ——
在 store 里 **db 和 generic 规则都没有**。

**预期**：两种切法都会把 −2 消掉，但**不是修正，是关闭**。剩余增益落在 0～+1，
仍在 §11 的 ±2% 噪声带内。全局版额外给出一个有价值的上界：**同库知识单独值多少**；
代价是有效样本从 86 缩到 52，跨库迁移变成未测。

## 8.3 只需重跑臂 B

臂 A 不带 store，本阶段对它零影响，`outputs/h_retry_refonly` 直接复用。成本约 3 小时。

## 8.4 验收

- 新标志默认关闭，关闭时检索结果与 H 轮**逐条相同**（mem_id 序列可直接比对）
- 打开后：那 34 个实例的 `retrieved_rules.json` 里 `selected` 全空
- `retrieved_rules.json` 记录本次配置，供审计
- 34 个实例上臂 B 的最终 SQL 与臂 A **逐字节相同**（这是"等于关闭"的最强检查）

---

# 阶段 9 — 让 FilterKnowledge 用完整任务上下文筛规则（J 轮）

**已实现，并已用 `always` + `--context-filter` 跑完 86 实例。** 结果见
`results_reference_track.md` §17：知识净差仍是 −1，伤害从无 db 规则组挪到了有 db 规则组。
实现前 FilterKnowledge 只看当前 CTE 和候选规则。它不知道用户真正要什么、
这个 CTE 在整条 SQL 中的职责、上下游是否已经完成了规则所要求的工作，也不知道主 agent
已经观察到了哪些事实。它因此实际在回答“规则文字与局部 SQL 是否有词面关联”，而不是
“规则能否帮助验证这个 CTE，且不会诱导 refiner 改坏整条查询”。

这不是把 FilterKnowledge 改成严格的语义裁判。**决策：保持高召回原则：宁可保留可能
相关的规则，只排除已有明确反证的规则。** H 轮已经表明有害但可运行的规则会真的落地，
但反过来把不确定规则过早删掉也会偏离 Alg 4 的 Retrieve 目标。

## 9.0 当前 prompt 的信息缺口

一条规则要经过两步筛选才能到 refiner 手里。第一步是**代码筛选**，函数
`search_index_for_sql`（`tkstore/tagger_index.py:492`）逐行读 store 的 CSV，按 SQL 操作、
数据类型、空值处理挑出候选，把每条规则打包成一个 Python 字典返回。第二步是**LLM 筛选**，
也就是 FilterKnowledge，函数 `_llm_filter_relevant_rules`（`:753`）把候选渲染成文字清单
放进 prompt，让模型挑出真正要用的。

第二步的 user prompt 目前只有：

- 当前数据库名；
- 当前 CTE 的文本；
- 候选规则的 `mem_id`、`scope`、`sql_operations`、`data_type`、`nulls` 和 rule 文本
  （截断到 500 字符）；
- “不确定就保留”“约 70% 相关就保留”的指令。

它**不含**完整用户问题、主 agent 的探库结果或错误、schema、`[PREVIOUS_CTES]`、
`[DOWNSTREAM]`，也不含候选规则的 `table` / `column` 字段。

> **更正一处此前的错误记载。** 本节原先写着 prompt 含"候选规则的来源 db"，那是错的。
> 实测把 `local330` 的 6 条候选按 `_llm_filter_relevant_rules` 的渲染方式打印出来，
> 每一条都是 `db=all`：
>
> ```
> mem_id=26 | scope=generic | db=all | table=all | column=all
> mem_id=42 | scope=generic | db=all | table=all | column=all
> ```
>
> 原因见 §9.1 的缺陷一。**FilterKnowledge 从来不知道任何规则来自哪个数据库**，
> 所以它连"这条规则来自别的库、迁过来要小心"这个念头都没有机会产生。

`local330 / normalized_activity` 是直接例子。FilterKnowledge 只见到一个路径归一化的
`CASE`，从 6 条候选中选出了：

| mem_id | scope | 来源 db | 规则主题 |
| --- | --- | --- | --- |
| 26 | generic | `California_Traffic_Collision` | CASE / 类别值必须显式处理 |
| 42 | generic | `SQLITE_SAKILA` | 下游引用的 CTE 必须被定义 |

但它没看见：用户问事件数；bare `/` 的特殊处理是故意的；主 agent 已查到 `/x` 与 `/x/`
并存；下游 `session_land_exit` 已按 `(session, path)` 去重。规则 26 随后诱导 refiner
给事件日志 CTE 加 `SELECT DISTINCT`，改变了事件计数（§16.4）。

## 9.1 两个既有缺陷，都出在两步筛选的交接处

### 缺陷一：第一步没把来源库传给第二步，第二步的分桶与排序因此从未生效

第一步 `search_index_for_sql` 在打包返回字典时列举的字段是
`mem_id`、`instance_id`、`scope`、`sql_operations`、`table`、`column`、`data_type`、
`nulls`、`rule`、`matches`（`tkstore/tagger_index.py:650-661`）。**其中没有 `db`。**
函数内部其实读过每行的库名（存在局部变量 `row_db` 里，用于库名匹配），但打包时没放进字典，
这个信息在返回的那一刻就丢了。实测确认：该函数返回的字典键只有上面那十个。

第二步 `_llm_filter_relevant_rules` 一开始要把候选分成"本库专属规则"和"通用规则"两桶，
让本库规则排在前面。分桶判断是 `rule.get('scope') == 'db' and db and rule.get('db') == db`
（`:778`）。`rule.get('db')` 在字典里找不到 `db` 键，返回 `None`；参数 `db` 是当前库名
（例如 `'log'`）。`None == 'log'` 恒为假，所以**本库规则那一桶始终是空的，所有规则都落进
通用桶**，"本库规则排前面"这个设计从未生效。渲染那行写的是
`db={rule.get('db', 'all')}`（`:837`），取不到就用 `'all'` 顶上，于是每条规则都显示
`db=all`。

后果有三层，第三层最糟：

1. **排序失效** —— 本库专属规则本该最先呈现，实际与别库通用规则混在一起。
2. **信息缺失** —— 模型看不到来源库，无法对跨库迁移保持警惕。
3. **prompt 含一句假陈述** —— 正文明确告诉模型规则已排好序、本库专属的在最前面
   （`:934-936`：`Database-specific rules for {db} come FIRST`），而实际顺序并非如此。
   模型被引导去相信一个不存在的顺序，可能因此更信任排在前面的规则，
   而那些只是恰好排在前面的别库通用规则。

同一个 `db` 键缺失还让 chunk 路径的重排函数 `rerank_priority`（`:859`）失效：
它用 `db_name = rule.get('db', '').lower()` 取来源库，恒为空字符串，
于是"本库 db 规则得 1000 分"那条分支永不触发。

### 缺陷二：prompt 在代码里有两份副本，改一份等于只改一半

`_llm_filter_relevant_rules` 按候选数量分两条路。候选**超过** 15 条时，把规则切成每 15 条
一组，逐组调用辅助函数 `_process_rule_chunk`，prompt 写在那个函数里（`:672-714`）。
候选**不超过** 15 条时，它不调用那个辅助函数，而是在函数体内部另拼一份 prompt
（`:921-982`）并自己调 `litellm.completion`。两份措辞高度重合但不相同 —— 短的那份筛选标准
有 4 条，长的那份有 10 条。也就是说同一个 store、同一个 refiner，会因候选规则多还是少
而收到两套不同的筛选指令。

实测 H 轮臂 B 的 354 个片段，两条路都不是边缘情况：

| 路径 | 片段数 | 占比 |
| --- | --- | --- |
| 候选 > 15 条，走 `_process_rule_chunk` | 198 | 56% |
| 候选 ≤ 15 条，走函数体内联的那份 | 156 | **44%** |

只改其中一份，44% 的片段会静默留在旧 prompt 上，而日志和产物不会有任何迹象。
这与阶段 6 踩的坑同型：那次自检挂在一条从不执行的分支上，实测 708/708 个片段绕过了它。

## 9.2 决策一：新实现放在我们自己的文件，上游一行不动

**已决策：在 `src/agents/sql_agent_runner.py` 里写一份新的 FilterKnowledge，
`tkstore/tagger_index.py` 保持原样。新标志关闭时继续调用上游那份。**

三条理由：

**两份 prompt 副本的风险从结构上消失。** 我们只写一个函数、一份 prompt，不存在"改了一半"
的可能。

**"关闭时与 H 轮逐字节一致"成为结构性保证而非测试纪律。** 关闭时调用的就是同一个未修改的
上游函数。若改上游，这条一致性只能靠测试保证，而阶段 6 已经证明测试通过也可能实际走的是
另一条分支。

**改动量已接近重写。** 要增加用户问题、探库证据、上游 CTE、下游 CTE 四个段落，
并替换整套筛选标准；放进上游文件反而更难说清哪部分是上游的、哪部分是我们的。

代价是两份实现并存，但每次运行只走其中一条：标志关闭走上游那份，打开走我们那份。

## 9.3 决策二：补上 `db` 字段，并让本库规则真正排到前面

**已决策：用阶段 8 已有的 `_store_rule_index` 给候选补 `db` 字段，
并且让"本库规则排前面"这个分桶第一次真正生效。**

补 `db` 的方法是现成的。阶段 8 实现的 `_store_rule_index`
（`src/agents/sql_agent_runner.py`）读 store 的 CSV，返回两样东西：每条规则的 `mem_id`
到其来源库名的映射（字段 `db_of`），以及哪些库拥有 db 作用域规则的集合
（方法 `has_db_rules`）。用它就能在候选进入我们的 FilterKnowledge 之前补上 `db` 字段，
不需要动上游的 `search_index_for_sql`。

> ### ⚠️ J 轮因此是复合改动，解读结果时必须写清
>
> 打开新标志后，J 轮同时改变了两件事：prompt 里多了完整任务上下文与新筛选标准，
> 规则呈现顺序也从"全部混在通用桶里"变成"本库规则在前"。**如果 J 轮结果变好，
> 无法区分是模型看到完整上下文后选得更准，还是本库规则终于排到前面更容易被选中。**
>
> 这是明知的取舍：本库规则优先本身是合理设计，方向上也与"减少别库通用规则的伤害"一致，
> 不值得为了单变量而刻意保留一个已知失效的排序。但记录 J 轮结果时必须把这句话写进结论，
> 不能把差值整体归因给 prompt。若 J 轮出现退步且需要定位，下一轮应把排序单独关掉复测。

顺序修正生效后，§9.0 引用的那句 `Database-specific rules for {db} come FIRST`
就不再是假陈述，可以在我们的新 prompt 里保留 —— 但措辞要与实际实现一致。

## 9.4 新 prompt：完整上下文，但不搬重复品

每个候选规则 chunk 的 FilterKnowledge prompt 改为明确分段：

```text
[USER_QUESTION]
完整用户问题

[DATABASE]
当前库名

[AGENT_PROBE_EVIDENCE]
主 agent 已执行的 <sql> 探针及其紧随的 SQL_RESULT_TABLE / SQL_ERROR

[PREVIOUS_CTES]
当前 CTE 之前的 CTE

[CURRENT_CTE]
当前要验证的 CTE

[DOWNSTREAM]
当前 CTE 之后的 CTE 与最终 SELECT

[CANDIDATE_RULES]
每条：mem_id、scope、source_db、sql_operations、table、column、data_type、nulls、rule
```

`[AGENT_PROBE_EVIDENCE]` 直接从 `messages.json` 提取**原始**探针—结果对，不做 schema
摘要；但不搬主 agent system prompt、原始问题、`<think>` / 无 SQL 的评述，或最终
`<solution>`。这些要么已在其他段出现，要么是思考流而非可核验证据。86/86 个 agent
目录都有 `messages.json`，且都能抽出至少一组这样的探针对。

这与阶段 10 共用一个“从 `messages.json` 提取探针—结果对”的 helper；本阶段把它渲染为
FilterKnowledge 的一个 `[AGENT_PROBE_EVIDENCE]` 段，阶段 10 把同一组消息作为 refiner
对话轮次注入。**提取代码在本阶段落地，阶段 10 复用**，因为阶段 9 先需要它。

在 `--refine-output` 路径下，`messages.json` 由 `_sync_instance_dirs` 连同整个实例目录
一起复制进臂目录，所以直接从正在精修的那个实例目录读即可，不必回头去找 agent 目录。

两处段落的取值需要明确：

**最终 SELECT 阶段用 `[FULL_QUERY]`，不输出 `[DOWNSTREAM]`。** 那一阶段
`perform_refinement_and_revision` 调用检索时传的 SQL 文本是**整条查询**
（`retrieve_for('final_select', '_final_select', complete_query)`），因为 refiner 在该阶段
审的就是整条查询，所以没有下游。硬塞一个空的 `[DOWNSTREAM]` 段只会让模型困惑。

**下游 CTE 文本的计算要从 `--validate-fix-in-context` 底下抽出来。** 目前
`sql_agent_runner.py` 的 CTE 循环里，下游文本只在 `--validate-fix-in-context` 打开时才计算
（那是阶段 6 加的标志，作用是让 refiner 看到谁在读它的输出）。阶段 9 也要用这段文本，
所以要把计算逻辑抽成两个标志共用，否则阶段 9 会莫名依赖阶段 6 的标志。

## 9.5 高召回选择规则

替换当前“70% 可能相关 / 不确定就保留”的模糊口径为下列可审计规则：

> 优先保留可能帮助验证当前 CTE 的规则。只要规则与当前 CTE、用户问题或上下游职责存在
> 合理关联，就保留；不能因为当前证据不足以**证明**规则适用而排除它。
>
> 仅在下列情况明确成立时排除：
>
> 1. 规则要求当前 CTE 完成的工作，已经明确由上游或下游 CTE 完成，且当前 CTE 的职责
>    与该规则检查的内容无关；
> 2. schema 或主 agent 已观察到的结果**直接反驳**规则的前提；
> 3. 规则依赖当前数据库明确不存在的表、列、函数或数据形态；
> 4. 规则来自其他数据库，且其适用依赖来源库专有 schema / 业务语义；当前问题、schema、
>    CTE、上下游与探库证据均没有对应支持。
>
> 不要仅因规则来自其他数据库而排除它；不要因为当前 CTE 自身没有做某事就排除它；
> 两者都可能是规则发现真实 bug 的信号。

这保留 generic 跨库迁移的可能性；阶段 8 只是先对没有任何同库知识锚点的实例做硬保护，
不把“来源 db 不同”本身当作 FilterKnowledge 的排除理由。

## 9.6 规则元数据必须完整呈现

即使阶段 8 的实测发现所有 generic 规则 `table=all` / `column=all`，FilterKnowledge
仍应看到这些字段：

- 对 db scope 规则，56/66 条有具体 table、53/66 条有具体 column；
- `all` 本身是证据：它表示该规则没有 schema 级锚点，不能与精确指向当前表列的规则享有
  同等的迁移可信度；
- 否则即使以后修 B2 让代码预筛使用 table / column，LLM 筛选层仍然看不见这些依据。

## 9.7 chunk、体量与成本

候选规则仍按 15 条一组分块，不合并为一个长列表。分块的目的是避免规则太多时模型对清单中部
注意力下降；如果我们不分块，候选超过 15 条的那 198 个片段就会同时改变上下文和分块方式，
又多一个变量。

完整上下文会在每个分块重发。实测估算每次调用多带的体量：主 agent 的原始探针—结果对中位
5,820 字符（最大 36,554），整条 SQL 中位 1,161 字符（最大 5,234），合计中位约 6,982 字符，
按四字符一 token 折算约 **1,745 token**。H 轮臂 B 的 FilterKnowledge 调用次数实测约
**580 次**（354 个片段，超过 15 条候选的按每 15 条一次），因此额外输入约 **1.0M token**。
墙上时间增加不多，主要是 token 成本。

如果实测发现 context window 或成本不可接受，后续才讨论“仅携带当前片段所引用表的探针”。
那是**筛选**而非摘要，但本阶段默认不做，以免再引入未经验证的选择机制。

## 9.8 顺带处理的两处，都在同一段代码里

**把模型返回的 `reasoning` 记进产物。** 模型返回的 JSON 里除了 `selected_indices`
（被选中规则的编号列表）还有一个 `reasoning` 字段，是它对"为什么选这几条"的简短说明。
上游代码只读 `selected_indices`（`:729` 与 `:998`），`reasoning` 解析出来后从未被读取、
直接丢弃。我们自己那份实现要把它记下来，这样"某条有害规则当初为什么被选中"就能事后查证，
而现在完全查不到 —— §16.4 里分析 `local330` 为什么选中规则 26 时，只能靠事后重跑推断。

**不要把上游的一处变量名覆盖抄过来。** `search_index_for_sql` 有一个参数叫 `instance_id`，
供调用方限定只看某个实例的规则；而循环体内第一行又写了
`instance_id = row.get('instance_id')`（`:532`），把参数值覆盖成当前这一行自己的实例号。
结果是从第二行起，那个"按实例号过滤"的条件比较的是同一个值、永远不会过滤掉任何东西。
我们目前调用时不传 `instance_id`，所以现在没有影响；写新实现时不要复制这个模式。

## 9.9 验收

- 新标志默认关闭。关闭时走上游 `_llm_filter_relevant_rules` 原函数，
  selected 的 mem_id 序列与 H 轮逐条一致（这由"上游一行不动"结构性保证，测试只需确认
  关闭时确实调的是上游那个函数）；
- 打开后，`retrieved_rules.json` 每个片段记下 candidates、selected、**excluded** 的
  mem_id，以及模型返回的 `reasoning`。完整 prompt 默认不落盘（580 次调用 × 约 7,000 字符
  太大），需要时另加一个标志；
- **活性检查：候选 ≤ 15 条与 > 15 条的片段都必须走新实现**。这是缺陷二的直接防线，
  实测两类片段分别占 44% 和 56%，只覆盖一类等于只改了一半；
- 补 `db` 字段后，prompt 里每条规则显示的是真实来源库而非 `db=all`；本库规则确实排在
  别库规则之前（这两条要分别断言，因为它们由同一个字段驱动但是两件事）；
- `local330 / normalized_activity` 定为固定用例：prompt 必须同时含用户问题、
  主 agent 探到的路径样本、下游按 `(session, path)` 去重的逻辑，以及规则 26 的来源库
  `California_Traffic_Collision` 与它的 `table=all` / `column=all` 元数据；
- 明确反证的规则被排除，而只是不确定的规则仍被保留（§9.5 的两类各写一条测试）；
- FilterKnowledge 调用或解析失败时沿用当前 fail-open 行为：返回候选全集，并记录失败，
  不得静默把规则丢掉。

## 9.10 实验与顺序

原计划阶段 8（I 轮）先跑。实际顺序反过来：先在四实例上分别探了 `always` 与 `never`，
再把 **`always` + `--context-filter`** 作为臂 B 跑满 86 实例（I 轮全量未跑）。臂 A
复用 H 轮的 `outputs/h_retry_refonly`。FilterKnowledge 只在有 store 的臂 B 调用。

因为候选集合与 H 轮相同（`always`），这轮的主比较是 **J 轮臂 B 对 H 轮臂 B / 臂 A**，
而不是对尚未跑的阶段 8 臂 B。阶段 9 自身仍是 §9.3 的复合改动（prompt 上下文与规则顺序
同时变）。结果见 `results_reference_track.md` §17。

---

# 阶段 10 — 让 refiner 继承主 agent 的探库消息（K 轮）

**尚未实现。** 针对一个独立于知识的信息断点：主 agent 探过的库，refiner 完全看不到。

## 10.0 现状与实测动机

两套对话完全独立。`run_refiner` 自建 `messages = [system, user_payload]`
（`cte_refiner.py:556-559`），不接收也不合并主 agent 的 history；
`--refine-output` 路径只读 `execution_query.sql`，连已有的 `messages.json` 都不读
（§4.1 当时的决策是"不改"，本阶段**反转**该决策，但只为 refiner 反转）。

实测代价：

| 度量 | 实测 |
| --- | --- |
| 主 agent 探库次数 | 中位 **9** 次/实例（25 turn 预算，未跑满） |
| refiner 探查的表中主 agent 已探过的比例 | **中位 100%，均值 98%** |
| refiner 探针类型 | `PRAGMA` 457、`sqlite_master` 353、`LIMIT` 抽样 186、`COUNT/DISTINCT` 109 |
| refiner **前两轮**的探针 | `sqlite_master` 353 + `PRAGMA` 129 = **482/494 是 schema 发现** |
| 两者字面完全相同的探针 | 仅 4 条（1%）|

`sqlite_master` 出现 353 次≈354 个片段各一次，因为 system prompt 硬性要求第一轮必须列表。
**5 轮预算里头两轮几乎固定用于重新发现 schema**，而主 agent 早就拿到了。这直接连上 §6.8
的病根：轮数耗尽 → 全部走兜底 → 阶段 6 的自检永不执行。

字面重复只有 1%，所以**省不下"相同 SQL"** —— 要省的是同一批信息的不同问法。

**`local330` 是直接证据**（§16.4 里被知识改坏的一条）。主 agent 的历史第 25 条明确记着两件
refiner 不知道的事：bare `/` 的特殊处理是**故意的**，以及去重**已经在下游
`session_land_exit` 里按 `(session, path)` 做过**。臂 B 的 refiner 恰恰给
`normalized_activity` 加了 `SELECT DISTINCT` 才把它弄坏。

## 10.1 做法：按原样注入为对话轮次，不做摘要

**决策：不蒸馏成 schema 摘要块。** `messages.json` 结构规整、可靠配对 —— 带 `<sql>` 的
assistant 消息紧跟一条以 `SQL_RESULT_TABLE:` 或 `SQL_ERROR:` 开头的 user 消息
（实测 86/86 都能抽出探针对，0 个失败）。所以按原样搬，作为**对话轮次**注入而非拼进
payload 文本：

```
system    refiner 的 system prompt
user      refiner payload（问题、CTE、CTE_GOAL+规则、PREVIOUS_CTES、DOWNSTREAM）
assistant <sql>SELECT name FROM sqlite_master WHERE type='table';</sql>   ← 继承
user      SQL_RESULT_TABLE: ...                                           ← 继承
assistant <sql>PRAGMA table_info(activity_log);</sql>                     ← 继承
user      SQL_RESULT_TABLE: ...                                           ← 继承
（refiner 的轮次循环从这里继续）
```

这样"同一段信息在 payload 里出现好多遍"天然不存在 —— 它们是独立消息，不是被复制的文本。

## 10.2 不搬的三类，去掉它们是"不搬重复品"而非摘要

| 不搬 | 理由 |
| --- | --- |
| 主 agent 的 system prompt 与第一条 user 消息 | 问题已在 `[USER_QUERY]`、hints 已在 `[PREDICTED_CTES_HINT]`，搬过去是同一段话出现两遍 |
| `<think>` 与无 SQL 的评述消息 | 占 `messages.json` 大头（`local330` 28 条消息里探针对只占 12 条），是思考流而非证据 |
| `<solution>` 那条 | 最终 SQL 已以 `[CTE]` + `[PREVIOUS_CTES]` + `[DOWNSTREAM]` 三段呈现，再搬一份完整 SQL 会与拆解版本并存互扰 |

## 10.3 体量与成本

| | 中位 | 最大 |
| --- | --- | --- |
| 继承块（只留探针+结果） | 5,813 字符 | 36,544 |
| 额外输入 token/实例（中位 4 片段 × 约 6 次调用） | 34,533 | 493,344 |
| 86 实例合计 | **约 4.0M token** | — |

放大来自"每片段都要带、每次调用都要重发"。一个**不涉及摘要**的收缩办法：
每个片段只继承涉及**它所引用表**的探针（筛选而非提炼，还顺带提高相关性）——
中位降到 3,103 字符/片段，合计约 **2.0M token**。

## 10.4 必须一起改的三处闸门

不改会重演阶段 6 那种"代码写了但从未生效"：

**探库下限 `min_required_sql=3`** 现在只数 refiner 自己的探针。继承的要不要计入？
计入则它第一轮就能出 verdict（也就让阶段 6 的自检第一次有机会执行）；
不计入则省下的轮数被闸门吃掉、收益归零。

**`[MANDATORY_PROBES]` 与 system prompt 里"第一轮必须 `sqlite_master`"** 要同步改，
否则模型会照做、把继承的 schema 再查一遍。

**`harness_executed` 那道闸门保留。** §6.8 与 `local018` 都证明"编译自检"是唯一能让
循环内 verdict 通过的钥匙；主 agent 从不按 CTE 名探库（CTE 不是表），所以继承不会误触它。

## 10.5 与上游的关系

**这不是修复回归，是在上游之上新增能力。** 上游 `tkboost.sql()` 调 `run_refiner` 时也只传
问题、CTE、前序 CTE、规则、库路径，没有 agent history 参数（`tkboost/__init__.py:633-645`）；
它自己生成 draft 更是单次 LLM 调用、完全不探库（`:500-521`）。写结论时必须说清，
免得日后被当成"对齐上游"。

## 10.6 验收

- 新标志默认关闭，关闭时 refiner 的 `messages` 与 J 轮前**逐条相同**
- 打开后 trace 能看到继承的探针（需要一个新的 trace section，否则无法审计继承了什么）
- refiner 自己的 `sqlite_master` 探针数从 353 显著下降（活性检查：没下降就说明闸门没改对）
- 循环内出 verdict 的片段数 > 0（§6.8 实测为 0；若继承计入探库下限，这里应该动）

## 10.7 顺序：阶段 8、9 先行，三者不可同轮

阶段 8 改变候选规则集合，阶段 9 改变 FilterKnowledge 从其中选出什么，阶段 10 才改变
refiner 在收到规则后的证据基础。同时上则无法判断收益或损伤来自哪一层。且 `local330`
同时落在三者的作用范围内（无 db 规则、被错误 generic 规则选中、主 agent 已有可反驳的
探库证据），是它们会互相掩盖的现成例子。

阶段 8 只重跑臂 B（约 3 小时），阶段 9 也只重跑臂 B，阶段 10 两臂都要重跑。

## 7.6 成本

| | 值 |
| --- | --- |
| E 轮两臂执行失败的片段 | 107 |
| 触发至少一次重试的比例（臂 B、CTE 阶段） | 52/85 = **61%** |
| 每次重试 | 最多 5 轮探库 + 1 次兜底 ≈ **6 次调用** |
| 最坏情况额外调用 | 107 × 3 × 6 ≈ 1900 |
| 实际预期 | **700–1100 次**，约 +1.5 小时/臂 |

合计约 **8 小时**。

## 7.7 预期与止损点

采纳数应从 E 轮臂 B 的 58 涨向 **80 以上**，臂 A 回到 **36–37**，知识净 **≥ +3**。

**止损点（现在就定）**：若采纳数涨了但知识净仍在 0 附近，指向 **E 轮那个 +3 本身不稳定**
（3.5%，仅略高于 §11 测得的 ±2% 噪声线）。那时应先重跑一次 E 轮验稳定性，
而不是继续加设计。

> ### ⚠️ 下面这个探针推断已被 86 实例数据推翻，见 `results_reference_track.md` §16
>
> 全量实测：ok 率只从 73% 升到 82%，**采纳数反而从 45 升到 59**，编辑面没有塌缩。
> H 轮的结果是臂 A 34→36（破坏被修掉）、臂 B 37→35，知识净 +3 → **−1**，
> 且损伤**全部集中在那 34 个没有 db 规则、只能收别库 generic 规则的实例**（净 −2），
> 有 db 规则的 52 个仍是 +1。真正的成因是"有害但能跑"的编辑不再被执行闸门拦住。
>
> ### ⚠️（历史）4 实例探针踩到止损线，但踩法和上面设想的不同
>
> 见 `results_reference_track.md` §15。17 个片段实测：跑不通的建议从 E 的 9–10 条压到 1 条，
> 但**采纳数没涨**（E 2–4 → H 3），涨的是"什么都不做"（ok 从 3–6 → 13）。
> 用同配置重跑 E 一次做噪声基线，确认这不是方差。
>
> **归因到阶段 6 而非阶段 7**：重试只介入 3 个片段，12 个 ok 出现在重试没触发的地方 ——
> 是 `[DOWNSTREAM]` 可见性让 refiner 不再动手。知识只能经由编辑动作起效，
> 编辑面从 14/17 压到 4/17，两臂就都趋近裸 agent、知识净差按构造趋近 0。
>
> 所以在跑 86 实例之前，应先把阶段 6 拆成两个开关（可见性 vs 校验器），
> 定位是哪一个让 refiner 变哑；若是可见性，可只留校验器 + 重试以保住编辑面。
