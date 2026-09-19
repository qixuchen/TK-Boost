# 参考轨道结果：上游 store × Spider2-SQLite 86 实例

记录阶段 4 的完整实验。**参考轨道**指用 repo 自带的上游 store
（`tkstore/tkstore_sqlite.csv`）而非我们自己 populate 的 store；主轨道尚未跑。

已跑五轮，全部共用**同一份**裸 agent 产出（agent 只运行过一次），因此跨轮严格可比：

| 轮次 | 探库 | store | 修复由谁应用 | 裸 agent | 臂 A（+refiner） | 臂 B（+知识） | refiner 净 | 知识净 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **A. 参考轮** | 25 | 上游 118 条 | agent 重写 | 36（41.9%） | **42（48.8%）** | 36 | **+6** | **−6** |
| **B. 对齐上游探库** | 5 | 上游 118 条 | agent 重写 | 36 | 37（43.0%） | **32（37.2%）** | +1 | **−5** |
| **C. 只用 db 规则** | 5 | 上游、仅 db | agent 重写 | 36 | 37 | 36 | +1 | −1\* |
| **D. 我们自己的 store** | 5 | **我们 24 条** | agent 重写 | 36 | 37 | 34 | +1 | **−3** |
| **E. 直接采纳 refiner 的 SQL** | 5 | 上游 118 条 | **refiner 的 SQL** | 36 | **34（39.5%）** | **37（43.0%）** | **−2** | **+3** |

\* C 那轮的 −1 是假象：54/86 个实例**一条规则都收不到**，差值全部来自那一组的运行间随机性；
真正收到规则的 32 个实例是 ±0。详见 §11。

**核心结论：知识的正收益只在"直接采纳 refiner 的 SQL"时出现，而那是上游代码的做法，
不是论文的做法。** A–D 轮走的是论文 Alg 5 的路径（反馈交给 agent 重写），知识一律净有害；
E 轮换成上游的直接采纳，知识首次转正（+3），同时 refiner 自身首次转负（−2）。
详见 §13。

其余四条已验证的结论：

1. **refiner 的增益全部来自深度探库**（+6 → +1，预算砍到 1/5 就没了）。
2. "我们的 refiner 太强、规则因冗余而沦为噪声"**被证伪** —— 削弱 5 倍后知识照旧有害（§10）。
3. **只留 db 规则没有消除伤害**，它只是让 63% 的实例不再接受任何知识（§11）。
4. **"注入量太大"不是主因** —— D 轮注入量恰好落进论文区间（每片段中位数 3、平均 3.62，
   论文报 2–8、平均 4.2），知识依然 −3（§12）。

**噪声底线已量出**：§11 用 54 个零注入实例做了天然重复实验 —— SQL 层面 63% 的实例会写出
不同结果，但**分数层面只有 2%（1/54）发生变化**。所以 −6 / −5 / −3 / +3 都在噪声之外，
而 E 轮的 `delta_paper`（+1）在噪声内，轮次之间 1–2 个实例的差异也不能据此排序。

**A–D 轮的共同点**：实际注入 prompt 的规则 **93–100% 是 generic**，被知识改坏的实例
`rules_used` 几乎全是纯 generic。

§1–§9 参考轮，§10 对齐上游探库，§11 db-only 与噪声测量，§12 我们自己的 store，
**§13 直接采纳 refiner 的 SQL**。

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

> **这条的后半段已被 §10 证伪。** 探库强度确实解释了 refiner 自身的增益
> （+6 → +1），但**不解释知识为何有害** —— 把预算砍到 5 轮后知识依旧是 −5。
> 下面关于"规则与探库冗余"的推论请当作已排除的假设读。

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

