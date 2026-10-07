# TK-Boost knowledge population 适配 DataClaw：设计记录

本文记录把 TK-Boost 的 knowledge population（从失败轨迹生成规则、存成 knowledge store）适配到
DataClaw 的具体方案。它细化 [`dataclaw.md`](./dataclaw.md) 的 5.3、5.4 节；DataClaw 的环境、
runner、评测设计、与 `~/baseline` 的关系仍以 `dataclaw.md` 为准。

状态：**阶段 A 已完成**（代码在 `tkstore/dataclaw/`，完成情况见 9.3）；阶段 B 的步骤 1–5 已实现并通过
测试，步骤 6 等待反思 prompt 人工确认与反思模型选定（9.3）。开发集已拷贝
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
| 网络 | 探查容器断网（`--network none`），反思 agent 只能看 `./database/`；重放 agent 的上网命令会直接失败 |
| 验证终止条件 | 必须从数据复现该 divergence 影响到的 milestone 值；只能用 `process_score.json` 中 `achieved=false` 的 milestone；只有验证通过的 divergence 进入第二阶段 |
| 非数据类错误 | 跳过，不产规则（算术失误、输出格式、提前放弃、judge 与 gold 分歧等） |
| 预算 | 每个 run 最多 20 次 probe；最多提交 3 次 `<final>`（即最多打回 2 次）；每次 probe 超时 60 秒；发给 LLM 的 probe 输出截断到 4000 字符、保留头尾，harness 保留完整输出供闸门核对。均为参数，试跑后可调 |
| 模型 | 待定；要求上下文至少 1M token。压缩后最长的轨迹为 215,865 字符，还要加上多轮 probe 输出 |

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
CATEGORY: <第 11.2 节的类别名，或「其他」>
EVIDENCE: probe#<n> → <关键输出摘录>
REPRODUCED: milestone "<key>" = <JSON 值> FROM probe#<m>     # 可有多行
SEMANTIC_MATCH: <理由>                                       # 字符串值与 gold 不逐字一致时必填，紧跟对应的 REPRODUCED 行
KIND: data | non_data | gold_suspect
```

`REPRODUCED` 写成机器可读的形式，由 harness 解析：`<key>` 须是 gold milestone 的 key；`<JSON 值>` 照抄
probe 输出里的写法（数值写数字，字符串写数据里的原值，例如 `"广东省"`）；字典类 milestone 用 gold 的
key 报告。`CATEGORY` 不参与闸门，只用于统计开发集上各类的分布、方便调 prompt；prompt 里写明类别只是
提示，归不进去就写「其他」。

每一轮反思 agent 只能二选一：`PLAN` 加一个 `<probe>...</probe>`，或者一个 `<final>`。`<final>` 里放若干
上述 divergence 块，也可以声明没有数据类 divergence。格式错误本身也作为打回理由发回。

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
3. **复现值由 harness 比对。** 依次检查：
   - `<key>` 是 gold milestone 的 key（空白规整后精确匹配），且该 milestone 在 `process_score.json` 中
     `achieved=false`。只允许用 agent 没做到的 milestone，防止复现一个 agent 本来就做对的简单 milestone
     来凑数；
   - 所写的值出现在所引 probe 的输出里：标量要求其写法在空白规整后是输出的子串；列表要求每个元素都
     出现；字典要求每个值都出现（key 是 gold 的英文 key，不要求出现）；
   - 再与 gold milestone 比较：
   - 数值：沿用 DataClaw `dataclaw/utils/process_grading.py` 的 `_numbers_match`，相对误差 1%
     （`NUMERIC_REL_TOL = 0.01`），与 process 评分口径一致；
   - 列表：规整后按集合比较；
   - 字典：逐 key 比较，例如 `task_054` 的「行业 → 企业数」映射。
   - 字符串：规整空白与大小写后相等即一致；否则交给反思 agent 做**语义判断**。gold 字符串是英文
     （`Guangdong Province`），数据是中文（`广东省`），中英对照 JSON 只覆盖公司名与政策名（开发集
     36 个字符串 milestone 只查到 4 个），逐字比较不可行。反思 prompt 写明：字符串不要求逐字一致，
     但语义须几乎完全一致，并要求写出判断。harness 仍校验所引原值确实出现在所引 probe 的输出里。

   数值、列表中的数值元素、字典中的数值由 harness 判定，agent 的自述不算数；只有字符串部分采纳反思
   agent 的语义判断，且要求对应的 `REPRODUCED` 行后面跟有 `SEMANTIC_MATCH`。阶段 A 的
   `milestones.compare` 对这种情况返回 `semantic`。
4. **按类型过滤。** `KIND: non_data`（算术失误、输出格式、提前放弃等）与 `KIND: gold_suspect`（gold
   本身可疑，例如 `task_054` 题面要求答 yes/no、gold 却是 `416`）不进第二阶段，只写日志。`KIND` 由
   反思 agent 自标，但标为 `data` 的必须同时通过闸门 1–3，把非数据错误冒充为数据错误过不了关。

另有一项一致性检查：harness 按 `TABLES` 与 `COLUMNS` 推出范围（规则见 6.2），与反思 agent 写的
`SCOPE` 不一致时打回。

### 5.6 修订：规则生成并入反思（阶段 C 试跑之后）

**为什么改。** 阶段 C 按第 6 节把规则生成做成独立的单次 LLM 调用，只看已验证的 divergence。在 5 条
divergence 上试跑（见 9.3 节「阶段 C 完成情况」），5 条都通过了全部检查和通用性判断，但人工检查有 4 条
不合格。逐条看问题出在哪一步：

- **问题大多在 divergence 里就已存在。** `task_011 #1` 的 `NEEDED` 把 gold 的纳入口径写成了普遍要求
  （题面只说 enterprise microdata，gold 只算中国交易所的公司）；`task_185`、`task_231` 的 `FACT` 里已经
  写了公司名、省份和 milestone 值，规则生成只是把它们搬进了例子。原因是反思 prompt 只约束了字段格式，
  没有约束内容：`NEEDED` 只写 “what it should have done”，没说是这道题该怎么做还是这类题普遍该怎么做、
  依据是什么；`FACT` 只写 “one fact about the data”，没区分列级性质和本题实体上的现象。
- **推广这一步没有数据验证。** `task_231` 的规则写「两个文件对同一概念用不同的 targetName」，实测两个
  文件的 75 个「中位数」类指标名完全相同，名字不同是因为同一指标有多种写法、每行只用其中一种（R5 的
  事实）。不探库的单次调用无法发现这种过度推广。

看到完整上下文并不能解决这些问题：`task_011 #1` 的错误就出在看得到完整上下文的反思 agent 手里，而完整
上下文里还有 gold 答案，泄漏只会更多。真正缺的是在推广时能用 probe 检验，所以把规则生成并入反思，由
反思 agent 在同一会话里写出规则并用 probe 支撑其通用性。

**新的 divergence 块。** 在 5.4 节的格式上修改和增加字段：

```text
DIVERGENCE: <agent 在哪一步（CALL #n）做了什么；只写 agent 的行为，不写正确的值>
NEEDED: <这道题本该怎么做>
BASIS: data | task | gold_only
BASIS_QUOTE: <题面原句片段；仅 BASIS: task 时必填>
INSTANCE: <本题实体上观察到的现象，可以含实体名和数值；只用于验证，不进规则>
MISSING_DATA_UNDERSTANDING:
  SCOPE / TABLES / COLUMNS: <只列这条事实涉及的文件和列，不列题目问到的其他属性>
  FACT: <列级的性质：换成同一列的其他取值仍然成立；不写具体实体>
CATEGORY / EVIDENCE / REPRODUCED / SEMANTIC_MATCH / KIND: <同 5.4 节>
GENERALITY: probe#<k> → <摘录；说明这条性质不只在本题的实体上成立，例如对其他行业、其他行做的统计>
ENSURE / WHEN_TO_CHECK / TRIGGER / CONTEXT / EXAMPLE_USAGE: <规则字段，要求同 6.2–6.5 节>
```

- `GENERALITY` 对 `KIND: data` 必填，可有多行。
- 规则的文件和列就是这条 divergence 的 `TABLES`、`COLUMNS`，不再单独写，也不再需要「规则引用只能比
  divergence 收窄」的检查。
- 每条 `data` divergence 只产出 1 条 `DATA_RULE`，不产出 `GENERIC_RULE`；规则正文用英文。
- `non_data`、`gold_suspect` 块仍只需 `DIVERGENCE`、`NEEDED`、`KIND`。`non_data` 的定义暂不修改
  （阶段 C 试跑中 `task_011 #2` 写反换算系数、错在中间步骤，算不算 `non_data` 边界不好定，待定）。

**`BASIS` 的定义与对规则的约束。** prompt 要写明三项的定义和例子，并在规则字段的要求里说明 `BASIS`
决定了规则能写成什么样。例子用虚构的实体，不照抄开发集实测到的事实。草稿：

