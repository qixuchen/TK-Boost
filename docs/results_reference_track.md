# 参考轨道结果：上游 store × Spider2-SQLite 86 实例

记录阶段 4 第一轮完整实验。**参考轨道**指用 repo 自带的上游 store
（`tkstore/tkstore_sqlite.csv`）而非我们自己 populate 的 store；主轨道尚未跑。

结论一句话：**refiner 有效（+6.98pp），知识有害（−6.98pp），两者刚好抵消，
带知识的那一臂回到了裸 agent 的水平。**

---

## 1. 实验设置

| 项 | 值 |
| --- | --- |
| 实例集 | `data/splits/spider2_sqlite_test_no_reference_leak.txt`，**86 个**（文件 90 行，含 4 行注释） |
| 选这个子集的理由 | 上游 store 未在这些实例上训练过，避免参考泄漏 |
| TK-Store | `tkstore/tkstore_sqlite.csv`，sha1 `3f75f169…`，**118 条**（generic 52 / db 作用域 66） |
| 模型 | agent 与 refiner 都用 `gpt-4.1`；`FilterKnowledge` 也用 `gpt-4.1` |
| LLM 网关 | `https://api.openlux.ai` |
| 每片段探库上限 | `max_turns=25`（见 §7 第 3 条，这不是上游参考配置） |
| gold | `evaluation/gold/exec_result`，86 个实例全部具备 |
| 代码版本 | `59da634` |

### 三臂架构（共享 agent 产出）

agent 只跑一次，两个精修臂都从**同一份** `execution_query.sql` 出发，所以
`delta_knowledge` 是严格配对比较，不含 agent 的 run-to-run 方差（3.3 实测同一实例
两次能跑出 5 个和 0 个 CTE，方差很大，必须消除）。

| setting | 含义 | 取数 |
| --- | --- | --- |
| 1. 裸 agent | 无 refiner、无知识 | 两臂目录里的 `score`（来自 `execution_result.csv`） |
| 2. 臂 A | + refiner，无知识 | `outputs/test_refonly` 的 `score_final` |
| 3. 臂 B | + refiner + 知识 | `outputs/test_tk` 的 `score_final` |

裸 agent 的分从两臂各取一份，互为交叉校验；共享的 `outputs/test_agent` 不跑
`evaluate.py`（它会删同实例的旧时间戳目录）。

### 复现命令

```bash
set -a; source .env; set +a
python -u scripts/run_pipeline.py \
  --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
  --out-prefix outputs/test --tkstore tkstore/tkstore_sqlite.csv \
  --model gpt-4.1 --filter-model gpt-4.1

python evaluation/evaluate.py --mode exec_result --result_dir outputs/test_refonly --gold_dir evaluation/gold
python evaluation/evaluate.py --mode exec_result --result_dir outputs/test_tk --gold_dir evaluation/gold
python scripts/compare_arms.py --arm-a outputs/test_refonly --arm-b outputs/test_tk \
  --tkstore tkstore/tkstore_sqlite.csv --out outputs/test_comparison.csv
```

产物：`outputs/test_agent`（8.1M）、`outputs/test_refonly`（17M）、`outputs/test_tk`（30M）、
`outputs/test_comparison.csv`、`outputs/test_pipeline.log`。

---

## 2. 完整性核验

数字可信的前提，全部核过：

- 两臂各 **86/86** 实例带 `refinement_complete.marker`，各 86 个 `execution_result_final.csv`
- 日志 `❌ Failed: 0/86`
- 两臂都通过 `✅ shares the agent's starting SQL`（起始 SQL 逐字节相同，配对成立）
- `compare_arms` 出 **86 行**，无 unpaired 警告
- 臂 A 有 **0** 个 `retrieved_rules.json`，臂 B 有 **86** 个 —— 知识开关确实只开在臂 B
- 两臂合计 **0** 个 `no_verdict` 兜底片段（refiner 每次都真出了 verdict）

`outputs/test_agent` 无残缺目录。运行横跨 09-15 至 09-17，中间因凭据未设、
SQLite 探库跑飞（C23）、会话断开各中断过一次，均由实例级续跑恢复，不影响已完成产物。

---

## 3. 主结果

| 分组 | n | 裸 agent | 臂 A（+refiner） | 臂 B（+知识） | B−A | 知识修好 | 知识改坏 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **总体** | 86 | 36（41.9%） | **42（48.8%）** | 36（41.9%） | **−6** | 4 | 10 |
| 无 db 规则 | 34 | 13（38%） | 17（50%） | 13（38%） | −4 | 1 | 5 |
| 有 db 规则 | 52 | 23（44%） | 25（48%） | 23（44%） | −2 | 3 | 5 |

两个必须分开报的口径：