| 消融 | 改什么 | 估计成本 | 回答什么 | 状态 |
| --- | --- | --- | --- | --- |
| 对齐上游探库配置 | `max_turns` 25→5 **且** `min_required_sql` 8→3 | 约 5 小时（两臂） | 是不是我们的 refiner 太强，把知识增益吃掉了 | **已跑，见 §10：不是** |
| 只注入 db 作用域规则 | `--rule-scope db` | 约 2 小时（一臂） | 是不是 95% 的 generic 规则在淹没信号 | **已跑，见 §11：没消除伤害，且样本塌掉 63%** |
| 直接采纳 `suggested_fix_sql` | 一段采纳逻辑 | 约 7 小时（一臂，省掉主 agent 重写） | 是不是"剪断的链"损失了知识 | **已跑，见 §13：是，知识首次转正 +3** |
| 压低每片段注入量至论文的 **4.2 条** | 一个截断参数 | 约 3 小时（一臂） | 是规则太多还是规则不对 | **已由 §12 无意做掉：不是量的问题** |

**注入量那一档现在有论文依据。** 论文原文报的是每个 CTE 检索 **2–8 条、平均 4.2 条**
（"scoped retrieval … typically retrieving 2–8 statements … average of 4.2"），
而且论文自己做过这个对照（Table 12）：SQLite 上 27.7 →（灌入全部知识）35.6 →
（scoped retrieval）**44.6**，并结论"scoped retrieval consistently outperforms applying
all knowledge"。我们现在是每片段中位数 **12** 条，落在论文两档之间偏"全部知识"一侧；
db-only 又掉到 0 条。**4.2 是唯一有外部依据的目标值。**

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

## 10. 消融 B：对齐上游探库预算（5 轮 / 3 探针）

### 设置

与参考轮的唯一差别是 `--refiner-turns 5 --refiner-min-probes 3`。
**裸 agent 完全没有重跑** —— `outputs/turns5_agent` 是 `outputs/test_agent` 的副本，
`verify_shared_agent_output` 确认两者起始 SQL 逐字节一致，所以两轮的 `score_bare`
是同一批 36 个实例，三个 setting 跨轮严格可比。

```bash
cp -r outputs/test_agent outputs/turns5_agent          # 让 agent 步整体跳过
python -u scripts/run_pipeline.py \
  --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
  --out-prefix outputs/turns5 --tkstore tkstore/tkstore_sqlite.csv \
  --refiner-turns 5 --refiner-min-probes 3 \
  --model gpt-4.1 --filter-model gpt-4.1
```

完整性同样核过：两臂各 86/86 带实例级 marker、各 86 个 `execution_result_final.csv`、
两臂都有目录级完成凭据（它只在零失败时写，故本身即 `Failed: 0/86` 的独立确认）、
臂 A 零个 `retrieved_rules.json` 而臂 B 八十六个、两臂都通过起始 SQL 配对校验。

### 结果

| 分组 | n | 裸 agent | 臂 A | 臂 B | B−A | 知识修好 | 知识改坏 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **总体** | 86 | 36（42%） | 37（43%） | **32（37%）** | **−5** | 2 | 7 |
| 无 db 规则 | 34 | 13（38%） | 14（41%） | 11（32%） | −3 | 0 | 3 |
| 有 db 规则 | 52 | 23（44%） | 23（44%） | 21（40%） | −2 | 2 | 4 |

`delta_paper` = **−4**：在对齐上游的配置下，带知识的那一臂**比裸 agent 还差**。
这是与 README 报的 +3.6% ～ +16.9% 最直接的冲突。

### 这一轮排除了什么

原 §7 第 3 条推论是"规则断言的性质（`data_type`、`nulls`）refiner 已靠
`PRAGMA table_info` / `DISTINCT 与 NULL 计数` 实测过，所以规则是冗余噪声"。
**若成立，把 refiner 削弱 5 倍应让规则变得更有用。** 实测没有：

| | refiner 净 | 知识净 |
| --- | --- | --- |
| 25 轮 | +6 | −6 |
| 5 轮 | **+1** | **−5** |