```text
BASIS says who requires what NEEDED describes. Pick exactly one:

- data: the data forces it, whatever the question says. Without it the correct
  values cannot be obtained.
  e.g. The task names "Hua Xin Tech Co., Ltd." but bmCompanyName only holds
  Chinese names, so the name must first be looked up in the translation file.
  e.g. Rows of one indicator carry different targetUnit values, so each row must
  be converted by its own unit before summing.

- task: the wording of the question decides it; a differently worded question
  would need a different action. Quote the words on a BASIS_QUOTE line, copied
  verbatim from the task.
  e.g. The task asks for the figure "in Shanghai", so the provincial file must be
  used rather than the national one.
  BASIS_QUOTE: in Shanghai

- gold_only: neither the data nor the question forces it; the reference answer
  simply made this choice. Another task's reference may choose differently.
  e.g. The task counts "enterprises in the industry" without restricting listing
  venue, and the reference counts only companies on domestic exchanges.

If you are unsure between task and gold_only, ask: would a careful analyst who
reads only the question make the same choice? If not, it is gold_only.

How BASIS shapes the rule:

- data: ENSURE may state a fixed action ("look the English name up in the
  translation file first").
- task: ENSURE states the action conditioned on the question ("when the question
  names a province, ..."), and WHEN_TO_CHECK describes that signal in the
  question.
- gold_only: ENSURE must NOT state the reference's choice as a fixed action. It
  says which dimension has to be decided and that it must be decided from the
  question ("check the distribution of exchange and restrict the population only
  as the question states"). The reference's choice may appear only in
  EXAMPLE_USAGE, explicitly marked as one task's convention.
```

**闸门，按顺序：**

1. 5.5 节的全部闸门，以及 `SCOPE` 一致性检查；
2. `KIND: data` 时的必填项：`NEEDED`、`INSTANCE` 和规则字段 `ENSURE`、`WHEN_TO_CHECK`、`CONTEXT`、
   `EXAMPLE_USAGE`（`TRIGGER` 保留但不校验），以及 6.4 节的正文取值检查，范围仍是 `ENSURE`、
   `WHEN_TO_CHECK`、`CONTEXT`；`FACT` 不做取值检查；不设「不能出现 gold 值」的检查。`KIND: data` 的全部
   必填项因此是 `SCOPE`、`FACT`、`EVIDENCE`、`REPRODUCED`、`NEEDED`、`BASIS`、`INSTANCE`、`GENERALITY`、
   `ENSURE`、`WHEN_TO_CHECK`、`CONTEXT`、`EXAMPLE_USAGE`；`non_data` 块不需要新字段；
3. `BASIS` 必须是三者之一；`BASIS: task` 时 `BASIS_QUOTE` 须在规整空白后是题面的子串；
4. `GENERALITY` 至少一行，所引 probe 存在，摘录是其输出的子串（同 `EVIDENCE` 的检查）；
5. **通用性判断**：以上都通过后，对每条 divergence 调一次 LLM，输入 divergence 的各字段（含 `BASIS`）和
   规则，判断规则是否只对个别实体成立、`gold_only` 的规则是否把 gold 的选择写成了固定动作。判为不合格时，
   按闸门打回的方式把理由发回反思 agent，占用一次 `<final>` 额度，由它补充 probe 或改写。

**通用性判断的实现**（`tkstore/dataclaw/reflector.py`，prompt 草稿为
`tkstore/dataclaw/prompts/reflector_judge.md`）：

- 只判两点：规则是否只对个别实体成立（包括 `GENERALITY` probe 只在本题实体上显示了该性质）；`BASIS` 为
  `gold_only` 时 `ENSURE` 是否把 gold 的选择写成了固定动作。
- 输入：divergence 的各字段与规则字段，以及每个 `GENERALITY` probe 的命令和输出（输出截断到 4000 字符、
  保留头尾）；不给题面、轨迹和 gold。（5.7 节改为加题面、gold 与全部 probe。）
- 回复格式为两行 `VERDICT: accept | reject` 与 `REASON: <一句话>`。`reject` 时该 divergence 记为打回，
  理由以 `GENERALITY JUDGE: <REASON>` 出现在 `HARNESS VERDICT` 里。
- 回复为空或找不到 `VERDICT` 行时重试一次；仍失败则记为 `judge_error`：不接受、不打回、不计入被打回的
  divergence，写进输出记录的 `judge_errors`，留待人工查看。
- 默认用反思 agent 的同一个模型；`scripts/dataclaw_reflect.py --judge-model` 可另指定。每次判断调用都记入
  `llm_calls`，用 `stage: "judge"`（修订二起改名 `judge_generality`，见 5.7 节）与反思调用（`stage: "reflect"`）区分；判断调用不占 `max_turns`。
- 同一 `<final>` 里有多条 divergence 通过确定性闸门时，每条各调一次判断；与已接受的 divergence 重复
  （`FACT`、`TABLES`、`COLUMNS` 相同）的不再判断。

harness 查不了 `GENERALITY` 的 probe 是否真的证明了通用性、`gold_only` 的规则是否真的写成了「按题目
确定」，这两点交给第 5 道的通用性判断。例子里泄漏实体事实和 gold 值的问题，目前只靠 prompt 约束
（`INSTANCE` 与 `FACT` 分开写、6.5 节对例子的要求）；通用性判断目前不检查例子（决策 48），修订后试跑
若泄漏仍常见再考虑。

**预算不变**：每个 run 最多 20 次 probe、3 次 `<final>`，试跑后再看是否需要调整。（`<final>` 上限已由
5.7 节改为默认 5 次。）

### 5.7 修订二：引用方式、两个判官与预算（`gpt51_low_sample` 试跑之后，待实施）

**试跑与发现。** 用 GPT-5.1、`--reasoning-effort low` 在 6 条开发集轨迹（`task_009`、`task_218`、
`task_352`、`task_383`、`task_464`、`task_477`）上跑反思，产物在
`tmp/dataclaw_dev/reflect/gpt51_low_sample/`。`task_009`、`task_218`、`task_352`、`task_383` 各接受 1 条
（`task_352` 另记 1 条 `gold_suspect`）；`task_464` 被打回后改交 `NO_DATA_DIVERGENCE`，没有产出；
`task_477` 3 次 `<final>` 都被打回，以 `final_budget` 结束。逐条对照 probe 输出与 gold，发现：

- **复现靠数字巧合通过（`task_383`）。** gold 的「有政策省份前20%企业数（家）」是 5；probe 打印的 5 是
  有政策省份（天津市、湖南省）的有效企业总数，按 gold 的 `steps` 这一组应有 21 家、前 20% 才是 5 家。
  `SEMANTIC_MATCH` 里的「ceil(21×0.2)=5」中的 21 是从 gold 的 `steps` 抄来的，probe 里没有。harness 只
  检查值是否出现在整段 probe 输出的任意位置、与 gold 是否相等，查不出两者是不是同一个量；小整数在输出里
  几乎总能找到。
- **把 gold 的口径选择标成 `BASIS: data`（`task_218`、`task_352`、`task_383`、`task_477`）。** probe 只证明
  两个数据源或两个 `targetName` 的结果不同，`ENSURE` 却写成「用 A，不用 B」。`task_352` 最明显：两个
  `targetName` 只差一个「和」字，题面是英文，两者与题面一样匹配，`NEEDED` 是按 gold 倒推的。通用性判断
  的第 2 条只在 `BASIS: gold_only` 时生效，而且判断看不到题面和 gold，于是都放过了。
- **正文取值检查让规则说不清用哪个指标。** `ENSURE` 不能写出 `targetName` 的取值，只能写
  「the appropriate enterprise-count targetName」「one specific revenue-type targetName」。
- **`task_477` 三次全是格式错误。** 第 1 次一个 probe 都没跑，就引用 `probe#32`、`probe#25`，内容实为
  被分析 agent 轨迹里的 `OUTPUT #32`、`OUTPUT #25`（轨迹与 probe 结果都用「#数字」编号）；第 2、3 次给
  摘录两端加了双引号，与 probe 输出对不上；第 1 次与 `task_464` 都因 `SCOPE` 与 `TABLES`、`COLUMNS` 推出
  的范围不一致被打回。另外报出的单元格取值「202」只是「2022」的子串。
- **`task_464` 被打回后放弃。** 20 次 probe 只用了 2 次，却以「不再跑 probe 就无法修正」为由改交
  `NO_DATA_DIVERGENCE`，记录里的结束原因是 `done`。打回消息只写「Resubmit a `<final>`」和剩余的
  `<final>` 次数，没提 probe 还能用。

根源是协议把 harness 能自己完成的记账交给模型（逐字抄输出、记 probe 编号、填 `SCOPE`），记错时与实质
错误一样扣 `<final>` 次数；判断复现与口径又缺少题面和 gold。修改如下。

**一、引用与结构（解决 `task_477`、`task_464` 暴露的问题）**

1. **行号指针。** `PROBE_RESULT` 给每行加行号，行号按完整输出编；发给反思 agent 的截断视图标明省略了
   哪几行。`EVIDENCE`、`GENERALITY` 写成 `P<n>:L<a>` 或 `P<n>:L<a>-L<b>`，`REPRODUCED` 写成
   `milestone "<key>" = <JSON 值> FROM P<n>:L<a>`。harness 检查所指 probe 和行存在、且在反思 agent 看到的
   视图里，再把原文回填进记录与判官的输入。`REPRODUCED` 的值须出现在所指的行里，不再是整段输出里。
   加引号、空白差异、长数字抄错、编造输出都不会再出现，不必单独做「去掉外层引号」的容错。
2. **probe 编号改为 `P<n>`**，与轨迹的 `CALL #n`、`OUTPUT #n` 区分；指向不存在的 probe 时，退回理由列出
   已运行的 probe，并说明 `P<n>` 只编号反思 agent 自己的 `<probe>`。
3. **`SCOPE` 由 harness 推出。** 不再要求反思 agent 填写，按 `TABLES`、`COLUMNS` 用 `derive_scope` 推出
   后写入记录。
