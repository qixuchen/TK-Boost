# TK-Boost knowledge population 适配 DataClaw：设计记录

本文记录把 TK-Boost 的 knowledge population（从失败轨迹生成规则、存成 knowledge store）适配到
DataClaw 的具体方案。它细化 [`dataclaw.md`](./dataclaw.md) 的 5.3、5.4 节；DataClaw 的环境、
runner、评测设计、与 `~/baseline` 的关系仍以 `dataclaw.md` 为准。

状态：**阶段 A 已完成**（代码在 `tkstore/dataclaw/`，完成情况见 9.3），阶段 B 未开始。开发集已拷贝
到 `data/dataclaw_dev/`（第 9.2 节）。三份 prompt 的最终文本尚未定稿，文中的 prompt 是草稿，需要逐条
确认（见第 10 节）；写 prompt 时对照第 11 节的数据理解问题清单与样例规则。开发顺序见第 9 节。

---

## 1. 总体思路

沿用 TK-Boost 的两阶段框架：先让一个 agent 找出错误轨迹和正确做法之间的差异（diff），再从 diff
生成规则、存进 knowledge store。重新设计的是**输入**（轨迹、gold 反馈）和**两阶段的 prompt**，
因为 DataClaw 没有 SQL，也没有 gold SQL。

```text
固定 train split 上的裸 OpenClaw 轨迹
  → 正误闸门：只取 outcome 判错的 run
  → 第一阶段：反思 agent 探库，找出有数据证据、且验证过的 divergence
  → 第二阶段：从 divergence 生成列级规则，harness 自动检查
  → 合并去重：按涉及的 (file, column) 分组，每组一次 LLM 调用
  → JSONL knowledge store
```

---

## 2. TK-Boost 原设计里对应的部分

读 `tkstore/harness.py` 得出，作为对照：

**diff 阶段不是单次 LLM 调用。** `generate_memory_diff_first_turn` 的 docstring 写的是单次调用，
但代码是一个最多 6 轮（`max_turns=6`）的「MINIMAL-REPAIR agent」：每轮先写 `EDIT_PLAN`，再给出
`<sql>`；harness 执行这条 SQL，把结果和 gold 执行结果比对；只有结果完全一致（数值容差 0.01）才能
声明 `MATCH_OK` 并给出 `MINIMAL_FIX`。结果仍有差异时声明 `NO_DIFF` 会被 harness 驳回。轮数用完时，
要求输出 `OBSERVED_DIFFS`、`MINIMAL_REQUIRED_EDITS`、`CLEAN_SUMMARY`。

所以 TK-Boost diff 阶段的质量锚点是：**diff 必须由执行结果证明，改到与 gold 一致才算数。**
DataClaw 版本保留这个精神：把「执行 SQL」换成「探查数据」，把「与 gold 结果一致」换成「复现 gold 的
milestone 值」。

**规则阶段。** `generate_rules_from_diff` 把 diff 分成 `DATABASE_MEMORIES`（引用真实表名、列名）和
`GENERIC_MEMORIES`（抽掉名字的通用规律），每条是四段式
`ENSURE/VERIFY/REMEMBER | CONTEXT | WHEN_TO_CHECK | EXAMPLE_USAGE`，prompt 里有反例禁止引用
单个查询里的 CTE 名。之后由 tagger 打 `table`、`column` 等标签，写成 10 列 CSV。

---

## 3. 决定设计的数据事实

以下来自对 `~/DataClaw/assets/database/` 与 gold 的脚本统计。

**关键表是长表。** `enterprise/company_operation_status.csv`（353,438 行）的列是
`id, year, bmCode, secondTargetNum, targetName, targetUnit, value, …`，一个指标一行；
`national_industry_status.csv`、`regional_industry_status.csv`、`policy_release_status.csv`
也按 `targetName` / `value` 组织。同一文件内单位随指标变化，例如
`targetName=总资产金额` 的 `targetUnit` 是元，`targetName=净利润额` 的是十万元。

**`targetName` 取值很多。** `company_operation_status.csv` 有 1,012 个不同的 `targetName`，两张行业
表分别有 1,426 和 4,482 个；`targetUnit` 只有 12 个。从单个失败样本出发，最容易写出的恰恰是
「某个指标的单位是什么」这种只对单个值成立的规则。

**多文件关联是常态。** gold `steps` 里点名的文件数：1 个文件 119 题、2 个 236 题、3 个 111 题、
4 个 26 题。跨表关联（`bmCode` 连接经营表与公司档案、拼音公司名经中英对照 JSON 转中文、四个
分洲 `company_profile_*` 文件合并）会大量出现。

**按统计特征无法可靠区分「个体值」和「标签值」。** 用「平均每个取值出现几行」判断时，
`company_operation_status.csv` 的 `bmCode` 平均 50.5 行（长表里一家公司几十个指标），会被误判成
标签；同表 `value` 平均 3.8 行，也被误判；两张行业明细表的 `targetName` 每行唯一（1.0 行），又会被
误判成个体。所以不采用按列分类的方案（见 6.3）。

**TK-Boost 的 store 实际不产出跨表规则。** builder 支持用分号拼接多个表名，但 tagger prompt 要求
填单个 exact table name；此前生成的 `artifacts/tkstore_bird_minidev.csv` 里 72 条 db 规则没有一条
涉及多张表。

**OpenClaw 用 shell 命令处理数据，不用 pandas。** 统计 95 份裸 glm-5.2 轨迹
（`~/archive/DA_Workflow/output/*/glm-5.2_2*/chat.jsonl`）：工具调用中 `exec`（在容器里执行 shell
命令）2,434 次，`read` 11 次，`web_search` 10 次。命令里出现最多的是 `head`（1,392）、`grep`（764）、
`python3`（340）、`sort`（268）、`awk`（168）。**0 份轨迹使用 pandas，0 份执行 `pip install`**；使用
`python3` 时是标准库 `import csv`（46 份轨迹）。这与 Dockerfile 一致：镜像只装了 `python3`，未装 pandas。

---

## 4. 输入与正误闸门

1. **固定 train / test split。** 在 492 个 task 上按 `category × level` 分层随机切分，先于**正式**
   populate 固定（见 `dataclaw.md` 5.2）。开发阶段不需要 split，用历史轨迹组成的开发集（见第 9 节）。
2. **只用裸 OpenClaw 轨迹。** 不用 `with_skill` run 的失败，它们带着旧 skill 的影响。正式 populate
   在 train 上每个 task 跑一次裸 agent；开发阶段用历史裸 glm-5.2 run 中判错的 33 个（第 9.2 节）。
3. **正误闸门。** 以 run 的 `score.json` 里 outcome 判定为准，只有判错的 run 进入反思阶段。
4. **轨迹适配。** 从 `chat.jsonl` 抽出每步的工具调用（代码或命令）、截断后的工具输出、最终答案。
   可复用 `~/baseline/adapters/skillopt/dataclaw_bridge/common.py` 的
   `chat_jsonl_to_conversation`、`select_run_dir`、`parse_task_markdown`、`load_gold`。

---

## 5. 第一阶段：反思 agent

### 5.1 已定决策

| 项 | 决定 |
| --- | --- |
| 形态 | ReAct agent，不是单次 LLM 调用 |
| 探查环境 | 与被测 OpenClaw **同一个 `dataclaw:0.1.0` 镜像**里的 shell 执行工具；可用工具与 agent 相同（`head`、`grep`、`awk`、`sort`、`python3` 标准库等），不装 pandas；数据以只读方式挂载 |
| 重放 | 反思 agent 可原样重放被测 agent 轨迹里的命令，观察失败原因 |
| 验证终止条件 | 必须从数据复现该 divergence 影响到的 milestone 值；只有验证通过的 divergence 进入第二阶段 |
| 非数据类错误 | 跳过，不产规则（算术失误、输出格式、提前放弃、judge 与 gold 分歧等） |