探库强度只解释 refiner **自身**的增益（+6 → +1，干净地证实收益来自深度探库），
**不解释知识为何有害**。冗余因此不是主因。

### 机制变了，但危害没变

| | 判 `issues` 的比例 | `issues` → 采纳率 | 探库中位数 |
| --- | --- | --- | --- |
| 25 轮 | 臂 A 36% → 臂 B **41%** | 95% → 96% | 22 轮 |
| 5 轮 | 臂 A 25% → 臂 B **25%** | 79% → **90%** | 3 轮 |

参考轮归因的"知识让 refiner 更爱挑问题"（36%→41%）**在 5 轮下完全消失**（25%→25%），
说明那是探库强度的产物，不是知识的固有效应。可知识照样有害，只是换了路径：
**被标记的片段数一样多，但知识让 refiner 更倾向于把改写落地**（采纳率 79%→90%），
而落地的那些改写质量更差。

7 个改坏的实例（`local049`、`local070`、`local152`、`local167`、`local196`、
`local270`、`local311`）机制依旧统一：**全部是"知识导致了不同的改写、结果更差"，
零个"漏掉修复"**，与参考轮的 10/10 一致。

`no_verdict` 兜底片段在四个目录里都是 **0**，即 5 轮 + 3 探针没有触发 §8 那个
"无诊断就改写"的坑；360 / 351 个片段的探库中位数是 3 轮，远未顶到 5 轮上限。

### 仍然活着的假设

排掉三号后剩两个，一号现在是首选：

- **一号（首选）：注入内容 95% 是 generic 规则**（§6）。本轮分组继续支持它 ——
  无 db 规则组 0 好 3 坏（净 −3/34），有 db 规则组 2 好 4 坏（净 −2/52），
  伤害仍集中在只有 generic 可用的一侧。
- **二号：我们丢掉了 refiner 的 `suggested_fix_sql`**（§7 第 2 条），改由一个
  没见过规则的 agent 重写。本轮的采纳率现象（79%→90%）与它有些呼应，但不能定论。

---

## 11. 消融 C：只注入 db 作用域规则（5 轮 / 3 探针）

### 设置

在消融 B 的配置上加 `--rule-scope db`，即把 52 条 generic 规则整体挡在闸门外。
**agent 与臂 A 都没有重跑** —— 臂 A 不含知识、与 `--rule-scope` 无关，直接复用
`outputs/turns5_refonly`（86/86，37 正确），所以只跑了新的知识臂：

```bash
python -u -m src.agents.sql_agent_runner \
  --refine-output outputs/turns5_agent --refine-output-dir outputs/dbonly5_tk \
  --tkstore tkstore/tkstore_sqlite.csv --rule-scope db \
  --refiner-turns 5 --refiner-min-probes 3 \
  --model gpt-4.1 --filter-model gpt-4.1

python evaluation/evaluate.py --mode exec_result --result_dir outputs/dbonly5_tk --gold_dir evaluation/gold
python scripts/compare_arms.py --arm-a outputs/turns5_refonly --arm-b outputs/dbonly5_tk \
  --tkstore tkstore/tkstore_sqlite.csv --out outputs/dbonly5_comparison.csv
```

核验：86/86 精修完成、`Failed: 0/86`、目录级凭据已写、86 个 `retrieved_rules.json` 的
`rule_scope` 字段全部为 `"db"`（两个 tk 目录内容形态相同，这是唯一的区分凭据），
**注入构成 generic 0 条 / db 202 条**，确认闸门生效。

### 结果：看起来伤害从 −5 降到 −1，但那是假象

| 分组 | n | 裸 agent | 臂 A | 臂 B | B−A | 修好 | 改坏 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 总体 | 86 | 36（42%） | 37（43%） | 36（42%） | **−1** | 1 | 2 |
| 无 db 规则 | 34 | 13 | 14 | 14 | **+0** | 0 | 0 |
| 有 db 规则 | 52 | 23 | 23 | 22 | −1 | 1 | 2 |

