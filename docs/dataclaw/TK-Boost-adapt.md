# TK-Boost knowledge population 适配 DataClaw：设计记录

本文记录把 TK-Boost 的 knowledge population（从失败轨迹生成规则、存成 knowledge store）适配到
DataClaw 的具体方案。它细化 [`dataclaw.md`](./dataclaw.md) 的 5.3、5.4 节；DataClaw 的环境、
runner、评测设计、与 `~/baseline` 的关系仍以 `dataclaw.md` 为准。

状态：**阶段 A 实现中**（代码在 `tkstore/dataclaw/`）。开发集已拷贝到 `data/dataclaw_dev/`
（第 9.2 节）。三份 prompt 的最终文本尚未定稿，文中的 prompt 是草稿，需要逐条确认（见第 10 节）。
开发顺序见第 9 节。

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

### 5.4 输出（每条 divergence 一个结构化块）

```text
DIVERGENCE: <agent 在哪一步做了什么>
NEEDED: <正确做法>
MISSING_DATA_UNDERSTANDING:
  SCOPE: column | multi_column | file | cross_table | generic
  COLUMNS: <file>.<column>[, <file>.<column> ...]   # 整个文件写 <file>.all；generic 为空
  FACT: <关于数据的事实>
EVIDENCE: probe#<n> → <关键输出摘录>
REPRODUCED: milestone "<key>" = <值>，由 probe#<m> 算出
KIND: data | non_data | gold_suspect
```

一条 divergence 只写一个数据事实；涉及多个事实时拆成多条。`SCOPE` 的含义与判定规则见 6.2 节。

### 5.5 harness 硬闸门

四道闸门都是 harness 用代码做的确定性检查，不靠 LLM 判断，对应 TK-Boost 驳回 `NO_DIFF` 的做法。
任何一道未通过，就在同一会话里把错误原因发回反思 agent 让它继续；轮数用完仍未通过的 divergence
直接丢弃，不部分采纳。

1. **证据必须真实存在。** harness 记录会话中每次 probe 的编号和完整输出。`EVIDENCE: probe#3 → <摘录>`
   必须满足：probe#3 确实执行过；`<摘录>` 在规整空白后是 probe#3 真实输出的子串。防止编造证据，或把
   推测写成观察结果。
2. **引用的文件和列必须真实存在。** `COLUMNS` 中每个 `file.column` 都要在 19 个文件的表头里找到；
   `file.all` 只校验文件存在。防止不存在的列名流进规则。
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

另有一项一致性检查：harness 按 `COLUMNS` 推出范围（规则见 6.2），与反思 agent 写的 `SCOPE`
不一致时打回。

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
  COLUMNS: <file>.<column>[, <file>.<column> ...]   # 最细到列；只涉及整个文件时写 <file>.all
  ENSURE: <行动建议>
  WHEN_TO_CHECK: <什么样的题会用到>
  CONTEXT: <为什么，基于 EVIDENCE 里的数据事实>
  EXAMPLE_USAGE: <正确 vs 错误做法；具体值只能出现在这里>
GENERIC_RULE:
  SCOPE: generic
  <同样四段，不带文件名>
```

**范围分五类**，在 divergence 输出、规则输出和存储里都显式记录：

| `SCOPE` | 含义 | DataClaw 例子 |
| --- | --- | --- |
| `column` | 一个文件的一列 | `company_profile.completedDate` 的日期格式是「日/月/年」，如 `2/7/1987` |
| `multi_column` | 同一文件的多列 | 长表里 `targetName`、`targetUnit`、`value` 要一起读，单位随指标变化 |
| `file` | 整个文件 | `policy_resource.csv` 字段带引号和内嵌逗号，不能按行、按逗号切；`internal_metrics.csv` 存指标定义 |
| `cross_table` | 跨多个文件 | 用 `bmCode` 关联经营表与公司档案；四个分洲 `company_profile_*` 要合并使用（写作 `A.all, B.all`） |
| `generic` | 不绑定文件 | 从数据事实中抽象出的通用检查 |

`SCOPE` 由 LLM 显式写出，促使它想清楚规则管多大范围；但 harness 根据 `COLUMNS` **自行推出**范围：
涉及两个以上文件为 `cross_table`，同一文件两列以上为 `multi_column`，`file.all` 为 `file`，列清单为空
为 `generic`，其余为 `column`。推出的与写的不一致时打回。

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
COLUMNS: enterprise/company_operation_status.csv.targetName,
         enterprise/company_operation_status.csv.targetUnit,
         enterprise/company_operation_status.csv.value
ENSURE: 跨不同 targetName 比较或加总 value 前，按每行的 targetUnit 换算到同一单位
WHEN_TO_CHECK: 题目涉及同一文件中多个指标的比较、求和或比值
CONTEXT: 该文件是长表，每个指标一行，targetUnit 随 targetName 变化
EXAMPLE_USAGE: 例如净利润额的单位是十万元、总资产金额是元，直接比较 value 会差 10^5 倍
```