**为什么不用宿主机 pandas（修订早先的决定）。** 最初选 pandas 是为了方便处理长文本和中文。但第 3 节
的统计表明 OpenClaw 从不使用 pandas，而是用 `head`、`grep`、`awk` 和标准库 `csv`。工具不一致对两件事
的影响不同：

- **对验证影响不大。** 复现 milestone 是为了证明数据事实正确且足以推出正确结果；事实不依赖工具，
  pandas 与 `awk` 应得到同一数值。
- **对诊断有实际风险。** 有一类错误是数据特性与 agent 所用工具相互作用造成的。例如
  `policy_resource.csv` 的正文字段带引号、内含逗号甚至换行，agent 用 `awk -F,` 或 `grep` 按行处理
  就会切错字段；`head` 截断也会让它漏看数据。pandas 会正确解析这个文件，反思 agent 自己碰不到这种
  失败，也就诊断不出「该文件不能按行、按逗号切」这类有价值的文件级知识。

用同一镜像、同一套工具还有两个好处：反思 agent 用这套工具复现了 milestone，就说明规则建议的检查
agent 也做得到；基础设施有先例，`regrade_process.py` 就是为 process judge 起一个短命容器。

### 5.2 输入

全部来自 train 侧，允许看 gold：

- 题目与输出要求（task prompt）；
- 压缩后的 agent 轨迹：工具调用、截断输出、最终答案；
- judge 的 `score.json` `notes`；`process_score.json` 的 `break_point`、`chain_summary`、逐 milestone
  的 `achieved` 与 `reason`；
- gold 的 `answer`、`milestone`、`steps`（对应 TK-Boost 提供的 gold SQL）；
- 19 个文件的清单与表头。

### 5.3 协议（prompt 草稿，待定稿）

```text
You are a REFLECTOR for a data-analysis agent that answered a question wrongly.
You have the agent's trajectory, the reference steps and milestone values, and a
shell in the SAME environment the agent used (same tools, read-only ./database/).
Your job is NOT to restate the reference steps, but to find which DATA
understanding the agent lacked.

Each turn: PLAN (which divergence you are testing and what data fact would prove it)
then one <probe>...</probe> containing a shell command. The harness returns
PROBE_RESULT. You may replay any command from the agent's trajectory to see
exactly why it went wrong.

For each divergence you claim, you MUST cite a probe whose output shows the data
fact, e.g. "company names are Chinese; the pinyin name matches 0 rows, the
translated name matches 1 row". A divergence without a probe citation is rejected.

Verify by reproducing, from the data, the milestone value(s) the divergence
affected, and report which probe produced each value. The harness, not you,
decides whether a divergence is VERIFIED.
```

定稿时要补充的内容见第 11 节：要让反思 agent 注意的数据理解问题清单（写成提示清单并留「其他」，
不是封闭分类）、每类可用的 probe 提示（包括分组计数类 probe）、应标为 `non_data` 的失败例子。

### 5.4 输出（每条 divergence 一个结构化块）

```text
DIVERGENCE: <agent 在哪一步做了什么>
NEEDED: <正确做法>
MISSING_DATA_UNDERSTANDING:
  SCOPE: column | multi_column | file | cross_table | generic
  TABLES: <file>[, <file> ...]                      # 事实针对整个文件时列在这里；可为空
  COLUMNS: <file>.<column>[, <file>.<column> ...]   # 事实针对具体列时列在这里；可为空
  FACT: <关于数据的事实>
EVIDENCE: probe#<n> → <关键输出摘录>
REPRODUCED: milestone "<key>" = <值>，由 probe#<m> 算出
KIND: data | non_data | gold_suspect
```

一条 divergence 只写一个数据事实；涉及多个事实时拆成多条。`TABLES` 与 `COLUMNS` 都为空时是
`generic`。`<file>` 一律写 `database/` 下的完整相对路径，例如 `enterprise/company_profile.csv`，
不写简写。`SCOPE` 的含义与判定规则见 6.2 节。

### 5.5 harness 硬闸门

四道闸门都是 harness 用代码做的确定性检查，不靠 LLM 判断，对应 TK-Boost 驳回 `NO_DIFF` 的做法。
任何一道未通过，就在同一会话里把错误原因发回反思 agent 让它继续；轮数用完仍未通过的 divergence
直接丢弃，不部分采纳。

1. **证据必须真实存在。** harness 记录会话中每次 probe 的编号和完整输出。`EVIDENCE: probe#3 → <摘录>`
   必须满足：probe#3 确实执行过；`<摘录>` 在规整空白后是 probe#3 真实输出的子串。防止编造证据，或把
   推测写成观察结果。
2. **引用的文件和列必须真实存在。** `TABLES` 中每个文件都要存在；`COLUMNS` 中每个 `file.column`
   都要在 19 个文件的表头里找到。防止不存在的文件名、列名流进规则。
3. **复现值由 harness 比对。** 先检查 `REPRODUCED` 里的值确实出现在所引 probe 的输出里，再与 gold
   milestone 比较：
   - 数值：沿用 DataClaw `dataclaw/utils/process_grading.py` 的 `_numbers_match`，相对误差 1%
     （`NUMERIC_REL_TOL = 0.01`），与 process 评分口径一致；
   - 列表：规整后按集合比较；
   - 字典：逐 key 比较，例如 `task_054` 的「行业 → 企业数」映射。
   - 字符串：规整空白与大小写后相等即一致；否则交给反思 agent 做**语义判断**。gold 字符串是英文
     （`Guangdong Province`），数据是中文（`广东省`），中英对照 JSON 只覆盖公司名与政策名（开发集
     36 个字符串 milestone 只查到 4 个），逐字比较不可行。反思 prompt 写明：字符串不要求逐字一致，
     但语义须几乎完全一致，并要求写出判断。harness 仍校验所引原值确实出现在所引 probe 的输出里。

   数值、列表中的数值元素、字典中的数值由 harness 判定，agent 的自述不算数；只有字符串部分采纳反思
   agent 的语义判断。
4. **按类型过滤。** `KIND: non_data`（算术失误、输出格式、提前放弃等）与 `KIND: gold_suspect`（gold
   本身可疑，例如 `task_054` 题面要求答 yes/no、gold 却是 `416`）不进第二阶段，只写日志。`KIND` 由
   反思 agent 自标，但标为 `data` 的必须同时通过闸门 1–3，把非数据错误冒充为数据错误过不了关。

另有一项一致性检查：harness 按 `TABLES` 与 `COLUMNS` 推出范围（规则见 6.2），与反思 agent 写的
`SCOPE` 不一致时打回。

---

## 6. 第二阶段：生成规则

### 6.1 输入

只输入**已验证的 divergence**（含 `EVIDENCE`），不输入整条轨迹，避免 LLM 复述参考步骤。另附 19 个
文件的表头，用来锚定真实文件名和列名。

### 6.2 输出格式

沿用 TK-Boost 的四段式，但把范围、涉及的文件和列作为显式字段，由生成器直接产出，**不再单独跑
tagger**：

```text
DATA_RULE:
  SCOPE: column | multi_column | file | cross_table
  TABLES: <file>[, <file> ...]                      # 规则针对整个文件时列在这里；可为空
  COLUMNS: <file>.<column>[, <file>.<column> ...]   # 最细到列；可为空
  ENSURE: <行动建议>
  WHEN_TO_CHECK: <什么样的题会用到>
  TRIGGER: <来源题目中触发这条知识的原句片段>
  CONTEXT: <为什么，基于 EVIDENCE 里的数据事实>
  EXAMPLE_USAGE: <正确 vs 错误做法；具体值只能出现在这里>
GENERIC_RULE:
  SCOPE: generic
  <同样的 ENSURE / WHEN_TO_CHECK / TRIGGER / CONTEXT / EXAMPLE_USAGE，不带文件名>
```

**表和列分开存。** 早先的写法是在 `COLUMNS` 里用 `<file>.all` 表示整个文件，现已改为单独的 `TABLES`
字段。原因有三：