4. **结构错误立即退回，不扣 `<final>` 次数**，另设单独的上限（参数）。结构错误指：无法解析、必填字段缺失、
   指针指向不存在或看不到的 probe 行、`REPRODUCED` 的值不在所指行里、`REPRODUCED` 的 key 不是 gold
   milestone、文件或列不存在、`BASIS` 不是三者之一、`BASIS_QUOTE` 不在题面里、还没跑过任何 probe 就提交
   `data` divergence。只有实质错误扣 `<final>`：值与 gold 不符、milestone 已被 agent 达成、复现判官或
   通用性判官拒绝。
5. **打回消息写明剩余的 probe 次数与 `<final>` 次数**，并说明可以先跑 probe 再重交。
6. **新增结束原因 `abandoned_after_reject`。** 本次 run 之前有 divergence 被打回，之后提交的 `<final>`
   只含 `NO_DATA_DIVERGENCE`（没有任何 `data` divergence）时，`stop_reason` 记为
   `abandoned_after_reject`，不再记为 `done`，便于统计。
7. **`<final>` 上限默认改为 5 次**（`scripts/dataclaw_reflect.py --max-finals` 与 `ReflectorConfig` 的默认
   值）。格式错误不再扣次数，但同一条 divergence 可能先后被确定性检查、复现判官、通用性判官各退回一次。

**二、取消正文取值检查。** `ENSURE`、`WHEN_TO_CHECK`、`CONTEXT` 不再检查单元格取值（取代 6.3、6.4 节的
取值检查与决策 7、21、44 的相应部分）。`Catalog` 只需保留表头，供文件与列的存在性检查；反思 prompt 删掉
「不要写取值、取值移到 `EXAMPLE_USAGE`」的要求。防止规则过于针对具体取值，改由通用性判官负责（见下），
后续试跑重点检查这一风险。

**三、通用性判官：扩充输入与判断。**

- 输入在 divergence 与规则字段之外，加题面（含 Output guidelines）、gold 的 `answer`、`steps`、`milestone`，
  以及全部 probe 的代码与完整输出（6 条试跑的 probe 输出合计只有 229–4,329 字符），不再只给 `GENERALITY`
  引用的 probe。
- 第 1 条改为可操作的测试：把规则正文里来自题面或本题实体的每个取值（公司、行业、年份、阈值等）换成同一列
  的其他取值，规则是否仍成立、仍说得通；不成立即拒绝。可以点名某个指标（`targetName` 的取值），但整条
  规则只陈述某一个指标自身的事实时仍拒绝。
- 第 2 条不再只看反思 agent 标的 `BASIS`：`ENSURE` 规定的做法能在 gold 的 `steps` 里找到，而 probe 只证明
  了「两种做法结果不同」，没有证明数据逼出了这种做法时，拒绝，并要求改标 `gold_only` 或按 `gold_only` 的
  要求改写。

**四、新增复现判官。** 确定性检查之后、通用性判官之前，对每条 `data` divergence 调一次 LLM，判断每条
`REPRODUCED` 引用的行算的量是否就是 milestone 所指的量。

- 输入：题面（含 Output guidelines）；gold 的 `answer`、完整 `milestone` 字典（标出本条声称复现的 key）与
  `steps`（milestone 没有单独的说明字段，492 个 gold 文件都只有 `id`、`question`、`guidelines`、`answer`、
  `metadata`、`steps`、`steps_num`、`milestone`，`steps` 就是各 milestone 的来历）；process judge 对各
  milestone 的 `details`；每条 `REPRODUCED` 的 key、gold 值、声称的值与回填的引用行；`SEMANTIC_MATCH`；
  `DIVERGENCE`、`NEEDED`、`INSTANCE`；全部 probe 的代码与完整输出。被分析 agent 的轨迹默认不给
  （6 条试跑为 38,000–224,000 字符），复现判断用不到。
- probe 代码是关键输入：输出里的标签是反思 agent 自己写的，不能当作证据，只有代码说明这个数是怎么算的。
- prompt 写明：只有 probe 输出能支撑复现，取自 gold `steps` 或题面的数字不算。
- 回复对每条 `REPRODUCED` 先写两行描述再下结论：`PROBE_QUANTITY`（probe 代码实际算的是什么：哪些行、
  什么筛选、什么聚合）、`MILESTONE_QUANTITY`（按题面与 `steps`，这个 milestone 算的是什么），然后
  `VERDICT: match | mismatch` 与 `REASON`。任一条 `mismatch` 即打回，理由以
  `REPRODUCTION JUDGE: <REASON>` 发回，扣一次 `<final>`；无法解析时的处理同通用性判官（重试一次，仍失败
  记为 `judge_error`）。判官调用在 `llm_calls` 里用不同的 `stage` 区分。
- 确定性比较保留为前置过滤：值与 gold 按现有规则相等（数值 1%）、值在引用行里、milestone 未被达成。
- `SEMANTIC_MATCH` 不再是闸门要求的字段，只是反思 agent 给复现判官的说明；文本 milestone 是否语义一致
  也交给复现判官（取代决策 20 中「由反思 agent 做语义判断」）。

**修订后的流程。** 规则始终由反思 agent 写，两个判官只给 accept / reject 和一句理由，不改写规则。

1. 反思 agent 拿到题面、压缩轨迹、outcome 与 process judge 的结论、gold 答案与 `steps`、未达成的
   milestone、全库表头；
2. 跑 probe（`P1`、`P2`……，输出带行号），最多 20 次；
3. 提交 `<final>`，每条 `data` divergence 带规则字段，引用都是行号指针；
4. 结构检查：不通过立即退回，不扣 `<final>`；
5. 确定性实质检查：不通过退回，扣一次 `<final>`；
6. 复现判官：`mismatch` 退回，扣一次 `<final>`；
7. 通用性判官：`reject` 退回，扣一次 `<final>`；
8. 全部通过的 divergence 连同规则入库。

第 4–7 步退回时，消息写明剩余 probe 与 `<final>` 次数；反思 agent 可以先补跑 probe，再提交只含被退回
条目的 `<final>`，已通过的保留、不再判断。`gold_suspect`、`non_data` 只记录，不进判官。

**实施顺序。** 先做「一」（改了 `REPRODUCED` 的格式，复现判官要用），再做「二」和「三」，最后做「四」；
每项单独走一次 TDD 循环。改完后在这 6 条与 `task_011`、`task_231` 上重跑，统计以 `final_budget` 和
`abandoned_after_reject` 结束的比例，人工检查规则是否过于针对具体取值。

### 5.8 修订三：引用来源与先探查（train_a 抽样 20 个样本试跑之后）

**试跑与发现。** 在 train_a 抽样的 20 个样本上跑反思（GPT-5.1，`reasoning_effort: low`），10 个样本没有
产出规则。逐条阅读其中被打回的 `<final>` 与 probe 记录，发现反思 agent 多次引用自己没跑过的 probe：

- **把 agent 的命令编号当成 probe 编号（`task_464`）。** 只跑了 2 次 probe，却引用 `P21`、`P25`、`P27`，
  这些编号对应被分析 agent 轨迹里的第 21、25、27 条命令。轨迹当时用 `CALL #n`、`OUTPUT #n` 编号，
  probe 用 `P<n>`，两者都是从 1 开始的整数，容易混用。
- **预先引用下一次 probe，并填入 gold 值（`task_453`、`task_346`）。** `<final>` 里引用的 probe 编号
  比已运行的最大编号大，`REPRODUCED` 的值与 gold milestone 相同，但没有任何 probe 输出打印过它。
  `task_346` 还把 gold 的英文写法当作取值，而 probe 输出里是中文。
- **一次 probe 都没跑就提交 `<final>`（`task_377`、`task_464`）。** §5.7 第 4 条只把零探查时提交 `data`
  divergence 当作结构错误，`non_data`、`gold_suspect` 与 `NO_DATA_DIVERGENCE` 不受限制。

原 harness 对这些情况只回复「probe never run」或「point at the line that prints the value」，全部算作格式
重试，不扣 `<final>`；抄写 gold 值因此没有代价，退回消息也没有指出该引用哪一行。

**改动。**

1. **agent 命令改用 `A<n>` 编号。** 压缩轨迹写成 `AGENT CMD A<n> <name>: <cmd>` 与 `AGENT OUTPUT A<n>:`，
   轨迹小标题注明 `A<n>` 不能引用。解析层遇到 `A27:L8` 这类指针时报专门的错误，要求把命令作为 probe
   重跑后引用新的 `P<n>`。`PROBE_RESULT` 表头列出已运行的 probe，例如
   `PROBE_RESULT P3 (exit 0; probes you have run: P1-P3):`。
2. **harness 按错误根源反馈。** `REPRODUCED` 的值不在所引用的行里时，harness 在全部已运行 probe 的可见行里
   查找这个值：
   - 找到时，退回消息给出所在位置（如 `it appears at P2:L7`），仍算结构错误；
   - 找不到、值不是字符串、且与 gold 相等时，判为「值取自 gold」，算实质错误，扣一次 `<final>`；
   - 值是字符串时，仍算结构错误，消息附上所引用行的原文，提示按数据原文书写并加 `SEMANTIC_MATCH`；
   - 其余情况为结构错误，消息说明没有任何已看到的 probe 输出打印过这个值。

   引用的 `P<n>` 超出已运行范围、但不超过 agent 命令总数时，退回消息补一句 `A<n>` 是 agent 的命令、
   不能引用。
