# 论文阅读笔记（给 slides 用）

来源：`papers/MAGIC.pdf`、`papers/Arming_Data_Agents_with_Tribal_Knowledge_Tech_Report.pdf`（TK-Boost）、`papers/MIRA.pdf`。第四篇 **SQLFixAgent**（Cen et al., AAAI 2025）本地没有 PDF，机制按 AAAI / arXiv:2406.13408 摘要与方法节记录。只记对串 slides 有用的内容。

相关工作按 **经验产物形态** 分成三类（prompt 第 2 点）。SQLFixAgent 是第四篇论文，对应第一类，不是 TK-Boost 里的 Memory 基线。

| 产物 | 代表 | 存什么 | 接到 SQL 流程的哪一步 |
| --- | --- | --- | --- |
| 原始 repair 轨迹 / 错误记录 | **SQLFixAgent**（Cen, Liu, Li, Wang, AAAI 2025） | SQLTool 的历史错误与相似修复；当次修正里还写 **failure memory**（执行失败 + 库反馈） | 检测到错之后：按当前题检索相似 repair，SQLRefiner 从候选里选最终修复 |
| 全局 guideline | MAGIC | 一份面向某套生成器的自检清单 | 生成或改写 **整条 SQL** 时整份塞进 prompt |
| 细粒度 repair knowledge | TK-Boost、MIRA | 带适用条件的规则 / 可独立判定的 repair memory item | TK-Boost：按 CTE / 子查询改写；MIRA：对上游 current SQL 做事后校正 |

---

## 1. MAGIC（Askari et al., AAAI 2025）

**一句话。** 人工写 self-correction guideline 又贵又漏模式；MAGIC 用三个 agent 在训练集失败案例上自动编出一份 guideline，推理时拿去改 SQL，效果超过专家手写清单。

**离线怎么造经验。** 固定一个初始 text-to-SQL 系统 \(M\)（论文主实验是 DIN-SQL + GPT-4）。训练集上 \(M\) 的失败 = SQL 跑不通，或执行结果与 gold 不同。对每条失败：

- **Feedback agent**：对照问题、错误 SQL、gold SQL，写出「错在哪、怎么改」的反馈。
- **Correction agent**：只看到错误 SQL（故意不给 gold），按反馈改写；有 SQL 执行器。改成功说明这条反馈真能覆盖 \(M\) 的错，而不是靠偷看答案。
- **Manager**：调度两者、必要时改它们的指令、最多 5 轮。成功反馈攒满一批（论文取 batch=10）就合并进全局 guideline，下一批是增量更新而不是重写。

整份 guideline 在训练集上一次性编完。论文说相对人工写清单，自动生成大约 2 小时。

**在线怎么用。** 把编好的 guideline 接到原来的生成流程上：模型对着 **整条** 已生成 SQL 按清单自问自答再改写（Fig. 1 那种「Aggregation within Aggregation? Yes…」）。不按表/列检索，所有新问题共用同一份清单。

**论文数字（slides 用到时注明设定）。**

- 主系统 DIN-SQL；BIRD / Spider 的 **dev**。
- 三种纠错场景：只改已知错的 SQL（有正确性 oracle）、只改执行失败的 SQL、改 **所有** 预测（DIN-SQL 原设定）。
- BIRD 上 MAGIC 的 guideline 把 DIN-SQL 从 **56.52 → 59.13** EX；超过 DIN-SQL 和 MAC-SQL 的专家手写 guideline。
- 代码开源（论文写 microsoft/SynQo）。

**对 slides 的含义。** MAGIC 对应「全局 guideline 指导 initial SQL 的规划 / 生成 / 重写」：清单是跨问题的，粒度是整条查询，适合画在流程图的 **整条 SQL 生成与整条改写** 上，而不是单个 CTE。代价是一份清单要覆盖很多错误模式，可能空泛，也可能在已经正确的 SQL 上乱改（MAGIC 自己也测了「改全部预测」）。

---

## 2. TK-Boost（Agarwal et al., tech report / CoRR 2602.13521）

**一句话。** 给任意 NL2SQL agent 外挂 **tribal knowledge**：不是把库内容复述成 fact，而是从 agent 在带 gold 的经验集上犯过的 **误概念** 里抽出可复用的自然语言规则，按 **SQL 特征** 检索，再按 **CTE / 子查询** 把反馈交给 agent。