- `all` 是占用列名位置的哨兵值，只是碰巧没有真实列叫这个名字（实测 19 个文件的表头里没有）；
- 同一文件不能同时写「整表」和「其中某列」，旧的推出规则遇到这种组合直接报错；
- 存储层要从 `columns` 里把 `.all` 反推成文件，多一层转换。

阶段 A 已提交的 `tkstore/dataclaw/scope.py` 仍按旧的 `<file>.all` 写法实现，需要在阶段 B 使用前按 TDD
改成 `TABLES` + `COLUMNS`（见第 10 节待定）。

**范围分五类**，在 divergence 输出、规则输出和存储里都显式记录：

| `SCOPE` | 含义 | DataClaw 例子 |
| --- | --- | --- |
| `column` | 一个文件的一列 | `company_profile.completedDate` 的日期格式是「日/月/年」，如 `2/7/1987` |
| `multi_column` | 同一文件的多列 | 长表里 `targetName`、`targetUnit`、`value` 要一起读，单位随指标变化 |
| `file` | 整个文件 | `policy_resource.csv` 字段带引号和内嵌逗号，不能按行、按逗号切；`internal_metrics.csv` 存指标定义 |
| `cross_table` | 跨多个文件 | 用 `bmCode` 关联经营表与公司档案；四个分洲 `company_profile_*` 要与 `company_profile.csv` 去重后合并使用（五个文件都写在 `TABLES` 里） |
| `generic` | 不绑定文件 | 从数据事实中抽象出的通用检查 |

`SCOPE` 由 LLM 显式写出，促使它想清楚规则管多大范围；但 harness 根据 `TABLES` 与 `COLUMNS`
**自行推出**范围，步骤如下：

1. 涉及的文件等于 `TABLES` 里的文件加上 `COLUMNS` 里各列所在的文件。
2. `TABLES` 与 `COLUMNS` 都为空时为 `generic`。
3. 涉及两个以上文件时为 `cross_table`。
4. 只涉及一个文件时：该文件列在 `TABLES` 里就是 `file`，即使 `COLUMNS` 同时点了它的某些列；否则
   `COLUMNS` 有两列以上为 `multi_column`，一列为 `column`。

推出的与写的不一致时打回。

**`WHEN_TO_CHECK` 的生成要求。** harness 只能检查这个字段不含单元格取值，查不了它写得准不准；写得
太宽（例如「题目涉及任何公司指标」）不会被拦下。它在 B0 里只是给 agent 的提示，写宽的代价不大；
到 B1、B2 按它过滤或触发时，太宽会让规则到处出现，太窄会漏用。因此：

1. **生成时写出触发短语。** 规则生成 prompt 要求 LLM 在 `TRIGGER` 中摘出来源题目里触发这条知识的
   原句片段，再从中去掉具体实体和取值，抽象成 `WHEN_TO_CHECK` 的问法形状（例如从「卫生和社会工作
   行业有多少家企业」抽象成「题目的统计对象是行业或省份，而不是具体公司」）。`TRIGGER` 只进
   `provenance`、供人工审查抽象得是否合理，不渲染给被测 agent，也不参加正文取值检查（它照抄题目，
   本来就会含实体名）。
2. **合并时比较问法。** 合并步骤（第 7 节）拿到同组各条规则的 `TRIGGER` 与 `WHEN_TO_CHECK`，取各来源
   题目问法的共同点作为合并后的 `WHEN_TO_CHECK`。多道真实题目抽象出的共同点，比单凭一题写出的更可靠。

第一版不做「统计每条 `WHEN_TO_CHECK` 命中哪些题目」的离线过宽检查，留到 B1、B2 之前再考虑。

**规则须与工具无关。** 规则描述「要检查数据的什么特性」，不写某个库或命令的具体调用。例如写
「`policy_resource.csv` 的字段可能含逗号和换行，统计前必须按 CSV 规则解析」，不写「用
`csv.reader`」。

### 6.3 粒度约束：正文只出现列名，具体值只在例子里

> 规则正文（`ENSURE`、`WHEN_TO_CHECK`、`CONTEXT`）只允许出现文件名、列名这类 schema 标识和一般性
> 描述词；任何具体取值只能写在 `EXAMPLE_USAGE` 里。

目的是保证规则的适用条件和动作落在列上，换成该列的另一个取值规则仍然成立；而不是禁止规则帮助理解
具体值。对所有列使用同一条规则，不维护任何名单。示例：

```text
SCOPE: multi_column
TABLES:
COLUMNS: enterprise/company_operation_status.csv.targetUnit,
         enterprise/company_operation_status.csv.value,
         enterprise/company_operation_status.csv.secondTargetNum
ENSURE: 对 value 加总、比较或求比值前，逐行按 targetUnit 换算到同一单位；输出前再按题目要求的单位换算
WHEN_TO_CHECK: 题目涉及多家公司的同一指标，或需要按指定单位作答
CONTEXT: 即使是同一个指标，不同公司的 targetUnit 也不同，从元到亿元都有
EXAMPLE_USAGE: 营收金额在 2022 年有元、万元、十万元、百万元、千万元五种单位，各约 200 行；直接加总 value
         会把不同数量级的数混在一起
```

（本节早先的示例写的是「`targetUnit` 随 `targetName` 变化，例如净利润额是十万元、总资产金额是元」。
第 11 节的实测表明同一指标在不同公司的单位也不同，所以那个例子本身就是「只对单个值成立」的错误
写法，已替换。）

| 写法 | 判定 |
| --- | --- |
| 上面那条（条件和动作只引用 `targetUnit`、`value`、`secondTargetNum`） | 合格 |
| 「营收金额的单位是十万元」 | 不合格：只对一部分行成立，而且只在问到这个指标时有用 |
| 「某公司的行业是银行」 | 不合格：实体事实，既不可复用也可能泄漏 |

**被否决的替代方案。** 曾考虑按列把取值分成「个体值」和「标签值」，以及维护一份固定禁用名单
（公司名、`bmCode`、政策标题）。前者按统计特征分不准（第 3 节），后者 ad hoc、不好维护，均不采用。

### 6.4 harness 自动检查

1. **正文不含单元格值。** 从数据里取所有列的取值集合，检查 `ENSURE`、`WHEN_TO_CHECK`、`CONTEXT`
   是否出现其中任一取值；只匹配长度至少 3 个字符的值，避免「中国」「元」这类短值在正常叙述里误报。
   **纯数字也算取值**（例如年份 `2022`），正文出现即打回，年份、阈值只能写在 `EXAMPLE_USAGE`。
   与文件名、列名相同的取值不算违规。取值集合只收 3–40 个字符的值：实测 19 个文件共 223 万个
   不同取值（非纯数字 14.3 万个），全量扫描约 17 秒，建好后缓存。
2. **文件名与列名真实存在。** `TABLES` 中的文件存在，`COLUMNS` 中的列在对应文件的表头里。
3. **`SCOPE` 与 `TABLES`、`COLUMNS` 推出的范围一致**（推出规则见 6.2）。
4. 不合规的规则打回重写，最多两次；仍不合规则丢弃。

实测提醒：第 11 节 R4 的第一版正文写了「政策数量」，被这项检查拦下，因为它恰好是
`policy_release_status.csv` 的一个 `targetName` 取值。这类词读起来像普通叙述，规则生成时可能频繁
被打回，阶段 C 要统计打回率。

### 6.5 对例子的要求

prompt 写明：`EXAMPLE_USAGE` 只用于说明列级规则，不陈述某个具体公司或政策的属性与数值（例如它属于
哪个行业、某项指标是多少），也不照抄来源任务的答案。

**不设 gold 字符串扫描闸门。** gold 答案里最常见的是 `"Yes"`（60 次）、`"No"`（51）、`"Equal"`（32）、
`0.0`（18），milestone 里有大量行业名等共享标签；一刀切禁止出现 gold 内容会让规则写不出来。
train / test 虽按任务分开，但约 78–98 道 test 题与 train 共享被点名的公司，真正的风险是 train 的中间
事实经例子被 test 用上；这一风险已被「正文只出现列名」限制在例子里，由上述 prompt 要求和合并阶段
的 reviewer 把关。**populate 之后的 test 侧诊断暂不做。**