3. **任何 `<final>` 提交前都必须先探查。** 零探查时提交的 `<final>`，不论包含 `data`、`non_data`、
   `gold_suspect` 还是只有 `NO_DATA_DIVERGENCE`，都不进入评审，按格式错误退回并计入格式重试；达到格式
   重试上限时以 `format_budget` 结束。
4. **prompt 补硬性规则。** `reflector.md` 在 How to work 里写明先探查的要求；在 Output 前新增 Hard rules：
   只引用已收到的 `PROBE_RESULT`，`A<n>` 不能引用；`REPRODUCED` 的值只能抄自看到过的 probe 输出，不能
   取自 gold；未打印却等于 gold 的值会被拒并扣 `<final>`；字符串按数据原文书写并加 `SEMANTIC_MATCH`。
   模板里 `EVIDENCE`、`GENERALITY`、`REPRODUCED` 后加注释，What the harness checks 同步补充。

本节取代 §5.7 第 2 条中「轨迹用 `CALL #n`、`OUTPUT #n`」的说法，并把 §5.7 第 4 条中「还没跑过任何 probe
就提交 `data` divergence」扩大到所有类型的 `<final>`。「值取自 gold」从结构错误改为实质错误，也修改了
§5.7 第 4 条对结构错误的列举。

**补丁：CSV 行里的数字。** 重跑后发现，harness 从输出行提取数字时把「逗号后跟三位数字」一律当作千位分隔，
`task_432` 的探查行 `364,263,86` 被读成 364263 和 86，正确复现的 263 因此被判为不在行里，并被当作取自 gold。
现在含逗号的数字同时保留两种读法：整串（`1,004` 读作 1004）与按逗号拆开的各段（`364,263` 读作 364 和 263）；
以 0 开头的多位段（如 `1,004` 里的 `004`）只可能是千位分隔的一部分，不单独算作一个数。

**不在本次范围。** `reasoning_effort` 的调整单独测；被打回后过早放弃（`abandoned_after_reject`、格式预算
耗尽）先观察是否由 low 推理强度造成，暂不处理；不新增 `<replay>A<n></replay>` 动作。改完后在相同的
20 个样本上以 low 重跑，对比引用未运行 probe 的次数、「值取自 gold」被拒次数、零探查 `<final>` 被拒次数、
结构性拒绝次数、接受的规则数与 `stop_reason` 分布。

---

## 6. 第二阶段：生成规则

> **修订**：阶段 C 试跑之后，规则生成并入反思阶段，不再是独立的 LLM 调用（见 5.6 节）。6.1 节的输入
> 约定不再适用；6.2–6.5 节对规则字段、粒度、自动检查和例子的要求仍然适用，由反思 agent 在 `<final>`
> 里写出规则，由反思闸门检查。

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

实现前核实的基础设施事实（读 DataClaw 代码与本机检查得出）：

- `dataclaw:0.1.0` 镜像已在本机，`docker` 可用。
- DataClaw 不挂载数据：`dataclaw/utils/docker_utils.py` 的 `setup_workspace` 用 `docker cp` 把 19 个文件
  拷进 `/tmp_workspace/database/`，再把 `/root/.openclaw/workspace` 软链接到 `/tmp_workspace`。agent 的
  命令写作 `./database/...` 或 `cd ./database && ...`，所以探查容器必须复刻这套目录结构，才能原样重放。
- TK-Boost 现有的 LLM 调用是 `litellm.completion`，读 `TK-Boost/.env` 的 `OPENAI_API_KEY`、
  `OPENAI_API_BASE`，默认模型 `TKBOOST_MODEL=gpt-4.1`。被测 agent 是 `glm-5.2`，走 DataClaw 的自定义
  网关；DataClaw 给它配置的上下文是 `OPENCLAW_MODEL_CONTEXT_WINDOW` 默认的 128000（是否为模型本身
  的上限未核实）。

实现步骤。为避免与评测臂 B0/B1/B2 重名，下面称「B 步骤 1–6」。除真起容器、真调 LLM 的两类测试
默认跳过外，都按 TDD 做：

1. **`scope.py` 改成 `TABLES` + `COLUMNS`**（决策 23）。新接口 `parse_tables`、不再接受 `.all` 的
   `parse_columns`、`derive_scope(tables, columns)`、`validate_refs(tables, columns, catalog)`、
   `check_scope_consistency(declared, tables, columns)`；同一文件既在 `TABLES` 又点了其中的列时判为
   `file`，不再报错；`check_body` 不变。
2. **probe 工具 `tkstore/dataclaw/probe.py`。** 每个反思会话起一个短命容器：
   `docker run -d --rm --network none -v ~/DataClaw/assets/database:/tmp_workspace/database:ro
   -w /tmp_workspace dataclaw:0.1.0 tail -f /dev/null`，再建软链接 `/root/.openclaw/workspace →
   /tmp_workspace`，会话结束删除容器。只读挂载代替拷贝 250 MB；agent 在工作目录写临时脚本不受影响。
   `run(command)` 用 `docker exec bash -c` 执行，合并 stdout 与 stderr，返回编号、完整输出、退出码、
   是否超时；发给 LLM 的是截断版。执行器可替换：单元测试用假执行器，另有一个真起容器的冒烟测试，
   默认跳过，设环境变量才跑。
3. **输出解析 `tkstore/dataclaw/reflector_io.py`。** 解析每轮的 `PLAN` + `<probe>` 或 `<final>`，以及
   `<final>` 中 5.4 节格式的 divergence 块（含 `TABLES`、`COLUMNS`、`CATEGORY`、多行 `REPRODUCED`、
   `SEMANTIC_MATCH`）。
4. **硬闸门 `tkstore/dataclaw/gates.py`。** 纯函数：输入 probe 记录、数据目录、gold 与
   `process_score.json`，输出每条 divergence 的通过或打回及理由。检查项为 5.5 节的四道闸门与 `SCOPE`
   一致性；逐条判定，通过的保留，打回的发回修改，轮数用完仍未通过的丢弃。
5. **反思循环 `tkstore/dataclaw/reflector.py`，prompt 放在 `tkstore/dataclaw/prompts/reflector.md`。**
   按 5.2 节组装输入：task prompt、压缩轨迹、judge `notes`、`process_score.json` 逐 milestone 的
   `achieved` 与 `reason`、gold 的 `answer`/`milestone`/`steps`、19 个文件的表头。LLM 调用以函数注入，
   默认实现 litellm；模型名只是参数，所以步骤 1–5 不依赖模型的选择。闸门与循环用假 LLM（预设回复）
   测试，覆盖引用不存在的 probe、摘录与真实输出不符、复现值偏差超过 1%、引用 `achieved=true` 的
   milestone、字符串缺 `SEMANTIC_MATCH`、文件或列名不存在、`SCOPE` 与推出的范围不一致、轮数用完等
   情况。每个 run 写一个 JSON 记录（通过的 divergence、打回的及理由、完整 probe 记录、轮数与 token
   用量）到 git 忽略的目录。
6. **prompt 定稿与试跑。** 先按 5.3 节草稿、11.2 节问题清单、11.3 节 `non_data` 例子写出完整的反思
   prompt，交人工逐条确认；定下反思模型后，在 5 个开发 run 上真实试跑：
   - `task_049`：同一指标有多个名字、单位不同、按文本排序；
   - `task_218`：有汇总表却从明细聚合，另有回答了错误的对象；
   - `task_195`：实体名称对不上，疑似 `gold_suspect`；
   - `task_206`：应判为 `non_data`（减法方向错）；
   - `task_011`：单位差 10 倍，香港的纳入口径。

   这 5 题覆盖了 data、`non_data`、`gold_suspect` 三种结论，用来检查反思 agent 能否分清。

退出条件：反思 prompt 定稿；上述 5 个开发 run 试跑后，人工确认 divergence 有真实数据证据、没有只是
复述参考步骤、`non_data` 与 `gold_suspect` 分得出。若质量不达标，先改 prompt，不进入阶段 C。

**阶段 B 完成情况（步骤 1–5）**：

- 代码：`scope.py`（改为 `TABLES` + `COLUMNS`）、`probe.py`、`reflector_io.py`、`gates.py`、`reflector.py`，
  反思 prompt 草稿在 `tkstore/dataclaw/prompts/reflector.md`，试跑脚本 `scripts/dataclaw_reflect.py`
  （`--dry-run` 只写出反思 agent 收到的消息，不调 LLM、不起容器）。
- 测试：DataClaw 相关 158 个通过；真起容器的冒烟测试默认跳过，设 `DATACLAW_DOCKER_TESTS=1` 时通过。
- 实测：镜像内有 `timeout`、`python3`、`awk`；只读挂载下写 `./database/` 报 Read-only；断网后域名解析
  失败；超时退出码 124。在探查容器里重放 `task_049`、`task_206` 各前 8 条 agent 命令，16 条输出与历史
  轨迹记录逐字相同。
- 开发集 184 条 milestone 明细的 key 全部能在 gold 中找到，其中 agent 做到 71 条，可用于复现验证的
  （`achieved=false`）113 条。
- 首轮输入长度（system prompt 6,882 字符 + 输入）：33 个 run 最短 14,612、中位数 53,113、最长 230,861
  字符；20 次 probe 每次最多 4,000 字符，最多再增加约 8 万字符。
- 反思 prompt 没有照抄 11.2 节的实测数字（例如具体企业数），只保留类别与一般性的 probe 提示，避免开发期
  测得的事实直接流进反思结论；这一点在定稿时一并确认。