**注入量塌了**：每片段中位数 **0** 条、平均 0.57、最大 6（消融 B 是中位数 12）。
**54/86 个实例一条规则都没收到**，它们的臂 B 在输入上与臂 A 完全相同。
所以正确的拆分是按"是否真收到规则"，而不是按库是否有 db 规则：

| | n | 裸 agent | 臂 A | 臂 B | B−A |
| --- | --- | --- | --- | --- | --- |
| **真收到规则** | **32** | 14 | 14 | 14 | **+0**（1 好 1 坏） |
| 零注入（等于臂 A） | 54 | 22 | 23 | 22 | **−1**（0 好 1 坏） |

**总体那个 −1 完全来自零注入的那 54 个实例** —— 那一组根本没有知识参与，
所以它量的不是知识，是运行间随机性。而真正收到规则的 32 个实例是 **±0**。

结论：**db-only 没有消除伤害，它只是让 63% 的实例不再接受任何知识。**
"−5 → −1"这个改善几乎全部由样本塌缩贡献。

### 在同一批 32 个有效实例上跨轮对比

把三轮都限制到这 32 个实例（即 db-only 下真能收到规则的那批）：

| 轮次 | n | 裸 agent | 臂 A | 臂 B | 知识净 |
| --- | --- | --- | --- | --- | --- |
| A. 25 轮 all-scope | 32 | 14 | 15 | 17 | **+2** |
| B. 5 轮 all-scope | 32 | 14 | 14 | 15 | **+1** |
| C. 5 轮 db-only | 32 | 14 | 14 | 14 | **+0** |

**方向与假设相反**：在这批实例上，带 generic 的两轮知识净值是正的（+2 / +1），
去掉 generic 后变成 0。也就是说 generic 规则在这个子集上是有贡献的，
拿掉它反而损失了那点贡献。不过 32 个样本上 ±2 的差异在噪声范围内（见下），
所以只能说"没有观察到 db-only 更好"，不能说"generic 在这里有益"。

### 副产品：终于量到了噪声底线

那 54 个零注入实例是一次天然的重复实验 —— 两臂输入完全相同、只差一次独立的 LLM 运行：

| 度量 | 值 |
| --- | --- |
| 最终 SQL 与臂 A **逐字节不同**的实例 | **34/54（63%）** |
| 其中**分数发生变化**的实例 | **1/54（2%）** |

所以 SQL 层面的随机性很大（六成的实例会写出不同的 SQL），但**分数层面相当稳健**，
噪声底线约 **±1 个实例 / 54**。两个推论：

- 参考轮的 **−6** 与消融 B 的 **−5** 远在噪声之外，是真实效应。
- 本轮的 −1、以及 32 个实例上的 +2 / +1 / +0，**都在噪声内**，不能据此排序。

这条也补上了此前一直缺的东西：之前所有单实例级的归因（谁修好谁改坏）都没有噪声参照，
现在知道分数级噪声约 2%，而 14 个"改坏"实例的规模（16%）显著高于它。

---

## 12. 主轨道 D：用我们自己 populate 的 store（5 轮 / 3 探针）

这一轮离开参考轨道 —— store 是我们自己从 train 集挖出来的，不是 repo 自带的上游 store。

### populate 的产出与它的天花板

**能提供 correction 信号的样本只有 6 个。** Alg 3 需要 gold SQL 做 diff，而 Spider2 只为
135 个 local 实例中的 **24 个**发布了 gold SQL（train 就是这 24 个，test 那 111 个一个都没有，
这也是 split 的切法）。这 24 个里：

| 正误闸门判定 | 数量 | 说明 |
| --- | --- | --- |
| 做错 → 进 populate | **6** | `local003` `local019` `local029` `local075` `local099` `local309` |
| 做对 → skip | 17 | agent 在 train 上做对了 71%，远高于 test 的 42% |
| 无法判定 → 记 error | 1 | `local301`，见下 |