---

## 7. 合并去重

所有 task 共用同一个 `bm_rag_qa`，同一条数据事实会被许多失败样本重复学到，而 B0 要把全量规则注入
prompt，所以需要合并去重。

- **分组键**：按规则引用的集合分组，集合元素是 `TABLES` 中的每个 `(file, None)` 加上 `COLUMNS` 中的
  每个 `(file, column)`。`column` 范围的规则就是单个 `(file, column)`；`multi_column`、`cross_table`
  为多个；`file` 含 `(file, None)`；`generic` 规则单独成一组。
- **每组一次 LLM 调用**，reviewer 就是合并这一步本身：
  1. 合并语义重复的规则；
  2. 只对单个值成立的规则，上提成列级规则，或丢弃；
  3. 检查 `EXAMPLE_USAGE` 是否陈述了具体公司或政策的事实，是则改写；
  4. 比较同组各条规则的 `TRIGGER` 与 `WHEN_TO_CHECK`，取各来源题目问法的共同点作为合并后的
     `WHEN_TO_CHECK`（见 6.2）。
- 输入每条规则的 `EVIDENCE` 摘要、`TRIGGER` 和来源任务数；输出合并后的规则、被丢弃的规则及理由。
- 每条合并后的规则记录由多少个 train 任务支撑。
- **已知的残留重复**：同一知识在不同样本里引用的列数不同（两列 vs 三列）时会落进不同组、合并不掉。
  第一版接受，不做跨组二次合并。

---

## 8. 存储与检索

### 8.1 格式：JSONL，一行一条规则

不沿用 TK-Boost 的 10 列 CSV：`sql_operations`、`data_type`、`nulls` 在这里没有意义；列表字段用分号
拼接不便；合并与 B0 receipt 需要规则 id 与来源信息。人工浏览时可另导出 CSV，但不作为正式存储。

```json
{
  "rule_id": "dc-0042",
  "kind": "cross_table",
  "tables": [
    "industry/national_industry_status.csv",
    "industry/regional_industry_status.csv"
  ],
  "columns": [
    {"file": "enterprise/company_profile.csv", "column": "industry"}
  ],
  "files": [
    "enterprise/company_profile.csv",
    "industry/national_industry_status.csv",
    "industry/regional_industry_status.csv"
  ],
  "ensure": "题目问某行业或某省某行业的企业数、合计、最大值、中位数等汇总量时，先在两张汇总表的 targetName 中找对应指标并直接取 value；……",
  "when_to_check": "题目的统计对象是行业或省份，而不是具体公司",
  "context": "汇总表的数值与按 company_profile.csv 逐行计数的结果不一致，口径无法从明细复现",
  "example_usage": "……（具体值只能出现在这里）",
  "provenance": {
    "source_task_ids": ["task_218_…", "task_008_…"],
    "support_count": 2,
    "merged_from": ["dc-0042a", "dc-0042b"],
    "triggers": ["<来源题目中触发这条知识的原句片段>"],
    "evidence": ["<反思 agent 的 probe 引用>"]
  },
  "task_categories": ["enterprise_industry_analysis"]
}
```

| `kind` | 含义 | `tables` | `columns` |
| --- | --- | --- | --- |
| `column` | 单列规则 | 空 | 一个 `(file, column)` |
| `multi_column` | 同一文件的多列规则 | 空 | 同一 `file` 的多个 `column` |
| `cross_table` | 跨表规则（关联键、标签对齐、多文件合并、汇总表与明细表的取舍） | 可有 | 可有；`tables` 与 `columns` 合计涉及两个以上文件 |
| `file` | 关于整个文件的规则（例如 `policy_resource.csv` 必须按 CSV 规则解析） | 一个文件 | 为空，或只含该文件的列 |
| `generic` | 不绑定文件的通用规律 | 空 | 空 |

`kind` 与规则输出的 `SCOPE` 相同，写入前由 harness 按 `tables` 与 `columns` 重新推出（规则见 6.2）。
`files` 由 `tables` 与 `columns` 推出（两者所涉文件的并集），单独存储以便按文件检索；`tables` 单独保存，
是为了记住哪些文件是作为整体被引用的。`provenance.triggers` 只供人工审查，B0 渲染时不输出。
`task_categories` 是辅助信息，不参与首轮过滤。

### 8.2 检索

- 加载时建立「文件 → 规则 id」索引。
- **B0**：取全部规则，按固定顺序渲染成 `[TRIBAL_KNOWLEDGE]` 块注入 agent prompt，渲染结果的 hash
  写进 `knowledge_receipt.json`（送达要求见 `dataclaw.md` 5.5）。
- **B1**（后续）：全部规则连同 `files`、`columns` 交给 DataClaw 版 FilterKnowledge 二次筛选。
- **B2**（后续）：agent 探查某个文件时，返回 `files` 包含该文件的规则。跨表规则在 agent 探查了它
  涉及的**任意一个**文件时即返回，以便提示关联对象。`generic` 规则无法按文件触发，只走 B0 / B1 的
  开跑前注入。

---

## 9. 开发与实施规划

### 9.1 总体顺序：先在历史轨迹上开发，产物合理后再做正式运行

populate 链路的主要风险在三份 prompt（反思、规则生成、合并）的质量，越早用真实轨迹暴露问题越好。
因此不先定 split、不先跑全量 train，而是：

1. 从归档里拷贝一批历史失败轨迹，组成**开发集**（9.2）；
2. 在开发集上依次开发输入适配、探查工具与反思 agent、规则生成、合并与存储四部分（9.3），每部分先在
   小样本上人工检查，三份 prompt 在各自那一步逐条定稿；
3. 产物合理之后，再定 train / test split、在 train 上跑全量裸 OpenClaw、跑全量 populate（9.4）；
4. 最后做 B0 注入与 A / B0 评测（见 `dataclaw.md` 5.5–5.7）。

### 9.2 开发集

**来源与筛选。** `~/archive/DA_Workflow/output` 下所有裸 glm-5.2 run（目录名形如
`glm-5.2_<YYYYMMDD>_<HHMM>_<6 位 hex>`，不含 `_with_skill_`）中，同时有 `score.json` 与 `chat.jsonl`、
且 `score == 0` 的 run。

**规模（脚本统计）。**

| 项 | 值 |
| --- | --- |
| 裸 run 中有打分的 | 94 个，覆盖 70 道题 |
| 判错的 run（开发集） | **33 个**，来自 **27 道题**；5 道题有不止一个失败 run |
| 带 `process_score.json`（含 GPR 明细） | 33/33 |
| 类别 | `enterprise_industry_analysis` 11、`comprehensive_decision` 6、`hypothesis_verification` 3、`international_comparison` 3、`enterprise_industry_policy_analysis` 3、`risk_assessment` 1；**`industry_planning` 0** |
| 难度 | easy 3、medium 16、hard 8 |
| `chat.jsonl` 体积 | 合计 11.6 MB，中位数 235 KB，最大 2.5 MB |

**存放位置。**

```text
data/dataclaw_dev/
  manifest.csv                        # 提交：开发集由哪些 run 组成
  runs/<task_id>/<run_dir>/           # git 忽略（.gitignore: data/dataclaw_dev/runs/）
    chat.jsonl
    score.json
    process_score.json
    usage.json
```

`manifest.csv` 每行一个 run，列为 `task_id`、`run_dir`、`category`、`level`、`score`、`gpr`、
`chat_bytes`、`chat_sha256`、`source_dir`（归档中的原路径）、`task_file`、`gold_file`。gold 与 task
文件不拷贝，直接引用 `~/DataClaw/tasks/` 与 `~/DataClaw/assets/qa_gold/` 的原路径。拷贝时文件权限从
归档的只读改为 `644`，内容不变，可用 `chat_sha256` 核对。