- 实现中新增的约定：`EVIDENCE` 可写多行；`REPRODUCED` 的数值按数值比对是否出现在 probe 输出里（与书写
  格式无关，`1,004` 与 `1004.0` 都算出现），字符串按空白规整后的子串比对；一条 divergence 里只要有一行
  `REPRODUCED` 不通过，整条打回；`<final>` 通过的 divergence 按「`FACT` + `TABLES` + `COLUMNS`」去重；
  LLM 调用总数上限为 `max_probes + max_finals + 3`，多出的 3 次留给格式错误的回复。
- 真实试跑暴露的两个问题及处理：
  - 网关会断开约 3 分钟没有数据返回的非流式请求（`task_011` 第 6 次调用在 185.8 秒断开），LLM 调用改为
    流式；
  - GLM 5.2 会把全部输出额度花在推理上，返回空正文（`task_352` 连续 4 次、每次约 800 秒、
    `finish_reason=length`、正文 0 字符；`task_195` 一次 748.5 秒）。处理：空回复发专门提示，连续两次
    结束 run（`stop_reason=empty_reply`）；单次调用默认 300 秒墙钟上限（流式下 HTTP 超时只管两个分块之间，
    管不住持续推理）；单个 run 默认 1500 秒，超时先提示提交 `<final>`，再超结束（`time_budget`）；
    LLM 或容器出错、Ctrl+C 中断时保留已有消息与 probe 并写盘（`error` / `interrupted`），batch 继续下一个
    run（中断则停止）；每次调用的 `finish_reason`、耗时、正文长度记入 `llm_calls`。
  - 推理长度控制（`--max-tokens`、`--reasoning-effort`、`--disable-thinking`，经 `extra_body` 传给网关）
    做成可选参数，默认不开：网关是否支持未验证，待在 `task_352`、`task_195` 上实测。

**阶段 C：规则生成**

- 第二阶段 prompt、`DATA_RULE` / `GENERIC_RULE` 解析（含 `TABLES` 与 `TRIGGER`）、6.4 节自动检查与
  最多两次重写；prompt 的正反例以第 11 节的样例规则为对照；
- 用假 LLM 测试检查与重写逻辑。

退出条件：规则生成 prompt 定稿；以阶段 B 的产物为输入试跑，人工确认规则是列级的、例子不陈述具体
实体事实。

**阶段 C 完成情况**：

- 输入：阶段 B 试跑中被接受的 5 条 divergence 导出到 `data/dataclaw_dev/divergences.jsonl`（提交进 git）。
  来源是 `task_011` 2 条、`task_185` 2 条、`task_231` 1 条；`task_185` 的两条来自两次试跑、是同一个
  事实，留给阶段 D 测试合并去重。编号形如 `task_011__b794e1__1`（task 前 8 个字符、run 目录后 6 个
  字符、divergence 序号），重复的编号加 `~2`。导出脚本是 `scripts/dataclaw_export_divergences.py`，
  读写在 `tkstore/dataclaw/divergence_store.py`。
- 代码：`tkstore/dataclaw/rulegen.py`，prompt 在 `tkstore/dataclaw/prompts/rulegen.md`（生成）和
  `tkstore/dataclaw/prompts/rulegen_generality.md`（通用性判断），脚本是 `scripts/dataclaw_rulegen.py`
  （`--dry-run` 只写出生成步骤收到的消息）。候选规则写到 git 忽略的 `tmp/dataclaw_dev/rules/candidates.jsonl`。
- 流程：每条 divergence 调一次 LLM，生成 1 条 `DATA_RULE`（不生成 `GENERIC_RULE`、不探库）；harness 依次
  检查解析与必填字段、文件和列存在、`SCOPE` 一致、引用不超出来源 divergence、正文不含单元格取值；
  不通过就把理由发回，最多重写 2 次，仍不通过记为 `dropped`；空回复算一次失败的尝试。通过检查的规则再
  调一次 LLM，拿 divergence 和规则判断是否只对个别实体成立：判为否记为 `accepted`，判为是记为
  `not_general`（不重写），判断回复无法解析记为 `judge_error`。生成步骤不接受 `NO_RULE`。
- 引用范围检查 `check_within_divergence`：规则里的文件必须出现在 divergence 的 `TABLES` 里、或是它
  `COLUMNS` 里某列所在的文件；规则里的列必须在 divergence 的 `COLUMNS` 里、或所在文件列在 divergence
  的 `TABLES` 里。
- prompt 里的正例由第 11 节 R2、R6、R7 译成英文，但 `EXAMPLE_USAGE` 改写成只讲列级现象：原文写了具体
  公司的中文名与省份、行业的企业数，与「例子不陈述实体属性、不照抄答案」冲突。三条正例在真实 catalog
  上都通过全部检查。
- 测试：DataClaw 相关新增 29 个（`test_dataclaw_divergence_store.py`、`test_dataclaw_rulegen_checks.py`、
  `test_dataclaw_rulegen.py`），全套 698 通过、1 个跳过（docker 冒烟测试）。
- 试跑（GLM 5.2，5 条，共 414 秒）：5 条都一次通过检查、都被判为通用，结论全部是 `accepted`，没有任何
  一次尝试被打回（单元格取值检查的打回率为 0/5）。生成调用 27–120 秒，判断调用 7–21 秒，
  `finish_reason` 都是 `stop`。
- 人工检查发现的问题，都不在现有检查和判断的覆盖范围内：
  - **例子泄漏实体事实和 gold 值（3 条）。** `task_185` 的两条在 `EXAMPLE_USAGE` 里写了该公司的中文名，
    以及「province 为广东省」，而这正是来源题目的答案；`task_231` 的例子写了具体行业的两个中位数，
    其中 11445832 是 gold milestone 值。
  - **例子照抄 milestone 值、正文固化了纳入口径（1 条）。** `task_011__b794e1__1` 的例子写了 671 和 644
    （644 是 milestone 值），`ENSURE` 写成「总是只保留中国交易所」。第 11 节 R8 的结论正相反：
    `task_383` 与 `task_011` 的 gold 口径相反，规则只能要求「按题目显式筛选」。
  - **用英文描述汉字来绕过取值检查（1 条）。** `task_011__b794e1__2` 的 `ENSURE` 用
    「thousand-character」「hundred-character」指代「千」「百」，把单位换算写成字符解析规则，读起来别扭。
  - 通用性判断只判断规则是否通用，不看例子是否泄漏，所以上面 4 条都判为通用。
- 用户检视的结论：
  - rule 1（`task_011 #1`）偏了，变成「只保留中国交易所」；来源 divergence 的 `NEEDED` 本身读起来就
    让人困惑，把 gold 的口径写成了必须遵守的要求；
  - rule 2（`task_011 #2`）偏了，在总结中英文单位的对应关系；合理的规则应是「按每行 targetUnit 的
    正确换算系数换算」这一类，即第 11 节 R6；
  - rule 3（`task_185 #1`）基本合理；
  - rule 4（`task_231`）偏离了 divergence，放宽了适用范围。实测（只读 probe）：national 与 regional 两个
    文件的「中位数」类指标名完全相同（各 75 个），「营业利润总额中位数」和「营业利润金额中位数」在两个
    文件里都有；租赁和商务服务业在 national 里用前者、在 regional 上海市一行用后者。规则「两个文件对
    同一概念用不同的 targetName」不成立。「没指定地区就用 national」也只有这一道题的 gold 作依据。
- **试跑后的决定**：取消独立的规则生成步骤，把规则生成并入反思（设计见 5.6 节）。规则生成的代码、测试、
  prompt、脚本与旧格式的 `data/dataclaw_dev/divergences.jsonl` 删除；解析规则字段与正文取值检查挪进
  `reflector_io.py` 与 `gates.py` 复用；导出工具保留。

**阶段 C（修订）：规则生成并入反思**

按 TDD 实现，测试用假 LLM 和构造的 `Catalog`：

1. 删除阶段 C 的独立规则生成：`tkstore/dataclaw/rulegen.py`、`tkstore/dataclaw/prompts/rulegen.md`、
   `tkstore/dataclaw/prompts/rulegen_generality.md`、`scripts/dataclaw_rulegen.py`、对应的 2 个测试文件
   （`test_dataclaw_rulegen.py`、`test_dataclaw_rulegen_checks.py`）和旧格式的
   `data/dataclaw_dev/divergences.jsonl`。导出工具 `divergence_store.py` 与
   `scripts/dataclaw_export_divergences.py` 保留，第 7 步扩展新字段后继续用。
2. `reflector_io.py`：解析新字段 `BASIS`、`BASIS_QUOTE`、`INSTANCE`、`GENERALITY`（格式同 `EVIDENCE`，
   可多行）和 5 个规则字段。
3. `gates.py`：加 5.6 节闸门的第 2–4 道（规则必填字段与正文取值检查、`BASIS` 与 `BASIS_QUOTE`、
   `GENERALITY` 的 probe 与摘录）。`BASIS_QUOTE` 的检查需要题面文本，由调用方传入。
4. `reflector.py`：确定性闸门都通过后调通用性判断（第 5 道）；判为不合格时，理由并入 `HARNESS VERDICT`
   发回，占用一次 `<final>` 额度；每次判断调用记入 `llm_calls`（区分反思与判断）。
5. 反思 prompt `tkstore/dataclaw/prompts/reflector.md`：加新字段、`BASIS` 的定义与例子、`BASIS` 对规则的
   约束（5.6 节草稿），以及 6.2–6.5 节对规则字段的要求和正反例（沿用阶段 C 的 R2、R6、R7 英文改写与两个
   反例）；通用性判断的 prompt 另写一份。两份 prompt 都要逐条确认。