- **`delta_knowledge` = 臂 B − 臂 A = −6**（−6.98pp）。两臂都带 refiner，只差 store，
  所以这个差值**可以归因给 tribal knowledge**。
- **`delta_paper` = 臂 B − 裸 agent = 0**。对齐论文 Fig. 6 的口径，但混着 refiner 的探库增益，
  **不用于归因**。

### 臂 B 与裸 agent 同为 36 是巧合，不是 bug

对的实例集合并不相同：相对裸 agent，臂 B 改好 5 个
（`local025`、`local032`、`local230`、`local263`、`local284`）、改坏 5 个
（`local049`、`local061`、`local070`、`local074`、`local195`），正好抵消。
`delta_paper` 分布是 +1 五个、−1 五个、0 七十六个。

分组那两行同理（1 好 1 坏、4 好 4 坏）。注意这三行不是三个独立巧合 —— 总体是两组之和，
两组各自平衡则总体必然平衡，实际只有两个巧合。

---

## 4. 逐实例分解：知识优先摧毁的正是 refiner 的修复

| 环节 | 修好 | 改坏 | 净 |
| --- | --- | --- | --- |
| refiner（臂 A vs 裸） | 8 | 2 | **+6** |
| 知识（臂 B vs 臂 A） | 4 | 10 | **−6** |

- refiner 修好的 8 个：`local025`、`local060`、`local170`、`local230`、`local269`、
  `local310`、`local330`、`local355`
- refiner 改坏的 2 个：`local061`、`local354`
- 知识改坏的 10 个：`local049`、`local060`、`local070`、`local074`、`local170`、
  `local195`、`local269`、`local310`、`local330`、`local355`
- 知识修好的 4 个：`local032`、`local263`、`local284`、`local354`

**关键观察：知识改坏的 10 个里有 6 个正是 refiner 自己修好的那些**
（`local060`、`local170`、`local269`、`local310`、`local330`、`local355`），
即 **refiner 的 8 个修复被知识撤掉了 6 个**。另外 4 个（`local049`、`local070`、
`local074`、`local195`）是裸 agent 和臂 A 都对、唯独臂 B 做错的。

知识修好的 4 个里也有一个特殊情况：`local354` 是被 refiner 改坏的，知识把它救回来了。
所以严格意义上"知识带来的新修复"只有 3 个（`local032`、`local263`、`local284`）。

三臂全对 30 个，三臂全错 39 个 —— 有 17 个实例在三个 setting 间发生过变化。

---

## 5. 机制：知识让 refiner 更爱挑问题

86 个实例的聚合数据支持"过度改写"假设：

| 度量 | 臂 A（无知识） | 臂 B（有知识） |
| --- | --- | --- |
| 片段总数 | 341 | 335 |
| 判 `issues` 的片段 | 122（**36%**） | 139（**41%**） |
| 被采纳的改写 | 116 | **133**（+15%） |
| 最终 SQL 平均增长 | +321 字符 | +321 字符 |
| 探库轮数中位数 | 22 | 22 |
| 顶到 25 轮上限的片段 | 20% | 22% |

把规则块注入 `cte_goal` 后，refiner 宣布"有问题"的倾向从 36% 升到 41%，多做了 15%
的改写，而多出来的改写**坏的多于好的**（10 vs 4）。

最终 SQL 的平均长度增长两臂完全相同（都 +321 字符），所以这**不是"把 SQL 写臃肿"**，
而是"改的次数变多"。同样的偏移在 72 个结果未变的实例上也存在（34% → 40%），
说明它是系统性倾向，不是少数实例的偶然。

### 10 个改坏的实例机制统一

区分了两种可能 —— "知识让 refiner 判 `ok`、于是漏掉臂 A 做过的修复" vs
"知识导致了不同的改写、结果更差"。判据是臂 A / 臂 B 各自是否改动过 SQL：

**10 个全部属于后者，前者 0 个。** 其中 `local049` 与 `local195` 最干净：
臂 A 一次改写都没做（保持裸 SQL 且正确），臂 B 做了 1 次改写就改错了。

---

## 6. 检索实况：注入的 95% 是 generic 规则

统计 86 个实例、344 个片段实际注入进 prompt 的规则：

| 度量 | 值 |
| --- | --- |
| 每片段注入规则数 | 中位数 **12** 条，平均 12.1，最大 39 |
| 撞上 `_knowledge_block` 的 40 条上限的片段 | **0** |
| 注入的规则里 generic 占比 | **3939 / 4146 = 95%** |
| 注入的规则里 db 作用域占比 | 207 / 4146 = **5%** |
| 而 store 本身构成 | generic 52 条、db 作用域 **66 条** |

**db 专属规则是 store 的多数（66 vs 52），却只占实际注入量的 5%。**
原因是检索的硬闸门只有库名：generic 规则对全部 86 个实例无条件通过，
db 规则只在库名相等时通过，而 34/86 的实例一条 db 规则都吃不到。