`local301` 是 `agent_result_matches_gold` 的一处异常泄漏：它只捕获 `read_csv` 的
`OSError/ValueError`，比较阶段抛的 `IndexError: positional indexers are out-of-bounds`
直接穿透。成因是该实例有 4 份 gold（5/2/4/4 列），预测 4 列，`compare_multi_pandas_table`
拿 2 列那份按位取列时越界。`populate_split` 每实例包了 `except Exception`，所以不中断整轮，
只是这个实例静默丢失。**未修。**

产出 **24 条规则**（`artifacts/tkstore_sqlite_ours.csv`，sha1 `abb288db…`），
scope 比例 **12 generic / 12 db**（比上游的 52/66 更均衡），覆盖 6 个库。
核验过四项：每条规则的 `db` 与来源实例在 jsonl 里的库逐条一致、`db` 列无 `all`
（**A7 那个坑避开了** —— 我们的入口显式传 `db_name`，不走 `harness`/`builder` 那两个入口）、
无 C15 的 list-repr 异常、`data_type` 取值都在规定集合内。

规则质量目测可用，db 规则确实落到了具体表列且是"不踩坑不会知道"的那类，例如
`DB_IMDB` 的 `ACTOR.PID` / `CASTS.ACTOR_PID` **存着前后空格、join 必须套 TRIM**。
generic 规则则是同一条 correction 的去语境孪生体（db 版讲 `order_items` 要先按
`order_id` 汇总，generic 版变成"聚合前先确认粒度"）。

### 可达性只有上游的一半

| | test 全量（111） | 免泄漏子集（86） |
| --- | --- | --- |
| generic 规则（对全部实例可用） | 12 条 | 12 条 |
| 有 db 规则可用的实例 | 32/111 = **29%** | 28/86 = **33%** |
| 上游同口径 | 77/111 = 69% | 52/86 = 60% |

`local019` 那 2 条 `WWE` 规则是**死规则** —— test 里没有那个库的实例。
实际可用的是 10 条 db 规则覆盖 32 个实例。

### 设置与结果

跑在免泄漏子集（86）上以便与前三轮严格可比；**agent 与臂 A 都复用**
（`turns5_agent` / `turns5_refonly`），只跑了新的知识臂：

```bash
python -u -m src.agents.sql_agent_runner \
  --refine-output outputs/turns5_agent --refine-output-dir outputs/ours5_tk \
  --tkstore artifacts/tkstore_sqlite_ours.csv \
  --refiner-turns 5 --refiner-min-probes 3 \
  --model gpt-4.1 --filter-model gpt-4.1

python scripts/compare_arms.py --arm-a outputs/turns5_refonly --arm-b outputs/ours5_tk \
  --tkstore artifacts/tkstore_sqlite_ours.csv --out outputs/ours5_comparison.csv
```

（`compare_arms` 的 `--tkstore` 必须指向我们的 store，否则 `n_db_rules` 会按上游算、分组就错。）

核验：86/86 精修完成、日志零失败、目录凭据已写、86 个 `retrieved_rules.json` 的
`tkstore.sha1` 全部指向 `abb288db…`（两个 tk 目录形态相同，这是唯一凭据）。

| 分组 | n | 裸 agent | 臂 A | 臂 B | B−A | 修好 | 改坏 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 总体 | 86 | 36（42%） | 37（43%） | 34（40%） | **−3** | 2 | 5 |
| 无 db 规则 | 58 | 26 | 25 | 23 | −2 | 1 | 3 |
| 有 db 规则 | 28 | 10 | 12 | 11 | −1 | 1 | 2 |

### 这一轮顺带否掉了"注入量太大"那个假设

**注入量恰好落进论文区间**，这是意外但关键的对照：

