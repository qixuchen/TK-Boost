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
  {"n_ctes": 2, "filter_model": "gpt-4.1", "use_llm_filtering": true,
   "retrievals": [{"stage": "cte", "name": "customer_months", "sql_sha1": "…",
                   "candidates": ["24", "26"], "selected": ["26"]}]}
  ```

  `n_ctes` 用于统计有多少实例因 C17 退化成 0 个 CTE，见 3.0 末尾。
  `sql_sha1` 是被检索的那段 SQL 的指纹，用于确认规则是针对**当前**而非陈旧的 CTE 检索的。

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

## 4.1 端到端编排脚本

**用途**：一条命令跑完 baseline 与 augmented 两侧，产出可对比的结果。

**位置**：新增 `scripts/run_pipeline.py`（或 Makefile 目标）。

**输入**：

| 参数 | 含义 |
| --- | --- |
| `--train-split` / `--test-split` | 阶段 1.3 的两个划分文件 |
| `--store` | tkstore CSV，默认 `artifacts/tkstore_sqlite.csv` |
| `--stage` | `populate` / `baseline` / `augmented` / `evaluate` / `all` |
| `--model` | LLM 模型 |

**流程**：

```
populate    : 2.1 train 集 agent 输出 → 2.3 批量 populate → store
baseline    : test 集跑 agent，不传 --tkstore 也不传 --refine-cte → outputs/test_baseline/
augmented   : test 集跑 agent，传 --tkstore + --refine-cte      → outputs/test_augmented/
evaluate    : 对两个目录分别跑 evaluate.py，汇总对比
```

**决策：只跑这两臂**，不做"有 refiner 无知识"的第三臂。理由是论文自己的 baseline 就是
完全不带 Alg 5 的原始 agent，两臂在口径上对齐 Fig. 6。代价是差值无法分离，
必须在结果里写清，见下方"本轮不做"的最后一段。

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
| 2.1 | 24（已跑 7） | 我们的 train 集 = 有 gold SQL 的 24 个，外层 ReAct 最多 25 轮 |
| 2.2/2.3 | 0 | 每实例约 3–4 次 LLM 调用（diff 循环最多 6 轮） |
| 3.3 冒烟 | 5 | 只跑 augmented 一侧 |
| 4 baseline | 86 | 上游 store 轨道的免泄漏子集 |
| 4 augmented | 86 | 同上，额外含 refiner 的 25 轮探库循环 |

**检索本身的 LLM 开销实测**：每个 CTE 约 20 条 code filter 候选，按 `CHUNK_SIZE=15` 切
就是 2 次 `FilterKnowledge` 调用。按每实例 3 个 CTE 加 1 个 final SELECT 算，
**光检索每实例约 8 次调用**，86 个实例约 690 次 —— 这还没算 refiner 每个 CTE 的 25 轮探库。
augmented 一侧单实例成本明显高于 baseline。

先按 3.3 的 5 个实例把链路跑通，再放开全量。

## 本轮不做

- [`deviations.md`](./deviations.md) **B 组**全部 —— 检索只用 3 维特征、正则抽特征、
  refiner 是 25 轮探库循环而非单次 `Feedback`、每 CTE 修订 5 次上限等
  （唯一例外是 3.0 那条陈旧快照，它是 Alg 5 的保真问题，本轮修）
- "有 refiner 无知识"的第三臂对照（决策：只跑两臂）
- BIRD / minidev 适配
- ReFORCE agent（论文 6.1.3 用它证明通用性，不是主结果）
- 论文 6.4 的各项消融

其中 **B10**（refiner 做的比论文多）和 **B11**（修订次数上限）会影响 augmented 一侧的绝对数值，
解读结果时必须一并说明：baseline 与 augmented 的差值里混入了 refiner 探库带来的增益，
不能全部归因于 tribal knowledge。要干净地分离，需要一个"有 refiner 无知识"的第三组对照，
那属于后续消融。