所以这一轮实测的其实是"每个片段塞 12 条通用 SQL 建议"，
而不是论文要检验的"注入关于这个库的部落知识"。这与 §3 的分组结果吻合：
只能吃到 generic 的那 34 个每实例净损 **−11.8%**，额外有 db 规则的 52 个只 **−3.8%**。

---

## 7. 与论文的差距及候选原因

README 报的是 **+3.6% ～ +16.9%** 的最大准确率增益；我们的 `delta_paper` 是 **0**，
`delta_knowledge` 是 **−6.98pp**。以下四条按可信度排序，互不排斥。

### 1) 注入内容被 generic 规则淹没（证据见 §6）

95% 的注入量是 generic 规则，而它们是从别的实例学来的通用建议 —— 这正是最容易
诱导 refiner 乱改的形态。分组数据显示伤害集中在只有 generic 可用的那一组。

### 2) 我们把"读规则的 agent"和"改 SQL 的 agent"拆开了

注入点本身**与上游参考实现完全一致**（措辞、40 条上限、`generic_only=False` 都同，
见 `tkboost/__init__.py:617` vs `src/agents/sql_agent_runner.py:115`），不是我们发明的。
真正的差别在**谁来应用修复**：

| | 上游 `tkboost.sql()` | 我们的 `sql_agent_runner.py` |
| --- | --- | --- |
| 修复怎么落地 | **直接采纳 refiner 的 `suggested_fix_sql`**（`tkboost/__init__.py:647-652`） | **丢掉 `suggested_fix_sql`**，只把 `suggested_fix` 文本反馈给主 agent，由主 agent 重出 `<solution>` |
| 主 agent 是否在环 | 不在 | 在 |

上游设计里读规则的 agent 就是写出修正 SQL 的 agent，其输出直接成为结果；
我们这条路把链剪断了 —— 真正改 SQL 的是另一个**从没见过规则**的 agent。规则对最终
SQL 的影响要走"规则 → 评语 → 另一个 agent 的理解 → 重写"这条更长更有损的路径。

这是有意的设计决策，理由是保持主 agent 在环里、对齐 Alg 5 Line 13 的
`C_{t+1} ← concat(C_t, s_t, R_t, f)`，论文这一点站在我们这边（`Feedback` 产出的是
反馈 `f` 而不是成品 SQL）。但 README 的数字是上游那条链跑出来的。

### 3) 我们的 refiner 探库强度是上游的 5 倍，baseline 因此强得多

三个数是三回事：

| | 每片段探库预算 | 出处 |
| --- | --- | --- |
| **论文 Alg 5** | **单次 LLM 调用，完全不探库** | `Feedback(q, c_i, K)`，见 `deviations.md` B10 |
| 上游 `tkboost.sql()` | **5** 轮 | `turns_per_cte = max(max_turns // (len(ctes)+1), 5)`，`max_turns` 默认 10，故下限恒生效 |
| 上游 `sql_agent_runner.py`（本轮走的） | **25** 轮 | `:549`、`:631`、`:689`、`:1286` |

`max_turns=25` **不是我们改的**，Phase 3.1 之前就在 `sql_agent_runner.py` 里。
这是上游两个入口自身不一致，而 README 的数字大概率来自 5 轮那条链。

实测这 25 轮是真用掉的：335 个片段中位数用 **22 轮**，22% 顶到上限，仅 9% 在 5 轮内收工。

更要紧的是一个巧合：**检索的三个特征维度正好是 refiner 亲自实测过的东西。**
检索用 `sql_operations` / `data_type` / `nulls`（B3），而 refiner 的
`[MANDATORY_PROBES]` 强制它做 `PRAGMA table_info`（→ 数据类型）、抽样行、
`DISTINCT 与 NULL 计数`（→ nulls）。规则在"断言"的性质，评审员已经**实测**过了 ——
探库轮数越多，规则的边际价值越低而噪声占比越高。

### 4) 模型与未登记的 prompt 污染

- **模型**：用 `gpt-4.1`，强于论文当时的模型，headroom 更小。
- **refiner prompt 里有未登记的硬编码领域知识**：
  `cte_refiner.py` 的 Instructions 无条件拼入
  `validate collision-level vs party-level needs (case_id uniqueness), and related
  tables if present (e.g., collisions, victims)` —— 全是
  `California_Traffic_Collision` 的专有名词，却对**所有**实例生效。
  它在两臂都存在，**不影响 A vs B 的归因**，但会同时抬高两臂绝对值，与论文比较时不干净。
  86 个实例里只有 1 个属于该库（`local018`）。**这条尚未写入 `deviations.md`。**

---

## 8. 候选消融