**经验从哪来。** 经验元组 \((q, \tau, s^\star)\)：问题、agent 答这题时的执行轨迹、能跑出正确答案的 SQL。用数据集约 **25%** 做经验集（有 gold），其余 75% 测试。Populate 时让 agent 先答这些题，对比错误 SQL 与 gold，迭代探库找根因，写出 knowledge，并生成 **applicability conditions**（这条规则适用的表 / 列 / SQL 操作 / 类型等）。索引进 TK-Store。

**在线怎么用（和流程图对齐的关键）。**

1. Agent 先探库、写出 **first-attempt SQL**（可含多个 CTE）。
2. 对每个 CTE（没有 CTE 就把整条当一个片段）：`Retrieve(s)` 用 SQL 特征匹配 applicability，再用 LLM（FilterKnowledge）丢掉不相关的。
3. 把留下的规则收成对该 CTE 的反馈，交给 **同一个 agent** 改这一段，然后处理下一段。

论文强调：用自然语言问题做 embedding 检索对不上「细粒度子任务」（例如某个 join），所以检索键是 **已写出的 SQL**，不是问题本身。整包规则塞进 prompt 会用错片段，所以要 **逐 CTE**。

**和「存轨迹」基线的对比。** TK-Boost 自己的 Memory（NL–SQL / Mem0）不是 SQLFixAgent。SQLFixAgent 是独立论文：多 agent 修 SQL，并用历史错误记录做相似 repair 检索。TK-Boost 的记忆类提升大约 **3.9–4.5** 个点，朴素知识大约 **4.7–5.8**，TK-Boost 最高 **Spider 2.0 +16.9、BIRD +13.7**。

**对 slides 的含义。** TK-Boost 对应「细粒度 knowledge 在 CTE 粒度改 subquery」。画流程图时箭头应落在 **initial SQL 已经写出来之后** 的逐段改写，而不是探库之前的规划。它仍依赖外层 agent 去采纳反馈（论文设定）；本仓库后续实验还测过「直接采纳 refiner SQL」。

**MIRA 论文里的 TK-Boost 数字不要和上面混用。** MIRA 把 TK-Boost 改成和别的方法一样的 **事后校正器**（给同一条 current SQL），报的是 adapted 实现：总体 EX **63.31 → 66.72（+3.42）**，修好 239 / 改坏 178。那是另一套接口，slides 若引 TK-Boost 原论文增益，应写 Spider 2.0 / BIRD 上的 bolt-on agent；若和 MAGIC、MIRA 横比，应标明 MIRA 的 post-generation 协议。

---

## 3. MIRA（Liu et al., arXiv 2608.06950）

**一句话。** 历史一次修正常常绑了好几处互不相关的改动；整案复用会把用不上的编辑也搬过来，把本来对的 SQL 改坏。MIRA 把修正切成可独立判定的 **memory item**，用当前问题、当前 SQL 和 **库证据** 决定是否激活，再局部适配。

**动机（Fig. 1，很适合做一页示意）。** 历史 SQL 同时修了三件事：订单必须存在（A）、客户去重（B）、只要活跃客户（C）。当前 SQL 只有 B 的错。粗粒度经验一次触发 A+B+C：`DISTINCT` 对了，但多出来的 `INNER JOIN` 和 `status='active'` 把不该删的行删了。MIRA 只激活并适配 B。

**离线。** 每个库一份记忆。历史是 \((q_i, s^-_i, s^+_i)\)。先从错误 SQL 出发多轮生成，直到执行结果与 \(s^+\) 一致，得到 **validated repair SQL**（避免 gold 里改别名等无关 diff）。再按 AST 把改动分组、一次只撤一组，用任务要求和库事实判断这组是否真在修一个行为。通过的组变成一条 memory item，含：语义契约（适用 / 错误行为 / 正确行为 / 必须保留什么）、结构签名、在当前库上要查的违规信号。记忆是 **按库** 的，建完不再为新题改模型参数。