6. 用 `--dry-run` 检查输入长度，然后在 `task_011`、`task_185`、`task_231` 上真实试跑，与阶段 B 试跑的
   divergence、阶段 C 试跑的规则对比。
7. 扩展 `divergence_store.py` 以带上新字段和规则，导出通过闸门的 divergence 与规则，作为阶段 D 的输入。

完成情况：第 1–4 步已完成，全套测试 699 passed、1 skipped。第 5 步两份 prompt 已写出草稿，待逐条确认：
反思 prompt 加了新字段及各字段写什么、`BASIS` 一节、「Writing the rule」一节（R2、R6、R7 三条 `data`
正例，按 R8 写的一条 `gold_only` 正例，三个反例）和 harness 检查说明；四条正例在真实 catalog 上实测
通过文件列存在、`SCOPE` 一致与正文取值检查。反思 prompt 现为 18,412 字符；`task_011` 首轮输入由
76,851 增至 88,289 字符，多出的 11,438 字符都来自 prompt。

第 6 步试跑：

- GLM-5.2（默认推理设置）：`task_185` 接受 1 条（约 275 秒）；`task_011`、`task_231` 都在该写 `<final>` 时
  连续两次单次调用超时（各约 300 秒），以 `empty_reply` 结束。重放 `task_231` 超时前的输入（70,523 字符）：
  801 秒内只输出 214,823 字符的思考内容、正文为 0，以 `finish_reason=length` 结束。对同一道短题实测
  网关参数：`reasoning_effort=low` 无效（思考 48,000 token 后 `length`），`max_tokens` 只截断思考、正文仍为空，
  只有关闭 thinking 生效。
- 改用 GPT-5.1、`reasoning_effort=low`：`task_011` 113 秒、`task_231` 127 秒，所有调用都是 `stop`，各接受 1 条
  （`task_011` 另有 1 条 `gold_suspect`）。人工检查质量可接受；`task_011` 那条的 `GENERALITY` 只引用了一个
  公司计数，证据偏弱。`EXAMPLE_USAGE` 泄漏实体名（`task_185`）暂不处理。

第 7 步未开始。

**阶段 C（修订二）：引用方式、两个判官与预算**（设计见 5.7 节；第 1–4 步已实现，全量测试 742 passed、
1 skipped；第 5 步待三份 prompt 确认后重跑）

在 `gpt51_low_sample` 的 6 条上试跑后定下。按 TDD 实现，顺序：

1. 引用与结构：probe 输出带行号、`P<n>` 编号与行号指针（`reflector_io.py` 解析、`gates.py` 检查与回填、
   `probe.py` 的视图）；`SCOPE` 由 harness 推出；结构错误不扣 `<final>`、另设上限；打回消息写明剩余
   probe 次数；结束原因 `abandoned_after_reject`；`--max-finals` 默认 5。
2. 取消正文取值检查，`Catalog` 只保留表头；反思 prompt 删去相应要求。
3. 通用性判官的输入加题面、gold 与全部 probe，prompt 按 5.7 节改写第 1、2 条。
4. 新增复现判官与其 prompt；`SEMANTIC_MATCH` 不再是闸门字段。
5. 在 6 条 `gpt51_low_sample` 与 `task_011`、`task_231` 上重跑，统计结束原因，人工检查规则。

退出条件：两份 prompt 定稿；在上述 3 道题上试跑，人工确认规则是列级的、`gold_only` 的规则没有把 gold
的选择写成固定动作、例子不陈述具体实体事实。

**阶段 D：合并与存储**

- 按 `TABLES` 的 `(file, None)` 与 `COLUMNS` 的 `(file, column)` 组成的集合分组、每组一次合并调用、
  合并时按各来源的 `TRIGGER` 归纳 `WHEN_TO_CHECK`、保留 `provenance` 与支撑任务数；
- 写 JSONL store，建立「文件 → 规则 id」索引；
- B0 渲染：把全部规则按固定顺序渲染成 `[TRIBAL_KNOWLEDGE]` 块并计算 hash。同一 store 每次必须渲染出
  同一段文本，否则 B0 的 receipt 无法验收；用确定性测试保证。

退出条件：合并 prompt 定稿；以阶段 C 的产物为输入试跑，得到一份开发期 store，人工检查各 `kind` 的
数量与质量。

### 9.4 开发完成后的正式运行

1. **定 split。** 已完成（决策 52）：按 `category × level` 分层 1:1，test 246 道，train 分成
   `train_a`、`train_b` 各 123 道，开发集题目不作特殊处理；文件与生成方式见 `dataclaw.md` 7.5 节；
2. **跑 train 上的裸 OpenClaw。** 用现有 `run_batch.py`，每题一次，独立的 `OUTPUT_SUBDIR`；先跑
   `train_a`，判错轨迹不够再跑 `train_b`；
3. **全量 populate。** 用阶段 A–D 的代码处理 train 上所有判错 run，生成正式 store；
4. **B0 注入与评测。** 按 `dataclaw.md` 5.5 节实现只给 agent 的注入与 `knowledge_receipt.json`，逐 run
   做送达验收；A、B0 分别用不同 `OUTPUT_SUBDIR` 在 test 上各跑一次。

### 9.5 注意事项

- **agent 模型改为 gpt-5.1，judge 用 deepseek-v4-flash**（决策 53），两者都走 TK-Boost `.env` 里
  同一个端点，配置与冒烟测试见 `dataclaw.md` 7.5 节。开发集是 glm-5.2 的轨迹，换模型后错误分布会变，
  开发期调好的 prompt 不一定适用，跑完 `train_a` 后要先看判错轨迹的类型是否与开发集相近。agent 用
  medium 推理（`OPENCLAW_THINKING=medium`，见 `dataclaw.md` 7.5 节）：low 下 4 道冒烟题有 3 道很快
  放弃（其中一次是 `exec` 带了 `host: "sandbox"` 而报错），medium 下同样三道 `train_a` 题答对 2 道、
  另一道算完给出错误答案。`host: "sandbox"` 这类环境造成的失败在反思阶段应归为非数据错误。
- **历史轨迹来自同一镜像。** 开发集由 `dataclaw:0.1.0`（OpenClaw 2026.3.24）产出，`chat.jsonl` 格式与
  新 run 一致；其 `score.json` 是当时 judge 的判定，开发期直接沿用。
- **开发期 store 不是最终 store。** 只用于检查链路与 prompt 是否合理。

### 9.6 自我注入测试：规则能否修好挖出它的那道题

**目的。** 把一条接受的规则注入挖出它的那道题，看 agent 能否因此做对。这是上限式检查：做对只说明
规则的知识足以修好这次失败，不说明规则对别的题有用。

**题目。** `rev2_gpt51_low` 重跑中有接受规则的 5 题：task_009、task_011、task_218、task_231、
task_383，每题一条规则。task_352、task_464、task_477 没有规则，不参加。

**注入哪些字段。** 只注入 `WHEN_TO_CHECK`、`ENSURE`、`CONTEXT`。逐条对照题面与 gold 后发现，
`EXAMPLE_USAGE` 是泄露 gold 的主要位置：task_383 写明「one task's convention (the gold here) is to
treat all 专用设备制造业 firms with country=="中国" and positive revenue as the universe」，task_218 写明
「as this task's gold solution does」，task_231 的「in Shanghai 用分省行、泛指行业用全国行」正好对应
本题两边，等于给出解法。因此 `EXAMPLE_USAGE` 不注入。`INSTANCE`、`FACT`、`TRIGGER`、`DIVERGENCE`、
`NEEDED` 含本题的值或题面原话，也不注入。

三个字段照 reflector 记录里接受的 divergence 原文填入，不做人工删减或改写；每题的规则文件由
记录自动生成，runner 只读这些文件。原文里残留的问题照样注入。

**规则来源只取最新一轮。** `build-rules` 只读 `--reflect-dir` 指定的那一个目录下的 `*.json`，不进子
目录；写之前清空输出目录里旧的 `*.md` 与 `index.json`。本次来源是 `tmp/dataclaw_dev/reflect/rev2_gpt51_low`，
生成 5 个规则文件（`data/dataclaw_dev/self_inject/rules/<task_id>.md`），`index.json` 记来源目录、
每题的规则 id 与 BASIS。

**注入方式。** DataClaw 仓库不改，注入由 TK-Boost 的包装脚本完成
（`scripts/dataclaw_self_inject.py`，逻辑在 `tkstore/dataclaw/self_inject.py`）：

- 包装脚本先设 `OUTPUT_SUBDIR`，再导入 `dataclaw.eval.run_batch`：`OUTPUT_DIR` 在导入时读取，
  `.env` 里的 `OUTPUT_SUBDIR=output` 不会覆盖已设的值；
- 对每题复制一份 task，把副本的 `prompt` 换成 `task.prompt + "\n\n" + 规则块`，交给 `run_single_task`，
  于是只有它进入 `/tmp/agent_prompt.txt`；
- `run_single_task` 把同一个 `task.prompt` 也传给 `grade_task`，所以运行期间临时替换
  `run_batch.grade_task`，把 `task_prompt` 换回原始题面。读代码确认：outcome judge 只读
  `task_prompt` 和 agent 最后一条 assistant 文本（`grading.py` 的 `_final_assistant_text`），process
  judge 只读 assistant 步骤和工具结果（`process_grading.py` 的 `build_gpr_judge_prompt`）与
  `gold_file`，两者都不读 transcript 的第一条 user 消息，因此看不到规则。替换不是线程安全的，逐题串行；