**与正式实验的关系。** 开发集只用于开发和调试 prompt，**不约束之后的 train / test split**；正式实验会在
新 split 的 train 上重新跑裸 agent、重新 populate，与开发期产出无关。开发期生成的规则不进入最终 store。

**覆盖缺口。** 开发集没有 `industry_planning` 类的题，反思 prompt 在这一类上的表现要到正式 populate
时才第一次暴露。5 道有多个失败 run 的题，开发时全部使用，作为额外测试样本。

### 9.3 开发阶段

四个阶段依次推进；每个阶段都按 TDD 先写失败测试、再实现，并以「在开发集小样本上人工检查通过」为
进入下一阶段的条件。

**阶段 A：输入适配与离线工具（不调用 LLM、不起容器）**

- 读 `manifest.csv`，加载每个 run 的轨迹、`score.json`、`process_score.json`、gold、task prompt；
- `chat.jsonl` 转成压缩轨迹（工具调用、截断输出、最终答案），可借用 `~/baseline` 的
  `chat_jsonl_to_conversation`；
- 正误闸门（只取判错 run）；
- 19 个文件的表头目录，以及每列的取值集合（供 6.4 节正文检查）；
- milestone 比对器：数值用 `_numbers_match`（1%）、列表按集合、字典逐 key；
- 由 `COLUMNS` 推出 `SCOPE`、校验列名存在、检查正文是否含单元格值（≥3 字符）。

退出条件：单元测试全过；33 个开发 run 全部能加载、压缩，压缩后轨迹长度分布合理。

**阶段 A 完成情况**（commit `b53a3d7`）：33 个 run 全部加载、sha 一致、判错闸门全判为失败，33/33 的
轨迹首条消息与 task prompt 一致，962 次工具调用全部按 `toolCallId` 配上结果。压缩参数已定：工具输出
截断到 2000 字符（保留头尾），思考保留 5000 字符，命令原样保留（反思 agent 要能按原样重放）；此时
压缩后轨迹长度中位数 39,222 字符，最长 215,865 字符。已知遗留：`scope.py` 仍按旧的 `<file>.all`
写法实现，要在阶段 B 开始时按 6.2 节改成 `TABLES` + `COLUMNS`。

**阶段 B：探查工具与反思 agent**

- 先按 TDD 把 `scope.py` 改成 6.2 节的 `TABLES` + `COLUMNS` 写法（含「同一文件既在 `TABLES` 又点了
  其中的列」这一新允许的组合）；
- probe 工具：在 `dataclaw:0.1.0` 容器里执行 shell 命令，数据只读挂载，输出截断，记录每次 probe 的
  编号与完整输出；独立于 LLM，单独测试；
- 反思循环：prompt、结构化输出解析、5.5 节四道硬闸门与 `SCOPE` 一致性检查、打回消息、轮数预算；
- 闸门逻辑用「假 LLM」（预设回复）测试，覆盖引用不存在的 probe、摘录与真实输出不符、复现值偏差超过
  1%、文件或列名不存在、`SCOPE` 与 `TABLES`、`COLUMNS` 推出的范围不一致等情况。

退出条件：反思 prompt 定稿；在 3–5 个开发 run 上试跑，人工确认 divergence 有真实数据证据、没有只是
复述参考步骤。若质量不达标，先改 prompt，不进入阶段 C。

**阶段 C：规则生成**

- 第二阶段 prompt、`DATA_RULE` / `GENERIC_RULE` 解析（含 `TABLES` 与 `TRIGGER`）、6.4 节自动检查与
  最多两次重写；prompt 的正反例以第 11 节的样例规则为对照；
- 用假 LLM 测试检查与重写逻辑。

退出条件：规则生成 prompt 定稿；以阶段 B 的产物为输入试跑，人工确认规则是列级的、例子不陈述具体
实体事实。

**阶段 D：合并与存储**

- 按 `TABLES` 的 `(file, None)` 与 `COLUMNS` 的 `(file, column)` 组成的集合分组、每组一次合并调用、
  合并时按各来源的 `TRIGGER` 归纳 `WHEN_TO_CHECK`、保留 `provenance` 与支撑任务数；
- 写 JSONL store，建立「文件 → 规则 id」索引；
- B0 渲染：把全部规则按固定顺序渲染成 `[TRIBAL_KNOWLEDGE]` 块并计算 hash。同一 store 每次必须渲染出
  同一段文本，否则 B0 的 receipt 无法验收；用确定性测试保证。

退出条件：合并 prompt 定稿；以阶段 C 的产物为输入试跑，得到一份开发期 store，人工检查各 `kind` 的
数量与质量。

### 9.4 开发完成后的正式运行

1. **定 split。** 在 492 道题上按 `category × level` 分层随机切分，开发集题目不作特殊处理；
2. **跑 train 上的裸 OpenClaw。** 用现有 `run_batch.py`，每题一次，独立的 `OUTPUT_SUBDIR`；
3. **全量 populate。** 用阶段 A–D 的代码处理 train 上所有判错 run，生成正式 store；
4. **B0 注入与评测。** 按 `dataclaw.md` 5.5 节实现只给 agent 的注入与 `knowledge_receipt.json`，逐 run
   做送达验收；A、B0 分别用不同 `OUTPUT_SUBDIR` 在 test 上各跑一次。

### 9.5 注意事项

- **agent 模型保持 glm-5.2。** 开发集是 glm-5.2 的轨迹；正式 train / test 若换模型，错误分布会变，
  开发期调好的 prompt 不一定适用。
- **历史轨迹来自同一镜像。** 开发集由 `dataclaw:0.1.0`（OpenClaw 2026.3.24）产出，`chat.jsonl` 格式与
  新 run 一致；其 `score.json` 是当时 judge 的判定，开发期直接沿用。
- **开发期 store 不是最终 store。** 只用于检查链路与 prompt 是否合理。

---

## 10. 决策记录

### 已定

| # | 项 | 决定 |
| --- | --- | --- |
| 1 | 框架 | 沿用 TK-Boost「先找 diff，再生成规则」两阶段；重新设计输入与 prompt |
| 2 | 反思形态 | ReAct agent，要求探库 |
| 3 | 探查环境 | 与 OpenClaw 同一 `dataclaw:0.1.0` 镜像的 shell，工具与 agent 一致，可重放 agent 命令（修订：早先定为宿主机 pandas，见 5.1） |
| 4 | 验证 | 复现受影响的 milestone 值才进入规则生成；由 harness 按 `_numbers_match`（1%）、集合、逐 key 比对 |
| 5 | 非数据类错误 | 跳过 |
| 6 | 粒度 | 正文只出现文件名、列名；具体值只在 `EXAMPLE_USAGE`；所有列同一规则 |
| 7 | 自动检查 | 正文不含单元格值（≥3 字符）、列名真实存在；不合规最多重写两次 |
| 8 | gold 扫描 | 不设硬闸门 |
| 9 | test 侧诊断 | 暂不做 |
| 10 | 合并去重 | 每组一次 LLM 调用；按 `TABLES` 的 `(file, None)` 与 `COLUMNS` 的 `(file, column)` 组成的集合分组；接受跨组残留重复 |
| 11 | 存储 | JSONL，`kind` 五类，`provenance` 记来源与支撑数 |
| 12 | B2 跨表返回时机 | 探查到任意一个涉及文件即返回 |
| 13 | 不采用 | operation tags；按列分类个体 / 标签值；固定禁用名单 |
| 14 | 范围 | `column` / `multi_column` / `file` / `cross_table` / `generic` 五类，在 divergence、规则、存储中显式记录；harness 按 `TABLES` 与 `COLUMNS` 推出并校验 |
| 15 | 硬闸门 | 证据须为真实 probe 输出的子串；列名须存在；复现值由 harness 比对；`non_data`、`gold_suspect` 不进第二阶段 |
| 16 | 规则与工具 | 规则描述要检查的数据特性，不写具体库或命令调用 |
| 17 | 开发顺序 | 先在历史轨迹开发集上开发阶段 A–D，产物合理后再定 split、跑全量 train 与全量 populate |
| 18 | 开发集 | 归档中 33 个判错的裸 glm-5.2 run（27 道题），拷贝到 `data/dataclaw_dev/runs/`（git 忽略），`manifest.csv` 提交 |
| 19 | 开发集与 split | 互不约束；正式实验重新跑 train 并重新 populate，开发期规则不进入最终 store |
| 20 | 字符串 milestone | 规整后相等即一致，否则由反思 agent 做语义判断（prompt 提示「不要求逐字一致，但语义须几乎完全一致」）；数值部分仍由 harness 判定 |
| 21 | 正文取值检查 | 纯数字也算单元格取值；与文件名、列名相同的取值除外 |
| 22 | 代码位置 | `tkstore/dataclaw/` 子包，测试为 `tests/test_dataclaw_*.py` |
| 23 | 表和列分开存 | divergence 与规则输出写 `TABLES` 和 `COLUMNS` 两个字段，存储有 `tables`、`columns`，另存推出的 `files`；取消 `<file>.all` 写法（见 6.2） |
| 24 | `WHEN_TO_CHECK` 生成 | 规则生成时在 `TRIGGER` 中摘出来源题目的触发原句，再抽象成问法形状；合并时比较同组各来源的问法取共同点（见 6.2、第 7 节） |
| 25 | 轨迹压缩 | 工具输出截断到 2000 字符、保留头尾；思考保留 5000 字符；命令原样保留 |

