# Slides skeleton：近段工作汇总（experience-based text-to-SQL）

格式约定（第三步才实现 HTML）：上下连续划页，每页固定 16:9，类似 pptx 导出 PDF；视觉语言对齐 `semantic-catalog/docs/presentations/`（navy / teal、页眉 tag + 页码、卡片），**不用**左右方向键翻页。

**On-slide copy is English only**（标题、正文、表头、图例、流程图节点、页脚全部英文）。本 skeleton 里的「目的 / 不要」仍用中文给作者看；标成 **Copy:** 的字符串是第三步要原样或微调后放进 HTML 的英文。

数字以本仓库实测为准；论文封面增益只作背景，并标明设定。

**字号硬约束：所有可见文字至少 30px**（含页眉 tag、页码、表头、表内数字、脚注、流程图节点、图例）。设计画布 1920×1080，`--fs` 下限 30px。排不下就删字或拆页，不要缩小字体。

叙事主线不变。合计约 **10 页**（原第 5–6 页合并；评测结果仍一页）。

F 轮 Agent type 命名：**Candidate-feedback agent**。含义：refiner 的 `suggested_fix_sql` 作为候选写进反馈，仍由 agent 整合改写（对应 `--include-candidate-sql`）。与 E 轮 **Agent provided by repo**（直接采纳 refiner SQL，上游 `tkboost.sql()`）对照。

---

## 第 1 页 · Title

**Copy:**

- Tag: `Work in progress`
- H1: `Experience-based SQL correction for text-to-SQL`
- Lead: `Aligning SQLFixAgent, MAGIC, TK-Boost, and MIRA on one agent pipeline — then a small Spider eval of TK-Boost`
- Footer: `Internal notes · Sep 2026`

**不要。** 封面不放论文 +16.9。

---

## 第 2 页 · Agenda

**Copy:**

- H2: `Agenda`
- `1  What a text-to-SQL agent does`
- `2  Three kinds of experience artifacts (four papers)`
- `3  Where they attach on the same pipeline`
- `4  Local plan and TK-Boost results on Spider`
- `5  Next: MAGIC, MIRA, then a possible combination`

**不要。** 目录页不写方法细节。

---

## 第 3 页 · Pipeline（prompt 第 1 点）

**目的。** 无经验的基线流程图。

**Copy / 节点标签（英文，≥30px）：**

- `User query + database`
- `Schema probes`（`sqlite_master` / `PRAGMA` / sample rows 可作副标签）
- `Initial SQL`（subtitle: `may include CTEs`）
- `Revised SQL`（subtitle: `one or more rewrites`）
- `Final SQL`（subtitle: `agent decides when to stop`）

H2: `The text-to-SQL agent loop`

**不要。** 本页不加论文注入箭头（留给合并后的第 6 页）。

---

## 第 4 页 · Offline experience loop（prompt 第 2 点）

**Copy:**

- H2: `Where the experience comes from`
- Left: `Wrong SQL on past tasks`
- Center: `Attempted repairs vs confirmed-correct SQL`
- Right: `Reusable artifacts`
- Foot: `No parameter update on the target database`

**口述。** SQLFixAgent 存的是 SQLTool error records，不一定是 gold 蒸馏规则。

---

## 第 5 页 · 三类产物 + 各篇机制（原第 5–6 页合并）

**目的。** 一页三列：产物形态、代表论文、一句机制。第一列是 **SQLFixAgent**（AAAI 2025），不要写成 SQL-Agent。

**Copy — 三张卡片：**

**Card A — Repair traces**

- Title: `Repair traces`
- Papers: `SQLFixAgent (Cen et al., AAAI 2025)`
- Stores: `SQLTool error records and similar repairs; session failure memory`
- How: `SQLReviewer checks intent; QueryCrafter proposes candidates; SQLRefiner retrieves similar repairs and retries from failure memory`

**Card B — Global guideline**

- Title: `Global guideline`
- Papers: `MAGIC`
- Stores: `One shared self-correction checklist`
- How: `Feedback / Correction / Manager compile the list; at inference the model audits the whole SQL`

**Card C — Fine-grained knowledge**

- Title: `Fine-grained knowledge`
- Papers: `TK-Boost · MIRA`
- Stores: `Rules or memory items tied to tables, columns, clauses`
- How: `TK-Boost retrieves per CTE and feeds the same agent. MIRA activates items with DB evidence and keeps the original SQL if the patch fails.`

页上不要放论文封面 EX。不要和 TK-Boost 的 Mem0 基线混名。

若 30px 三列装不下：机制句放到每张卡片第二行，删副标题，仍保持一页。