**在线。** 上游 agent 当黑盒，只看它的 current SQL。语义通道用问题，结构通道用当前 SQL 的 AST。检索到的只是候选：还要用当前问题是否要求该行为、当前 SQL 是否出现该违规、必要时一次只读 probe 来 **激活**。激活后绑定到当前表/列/位置，多条 item 合成一次改写。改写必须能 parse、能执行、结果与原文不同；失败则 **原样留下** current SQL。

**论文数字。**

- 三个上游：CHESS、DeepEye-SQL、OmniSQL-32B；BIRD Mini-Dev（校正版）和 ScienceBenchmark；每库约 25% 训练 / 75% 测试。六套设定合计 **1785** 条测试。
- Current SQL 正确 1130/1785（63.31% EX）。MIRA 到 **76.92%（+13.61）**；修好 **261**，改坏 **18**。
- 分基准：BIRD **+16.53**（192 修 / 8 坏），ScienceBenchmark **+8.78**（69 修 / 10 坏）。
- 同协议下 MAGIC 总体 +9.36（247 修 / **80** 坏）；TK-Boost adapted +3.42（239 修 / **178** 坏）。MIRA 的卖点是修得并不更多，但 **改坏少一个数量级**。
- 消融（BIRD–DeepEye 371 题）：去掉切分、去掉证据激活、去掉局部适配，EX 分别掉 5.39 / 3.50 / 4.58 个点。切分主要影响「能修多少」，激活主要影响「别乱改对的」。
- 261 次成功修复里 **225（86.2%）** 只用一条 item，36 次是 2–3 条组合。

**对 slides 的含义。** 和 TK-Boost 同属细粒度 knowledge，但接法不同：MIRA 是 **生成结束之后的 pluggable corrector**，用库证据当闸门。画流程图时箭头在 **final SQL 交出去之前的校正框**，并单独标「先验证再改」。代码未公开，要跟作者要。

---

## 4. SQLFixAgent（Cen et al., AAAI 2025）

**引用。** Jipeng Cen, Jiaxin Liu, Zhixu Li, and Jingjing Wang. 2025. SQLFixAgent: Towards Semantic-Accurate Text-to-SQL Parsing via Consistency-Enhanced Multi-Agent Collaboration. In AAAI. 49–57. doi:10.1609/aaai.v39i1.31979. arXiv:2406.13408。

**一句话。** 微调 SQL 模型语法往往过关、语义经常不对。SQLFixAgent 用三个 agent 查错、出候选、再选最终修复；选的时候检索 **相似历史 repair**，失败则写入 **failure memory** 再试。

**三个 agent。**

- **SQLReviewer**：小黄鸭式逐段对照用户意图，抓语义不一致（语法错则靠连库检查）。没发现问题就原样返回。
- **QueryCrafter**：改写若干语义等价的用户问句，调 fine-tuned **SQLTool** 生成多条候选 SQL，预执行过滤后交给 Refiner。
- **SQLRefiner**（核心）：从 SQLTool 的 **error record** 里 `retrieve` 相似 repair 当例子，在候选中选出最贴用户问题的一条；候选无效时自己改。执行不过关则把 SQL 和库报错写入 failure memory，下一轮 reflexion，直到通过或达到 `maxTryTimes`。

**经验产物（对应 prompt 2a）。** 可复用的是 **历史错误–修复记录**（论文用 Spider / BIRD 训练集模拟 runtime error record），加上当次会话的 failure memory。不是全局 guideline，也不是绑到表列的 tribal knowledge。接到流程的位置是 **已经有一条可疑 SQL 之后的检测与修复**，不是探库前的规划。

**论文数字（slides 不作横比）。** 五个基准：BIRD、Spider、Spider-DK / Syn / Realistic。摘要写 BIRD 上相对基线 EX 提升超过 3%（一处报道 CodeS-3B/7B + GPT-3.5 约 +3.65 / +3.00）。强调 token 效率高于若干多 agent 方法。

**对 slides 的含义。** 第一列卡片必须写 **SQLFixAgent**，不要写成「SQL-Agent」或「不是一篇论文」。与 MAGIC / TK-Boost / MIRA 并列时，它代表「把 repair 执行记录拿来检索」，粒度最粗。

---

## 5. 四篇对照（写 slides 时第 5–6 页）