| | 每片段注入量 |
| --- | --- |
| 论文 scoped retrieval | 2–8 条，**平均 4.2** |
| **本轮（我们的 store）** | 中位数 **3**，平均 **3.62**，最大 12 |
| 上游 store 那两轮 | 中位数 **12**（整条 SQL 24 条） |

也就是说 §8 待办里"压低每片段注入量至论文的 4.2 条"这个消融**已经被这一轮无意中做掉了**：
在论文的注入量下，知识依然是 **−3**。所以"我们注入太多"不是主因。

而且**零注入实例只有 6/86**，不存在消融 C 那种样本塌缩 —— 有效样本 80 个，
这个 −3 是实打实的。

### 但构成比上游更偏 generic

| | 注入构成 |
| --- | --- |
| 我们的 store（按条数 12/12 均衡） | generic **97%** / db **3%** |
| 上游 store（按条数 52/66） | generic 95% / db 5% |

尽管我们的 store 在**条数**上是 1:1，注入时却比上游**更**偏 generic。算术很简单：
12 条 generic 对全部 86 个实例可用，而 10 条可达的 db 规则只对 28 个实例、每实例 1–3 条。

7 个被知识改变结果的实例里，**只有 1 个（`local355`）的 `rules_used` 沾到 db 规则**
（8 generic + 1 db），其余全是纯 generic 或空。与前三轮同源。

### 与前三轮并排

| 轮次 | 探库 | store | 裸 agent | 臂 A | 臂 B | 知识净 |
| --- | --- | --- | --- | --- | --- | --- |
| A 参考轮 | 25 | 上游 118 条 | 36 | **42** | 36 | **−6** |
| B 对齐上游 | 5 | 上游 118 条 | 36 | 37 | 32 | **−5** |
| C 只用 db 规则 | 5 | 上游、仅 db | 36 | 37 | 36 | −1\* |
| **D 我们的 store** | 5 | **我们 24 条** | 36 | 37 | 34 | **−3** |

\* C 那轮的 −1 是样本塌缩造成的假象，见 §11。

**−3 与 −5 的差距（2 个实例）在噪声内**（§11 测得分数级噪声约 ±1/54），
所以不能说我们的 store 比上游的好；能说的是**换成我们自己挖的规则、把注入量降到论文水平，
知识依然净有害**。

### 由此得到一个关于数据集的结论

Spider2-SQLite 给 TK-Boost 的学习信号先天不足：135 个实例只有 24 个有 gold SQL，
其中只有 6 个能提供 correction，学出的 db 专属知识只覆盖 29% 的 test 实例。
论文在 Spider2 上报的 store 是 80 条，我们复现不出那个量级 —— 瓶颈在数据集，不在实现。

根因是**每库题量太少**：Spider2-SQLite 平均 **4.5 题/库**，db 专属规则几乎没有复用机会。
`deviations.md` 末尾记过 BIRD 是 **45 题/库**，差一个数量级，是检验 db-scoped 知识的更合适设置。

---

## 13. 消融 E：直接采纳 refiner 的 SQL（复现上游，**首次出现正增益**）

这一轮复现的是**上游代码**，不是论文 —— 两者在这一点上直接冲突，见下。

### 为什么做这一轮

refiner 的 verdict 里一直有 `suggested_fix_sql`（完整的修正片段），落盘在
`refiner_<name>.json` 里，而 `sql_agent_runner.py` 里这个字段名出现 **0 次**。
默认路径只把散文式的 `suggested_fix` 转给主 agent，**而那个 agent 从未见过任何规则**。

实测这条链损耗极大 —— 主 agent 的改写与 refiner 建议的相似度：

| | 可比片段 | 相似度中位数 | <0.5 的占比 |
| --- | --- | --- | --- |
| A 轮（25 轮探库） | 126 | **0.47** | 55% |
| B 轮（5 轮） | 73 | **0.23** | 78% |
| D 轮（我们的 store） | 58 | 0.28 | 83% |