| 消融 | 改什么 | 估计成本 | 回答什么 |
| --- | --- | --- | --- |
| 对齐上游探库配置 | `max_turns` 25→5 **且** `min_required_sql` 8→3 | 约 5 小时（两臂） | 是不是我们的 refiner 太强，把知识增益吃掉了 |
| 只注入 db 作用域规则 | 一个检索过滤条件 | 约 9 小时（一臂） | 是不是 95% 的 generic 规则在淹没信号 |
| 直接采纳 `suggested_fix_sql` | 一段采纳逻辑 | 约 7 小时（一臂，省掉主 agent 重写） | 是不是"剪断的链"损失了知识 |
| 压低每片段注入量至 3–5 条 | 一个截断参数 | 约 9 小时（一臂） | 是规则太多还是规则不对 |

### 参数已就位

`--refiner-turns`（默认 25）与 `--refiner-min-probes`（默认 `None`，即沿用
`run_refiner` 自己的 8）已加到 `sql_agent_runner.py` 与 `scripts/run_pipeline.py`，
**不传时行为与本轮逐字节一致**。对齐上游配置：

```bash
python -u scripts/run_pipeline.py \
  --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
  --out-prefix outputs/turns5 --tkstore tkstore/tkstore_sqlite.csv \
  --refiner-turns 5 --refiner-min-probes 3 \
  --model gpt-4.1 --filter-model gpt-4.1
```

只传给两臂、不传给 agent 步（agent 没有 refiner）。**不可达的组合会被当场拒绝**
（见下），两个 CLI 共用 runner 里那一份闸门逻辑，默认值不会漂移。

### 第一个消融有个必须同时改的坑

`cte_refiner.py:391` 在 `min_required_sql` 为 `None` 时取默认值 **8**，而 `:521-526`
会在探库次数不足时**驳回 refiner 自己的 verdict**（回 `Before verdict, do: more
probes (n/8)` 后 `continue`）。一轮一条探针，**5 轮永远凑不满 8 次探库**，
于是 5 轮全耗在催促上 → 撞上限 → 走强制出 verdict 的兜底 → 万一仍失败，
`:696` 返回 `{"status": "issues", "issues": ["no_verdict"], …}`。
该 `status` 在我们的 runner 里**会触发改写**，等于让 refiner 在毫无真实诊断的情况下
系统性要求重写每个片段 —— 比 25 轮和上游 5 轮都差，且会以"结果"的形式呈现。

上游**同时传了 `min_probes=3`**（`tkboost/__init__.py:540`，在 `:642`/`:688`/`:720`
传成 `min_required_sql`），而**我们这条路一次都没传**，吃的是默认 8。
所以对齐上游必须两个参数一起改。

**已加闸门**：`_refiner_options` 在 `min_probes >= turns` 时抛 `ValueError`，
两个 CLI 都在跑任何东西**之前**校验。这一步不是可选的洁癖 —— 实测过它的后果：
`--refiner-turns 5` 单独传时，`ValueError` 会在逐实例的 `except Exception` 里被吞掉，
86 个实例**全部记为失败而进程退出 0**，等于一次静默的全量报废。

也不能只交给子进程去校验：`run_pipeline.py` 会先跑完 agent 步（约 2 小时）才轮到两臂，
那时才报错太晚，所以 `scripts/run_pipeline.py` 复用 runner 那份闸门在启动时校验。

---

## 9. 运行成本实测

| 项 | 实测 |
| --- | --- |
| agent 阶段 | 约 **1.4 分钟/实例**，86 个约 2 小时（09-15 10:28 → 12:27） |
| 精修单实例 | 中位数 **6.1 分钟**，平均 6.2，最短 1.6，最长 13.1（`local275`） |
| 单臂 86 个 | 约 9 小时 |
| 每片段 LLM 往返 | refiner 中位数 22 轮 + 约 2 次 `FilterKnowledge` |
| 单实例总 LLM 往返 | 40（0 个 CTE）～158（5 个 CTE，实测 `local074`） |

两处都不要沿用 3.3 冒烟的外推值，方向还相反：

- **agent 阶段**冒烟估的是 2.4 分钟/实例，实际 1.4。冒烟那次 agent 与精修在同一趟里跑完，
  我是用"最早一个 refiner trace 的 mtime"切分两段的，而那个 trace 是在该片段**精修完成后**
  才写盘的，所以切分点偏后、agent 段被高估。
- **精修阶段**冒烟估的是 4.4 分钟/实例，实际中位数 6.1。本批实例（`oracle_sql`、`f1`）
  的 CTE 更多更重，而冒烟那 5 个不具代表性。

---

## 10. 待办

- [ ] 把 §7 第 4 条的硬编码领域知识补进 `deviations.md`
- [ ] 决定并执行 §8 的消融（建议从"对齐上游探库配置"开始）
- [ ] 主轨道：用我们自己 populate 的 store 重跑（需先跑规模 populate）