### 待定

- 三份 prompt（反思、规则生成、合并）的最终文本（第 5.3、6.2 节为草稿；合并 prompt 尚未起草），
  **必须逐条确认**；
- train 占 492 道题的比例；
- 反思 agent 的轮数预算与每次 probe 输出的截断长度；
- 反思 agent、规则生成、合并三处使用的模型；
- `tkstore/dataclaw/scope.py` 按决策 23 改写（阶段 B 开始时，按 TDD）；
- divergence 输出是否加一个 `CATEGORY` 字段（取第 11.2 节的类别名或「其他」）：不参与闸门，只用于统计
  开发集上各类的分布、方便调 prompt；
- `WHEN_TO_CHECK` 的离线过宽检查（统计每条命中哪些题目），B1、B2 之前再定；
- 同一 train task 若有多个裸 run，取哪一个（首版每 task 只跑一次，暂不涉及）。

---

## 11. 对照材料：数据理解问题类型与样例规则

本节是写反思 prompt（5.3）、规则生成 prompt（6.2）和合并 prompt（第 7 节）时的对照，不是 prompt
正文。样例规则是**人工写的**，用来说明「能归到具体表或列、有事实支撑、又不过于泛化」的写法，
不进入任何 store。

### 11.1 依据从哪里来

- **失败线索**：33 个开发 run 的 judge `notes` 和 `process_score.json` 的 `chain_summary`。这两者
  都是 LLM 写的，只作线索。
- **核对**：对照 gold `steps` 读了部分 agent 的实际命令（`task_049`、`task_195`、`task_218` 等）。
- **数据事实**：在 `~/DataClaw/assets/database` 上用脚本统计，本节所有数字都来自这些统计。
- **样例规则的检查**：11 条样例（加上 R11b）都用阶段 A 的 `validate_columns`、`derive_scope`、
  `check_body` 跑过；正文不含单元格取值，文件和列都存在。检查时 `scope.py` 还是旧写法，所以用的是
  等价的 `<file>.all` 形式；下面已按决策 23 改写成 `TABLES` + `COLUMNS`。

### 11.2 数据理解问题清单（写进反思 prompt 时用作提示，末尾留「其他」）

| # | 类别 | 这份数据里的具体表现（实测） | 开发集例子 |
| --- | --- | --- | --- |
| 1 | 多表关联关系 | `company_profile.csv` 的 `bmCode` 唯一；经营表里有 110 个带前导零的 `bmCode` 不在任何档案文件中 | `task_021`：gold 295 家，agent 305 家，结果里出现 `Tokyo` |
| 2 | 找错表：有汇总表却从明细聚合 | 卫生和社会工作企业数：汇总表 29；档案全部 31 行、其中境内 26 行，都对不上 29 | `task_008`（13 对 22 个省）、`task_477`（38 对 24 个行业）、`task_218`（29 对 31）、`task_223`（取错汇总表的行） |
| 3 | 缺失值与异常值 | 长表的缺失表现为 `value` 为空；2022 年营业收入编码下 20 行为空、5 行为负 | `task_021`（不同指标用了不同样本）、`task_008`、`task_464` |
| 4 | 有更好的列作为指标 | 在这份数据里多与 5、10 重合；单独成立的例子是政策计数：发文机关写在 `policy_release_status.csv` 的 `targetName` 里 | `task_352`（gold 1，agent 4，来源未核实） |
| 5 | 同一指标有多个名字 | `company_operation_status.csv` 的 49 个 `secondTargetNum` 全部对应多种 `targetName` 写法 | `task_049`：只按一种写法筛选，取到约 1,000 家，营收最高的省份判错 |
| 6 | 同一指标每行单位不同 | 同一指标在不同公司的 `targetUnit` 不同，营收金额有 5 种单位、各约 200 行 | `task_049`、`task_088`（差 10^6 倍）、`task_011`（差 10 倍） |
| 7 | 实体名称对不上 | 中英对照 JSON 只收 173 家公司；很多公司共享前两个字 | `task_195`（取了同前缀的另一家公司）、`task_185`、`task_204` |
| 8 | 统计范围与纳入口径 | 档案 7,295 行：`country` 为中国 6,509、中国香港 345（`province` 为香港特别行政区），其余为境外 | `task_218`（31 家里含港交所 15、NASDAQ 1、XETRA 1）、`task_383` |
| 9 | 文件名暗示的含义与内容不符 | `company_profile_eu.csv` 最多的 `province` 是 California、Tokyo；四个分洲文件 3,675 行中 416 个 `bmCode` 也在 `company_profile.csv` | `task_021` 出现 `Tokyo`（推断，未验证） |
| 10 | 结构化字段优先于全文搜索 | `policy_resource.csv` 的 `industry` 在 1,129 条中 318 条用全角分号连接多个行业、371 条为空 | `task_054`（未拆开多行业字段）；`task_382`（4 对 9 个省）、`task_388`（15 对 17 个省）据 `chain_summary` 是关键词搜索所致，未核实 |
| 11 | 数值格式与工具的相互作用 | 2022 年有 224 行 `value` 是科学计数法；`policy_resource.csv` 1,129 条记录中 1,110 条有字段内含换行，文件共 92,058 个物理行 | `task_049`（`sort -nr` 排错） |

写进 prompt 时的要求：

- 写成**提示清单**，不是封闭分类；末尾留「其他」，避免反思 agent 把观察硬套进某一类。
- 每类都要求 probe 证明。第 2、5 类的事实（汇总表复现不出来、同一编码有多种写法）看几行数据发现
  不了，要提示反思 agent 可以做**分组计数类的 probe**，不要只用 `head` 和 `grep`。
- 第 7 类要提示：英文名查不到时按拼音逐字对应，并列出所有同前缀候选逐字核对。

### 11.3 应标为 `non_data` 的失败（反思 prompt 里举例）

| 类型 | 开发集例子 |
| --- | --- |
| 减法方向或取了绝对值 | `task_206`、`task_248`：中间值全对，只是符号错 |
| 最后一步算术错误 | `task_290` |
| 回答了错误的对象 | `task_284`、`task_231`、`task_218`：要公司名，答了行业或中文名 |
| 从外部网页取数 | `task_452`、`task_434` |

另有两道疑似 `gold_suspect`，不能用它们的 milestone 做复现验证：