也就是说**主 agent 基本在自己另写一版**，refiner 那份带着规则信息的成品几乎没传下去。

### 三方在这一点上的分歧

| | 修复怎么落地 |
| --- | --- |
| **论文 Alg 5** | `Feedback` 返回自然语言 `f` → 第 13 行 `concat(C_t, s_t, R_t, f)` 拼进 agent 上下文 → **agent 重写**（正文与 Fig. 1 均为 "Agent corrects SQL"） |
| **上游 `tkboost.sql()`** | 取 `suggested_fix_sql` → `fixed_ctes[0]` 取第一个 CTE 的 body → **直接替换、字符串重组**，主 agent 完全不参与（`tkboost/__init__.py:648-654`） |
| **我们默认路径（A–D 轮）** | 同论文 ✅ |
| **本轮 `--adopt-refiner-sql`** | 同上游 |

上游那段代码经 git 确认是上游自己写的（我们唯一碰过 `tkboost/__init__.py` 的提交只改了
文档与 `.gitignore`）。**README 报的 +3.6% ～ +16.9% 出自上游那条链。**

### 保留的两处有意改进

**采纳前执行校验。** 上游不验证就替换，一个跑不通的 body 会污染后面每个片段。
**这一项的影响面非常大** —— 本轮实测：

| | 判 `issues` | 带建议 SQL | 实际采纳 | **被校验拦下** |
| --- | --- | --- | --- | --- |
| 臂 A | 95 | 94 | 45 | **49（52%）** |
| 臂 B | 118 | 117 | 58 | **59（50%）** |

**refiner 的建议有一半跑不通**（语法错或引用不存在的列，如 `local018` 的
`no such column: speeding_incidents`）。上游会把这 108 条全部吞进 SQL，
所以**本轮给出的是上游那条链的上界，不是它的真实表现**。

**采纳失败不回退给主 agent。** 混用两种采纳方式会产生既不像上游也不像论文的第三种语义。

### 设置与核验

```bash
cp -r outputs/test_agent outputs/adopt5_agent     # 复用同一份裸 agent 产出
python -u scripts/run_pipeline.py \
  --split data/splits/spider2_sqlite_test_no_reference_leak.txt \
  --out-prefix outputs/adopt5 --tkstore tkstore/tkstore_sqlite.csv \
  --adopt-refiner-sql --refiner-turns 5 --refiner-min-probes 3 \
  --model gpt-4.1 --filter-model gpt-4.1
```

**两臂都要重跑**：`--adopt-refiner-sql` 必须两臂同开（否则配对量的是"采纳方式"而非知识），
所以臂 A 不能复用 `turns5_refonly`。核验：两臂各 86/86、日志零失败、目录凭据已写、
86 个 `retrieved_rules.json` 的 `adopt_refiner_sql` 全为 `true` 且 `tkstore.sha1` 为
`3f75f169…`（两个 tk 目录形态相同，这是唯一凭据）。

### 结果

| 分组 | n | 裸 agent | 臂 A | 臂 B | B−A | 修好 | 改坏 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **总体** | 86 | 36（42%） | **34（40%）** | **37（43%）** | **+3** | 4 | 1 |
| 无 db 规则 | 34 | 13 | 12 | 13 | +1 | 2 | 1 |
| 有 db 规则 | 52 | 23 | 22 | 24 | +2 | 2 | 0 |

**两件事同时发生，必须一起读：**

1. **知识首次转正：+3**（4 修好、1 改坏）。这是五轮里唯一的正值。
2. **refiner 自身首次转负：−2**（裸 36 → 臂 A 34）。直接采纳时，**没有知识的 refiner
   写出的 SQL 比主 agent 自己的改写更差**。

连起来的解释是自洽的：采纳路径下 refiner 的 SQL 质量**直接决定结果**。无知识时它的建议
不如主 agent 的重写；注入知识后它的建议变好，于是相对臂 A 净赚 +3。
知识的作用是**让 refiner 写出更好的 SQL**，而这个好处只有在真正采用那份 SQL 时才看得见 ——
前四轮之所以看不到，是因为主 agent 把它丢掉重写了。