---

## 第 6 页 · Overlay on the pipeline（原第 7 页）

**Copy:**

- H2: `Where experience can help`
- Same nodes as slide 3
- Teal band — `MAGIC: guideline for planning, initial SQL, and whole-query rewrites`
- Amber band — `TK-Boost / MIRA: CTE-level (or subquery) edits after the first SQL exists`
- MIRA dashed box before Final SQL — `Evidence gate; keep original SQL if verification fails`
- Slate band — `SQLFixAgent: similar-repair retrieval during detect-and-fix`（on Revised SQL only, not before probes）

Lead: `These attachments are a candidate design, not a system we have built.`

---

## 第 7 页 · Combination hypothesis（原第 8 页）

**Copy:**

- H2: `A combination is plausible — we will not build it first`
- Bullets:
  - `Repair traces may help pick among candidate fixes`
  - `A global guideline may help initial SQL and whole-query rewrites`
  - `Fine-grained rules may fix CTE-level mistakes`
  - `An evidence gate may reduce harming already-correct SQL`
- Foot: `Evaluate each method on a small local split before stacking them`

---

## 第 8 页 · Local evaluation plan（原第 9 页）

**Copy:**

- H2: `Local evaluation plan`
- Lead: `Benchmark: Spider`（不写 BIRD；实现注释：数字来自 Spider 2.0 SQLite、86 no-leak）

| Method | Code | Status |
| --- | --- | --- |
| TK-Boost | Private; we obtained the repo | Evaluated (follow upstream behavior) |
| MAGIC | Public | Evaluation started |
| MIRA | Private; request from authors | Not started |
| SQLFixAgent | Related work only | Not in this eval |

**不要。** 本页不放结果数字。

---

## 第 9 页 · TK-Boost on Spider（原第 10 页；无裸 agent、无轮次列）

**目的。** 只比臂 A / 臂 B。行用 Agent type 区分两条实现，不要 E/F 列，不要裸 agent 列或裸 agent 正确数。

**Copy — 臂定义（两行即可）：**

- `Arm A: same refiner, no tribal knowledge`
- `Arm B: same refiner, TK-Store injected`
- Metrics: `B − A`, `fixed`, `harmed`
- Setting: `Spider. Bugs in the upstream repo were fixed; runs still follow repo behavior.`

**Copy — table（§13–§14 数字；列名如下）：**

| Agent type | Arm A | Arm B | Δ knowledge | Fixed | Harmed |
| --- | --- | --- | --- | --- | --- |
| Agent provided by repo | 34 | 37 | **+3** | 4 | 1 |
| Candidate-feedback agent | 35 | 35 | **+0** | 3 | 3 |

- **Agent provided by repo** = 直接采纳 `suggested_fix_sql`（上游实现，不是论文 Alg 5）。
- **Candidate-feedback agent** = 候选 SQL 进反馈，agent 仍负责改写。

表下（英文，不要引用裸 agent 的 36 / −2 / +1）：

- `With the repo agent, knowledge helps Arm A (+3; 4 fixed / 1 harmed).`
- `The candidate-feedback agent does not recover that +3 (3 fixed / 3 harmed).`
- `Arm A is a weak baseline; we do not yet know if a stronger agent would still gain.`
- `Do not read +3 as validation of the stacked design.`

**不要。** 表中不要 `Round` / `E` / `F` / `Bare`。不要 README +16.9。不要 A–D / H / J 全表。

---

## 第 10 页 · Next（原第 11 页）

**Copy:**

- H2: `Next`
- `1  Finish the MAGIC mini-eval on the same Spider split`
- `2  If we obtain MIRA, test whether the evidence gate cuts harm`
- `3  Stack methods only if the separate signals hold; otherwise strengthen the base agent first`

Foot: `The combination is a hypothesis. What we have now is a pipeline map plus a cautious TK-Boost signal on Spider.`

---

## 第三步实现时的版式备注（现不写 HTML）

- **Language: English only on the slides.**
- 纵向滚动，`scroll-snap-type: y mandatory`，每页 100vh。
- 内容区 1920×1080 居中等比缩放。
- **font-size ≥ 30px** everywhere；禁止 `small` / 12–18px 页脚。
- 流程图用 SVG 或纯 CSS；不要 `keydown` 翻页。
- 第 6 页与第 3 页共用节点坐标。
- 第 9 页数字对齐 `docs/results_reference_track.md` §13–§14；主文基准写 **Spider**；表只有 Agent type / Arm A / Arm B / Δ / Fixed / Harmed。
- 不收入 H/J 轮。