- `task_054`：题面要求回答 yes/no，gold 是 `416`（其 gold `steps` 的数据路径仍可参考，见 R10）；
- `task_195`：档案里「润会数智系统公司」的 `industry` 是「信息传输、软件和信息技术服务业」，gold 写的是
  Scientific Research and Technical Services（可能 gold 用了别的列，交给反思 agent 核实）。

### 11.4 写规则的三条标准

- **判断是否太泛**：去掉文件名和列名后规则还读得通，就是太泛。「比较前注意单位」太泛；「同一
  `secondTargetNum` 下不同公司的 `targetUnit` 也不同」离开这两列就不成立，是锚在列上的写法。
- **判断是否太细**：把例子里的具体值换成同一列的另一个值，规则仍应成立。「营收金额单位是十万元」
  换一个值就不对了，是太细。
- **`CONTEXT` 必须能被一次 probe 验证**：写分布、格式、覆盖范围这类可统计的性质，不写「数据可能有
  问题」。

### 11.5 样例规则

以下 `WHEN_TO_CHECK` 是人工从来源题目的问法抽象出来的，没有经过第 6.2 节的 `TRIGGER` 流程。
`TRIGGER` 一栏摘自来源题目原文（`R11b` 摘自 gold `steps`，`R9` 是 agent 结果中的现象），「……」表示
省略。所有 `<file>` 都是 `database/` 下的完整相对路径。

**R1 多表关联关系**

```text
SCOPE: cross_table
TABLES:
COLUMNS: enterprise/company_profile.csv.bmCode,
         enterprise/company_operation_status.csv.bmCode
ENSURE: 用 bmCode 关联公司档案与经营表之前，先核对两边 bmCode 的书写格式和覆盖范围；只在经营表出现的
        bmCode 不能靠去掉前导零去硬配档案
WHEN_TO_CHECK: 题目需要把公司的经营指标与它的行业、省份、所有制等档案属性一起使用
TRIGGER: task_021「Profitability is measured by the average net profit margin of enterprises in that province」
CONTEXT: company_profile.csv 中 bmCode 唯一；company_operation_status.csv 有一批带前导零的 bmCode 不在任何
         档案文件中，这批公司的指标还按半年报和年报各出现一次
EXAMPLE_USAGE: 经营表 7,005 个 bmCode 中有 110 个（形如 000001）不在任何档案文件里，去掉前导零后只有 15 个
         能对上，且不能保证是同一家公司；这 110 家的每个指标都有「(半年度)」「(年报)」两行
```

**R2 找错表：有汇总表却从明细聚合**

```text
SCOPE: cross_table
TABLES: industry/national_industry_status.csv,
        industry/regional_industry_status.csv
COLUMNS: enterprise/company_profile.csv.industry
ENSURE: 题目问某行业或某省某行业的企业数、合计、最大值、中位数等汇总量时，先在两张汇总表的 targetName
        中找对应指标并直接取 value；汇总表没有该指标时才从企业明细聚合
WHEN_TO_CHECK: 题目的统计对象是行业或省份，而不是具体公司
TRIGGER: task_218「the number of enterprises in Health and Social Work in the industry of …」
CONTEXT: 汇总表的数值与按 company_profile.csv 逐行计数的结果不一致，口径无法从明细复现
EXAMPLE_USAGE: 卫生和社会工作的企业数：national_industry_status 为 29；company_profile 全部为 31 行、其中
         country 为中国的 26 行，两种数法都得不到 29（task_218 按明细数出 31，被判错）
```

这条成立的关键是实测 29 既不等于 31 也不等于 26：自己聚合无论取哪种口径都对不上汇总表。

**R3 缺失值与异常值**

```text
SCOPE: multi_column
TABLES:
COLUMNS: enterprise/company_operation_status.csv.bmCode,
         enterprise/company_operation_status.csv.secondTargetNum,
         enterprise/company_operation_status.csv.value
ENSURE: 按公司组合多个指标计算时，先分别统计每个指标在哪些公司上为空、为零或为负，再在所有指标都有效的
        同一批公司上计算，不让不同指标各用各的样本；题目没有要求时不要自行剔除异常值
WHEN_TO_CHECK: 题目要求比值、加权得分或多个指标的组合
TRIGGER: task_021「Financial health = Profitability score × 0.4 + Solvency score × 0.3 + Growth capability score × 0.3」
CONTEXT: 该文件是长表，一个公司一个指标一行，缺失表现为 value 为空
EXAMPLE_USAGE: 2022 年营业收入编码下有 20 行 value 为空、5 行为负；task_021 gold 在同一批 295 家上计算并
         注明「不按资产负债率剔除」，agent 对资产负债率用了 22 家、对盈利能力用了 36 家
```

不写「剔除异常值」或「保留异常值」这种固定动作：gold 的做法因题而异，可复用的是「先统计、同一
样本、不擅自剔除」。实测 6,509 家境内公司都有营业收入那一行，所以这里的缺失不是缺行。

**R4 有更好的列作为指标（政策计数）**

```text
SCOPE: multi_column
TABLES:
COLUMNS: policy/policy_release_status.csv.targetName,
         policy/policy_release_status.csv.value,
         policy/policy_release_status.csv.industry,
         policy/policy_release_status.csv.province
ENSURE: 按发文机关或政策类型统计政策条数时，在 policy_release_status.csv 中按 industry、province 过滤，
        从 targetName 里识别发文机关，直接取 value；不要改用政策全文去自己数
WHEN_TO_CHECK: 题目问某机关、某省或某行业有多少条政策
TRIGGER: task_352「Ministry of Housing and Urban-Rural DevelopmentNumber of policies (indicator)」
CONTEXT: 发文机关没有单独的列，而是写在 targetName 里，数量在 value 里
EXAMPLE_USAGE: targetName 形如「地方政策-山东省发展和改革委员会政策数量」；task_352 gold 取值为 1，agent 得到 4
```

第一版正文写的是「统计政策数量」，被 `check_body` 拦下（「政策数量」是 `targetName` 的一个取值），
改为「政策条数」后通过。agent 的「4」是怎么数出来的尚未核实，要由反思 agent 用 probe 确认。

**R5 同一指标有多个名字**

```text
SCOPE: multi_column
TABLES:
COLUMNS: enterprise/company_operation_status.csv.secondTargetNum,
         enterprise/company_operation_status.csv.targetName
ENSURE: 取公司指标时按 secondTargetNum 筛选，不按 targetName 精确匹配：先用关键字找到指标对应的
        secondTargetNum，再列出这个编码下的全部 targetName，确认它们是同一指标
WHEN_TO_CHECK: 题目涉及任何公司层面的财务或经营指标
TRIGGER: task_049「the region with the highest total operating revenue」
CONTEXT: 同一个 secondTargetNum 下有多种 targetName 写法，每家公司只用其中一种；只按一种写法筛选会漏掉
         大部分公司
EXAMPLE_USAGE: Y_EC_5 下有营业收入金额（2,778 行）、营收额、营业收入总额、营收金额（1,004 行）、营业收入；
         task_049 只按营收金额筛选，只取到约 1,000 家，得出营收最高的是广东（gold 为北京）
```

实测 49 个编码全部有多种写法，是适用面最广的一条。它的 `WHEN_TO_CHECK` 写得很宽，在 B0 里无妨，
到 B1、B2 时需要重看。

**R6 同一指标每行单位不同**

```text
SCOPE: multi_column
TABLES:
COLUMNS: enterprise/company_operation_status.csv.targetUnit,
         enterprise/company_operation_status.csv.value,
         enterprise/company_operation_status.csv.secondTargetNum
ENSURE: 对 value 加总、比较或求比值前，逐行按 targetUnit 换算到同一单位；输出前再按题目要求的单位换算
WHEN_TO_CHECK: 题目涉及多家公司的同一指标，或需要按指定单位作答
TRIGGER: task_088「what is the difference in total liabilities」（guidelines 要求输出不带单位的数值）
CONTEXT: 即使是同一个指标，不同公司的 targetUnit 也不同，从元到亿元都有
EXAMPLE_USAGE: 营收金额在 2022 年有元、万元、十万元、百万元、千万元五种单位，各约 200 行；task_088 gold
         以元作答，agent 输出百万元，差了 10^6 倍；task_011 差了 10 倍
```