- 每个 run 目录写 `knowledge_receipt.json`：规则文件路径与 SHA-256、规则 id、BASIS、注入文本、注入
  文本的 SHA-256 与字符数、送达是否通过及原因；
- suite 里有题找不到规则文件时，开跑前报错退出，不按裸跑处理；
- 两组靠不同的 `OUTPUT_SUBDIR` 区分：裸跑 `output_rules_self_bare`，注入 `output_rules_self_inject`。
  run 目录名与裸跑相同（包装脚本改不了目录名）。

运行：

```bash
cd ~/TK-Boost
python scripts/dataclaw_self_inject.py build-rules --reflect-dir tmp/dataclaw_dev/reflect/rev2_gpt51_low
SUITE=$(python -c "import json;print(','.join(json.load(open('data/dataclaw_dev/self_inject/rules/index.json'))['tasks']))")

# 裸跑组（7.5 节的写法）
(cd ~/DataClaw && OUTPUT_SUBDIR=output_rules_self_bare python dataclaw/eval/run_batch.py --suite "$SUITE")
# 注入组；模型与 judge 默认取 DataClaw .env 的 DEFAULT_MODEL、JUDGE_MODEL
python scripts/dataclaw_self_inject.py run --suite "$SUITE"
# 对照表
python scripts/dataclaw_self_inject.py report
```

规则块模板。它排在题面的 Output guidelines 之后，所以末尾重申输出格式以题面为准：

```text
<原始 task.prompt，原样不动>

[DATABASE NOTES]
The notes below come from earlier analyses of this same database. They describe
pitfalls in the data files, not answers to the question above. For each note:
1. Read "Applies when". If it does not describe the question above, ignore the note.
2. If it applies, carry out "Check" before computing the quantity it concerns.
3. "Why" states the data property behind the note; verify it in the data if in doubt.
4. If the question explicitly requires something different, follow the question.

Note 1
Applies when: <WHEN_TO_CHECK>
Check: <ENSURE>
Why: <CONTEXT>
[END DATABASE NOTES]

The answer must still follow the output guidelines in the question above.
```

**评测方案。**

- agent 用 DataClaw `.env` 里的 `DEFAULT_MODEL=gpt-5.1`，judge 用 `JUDGE_MODEL=deepseek-v4-flash`，
  均保持不变；
- 两组：裸跑（原始题面）与注入（题面加规则块），每题每组各一次，共 10 个 run，两组用不同的
  `OUTPUT_SUBDIR`；
- 规则挖自 glm-5.2 的轨迹，gpt-5.1 未必犯同样的错，所以裸跑组不能省：裸跑已做对的题，注入组的结果
  不能说明规则有效；
- 验收：注入组每个 run 跑完立即检查 `chat.jsonl` 的第一条 user 消息是否包含 receipt 里的注入文本
  （去掉首尾空白后）。不要求整条消息的 hash 相等：OpenClaw 在消息前加时间戳（如
  `[Fri 2026-07-10 07:36 UTC] `），bash 的 `$(cat ...)` 去掉末尾换行。不通过时 receipt 记
  `delivery_ok: false` 与原因，`<OUTPUT_DIR>/self_inject_failures.jsonl` 追加一行，`run` 以非零码
  退出、后面的题不再跑；`report` 再核对一次，不通过的 run 不计分；
- 报告：每题两组的得分（task_383 按 part 得分），按 BASIS 分开列。gold_only 规则（task_218、task_231、
  task_383）的 ENSURE 只要求「两种都查、按题意选」，不说选哪个，注入后仍做错不一定说明规则写错；