| 写法 | 判定 |
| --- | --- |
| 上面那条（条件和动作只引用 `targetName`、`targetUnit`、`value`） | 合格 |
| 「净利润额的单位是十万元」 | 不合格：只在问到这个指标时有用 |
| 「某公司的行业是银行」 | 不合格：实体事实，既不可复用也可能泄漏 |

**被否决的替代方案。** 曾考虑按列把取值分成「个体值」和「标签值」，以及维护一份固定禁用名单
（公司名、`bmCode`、政策标题）。前者按统计特征分不准（第 3 节），后者 ad hoc、不好维护，均不采用。

### 6.4 harness 自动检查

1. **正文不含单元格值。** 从数据里取所有列的取值集合，检查 `ENSURE`、`WHEN_TO_CHECK`、`CONTEXT`
   是否出现其中任一取值；只匹配长度至少 3 个字符的值，避免「中国」「元」这类短值在正常叙述里误报。
   **纯数字也算取值**（例如年份 `2022`），正文出现即打回，年份、阈值只能写在 `EXAMPLE_USAGE`。
   与文件名、列名相同的取值不算违规。取值集合只收 3–40 个字符的值：实测 19 个文件共 223 万个
   不同取值（非纯数字 14.3 万个），全量扫描约 17 秒，建好后缓存。
2. **文件名与列名真实存在。**
3. 不合规的规则打回重写，最多两次；仍不合规则丢弃。

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

- **分组键**：按规则涉及的 `(file, column)` 集合分组。`column` 范围的规则就是单个 `(file, column)`；
  `multi_column`、`cross_table` 为多个；`file` 为 `(file, all)`；`generic` 规则单独成一组。
- **每组一次 LLM 调用**，reviewer 就是合并这一步本身：
  1. 合并语义重复的规则；
  2. 只对单个值成立的规则，上提成列级规则，或丢弃；
  3. 检查 `EXAMPLE_USAGE` 是否陈述了具体公司或政策的事实，是则改写。