它与 R5 的列相近但讲的是另一个事实，所以分开写；两者的 `(file, column)` 集合不同，不会被合并。

**R7 实体名称对不上**

```text
SCOPE: cross_table
TABLES: bilingual_translation_english_chinese.json
COLUMNS: enterprise/company_profile.csv.bmCompanyName
ENSURE: 题目中的英文公司名先查中英对照文件；查不到时把它当作中文名逐字的拼音，列出 bmCompanyName 中所有
        读音相符的候选并逐字核对，确认唯一后再用
WHEN_TO_CHECK: 题目按英文名点名具体公司
TRIGGER: task_195「Run Hui Shu Zhi Xi Tong Co., Ltd.」
CONTEXT: 对照文件只覆盖一部分公司；bmCompanyName 中很多公司共享同样的前两个字，音近字多，第一个近似匹配
         常常不是目标公司
EXAMPLE_USAGE: Run Hui Shu Zhi Xi Tong 对应「润会数智系统公司」（吉林省）；task_195 的 agent 搜「润汇数智」
         没有结果，改取「润会数创系统公司」（山东省）；以「润会」开头的公司至少有 10 家
```

**R8 统计范围与纳入口径**

```text
SCOPE: multi_column
TABLES:
COLUMNS: enterprise/company_profile.csv.country,
         enterprise/company_profile.csv.province,
         enterprise/company_profile.csv.exchange,
         enterprise/company_profile.csv.ownership
ENSURE: 确定参与统计的企业集合前，先看 country、province、exchange、ownership 的取值分布，按题目给出的范围
        显式筛选；题目没有限定时不要自行排除某类企业
WHEN_TO_CHECK: 题目按行业、省份或所有制统计企业，或按省份分组比较
TRIGGER: task_383「在2022年专用设备制造业上市企业中……有政策省份……无政策省份」（gold 剔除港澳台企业）
CONTEXT: company_profile.csv 同时收录境内、香港和境外上市公司，香港公司的 province 是一个特别行政区，会作为
         一个省份参与分组
EXAMPLE_USAGE: country 为中国的 6,509 行、中国香港 345 行（province 均为香港特别行政区），其余为境外；
         task_383 gold 剔除港澳台企业，而 task_011 gold 的第一名正是香港特别行政区
```

不能写成「总是剔除香港」：`task_383` 与 `task_011` 的 gold 口径相反。规则只要求「按题目显式筛选」，
具体口径放在例子里。

**R9 文件名暗示的含义与内容不符**

```text
SCOPE: cross_table
TABLES: enterprise/company_profile.csv,
        enterprise/company_profile_as.csv,
        enterprise/company_profile_eu.csv,
        enterprise/company_profile_na.csv,
        enterprise/company_profile_oc.csv
COLUMNS:
ENSURE: 不要按文件名推断四个分洲档案文件的内容；与 company_profile.csv 合并前，先查它们 province、country
        的实际取值以及与 company_profile.csv 重叠的 bmCode，去重后再用
WHEN_TO_CHECK: 题目涉及境外公司，或需要把全部公司档案合在一起
TRIGGER: task_021（agent 结果中出现 Tokyo 省份）
CONTEXT: 分洲文件的 province 取值与文件名所示的洲不一致，且部分 bmCode 同时出现在 company_profile.csv 中
EXAMPLE_USAGE: company_profile_eu.csv 中最多的 province 是 California（166 行）和 Tokyo（162 行）；四个分洲
         文件共 3,675 行，其中 416 个 bmCode 也在 company_profile.csv 里
```

「`task_021` 的 Tokyo 来自合并分洲文件」是推断，未用 probe 验证，所以没写进例子。

**R10 结构化字段优先于全文搜索**

```text
SCOPE: column
TABLES:
COLUMNS: policy/policy_resource.csv.industry
ENSURE: 判断一条政策涉及哪些行业时，读该行 industry 字段，按分隔符拆成多个行业，逐个与 company_profile.csv
        的 industry 精确比较；不要用关键词搜索标题或正文
WHEN_TO_CHECK: 题目问某政策适用于哪些行业、影响多少企业，或某行业有哪些政策
TRIGGER: task_054「Total number of all enterprises affected by the policy '…'」
CONTEXT: 该字段可能为空、只有一个行业，或用全角分号连接多个行业
EXAMPLE_USAGE: 1,129 条政策中有 318 条是多行业（用「；」连接），371 条为空；task_054 的 gold 从一条政策的
         industry 字段拆出 8 个行业再逐个统计企业数，agent 没有拆开这个字段，答了 No relevant data found
```

分隔符只能是全角分号「；」：很多行业名本身带顿号（例如「电力、热力、燃气及水生产和供应业」），按
顿号拆会拆错，第一次统计时就犯过这个错。

这条的证据有两处限制：

- `task_054` 属于 `gold_suspect`（11.3）：题面要求 yes/no、gold 却是数字。它的 gold `steps` 里「拆
  industry 字段」这条数据路径仍可参考，但不能用它的 milestone 来做复现验证。
- `task_382`、`task_388` 的「关键词搜索而非按字段筛选」来自 `chain_summary`，未核实；而且 gold 用的是
  `policy_release_status.csv` 的 `industry`，不是本条的 `policy_resource.csv.industry`。若反思 agent
  在这两题上证实了这一点，应另写一条针对 `policy_release_status.csv.industry` 的规则。

**R11 数值格式与工具的相互作用**

```text
SCOPE: column
TABLES:
COLUMNS: enterprise/company_operation_status.csv.value
ENSURE: 排序或比较 value 前先把它解析为数值，不要按文本排序
WHEN_TO_CHECK: 题目需要按某个指标排名、取最大最小值或中位数
TRIGGER: task_049「the region with the highest total operating revenue」
CONTEXT: 该列有一部分值用科学计数法书写
EXAMPLE_USAGE: 2022 年有 224 行形如 1.56251E+11；task_049 中 sort -nr 把 9.58e+10 排在了 141690 之后
```

**R11b 数值格式与工具的相互作用（文件级）**

```text
SCOPE: file
TABLES: policy/policy_resource.csv
COLUMNS:
ENSURE: 按字段读取 policy_resource.csv 前，先按 CSV 规则解析带引号的字段；不要按行或按逗号切分
WHEN_TO_CHECK: 题目需要读取政策的标题、正文、行业或发文机关
TRIGGER: task_382「从 policy_resource.csv 中读取上述 32 条地方政策全文」（gold steps）
CONTEXT: 该文件的文本字段带引号，内部含逗号和换行，一条记录可能跨越多行
EXAMPLE_USAGE: 1,129 条记录中 1,110 条有字段内含换行，文件共 92,058 个物理行；按行 grep 会把一条政策切成
         许多片段
```

按决策 16，规则不写具体命令，所以正文只说「不要按文本排序」「不要按行或按逗号切分」，`sort -nr`
只出现在例子里。

### 11.6 由这些样例得出的提醒

- **正文取值检查能拦住把值写进正文**（R4 的「政策数量」），但这类词读起来像普通叙述，阶段 C 要统计
  打回率，必要时在规则生成 prompt 里提示「指标名、行业名都是取值」。
- **R2、R5 的事实只能靠聚合统计验证**，反思 prompt 要允许并鼓励分组计数类 probe。
- **`gold_suspect` 必须单独分出**：`task_054` 与 `task_195` 若被当作数据证据，会产出错误规则。
- **`WHEN_TO_CHECK` 宽窄不一**：R5「任何公司层面指标」很宽，R7「按英文名点名公司」较窄。宽的在 B0
  里无妨，到 B1、B2 时按第 6.2 节的 `TRIGGER` 与合并流程重新归纳。