- 每组只跑一次，agent 的随机性无法排除，单题结果只作为线索，不作为规则有效与否的结论。

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
| 7 | 自动检查 | 正文不含单元格值（≥3 字符）、列名真实存在；不合规最多重写两次（取值检查已被决策 61 取消） |
| 8 | gold 扫描 | 不设硬闸门 |
| 9 | test 侧诊断 | 暂不做 |
| 10 | 合并去重 | 每组一次 LLM 调用；按 `TABLES` 的 `(file, None)` 与 `COLUMNS` 的 `(file, column)` 组成的集合分组；接受跨组残留重复 |
| 11 | 存储 | JSONL，`kind` 五类，`provenance` 记来源与支撑数 |
| 12 | B2 跨表返回时机 | 探查到任意一个涉及文件即返回 |
| 13 | 不采用 | operation tags；按列分类个体 / 标签值；固定禁用名单 |
| 14 | 范围 | `column` / `multi_column` / `file` / `cross_table` / `generic` 五类，在 divergence、规则、存储中显式记录；harness 按 `TABLES` 与 `COLUMNS` 推出并校验 |
| 15 | 硬闸门 | 证据须为真实 probe 输出的子串；列名须存在；复现值由 harness 比对；`non_data`、`gold_suspect` 不进第二阶段（证据引用方式已被决策 54 取代） |
| 16 | 规则与工具 | 规则描述要检查的数据特性，不写具体库或命令调用 |
| 17 | 开发顺序 | 先在历史轨迹开发集上开发阶段 A–D，产物合理后再定 split、跑全量 train 与全量 populate |
| 18 | 开发集 | 归档中 33 个判错的裸 glm-5.2 run（27 道题），拷贝到 `data/dataclaw_dev/runs/`（git 忽略），`manifest.csv` 提交 |
| 19 | 开发集与 split | 互不约束；正式实验重新跑 train 并重新 populate，开发期规则不进入最终 store |
| 20 | 字符串 milestone | 规整后相等即一致，否则由反思 agent 做语义判断（prompt 提示「不要求逐字一致，但语义须几乎完全一致」）；数值部分仍由 harness 判定（语义判断已由决策 64 改为复现判官负责） |
| 21 | 正文取值检查 | 纯数字也算单元格取值；与文件名、列名相同的取值除外（已被决策 61 取消） |
| 22 | 代码位置 | `tkstore/dataclaw/` 子包，测试为 `tests/test_dataclaw_*.py` |
| 23 | 表和列分开存 | divergence 与规则输出写 `TABLES` 和 `COLUMNS` 两个字段，存储有 `tables`、`columns`，另存推出的 `files`；取消 `<file>.all` 写法（见 6.2） |
| 24 | `WHEN_TO_CHECK` 生成 | 规则生成时在 `TRIGGER` 中摘出来源题目的触发原句，再抽象成问法形状；合并时比较同组各来源的问法取共同点（见 6.2、第 7 节） |
| 25 | 轨迹压缩 | 工具输出截断到 2000 字符、保留头尾；思考保留 5000 字符；命令原样保留 |
| 26 | 反思预算 | 每个 run 最多 20 次 probe；最多提交 3 次 `<final>`；probe 超时 60 秒；发给 LLM 的 probe 输出截断到 4000 字符、保留头尾（均为参数；`<final>` 上限已被决策 60 改为 5 次） |
| 27 | 探查容器网络 | 断网（`--network none`） |
| 28 | 复现用的 milestone | 只能用 `process_score.json` 中 `achieved=false` 的 milestone |
| 29 | `CATEGORY` 字段 | divergence 输出加 `CATEGORY`（11.2 节类别名或「其他」），不参与闸门，只用于统计 |
| 30 | 试跑 run | `task_049`、`task_218`、`task_195`、`task_206`、`task_011` |
| 31 | `REPRODUCED` 格式 | `milestone "<key>" = <JSON 值> FROM probe#<m>`，可多行；字符串不逐字一致时跟 `SEMANTIC_MATCH`；值须出现在所引 probe 的输出里（已被决策 54 取代） |
| 32 | 反思模型的约束 | 上下文至少 1M token；具体模型待定 |
| 33 | 阶段 C 输入 | 阶段 B 已接受的 divergence 导出为 `data/dataclaw_dev/divergences.jsonl` 并提交；候选规则输出到 git 忽略的 `tmp/dataclaw_dev/rules/`（已被决策 39 取代） |
| 34 | 规则形态 | 每条 divergence 生成且只生成 1 条英文 `DATA_RULE`，不生成 `GENERIC_RULE`；生成步骤不接受 `NO_RULE`（「单次 LLM 调用、不探库」已被决策 39 取代） |
| 35 | `TRIGGER` | 保留，不做任何校验 |
| 36 | 引用范围 | 规则引用的文件和列只能比来源 divergence 收窄（已被决策 39 取代：规则的文件和列就是 divergence 的 `TABLES`、`COLUMNS`） |
| 37 | 通用性判断 | 调一次 LLM 判断规则是否只对个别实体成立（修订：并入反思闸门，不合格时打回反思 agent，见决策 43） |
| 38 | 规则生成模型 | 暂用 GLM 5.2 |
| 39 | 规则生成的位置 | 并入反思：反思 agent 在 `<final>` 的每条 `data` divergence 里同时写出规则字段；取消独立的规则生成步骤（见 5.6 节） |
| 40 | divergence 字段 | `DIVERGENCE` 只写 agent 的行为；`INSTANCE` 写本题实体上的现象，可含实体和数值；`FACT` 写列级性质；`TABLES`、`COLUMNS` 只列事实涉及的文件和列 |
| 41 | `BASIS` | `NEEDED` 须标明依据：`data`（数据逼出来的）、`task`（题面决定，附 `BASIS_QUOTE` 原句，harness 检查是题面子串）、`gold_only`（只有 gold 这么选）。prompt 写明三项定义和例子，并说明 `BASIS` 对规则的约束；`task` 与 `gold_only` 的分界：只读题面的细心分析者会不会做同样的选择 |
| 42 | `gold_only` 的规则 | `ENSURE` 不能把 gold 的选择写成固定动作，只写「这个维度要按题目确定」；gold 的做法可以出现在 `EXAMPLE_USAGE`，但要注明这只是某道题的约定 |
| 43 | 通用性的验证 | `KIND: data` 必须有 `GENERALITY` probe（harness 检查 probe 与摘录）；确定性闸门通过后调 LLM 做通用性判断，不合格按闸门打回，占用一次 `<final>` 额度 |
| 44 | 并入后的检查范围 | 沿用现有的正文取值检查（`ENSURE`、`WHEN_TO_CHECK`、`CONTEXT`）；`FACT` 不做取值检查；不设「不能出现 gold 值」的检查（取值检查已被决策 61 取消） |
| 45 | `non_data` 定义 | 暂不修改 |
| 46 | 反思预算 | 并入后暂不修改（20 次 probe、3 次 `<final>`），试跑后再看（`<final>` 上限已被决策 60 改为 5 次） |
| 47 | 修订后的试跑 | `task_011`、`task_185`、`task_231` |
| 48 | 通用性判断的范围 | 只判两点：规则是否只对个别实体成立；`gold_only` 的规则是否把 gold 的选择写成固定动作。不检查例子（输入与第 1、2 条已由决策 62 修订） |
| 49 | 判断回复无法解析 | 为空或没有 `VERDICT` 行时重试一次；仍失败记为 `judge_error`，不接受、不打回，写进 `judge_errors` |
| 50 | `NEEDED` 与 `INSTANCE` | `KIND: data` 时两者都是必填项，缺失即打回 |
| 51 | 反思与判断的模型 | 用 `.env` 配置的 GPT-5.1；`scripts/dataclaw_reflect.py` 默认 `--reasoning-effort medium`（`omit` 为不发送），`--judge-model` 另指定判断模型时沿用同样的推理设置 |
| 52 | train / test split | 按 `category × level` 分层 1:1（seed 0）：test 246 道；train 再分层对半成 `train_a`、`train_b` 各 123 道，先跑 `train_a`，不够再跑 `train_b`；文件在 `data/splits/dataclaw_*.txt` |
| 53 | 正式运行的模型 | agent 用 gpt-5.1，judge 用 deepseek-v4-flash，同一端点；取代 9.5 节原先的「agent 保持 glm-5.2」 |
| 54 | 引用方式 | probe 输出带行号；`EVIDENCE`、`GENERALITY` 写 `P<n>:L<a>[-L<b>]`，`REPRODUCED` 写 `FROM P<n>:L<a>`，harness 回填原文；`REPRODUCED` 的值须在所指行里（取代决策 15 的「子串」与决策 31 的 `FROM probe#<m>`；见 5.7 节） |
| 55 | probe 编号 | 改为 `P<n>`，与轨迹的 `CALL #n`、`OUTPUT #n` 区分 |
| 56 | `SCOPE` | 不再由反思 agent 填写，由 harness 按 `TABLES`、`COLUMNS` 推出 |
| 57 | 结构错误 | 立即退回、不扣 `<final>` 次数，另设单独上限；只有实质错误（值不符、milestone 已达成、判官拒绝）扣次数；范围见 5.7 节 |
| 58 | 打回消息 | 写明剩余 probe 与 `<final>` 次数，并说明可以先跑 probe 再重交 |
| 59 | 结束原因 `abandoned_after_reject` | 之前有 divergence 被打回、之后的 `<final>` 只含 `NO_DATA_DIVERGENCE` 时使用，不记为 `done` |
| 60 | `<final>` 上限 | 默认 5 次（取代决策 26、46 中的 3 次） |
| 61 | 正文取值检查 | 取消（取代决策 7、21、44 中的取值检查）；`Catalog` 只保留表头；过于针对具体取值的风险由通用性判官负责，后续试跑重点检查 |
| 62 | 通用性判官 | 输入加题面、gold 的 `answer`、`steps`、`milestone` 与全部 probe；第 1 条改为「换成同列其他取值是否仍成立」的测试，可以点名指标；第 2 条不再只看 `BASIS`，`ENSURE` 的做法出自 gold `steps` 而 probe 只证明结果不同即拒绝（修订决策 48） |
| 63 | 复现判官 | 确定性检查之后、通用性判官之前调一次 LLM，按题面、gold `steps` 与 probe 代码判断引用行算的量是否就是 milestone 所指的量；取自 gold `steps` 的数字不算复现；`mismatch` 打回、扣一次 `<final>` |
| 64 | `SEMANTIC_MATCH` | 不再是闸门字段，只作给复现判官的说明；文本 milestone 的语义判断交给复现判官（修订决策 20） |
| 65 | 通用性判官与 `BASIS` | 判官 prompt 写入 `data`、`task`、`gold_only` 的定义（与反思 prompt 同义）；第 2 条豁免 `BASIS_QUOTE` 引用的题面措辞确实要求的做法，由判官对照题面核实，否则正当的 `task` 规则会因做法出现在 gold `steps` 里被误判为照搬 gold |
| 66 | 判官的字段释义 | 字段含义集中写在 `prompts/divergence_fields.md`，两个判官的 system prompt 只嵌入各自看得到的字段：通用性判官 17 个（`DIVERGENCE` 到 `EXAMPLE_USAGE`），复现判官 5 个（`DIVERGENCE`、`NEEDED`、`INSTANCE`、`REPRODUCED`、`SEMANTIC_MATCH`）；`BASIS` 三个取值的定义也在其中 |
| 67 | 判官的表头与第 1 条两步 | 两个判官都拿到全库表头（19 个文件、251 列，3,598 字符），用来确认「同一列」并读懂 probe 代码取的列；判官不能自己跑 probe。通用性判官第 1 条拆成 a 措辞（换值后是否说得通）与 b 证据（只凭 probe 输出判断，输出没覆盖同列其他取值即拒绝，并说明需要什么 probe） |
| 68 | 保存判官回复 | `llm_calls` 里每次判官调用（`judge_reproduction`、`judge_generality`）都记下 `divergence` 序号与完整回复 `text`，通过的也记，供事后核查判官理由；`rev2_gpt51_low` 这一轮没有保存，理由只能重放 |
| 69 | 通用性判官多数表决 | 最多 3 票，一方过半即停（两票一致就不投第三票）；每票无法解析时重试一次，仍失败整条记为 `judge_error`；拒绝时发回每张拒绝票的理由。依据：对 `rev2_gpt51_low` 的输入重放，task_352 的通用性判官 6 次里 5 次拒绝、1 次通过（正式运行恰是通过），task_383 的 5 次里 4 次通过、1 次拒绝 |
| 70 | 指针的宽松写法 | EVIDENCE 与 GENERALITY 一行可写多个指针，以逗号或分号分隔，每个都单独解析，坏的报错、好的保留；范围另接受 `P1:L22-P1:L28`（两端同一 probe）与 `P1:L22-28`；两端不是同一 probe（`P1:L1-P2:L3`）仍是结构错误。REPRODUCED 仍只引一处，但接受同样的范围写法。依据：`rev2_gpt51_low` 中 task_009、task_218 以 `format_budget` 结束，被拒的格式正是这几种 |
| 71 | 数值按报告精度舍入核对 | 结构检查核对 REPRODUCED 的数值是否在引用行上时，引用行上的数按报告值的小数位舍入后相等也算出现（`0.455843` 对 `0.4558`；整数报告值按整数舍入）。依据：task_009 报告 `0.4558`、引用行印 `0.455843` 而被拒 |

### 待定

- 三份 prompt（反思、规则生成、合并）的最终文本（第 5.3、6.2 节为草稿；合并 prompt 尚未起草），
  **必须逐条确认**；
- 反思 agent 已改用 GPT-5.1（决策 51），决策 32 的「上下文至少 1M token」是否随之放宽待确认；合并使用的
  模型；
- `WHEN_TO_CHECK` 的离线过宽检查（统计每条命中哪些题目），B1、B2 之前再定；
- 同一 train task 若有多个裸 run，取哪一个（首版每 task 只跑一次，暂不涉及）。
- 规则 `EXAMPLE_USAGE` 泄漏实体事实和 gold 值（阶段 C 试跑 5 条中 4 条）：目前只靠 prompt 约束
  （`INSTANCE` 与 `FACT` 分开写），不设确定性检查（决策 44）；修订后试跑若仍常见，再考虑让通用性判断
  同时检查例子；
- `non_data` 的边界：计算步骤里写错系数（例如 `task_011 #2` 把千万元与百万元的换算系数写反）算不算
  `non_data`；
- 反思 prompt 与通用性判断 prompt 的最终文本（5.6 节为 `BASIS` 部分的草稿），必须逐条确认；
- 阶段 B 的缺口：divergence 引用的文件应出现在它所引 probe 的命令或输出里，暂未检查。
- 5.7 节的结构错误单独上限取多少（决策 57）；
- 是否把规则正文里出现的单元格取值列给通用性判官作提示（不作闸门），需要为此保留 `Catalog` 的取值集合；
- 复现判官是否需要被分析 agent 轨迹中 `DIVERGENCE` 点名的 `CALL`（默认不给）；
- 复现判官与通用性判官 prompt 的最终文本，必须逐条确认。

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