| | SQLFixAgent | MAGIC | TK-Boost | MIRA |
| --- | --- | --- | --- | --- |
| 经验产物 | SQLTool 错误记录 + 当次 failure memory | 一份全局 self-correction guideline | 带 applicability 的 tribal knowledge 规则 | 按库的独立 repair memory item |
| 粒度 | 整次 repair 轨迹 / 相似例子 | 整条 SQL / 错误模式清单 | CTE / 子查询 + 表列操作 | 一条可判定的结构改动 |
| 检索键 | 当前题与历史 error record | 不用检索，全题共用 | 当前 CTE 的 SQL 特征 | 问题语义 + 当前 SQL 结构 |
| 防误用 | 执行检查 + failure memory | 弱（清单可能改对的 SQL） | FilterKnowledge；仍可能跨库用错 | 库证据激活 + 校验失败则保留原文 |
| 接到流程 | 查错后选候选 / 再修 | 生成后对整条自检改写 | first-attempt 之后逐 CTE | 上游 SQL 的事后校正 |
| 代码 | 论文未作为本仓库评测对象 | 公开 | 未公开（本仓库已拿到） | 未公开 |
| 论文主数字 | BIRD EX 相对基线约 +3% | DIN-SQL BIRD 56.52→59.13 | 原设定最高 +16.9 / +13.7 | 共同校正协议 +13.61 EX，261 修 / 18 坏 |

共同前提：MAGIC / TK-Boost / MIRA 离线编经验时都要历史 **错误 SQL ↔ 正确 SQL**。SQLFixAgent 的可复用库是 **SQLTool 运行错误记录**（论文用训练集模拟），当次 failure memory 不跨题蒸馏成规则。

---

## 6. 和「探库 → initial SQL → 多轮改写 → final SQL」怎么对齐

Text-to-SQL agent 典型路径：问题 + 库 → 探库（`sqlite_master` / `PRAGMA` / 抽样）→ **initial SQL** → 多轮改写 → agent 宣布 **final SQL**。

经验可以插在这些位置：

1. **整条查询的规划与改写（MAGIC）。** Guideline 是全局的，适合在写 initial SQL 时当检查清单，也适合每一轮「整条重写」时再用一遍。
2. **片段级改写（TK-Boost / MIRA）。** 规则绑定表、列、子句；TK-Boost 在 CTE 循环里改 subquery；MIRA 在整条已写出的 SQL 上做经验验证后的局部补丁。
3. **查错与选修复（SQLFixAgent）。** Reviewer 判定有错之后，用历史相似 repair 帮助 Refiner 在候选里做选择；画在 revised SQL 那一段，不要画进探库之前。

可以叠：SQLFixAgent 管「拿历史 repair 例子帮这次选修复」；MAGIC 管「这类题别再犯清单上的错」；TK-Boost / MIRA 管「这个 CTE / 这个 JOIN 在这个库里该怎么写」。MIRA 相对 TK-Boost 多了一道「当前库是否真的出现了这条错误」的闸门。

---

## 7. 写 slides 时不要混的几件事

- **增益不可横比原论文封面数字。** MAGIC 的 +2.6 是在已经很强的 DIN-SQL 上加 guideline；TK-Boost 的 +16.9 是弱 agent + Spider 2.0；MIRA 的 +13.61 是三个上游、统一事后校正接口。横比请用 MIRA Table 1，或本仓库自己的臂 A/B。
- **TK-Boost「修 239 / 坏 178」只属于 MIRA 的 adapted 评测**，不是 Berkeley 技术报告 Fig. 6 的设定。
- MAGIC 编 guideline **必须看 gold**；推理时 guideline 不再看 gold。MIRA 编 memory 看历史 \(s^+\)，校正当前题不看 gold。
- **SQLFixAgent 是第四篇论文**（Cen et al., AAAI 2025），slides 不要写成「SQL-Agent」或「不是一篇论文」。TK-Boost 技术报告里的 Memory / Mem0 是另一套弱基线，不要和 SQLFixAgent 混名。
- 本仓库当前实测基准对外写 **Spider**；具体数字来自 Spider 2.0 SQLite 免泄漏 86 题（`docs/results_reference_track.md`），不要改成 BIRD。
- **Slides 正文全英文。** 评测表不要裸 agent 列、不要 E/F 轮次列；Agent type 两行是 `Agent provided by repo` 与 `Candidate-feedback agent`。