知识让 refiner 更爱挑问题这个信号依旧存在（判 `issues` 95 → 118），采纳数也跟着涨
（45 → 58），但这一轮多出来的改写**好 4 坏 1**，而 A 轮是好 4 坏 10。

### 但不要过度解读

**`delta_paper` 只有 +1**（臂 B 37 − 裸 36），**落在噪声内**（§11 测得分数级噪声约 ±1/54）。
`delta_knowledge` 的 +3（3.5%）略高于噪声，但也只是略高。

**而且这是上界**：上游会吞下被我们拦掉的 108 条跑不通的建议，它的真实表现只会更差。

**最重要的是定位**：这一轮复现的是**上游实现**。论文 Alg 5 明确让 agent 依据反馈自行修正，
我们的默认路径（A–D 轮）才是忠于论文的那个。所以能得出的结论是：

> README 报告的增益，很可能依赖一个**与论文算法不一致**的实现细节 ——
> 直接采纳 refiner 的 SQL。按论文的方式（反馈交给 agent 重写），
> 同一个 store 在同样配置下给出的是 **−5**。

这比"知识有害"更强，但表述必须精确：**不是论文错了，是上游代码与论文不一致，
而可复现的增益出现在代码那一侧。**

### 五轮总表

| 轮次 | 探库 | store | 采纳方式 | 裸 agent | 臂 A | 臂 B | refiner 净 | 知识净 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 25 | 上游 118 条 | agent 重写 | 36 | **42** | 36 | **+6** | **−6** |
| B | 5 | 上游 118 条 | agent 重写 | 36 | 37 | 32 | +1 | **−5** |
| C | 5 | 上游、仅 db | agent 重写 | 36 | 37 | 36 | +1 | −1\* |
| D | 5 | 我们 24 条 | agent 重写 | 36 | 37 | 34 | +1 | **−3** |
| **E** | 5 | 上游 118 条 | **直接采纳** | 36 | **34** | **37** | **−2** | **+3** |

\* C 轮的 −1 是样本塌缩造成的假象，见 §11。

---

## 14. 待办

§7 的四条候选原因现在全部有了结论：第 1 条（generic 淹没）由 C 轮证明不能靠 scope 分流解决，
第 2 条（剪断的链）由 **E 轮证实是关键**，第 3 条（refiner 太强）由 B 轮证伪，
第 4 条的 prompt 污染尚未登记。下一步的重点从"找原因"转为"把 E 轮的结论坐实"。

- [ ] **E 轮只有 86 个实例、+3 刚过噪声线，需要提高置信度**。最便宜的做法是同配置重复一次
      （约 4–5 小时），看 +3 是否稳定；或放到 test 全量 111 个上
- [ ] E 轮的 refiner 建议有 **一半跑不通**（108/211 被执行校验拦下）。
      若能提高这个比例，采纳路径的上限还会更高 —— 值得看看那 108 条错在哪
- [ ] 修 `agent_result_matches_gold` 的异常泄漏（§12 的 `local301`），
      让判不了的实例报错而非静默丢失
- [ ] 把 §7 第 4 条的硬编码领域知识（refiner prompt 里无条件注入
      `California_Traffic_Collision` 的专有名词）补进 `deviations.md`
- [ ] 记一条口径误差：同名 CTE 被重复访问时 `refiner_<name>.json` 只保留最后一次的
      verdict，而 `retrieved_rules.json` 两次检索都记着，使 `rules_used` 略偏大
      （实测受影响 9 / 8 / 4 个实例，不改变定性结论）
- [ ] 可选：转向 BIRD（45 题/库 vs Spider2-SQLite 的 4.5 题/库），
      需补 instance loader、gold 加载、评测函数三块