- 输入每条规则的 `EVIDENCE` 摘要和来源任务数；输出合并后的规则、被丢弃的规则及理由。
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
  "columns": [
    {"file": "enterprise/company_operation_status.csv", "column": "bmCode"},
    {"file": "enterprise/company_profile.csv", "column": "bmCode"}
  ],
  "files": ["enterprise/company_operation_status.csv", "enterprise/company_profile.csv"],
  "ensure": "取某公司所属行业或名称时，先用 company_operation_status.bmCode 关联 company_profile.bmCode",
  "when_to_check": "题目同时涉及公司的经营指标与它的行业、名称、地区",
  "context": "company_operation_status 只有 bmCode，不含 bmCompanyName 和 industry",
  "example_usage": "……（具体值只能出现在这里）",
  "provenance": {
    "source_task_ids": ["task_012_…", "task_087_…"],
    "support_count": 2,
    "merged_from": ["dc-0042a", "dc-0042b"],
    "evidence": ["<反思 agent 的 probe 引用>"]
  },
  "task_categories": ["enterprise_industry_analysis"]
}
```

| `kind` | 含义 | `columns` |
| --- | --- | --- |
| `column` | 单列规则 | 一个 `(file, column)` |
| `multi_column` | 同一文件的多列规则 | 同一 `file` 的多个 `column` |
| `cross_table` | 跨表规则（关联键、标签对齐、多文件合并） | 来自多个 `file` |
| `file` | 关于整个文件的规则（例如浓度类指标的定义要查 `internal_metrics.csv`） | `column` 为 `all` |
| `generic` | 不绑定文件的通用规律 | 空 |

`kind` 与规则输出的 `SCOPE` 相同，写入前由 harness 按 `columns` 重新推出（规则见 6.2）。`files`
由 `columns` 推出，单独存储以便按文件检索。`task_categories` 是辅助信息，不参与首轮过滤。

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

**阶段 B：探查工具与反思 agent**

- probe 工具：在 `dataclaw:0.1.0` 容器里执行 shell 命令，数据只读挂载，输出截断，记录每次 probe 的
  编号与完整输出；独立于 LLM，单独测试；
- 反思循环：prompt、结构化输出解析、5.5 节四道硬闸门与 `SCOPE` 一致性检查、打回消息、轮数预算；
- 闸门逻辑用「假 LLM」（预设回复）测试，覆盖引用不存在的 probe、摘录与真实输出不符、复现值偏差超过
  1%、列名不存在、`SCOPE` 与 `COLUMNS` 不一致等情况。

退出条件：反思 prompt 定稿；在 3–5 个开发 run 上试跑，人工确认 divergence 有真实数据证据、没有只是
复述参考步骤。若质量不达标，先改 prompt，不进入阶段 C。

**阶段 C：规则生成**

- 第二阶段 prompt、`DATA_RULE` / `GENERIC_RULE` 解析、6.4 节自动检查与最多两次重写；
- 用假 LLM 测试检查与重写逻辑。

退出条件：规则生成 prompt 定稿；以阶段 B 的产物为输入试跑，人工确认规则是列级的、例子不陈述具体
实体事实。

**阶段 D：合并与存储**

- 按 `(file, column)` 集合分组、每组一次合并调用、保留 `provenance` 与支撑任务数；
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
| 10 | 合并去重 | 每组一次 LLM 调用；单列按 `(file, column)`、跨表按列集合分组；接受跨组残留重复 |
| 11 | 存储 | JSONL，`kind` 五类，`provenance` 记来源与支撑数 |
| 12 | B2 跨表返回时机 | 探查到任意一个涉及文件即返回 |
| 13 | 不采用 | operation tags；按列分类个体 / 标签值；固定禁用名单 |
| 14 | 范围 | `column` / `multi_column` / `file` / `cross_table` / `generic` 五类，在 divergence、规则、存储中显式记录；harness 按 `COLUMNS` 推出并校验 |
| 15 | 硬闸门 | 证据须为真实 probe 输出的子串；列名须存在；复现值由 harness 比对；`non_data`、`gold_suspect` 不进第二阶段 |
| 16 | 规则与工具 | 规则描述要检查的数据特性，不写具体库或命令调用 |
| 17 | 开发顺序 | 先在历史轨迹开发集上开发阶段 A–D，产物合理后再定 split、跑全量 train 与全量 populate |
| 18 | 开发集 | 归档中 33 个判错的裸 glm-5.2 run（27 道题），拷贝到 `data/dataclaw_dev/runs/`（git 忽略），`manifest.csv` 提交 |
| 19 | 开发集与 split | 互不约束；正式实验重新跑 train 并重新 populate，开发期规则不进入最终 store |
| 20 | 字符串 milestone | 规整后相等即一致，否则由反思 agent 做语义判断（prompt 提示「不要求逐字一致，但语义须几乎完全一致」）；数值部分仍由 harness 判定 |
| 21 | 正文取值检查 | 纯数字也算单元格取值；与文件名、列名相同的取值除外 |
| 22 | 代码位置 | `tkstore/dataclaw/` 子包，测试为 `tests/test_dataclaw_*.py` |

### 待定

- 三份 prompt（反思、规则生成、合并）的最终文本（第 5.3、6.2 节为草稿；合并 prompt 尚未起草），
  **必须逐条确认**；
- train 占 492 道题的比例；
- 反思 agent 的轮数预算与每次 probe 输出的截断长度；
- 反思 agent、规则生成、合并三处使用的模型；
- 轨迹压缩的具体方式（保留哪些工具输出、截断到多长）；
- 同一 train task 若有多个裸 run，取哪一个（首版每 task 只跑一次，暂不涉及）。
