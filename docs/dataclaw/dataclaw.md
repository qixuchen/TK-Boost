# DataClaw 调研：运行环境、Agent、已有轨迹

读 `~/DataClaw` 的代码与产物、以及 `~/archive/DA_Workflow` 的历史输出得出。调研日期
2026-09-25，DataClaw 当前 HEAD 是 `453550c`（`Port generic skill injection and process
regrade from DA_Workflow`），remote 是 `git@gitcode.com:qixuchen/DataClaw-bench.git`，
upstream 是 `https://github.com/GTML-LAB-sysu/DataClaw.git`。

结论先说：**有 Docker image，而且本机已经 build 好了**（`dataclaw:0.1.0`，2.5 GB）；
**agent 是 OpenClaw CLI**（npm 包 `openclaw@2026.3.24`），不是自研 agent；
**有已跑好的轨迹，但不在 `~/DataClaw/output` 里，而在 `~/archive/DA_Workflow/output`**，
共 **362 个 run / 70 个 task / 340 份 `chat.jsonl` / 79 MB**。

---

## 1. Docker image 与 runner

### 1.1 image

`~/DataClaw/Dockerfile` 只有 26 行，做四件事：Ubuntu 22.04 基础镜像、装
`python3 / git / curl / tini`、装 Node 22、然后 `npm install -g openclaw@2026.3.24`，
最后建出 `/tmp_workspace` 和 `/root/.openclaw/workspace` 两个目录。

也就是说 image 里**没有 benchmark 代码、没有数据、没有 agent 逻辑**，它只是一个装了
OpenClaw CLI 的干净容器。任务数据、prompt、skill 都是 run 的时候用 `docker cp` 拷进去的。

image tag 由 `DOCKER_IMAGE` 环境变量决定，`dataclaw/utils/docker_utils.py:30` 的默认值是
`dataclaw:0.1.0`。本机 `docker images` 实测已有这个 tag：

| IMAGE | ID | DISK USAGE |
| --- | --- | --- |
| `dataclaw:0.1.0` | `73f18a8e8938` | 2.5 GB |

所以**不需要重新 build 也不需要下载**就能跑。另外 `docker ps -a` 里还留着一个两个月前的
退出容器 `task_383_hypothesis_verification_hard_hard011_glm-5.2_20260710_1759_e38ace`，
说明这套流程在本机真实跑过。

image 的分发方式有两条：`script/docker_save_image.sh` 会 `docker build` 再
`docker save` 成 `Images/dataclaw_ubuntu_v<version>.tar`（`Images/` 目录当前只有
`.gitkeep`，本地没有 tar 包）；README 第 83–91 行则说从 Releases 下载预构建 tar 再
`docker load`。数据集本身发布在
[HuggingFace GTML-Lab/DataClaw](https://huggingface.co/datasets/GTML-Lab/DataClaw)。

### 1.2 runner

宿主机上的编排器是 `dataclaw/eval/run_batch.py`（1089 行），**一个 task 一个容器**。
`run_single_task()` 的九步流程（`run_batch.py:398-604`）：

1. `start_container` —— `docker run -d --name <task_id>_<suffix> dataclaw:0.1.0`，容器里
   跑 `tail -f /dev/null` 挂着
2. `setup_workspace` —— 把 task 声明的 `workspace_files` 从 `assets/` 拷进
   `/tmp_workspace`，再把 `/root/.openclaw/workspace` 软链到它
3. `onboard_openclaw` —— `openclaw onboard --non-interactive`，写入 API key、
   gateway 端口；随后打三个 patch（流式 usage 兼容、模型 contextWindow/maxTokens/cost、
   Brave web_search）
4. 可选 `inject_workflow_skill` —— 把宿主机的 `SKILL.md` 拷成
   `/tmp_workspace/skills/dataclaw-workflows/SKILL.md`，frontmatter 的 `name` 必须是
   `dataclaw-workflows`，并记录 sha256
5. `start_gateway` —— 容器内后台跑 `openclaw gateway run --bind loopback --port 3333`
6. `set_model` —— `openclaw models set <provider/model>`
7. 跑 agent —— prompt 先 `docker cp` 成 `/tmp/agent_prompt.txt`（绕开命令行长度上限），
   再 `openclaw agent --session-id chat --timeout N --message "$prompt"`
8. 收 transcript（`docker cp /root/.openclaw/agents/main/sessions/chat.jsonl`）、
   算 token/cost、检查 transcript 里有没有 API 报错；没报错才进 LLM judge 打分，
   再做 process grading
9. `docker rm -f` 删容器

CLI 的主要开关：`--model` / `--judge` / `--parallel` / `--suite` / `--task` /
`--runs` / `--resume` / `--timeout-multiplier` / `--skill-path`。
入口脚本 `script/run.sh` 读 `.env` 的 `DEFAULT_MODEL` 和 `DEFAULT_PARALLEL`。

### 1.3 task 集合

`tasks/` 下 **492** 个 `.md`，`assets/qa_gold/` 下 **492** 个 gold JSON，`assets/` 共 243 MB。
按类别 × 难度分布：

| 类别 | easy | medium | hard |
| --- | --- | --- | --- |
| enterprise_industry_analysis | 115 | 111 | — |
| enterprise_industry_policy_analysis | 10 | 66 | — |
| comprehensive_decision | 6 | 45 | 19 |
| international_comparison | — | 25 | 14 |
| industry_planning | — | 14 | 14 |
| hypothesis_verification | — | 14 | 15 |
| risk_assessment | — | 11 | 13 |

gold JSON 的字段是 `answer` / `question` / `guidelines` / `milestone` / `steps` /
`steps_num` / `metadata`，即除了唯一答案还带**里程碑和参考步骤**，这是 process grading
的依据。

---

## 2. Agent 是什么

**OpenClaw，通过它的 CLI 以 agent 模式驱动。** 三处证据：Dockerfile 第 21 行
`npm install -g "openclaw@${OPENCLAW_VERSION}"`（`OPENCLAW_VERSION=2026.3.24`）；
`docker_utils.py` 里全部用 `openclaw onboard` / `openclaw gateway run` /
`openclaw models set` / `openclaw agent` 这几个子命令；README 第 37 行写明
"DataClawBench adopts OpenClaw as its unified agent framework"。

benchmark 自己**不实现 agent 循环**——探库、写代码、读文件、多轮反思全在 OpenClaw 内部，
宿主机只负责发一条 prompt 然后等 transcript。这跟 TK-Boost 的 `sql_agent_runner.py`
自己控制 turn 循环是完全不同的形态。

模型是可换的，跟 agent 框架解耦。三种接法（README 表格）：OpenRouter
（`OPENROUTER_API_KEY`）、自定义 OpenAI 兼容端点（`OPENCLAW_CUSTOM_BASE_URL` +
`OPENCLAW_CUSTOM_API_KEY` + `OPENCLAW_CUSTOM_MODEL_ID`）、以及给 judge 单独配一个端点
（`JUDGE_CUSTOM_*`）。`.env` 里当前走的是自定义端点那条。

打分也是 OpenClaw agent，只是换了 agent id。judge 在**同一个容器**里以
`openclaw agent --agent judge --session-id judge_chat` 运行（`docker_utils.py:533-583`）。
两层评分：

- **outcome**：`dataclaw/utils/grading.py`，`grading_type` 实测全是 `llm_judge`，
  产出 `score.json`（`score` / `max_score` / `breakdown` / `notes`）
- **process**：`dataclaw/utils/process_grading.py`，算 GPR（Goal Progress Rate，按 gold
  `milestone` 逐条判是否达成）和 TGPR（带时序的版本），产出 `process_score.json`。
  `dataclaw/eval/regrade_process.py` 可以对已有轨迹**离线重算** process 分，不用重跑 agent

---

## 3. 已跑好的轨迹在哪

### 3.1 位置：`~/archive/DA_Workflow/output`，不是 `~/DataClaw/output`

`~/DataClaw/output` 里只有 **1 个 task、2 个 run 目录**（其中一个还是空的），加一份
`summary_glm-5.2.json`，总共 160 KB —— 那是 8 月 25 日的一次 smoke 跑，不是历史数据。

真正的历史轨迹在前身仓库 `~/archive/DA_Workflow/output`。它的 `ARCHIVED.md` 写着：

```
This tree is frozen.
- Git tag: archive/da-workflow-final-20260825
- Historical runs: ./output  (also symlinked from ~/DataClaw/output)
```

**注意这句里的软链说法已经失效**：`readlink -f ~/DataClaw/output` 返回它自己，是个真实
目录而不是软链。要用历史轨迹得显式走 `~/archive/DA_Workflow/output` 这个路径。

### 3.2 规模

| 指标 | 值 |
| --- | --- |
| 总体积 | 79 MB |
| task 数 | 70（benchmark 共 492，覆盖 14%） |
| run 数 | 362 |
| `chat.jsonl`（agent 轨迹） | 340 份，合计 62.4 MB，中位数 106 KB，最大 2.5 MB |
| 有 outcome 分 | 339 |
| 有 process 分 | 338 |
| 时间跨度 | 2026-07-10 → 2026-08-20（7 月 51 个 run、8 月 311 个） |
| 每 task run 数 | 最少 1、中位数 2、最多 17 |

模型只有一个：**glm-5.2**。但分成两个臂，靠目录名区分：

| run 目录前缀 | run 数 | 含义 |
| --- | --- | --- |
| `glm-5.2_with_skill` | 266 | 注入了 `SKILL.md`（`--skill-path`） |
| `glm-5.2` | 96 | 裸 agent，无 skill |

目录命名是 `<task_id>/<model><_with_skill>_<YYYYMMDD>_<HHMM>_<6位hex>/`。

两臂的 outcome 分（读 339 份 `score.json` 算的）：

| 臂 | n | 平均分 | 满分数 | 零分数 |
| --- | --- | --- | --- | --- |
| `with_skill` | 245 | 0.767 | 188 | 57 |
| 裸 agent | 94 | 0.630 | 58 | 33 |

这个差值**不能直接当 skill 的增益读**：两臂的 task 集合和 run 次数都不对齐（有的 task
跑了 17 次、有的 1 次，`with_skill` 的 run 数是裸 agent 的 2.8 倍），要归因得先按 task
配对。

### 3.3 每个 run 存了什么

以 `task_054_comprehensive_decision_medium_medium029/glm-5.2_with_skill_20260820_1446_13a0a3/`
为例，8 个文件：

| 文件 | 内容 | 覆盖率 |
| --- | --- | --- |
| `chat.jsonl` | **agent 轨迹主体**，OpenClaw session 格式 | 340/362 |
| `judge_chat.jsonl` | outcome judge 的对话 | 339/362 |
| `score.json` | outcome 分（`score` / `max_score` / `breakdown` / `notes`） | 339/362 |
| `process_score.json` | GPR / TGPR，含逐里程碑的 `achieved` 与 `reason` | 338/362 |
| `judge_process_chat.jsonl` | process judge 的对话 | 305/362 |
| `usage.json` | token 数与 cost、`elapsed_time` | 340/362 |
| `agent.log` | agent 子进程 stdout | 341/362 |
| `gateway.log` | OpenClaw gateway 日志 | 341/362 |

`chat.jsonl` 是逐行 JSON，`type` 有 `session` / `model_change` /
`thinking_level_change` / `custom` / `message`。上例 31 行里 27 行是 `message`，角色分布是
`user` 1、`assistant` 9、`toolResult` 17 —— 也就是**工具调用与工具返回都在轨迹里**，
可以还原 agent 读了哪些文件、跑了什么代码。

`usage.json` 长这样：

```json
{
  "input_tokens": 135312,
  "output_tokens": 1496,
  "cache_read_tokens": 415936,
  "cache_write_tokens": 0,
  "total_tokens": 552744,
  "cost_usd": 0.215612,
  "request_count": 9,
  "elapsed_time": 70.25
}
```

`process_score.json` 的 `gpr.details` 是逐里程碑的判定，带 `expected` / `achieved` /
`evidence_type` / `reason`，还有 `break_point`（agent 在第几步断链）和 `chain_summary`。
这个结构对"失败在哪一步"的分析比单个 outcome 分有用得多。

### 3.4 缺口

22 个 run 目录没有 `chat.jsonl`（362 − 340）。上例中
`~/DataClaw/output/.../glm-5.2_20260825_1131_a2f590/` 就是个空目录，属于起容器后失败、
没收到 transcript 的情况。另有 34 个 run 有 outcome 分但没有
`judge_process_chat.jsonl`，多半是 process grading 走的离线重算路径。

`~/archive/DA_Workflow` 整棵树是 `dr-xr-xr-x` 只读权限，所以读取安全，但要写任何衍生
产物得输出到别处。另外 `~/archive/DA_Workflow` 还有一个 `artifacts/` 目录（gate4 系列
实验），以及 `workflow_gen`（按 `ARCHIVED.md` 只存在于这里，没有搬到 DataClaw）。

---

## 4. 对 TK-Boost 的可复用性（初步判断，未验证）

有利的三点：轨迹格式统一、带工具调用明细，可以直接当 correction 的输入；
`process_score.json` 已经标出断链位置和原因，相当于免费的失败诊断；
两臂（有/无 skill）结构与 TK-Boost 的臂 A / 臂 B 同构，`--skill-path` 就是现成的
知识注入口。

需要留意的三点：轨迹只覆盖 70/492 个 task 且 run 次数极不均衡，做配对比较要先筛；
只有 glm-5.2 一个模型，跟 Spider2 那几轮用的 gpt-4.1 不同口径；
DataClaw 没有 SQL 这一层抽象（任务是对 CSV/JSON 做分析），TK-Boost 现有的
`sql_operations` / `table` / `column` 四维标签和 `compare_pandas_table` 比较器都不能直接搬。

---

## 5. 将 TK-Boost knowledge generation 适配到 DataClaw 的设计

### 5.1 决策：保留 OpenClaw，移植 knowledge pipeline

实验中的执行 agent 应继续用 **DataClaw 的 OpenClaw**，不把 TK-Boost 自带的 SQL agent
搬过来。TK-Boost 的 `run_agent` 是 SQL 专用、文本标签驱动的 ReAct 循环：模型只能输出
`<think>`、`<sql>` 或 `<solution>`，runner 只会执行 SQL 并回传 `SQL_RESULT_TABLE` 或
`SQL_ERROR`。DataClaw 则要求 agent 自主读 CSV / JSON、运行分析代码、处理政策名称与中英
对照表；把它们先灌进 SQLite 会改变 benchmark 的执行协议，也不能与既有 OpenClaw 的轨迹
和分数并排比较。

应移植的是 TK-Boost 的离线链路：

```text
裸 OpenClaw 轨迹 + gold / process feedback
  → 失败诊断与规则抽取（DataClaw populate）
  → 带来源范围的 knowledge store
  → 任务级候选检索 + LLM FilterKnowledge
  → 只注入待测 OpenClaw agent 的 prompt
  → outcome + process 评测
```

这与现有 DataClaw 的 `--skill-path` 不冲突：可以把后者作为单独的 workflow-skill
基线，但 TK-Boost 规则应按**每个任务**动态检索，不能靠全 run 共享一份静态 SKILL.md。

### 5.2 固定划分、轨迹收集与正误闸门

1. 在 492 个 task 上先生成并提交固定的 train / test split；不从已有 70 个历史 task
   覆盖中反推划分。因为规则从 train 的 gold 和失败中学习，split 必须先于 populate 固定。
   可以随机切分，但按 `category × level` 分层：7 个 category、easy / medium / hard 的组合
   分布不平衡，最小的 `comprehensive_decision × easy` 只有 6 题。
2. 训练轨迹应运行**裸 OpenClaw**。`with_skill` 失败轨迹已经受旧 skill 影响，不能作为
   “基础 agent 犯了什么错”的无偏来源。已有 96 份裸 glm-5.2 run 可以先做 adapter /
   populate 的 smoke test，正式结果仍应在固定 train 集上重跑并记录模型版本。
3. 正误闸门以 `score.json` 的 outcome `match` 判定，`match=0` 才进入 populate；
   `match=1` 跳过。这对应 TK-Boost 的“只从 agent 错误学习”规则。
4. `process_score.json` 不替代 outcome 闸门，但提供补充监督：`break_point`、
   `chain_summary` 和逐 milestone 的 `reason` 说明 agent 在哪一步偏离参考流程。

不存在完全重复的题面：492 道问题归一化后没有重复。但企业实体重复很多：277 题点名公司，
其中 107 家被不止一题引用；最大共享公司连通块有 79 题。若 25% 随机取 train，10 个 seed
的模拟中，369 道 test 题有 78–98 道的公司名同时出现在 train 的问题或 gold 中。因此不能
把“同一公司不能跨 split”作为划分规则，否则会让大块任务无法平衡；必须通过 populate 的
**规则防泄漏检查**保证规则不含公司名、政策名、具体中间值或答案。

### 5.3 DataClaw populate adapter

现有 `tkstore.populate` 假设输入是 `execution_query.sql`、SQL 执行结果和 gold SQL；
其 `generate_memory_diff_first_turn` prompt 也以 CTE diff 为中心。这一层不能直接复用，
需要写 DataClaw 专用 adapter 和 prompt：

| SQL pipeline 的输入 | DataClaw 的替代输入 |
| --- | --- |
| agent SQL 与 `messages.json` | `chat.jsonl` 中的 assistant 轨迹、工具调用和 tool result，外加最终答案 |
| gold SQL | `answer`、`steps`、`milestone`、`guidelines` |
| SQL / CTE diff | agent 走过的步骤与 gold `steps` 的过程差异，优先使用 process grader 的 `break_point` 与 `reason` 定位 |
| SQL rule | 可迁移的数据分析操作规则和检错规则 |

抽取器应让 LLM 形成“错误行为 → 为什么错误 → 下次应该检查什么”的规则，而不是重述某道题
的答案。规则不得含有 gold 中的特定答案、确切实体、政策标题、企业名或中间计数；这些字段
需要以自动扫描加 LLM review 双重检查。比如 `task_054` 的 gold 有具体行业名称和每类企业
计数，直接写入 rule 就是把训练答案泄漏给 test。

另外，task_054 的 output guideline 写“只能输出 yes/no”，其 gold answer 却是 `416`。
这类题面 / gold 不一致会让“输出格式规则”被学坏，populate 前需要扫描并隔离这类样本，
或至少在生成规则时显式禁止从格式冲突处归纳通用规则。

### 5.4 重定义 scope 与索引字段

不能原样沿用 TK-Boost 的 `db` scope。实测 492 个 DataClaw gold 的 metadata 只有
`db` / `category` / `level` 三个字段，且 `metadata.db` 全部是 `bm_rag_qa`；没有
subset、table set 或下一级 database 标识。`bm_rag_qa` 是整套 benchmark data environment
的统一名字，不是可用于检索隔离的库名。因此按它过滤等于每条 db rule 都进入所有 task，
没有任何隔离效果。

`enterprise`、`industry`、`policy` 也不是 task 的 metadata，而是
`assets/database/` 下的一级 theme-domain 目录；其下有 7 个 secondary theme：
企业 profiles / core competitiveness / business status，行业 regional / national，政策
release status / full text。另有根目录的 `internal_metrics.csv`（内部业务逻辑知识库）和
中英对照 JSON。每一个 task 的 `workspace_files` manifest 都相同：**492/492 都挂载相同的
19 个文件**。agent 题面通常只说 “Only use files under `./database/`”，不会声明“本题使用
industry + policy”；frontmatter 的 `category` 也不会进入 `Task.prompt`。

因此“按当前 task workspace 与 rule 的数据源相交”在当前版本也没有筛选力。rule 的标签先按
下面的优先级设计，为后续实际探索驱动的检索做准备：

- **`file_scope`（主标签）**：规则涉及的具体 CSV / JSON 文件，如 `company_profile.csv`，
  放在 store 的 `table` 列；涉及具体列时放 `column` 列。不针对某个文件的数据环境规则
  （比如“公司名是拼音，中文名要查中英对照表”）填 `all`。主题域（enterprise / industry /
  policy）可以直接从文件路径推出，不单独设标签。
- **`task_category`（保留，辅助）**：7 个题型。它描述的是题型而不是数据源，区分度不如
  `file_scope`，只作为辅助信息保留，不参与首轮过滤。
- **不设 operation tags。** TK-Boost 的 `sql_operations` 在 DataClaw 上没有对应物，
  首轮用不上。

从 gold `steps` 统计的参考路线说明文件粒度有潜在价值，但只能用于 train 标注，不能在 test
检索时当真值：企业相关 431 题、行业相关 199 题、政策相关 148 题；其中
`company_profile.csv` 被 389 道 gold 路线使用，`company_operation_status.csv` 197 道，
`policy_resource.csv` 44 道。主题域太粗，文件粒度才有可能提供过滤力。

### 5.5 任务级 retrieve、FilterKnowledge 与注入

TK-Boost 当前的 retrieve 是对某个 CTE 文本做候选匹配，随后
`_llm_filter_relevant_rules` 用“SQL CTE validator”提示词二次筛选。DataClaw 没有 CTE，
而且从问题预测所需文件不可靠。因此实现分阶段推进：

1. **最小 baseline：不做过滤。** 所有 train-derived rule 直接作为一个明确标记的
   `TRIBAL_KNOWLEDGE` 块，注入 test agent prompt。它量的是“全量知识注入”的净效应，
   也是之后 FilterKnowledge / 文件检索的下限对照。
2. **任务级 FilterKnowledge：** 之后再以问题、输出约束、候选规则及其 `file_scope` 作为
   输入，重写 SQL CTE validator 的筛选 prompt。该步骤不把“预测文件”当硬过滤条件；
   FilterKnowledge 可以保留题面看似没有点明的、但真正有用的规则。
3. **探索驱动的文件检索：** 最后为 agent 提供或包装一个 probe-file / probe-table tool。
   agent 真正探查某个 CSV 的表头、样本或值域时，tool response 同时返回该文件相关的规则；
   可在该时点做 FilterKnowledge。这个方案不依赖先验文件预测，并最接近 TK-Boost 在 refiner
   已经看到 CTE 后再检索知识的时机。

第三阶段等价于“为每个 CSV 放说明文件、agent 探索到 CSV 后再读说明”的思路，但 tool
response 的送达保证更强：agent 可自行决定不读一个 `KNOWLEDGE/company_profile.md`，而
只要它调用受包装的 probe tool，规则就会进入当前上下文。OpenClaw 也可能直接用 pandas /
shell 读文件、绕开该 tool；是否能注册或拦截这种自定义工具须单独验证，不能作为首版依赖。

不得直接修改 `task.prompt`。`run_batch.py` 既把 `task.prompt` 写进 agent 的
`/tmp/agent_prompt.txt`，又把同一个字段作为 `grade_task(..., task_prompt=...)` 的 judge
输入。若修改该字段，judge 也会看见知识，评分就被污染。实现应单独构造
`agent_prompt = task.prompt + knowledge_block`，而 judge 继续收到原始 `task.prompt`。

`--skill-path` 适合做“固定 workflow skill”基线，不适合承载 task-specific retrieval：
当前 CLI 对整个 batch 只收一个 path。更重要的是，历史数据实测 245 份
`glm-5.2_with_skill` transcript 中，只有 **10** 份显式读取过
`skills/dataclaw-workflows/SKILL.md`；95 份裸 transcript 则为 0。一个命中样本显示，
agent 是主动决定通过工具读该文件，而非技能正文自动进入上下文。因此不能依赖 SKILL.md
承载最小 baseline 的规则，必须直接在 agent prompt 注入，并将 rule-store hash、注入文本
hash 与 rule ids 落盘。`with_skill` / bare 历史 run 的平均分差也不能被解读为 skill 的
因果效果，因为两组 task 覆盖和重复次数不配对，且绝大多数 skill run 未读正文。按
`~/baseline` 各方法评测批次的逐批统计见 6.3 节。

#### 必须先修复：knowledge / skill 正文没有送达 agent

这是首轮实验的**硬性前置条件**，不是可选优化。`--skill-path` 当前只把 skill 注册为
“可自行读取”的文件；agent 可以忽略它。若没有修复，B0 与 A 的差异不能解释为
TK-Boost knowledge 的效果，因为 B0 里的规则可能从未出现在 agent 上下文。

首版修复方式必须是**直接 prompt 注入**，而不是试图让 agent 更愿意读 SKILL.md：

1. `run_batch.py` 为 B0 接收一个全量 knowledge store 或已渲染的 rules 文件；
2. 每个 task 构造 `agent_prompt = task.prompt + "\n\n[TRIBAL_KNOWLEDGE]\n" + rules`；
3. 只将 `agent_prompt` 写入 `/tmp/agent_prompt.txt`；outcome / process judge 仍使用未修改的
   `task.prompt`，避免评分污染；
4. 在每个 run 目录写 `knowledge_receipt.json`，至少包含 store SHA-256、规则 id 列表、
   渲染后 prompt SHA-256、注入字符数和注入文本（或可验证的文本副本）；
5. B0 完成后逐 run 验收：`chat.jsonl` 的第一条 user message 必须包含
   `[TRIBAL_KNOWLEDGE]`，并且其 hash 与 receipt 一致。缺 receipt、缺标记或 hash 不一致的
   run 不得计入 B0 分数。

后续 B1 / B2 可以改变“哪些规则”被注入，但不能改变这个送达保证。将规则另存为
`SKILL.md`、`KNOWLEDGE/<file>.md` 或只在系统提示中列文件路径，都不能替代上述验收；
它们可以作为 agent 探索时的补充信息，却不是可归因实验的主注入渠道。

### 5.6 评测设计

DataClaw 不具备 TK-Boost 三臂里“两个 refinement arm 从同一份初始 SQL 起步”的配对条件。
知识在 OpenClaw 的第一轮就改变轨迹，所以不能把单次 A/B 的差值直接解释成知识因果。

**首轮只比较 A 和 B0，每个 task 每个臂只跑一次：**

| 臂 | 目的 |
| --- | --- |
| A：裸 OpenClaw | 主 baseline；不注入 workflow skill 或 TK rule |
| B0：OpenClaw + 全量 TK-Boost-derived rules | 首个主比较；不做 retrieval / FilterKnowledge，建立全量注入基线 |

其余 setting 优先级靠后，等 A / B0 有结果再决定是否做：

| 臂 | 目的 |
| --- | --- |
| B1：OpenClaw + FilterKnowledge rules | 衡量任务级 LLM 二次过滤相对 B0 的作用 |
| B2：OpenClaw + probe-file rules | 衡量随实际文件探索送达规则的作用 |
| C：OpenClaw + 其他方法的 SKILL.md 正文（走同一条 prompt 注入渠道） | 与 `~/baseline` 的 skill 生成方法对照，见第 6 节 |
| D：OpenClaw + workflow skill + TK rules | 测组合是否互补，不能替代 A/B 主比较 |

固定 test 集、模型、容器 image、task timeout 和 judge model。单次运行的代价是噪声大：
第 6.3 节从 `~/baseline` 的历史批次看到，20 题单次运行、内容实际没送到 agent 的情况下，
Acc 就能在 0.65 到 0.90 之间摆动。所以首轮 A / B0 的差值要结合 test 集规模和逐题翻转
（B0 修好几题、改坏几题）一起读，不能只看总分；若差值落在这个量级内，再对翻转的 task
补跑重复 run。评测同时报告 `score.json` 的 outcome 和 `process_score.json` 的 GPR / TGPR，
避免“最终答对但过程靠偶然”被误写成规则收益。指标口径沿用 `~/baseline` 的约定，见 6.4 节。

建议先做一个小而固定的 test smoke（覆盖所有 category × level），核对：agent 实际收到了
规则、judge 没看见注入内容、规则中没有 gold 泄漏、轨迹和 rule-store / prompt receipt 都已
落盘。通过后再扩到全 test。B1 / B2 增加后，receipt 还须记录候选、被 FilterKnowledge
拒绝的规则和实际 probe 到的文件。

### 5.7 实施顺序

1. 生成并冻结 task split，审计重复实体和 guideline / gold 冲突。
2. 编写 OpenClaw `chat.jsonl` adapter（复用 `~/baseline` 的转换函数，见 6.4 节），并用
   已有 96 个裸 glm-5.2 run 做离线 smoke。
3. 设计 DataClaw rule schema（`file_scope` 为主、`task_category` 辅助）以及防泄漏检查。
4. 适配 populate 的 diff / rule-generation prompt，生成一个只来自 train 的 store。
5. **先修复 skill / knowledge 送达问题：** 在 `run_batch.py` 增加全量 store 的 agent-only
   prompt injection、`knowledge_receipt.json` 与逐 run transcript 验收（5.5 节）。保持原
   CLI 行为不变。此时不改 harness 的 retrieve / probe 行为。
6. 小规模 A / B0 smoke，先确认所有 B0 run 通过送达验收和 judge 隔离，再在固定 test 全量
   上各跑一次。
7. 仅在 B0 有可解释的结果后，再考虑 B1 / B2 / C / D 与重复运行。

这是一份设计提案，不表示实现已经完成。

---

## 6. 与 `~/baseline` 的关系与可复用部分

`~/baseline`（GitCode `skillgen-baselines`）是之前在 DataClaw 上测各种 skill 生成方法的评测
仓库：适配器在 `adapters/<algo>/dataclaw_bridge/`，各方法产出的 `SKILL.md` 快照在
`results/skills/<method>/`，指标总表在 `results/metrics.md`，用法笔记在 `repo.md`。
我们沿用它的**测试方式**（生成 knowledge、注入 OpenClaw、在 test 上 A/B），但不沿用它的
setting：split、注入渠道和运行规模都要改，原因见下。以下结论来自读 `repo.md`、
`metrics.md`、各方法的 `SOURCE.md`，以及对归档轨迹的统计。

### 6.1 它的流程

用 `learn_skill_20`（20 题，列表在 `~/archive/DA_Workflow/.tmp/learn_skill_20/task_ids.txt`）
上 8 月 10 日的裸 glm-5.2 轨迹，让各 skill 生成算法产出 `SKILL.md`；再用
`run_batch --skill-path` 注入，在**同样 20 题**上与裸 agent 对比。对比的指标是 Acc、EE、
GPR、TGPR、TPE、步数和 explore / solve 拆分。

### 6.2 问题一：训练与测试是同一批题

每个方法的 `SOURCE.md` 都写 `Suite: .tmp/learn_skill_20`，而 skill 正是从这 20 题的裸
agent 轨迹学出来的。所以 `metrics.md` 的数字都是 in-suite（在训练题上测）的，天然偏乐观，
和我们在 held-out test 上的结果不可比。归档里另有 `skill_transfer_holdout_20`，但
`metrics.md` 没有用它。

### 6.3 问题二：多数方法的 skill 正文没有送到 agent

OpenClaw 的 system prompt 只列出 skill 的名字、描述和位置，正文要 agent 自己决定去读。
宿主机 OpenClaw 2026.5.28 的 `buildSkillsSection` 写的是：

> Scan <available_skills>. If one clearly applies, read its SKILL.md at exact
> <location> … If none clearly apply, read none.

容器里是 2026.3.24，措辞可能略有差异，但下面的轨迹证据不依赖版本。按 `metrics.md` 记录
的评测批次，逐份检查 `chat.jsonl` 里是否出现 skill 名或从正文均匀抽取的 6 句原文：

| 方法 | skill 正文出现在几份轨迹里 | `metrics.md` 的 Acc |
| --- | --- | --- |
| datacope | **10/20** | 0.80 |
| schema_only | 0/20 | **0.90** |
| evoskill | 0/20 | 0.80 |
| skillopt | 0/20 | 0.75 |
| trace2skill | 0/20 | 0.75 |
| skillrevise | 0/20 | 0.75 |
| skillx | 0/20 | 0.65 |
| no_skill | — | 0.68（13/19） |

7 个方法里 6 个的正文从未进入 agent 上下文，它们与裸 agent 的唯一差别是 system prompt
里那一行描述，这 6 批实际上等于裸 agent 重跑。因此：

- `schema_only` 的 0.90 不能归因于 schema 内容；表中方法之间的排序也不能解读为方法优劣。
- 这 6 批给出一个免费的噪声估计：20 题单次运行，Acc 在 0.65 到 0.90 之间摆动，约 ±4–5 题
  （混有运行随机性、不同日期的模型服务漂移和那一行描述，无法拆开）。
- `metrics.md` 对 datacope 的注释写“20/20 chat 含 skill 正文标记”，本次统计为 10/20，
  差异原因未追查。

### 6.4 对我们的影响与可复用部分

1. **注入渠道。** B0 直接把规则写进 agent prompt，不走 `--skill-path`（见 5.5 节）。若要做
   C 臂与这些方法对照，要把它们的 `SKILL.md` 正文也走同一条 prompt 注入重跑，否则比较的是
   “内容送到了”对“内容基本没送到”。
2. **split 与规模。** 用我们自己的固定 split（5.2 节），在 held-out test 上测；test 规模要远大于
   20 题，才能让单次运行的噪声不淹没 A / B0 差值。
3. **轨迹转换代码。** `~/baseline/adapters/skillopt/dataclaw_bridge/common.py` 已有可复用的函数：
   `chat_jsonl_to_conversation`（把 `chat.jsonl` 转成对话）、`select_run_dir`（同一 task 多个
   run 时选一个）、`parse_task_markdown`、`load_gold`、`gold_strings_from_items`（收集 gold
   字符串，可用于规则防泄漏扫描）。其他方法的 `convert_*.py`（`convert_failures.py`、
   `convert_traces.py`、`convert_predictions.py`）也是从同一批轨迹转换，可作参考。
4. **防泄漏约定一致。** `repo.md` 规定生成过程可用 `qa_gold`、milestone 和 `score.json` 作监督，
   但“不得把答案或里程碑期望值写入最终 `SKILL.md`”，与 5.3 节的规则防泄漏检查相同。
5. **指标口径对齐。** 沿用 `metrics.md` 的聚合方式：Acc 取全部 scored 任务的平均 outcome；
   EE 只算满分任务；GPR / TGPR / TPE 取全部 scored 任务；步数按 `chat.jsonl` 的 assistant
   turn 计；explore / solve 用 `~/baseline/results/scripts/explore_vs_solve.py` 重算。process 分
   可用 `~/DataClaw` 的 `dataclaw/eval/regrade_process.py` 离线补算。这样我们的结果能按
   同一格式并排。
6. **路径说明已过时。** `repo.md` 写 `~/DataClaw/output` 是指向归档的软链、新 run 也会写进
   归档；实测它是真实目录，新 run 写在 `~/DataClaw/output`，历史轨迹仍需从
   `~/archive/DA_Workflow/output` 读取（见 3.1 节）。

---

## 7. Runner、评分与复现命令

### 7.1 当前应使用的代码位置

运行与评分应都从 **`~/DataClaw`** 调用：

| 职责 | 当前入口 | 说明 |
| --- | --- | --- |
| runner | `~/DataClaw/dataclaw/eval/run_batch.py` | 每 task 独立 Docker 容器；运行 OpenClaw agent、收集轨迹、打 outcome / process 分、删除容器 |
| 便捷 runner | `~/DataClaw/script/run.sh` | 读 `.env` 的 `DEFAULT_MODEL` 和 `DEFAULT_PARALLEL`，转调 `run_batch.py` |
| outcome 评分 | `~/DataClaw/dataclaw/utils/grading.py:grade_task` | runner 在同一容器中调用 judge agent；写 `score.json` |
| process 评分 | `~/DataClaw/dataclaw/utils/process_grading.py` | runner 在同一容器中计算 EE / GPR / TGPR / TPE；写 `process_score.json` |
| 事后重算 process 分 | `~/DataClaw/dataclaw/eval/regrade_process.py` | 不重跑 solving agent，复用 `chat.jsonl` 和 `score.json`，仅为 process judge 起短命容器 |
| 整批汇总 | `run_batch.py:_write_global_summary` | 写 `output/summary_<model-slug>.json` |
| explore / solve 分析 | `~/baseline/results/scripts/explore_vs_solve.py` | 事后从 `chat.jsonl` 统计步数，不负责 outcome / process 评分 |

`~/archive/DA_Workflow/dataclaw/` 的 `run_batch.py`、`regrade_process.py`、`grading.py`、
`process_grading.py`、`docker_utils.py`、`lib_tasks.py` 与当前 `~/DataClaw/dataclaw/` 完全
相同（目录级 `diff -rq` 实测无差异），但 archive 是冻结树，不应作为本实验的执行入口。

`~/baseline` 不含 agent runner 或 benchmark 评分器；它保存各方法的轨迹转换 adapter、
`SKILL.md` 快照、手工维护的指标表和 explore / solve 分析脚本。`metrics.md` 没有对应的自动
汇总脚本，不能把它当 runner 的输出。

### 7.2 runner 在一次 task 中做什么

`run_batch.py:run_single_task()` 的实际顺序是：

1. `docker run` 起一个 `dataclaw:0.1.0` 容器，并复制 workspace 文件；
2. `openclaw onboard`，起 gateway，设置被测模型；
3. 将 task prompt 写为 `/tmp/agent_prompt.txt`，调用 `openclaw agent`；
4. 从容器复制 `chat.jsonl`，记录 `usage.json`；
5. 若轨迹没有 API error，调用 outcome judge，写 `score.json`；
6. 若 outcome judge 成功且 task 有 process gold，调用 process grader，写
   `process_score.json`；
7. 收集日志并删除容器。

因此 outcome 不是在 runner 结束后另跑 `evaluate.py` 得到的；它是每 task run 的组成部分。
若轨迹含 API / provider error，runner 会跳过 judge，该 run 没有有效的 `score.json`。
当前没有“只重判 outcome”的独立脚本；换 judge 或补 outcome 需要重跑，或另写对应工具。

### 7.3 A / B0 的输出目录必须隔离

`run_batch.py` 的 `OUTPUT_DIR` 来自 `OUTPUT_SUBDIR`，默认是 `~/DataClaw/output`。全局汇总
文件和续跑进度文件都只按 model slug 命名：

```text
<OUTPUT_DIR>/summary_<model-slug>.json
<OUTPUT_DIR>/progress_<model-slug>.json
```

它们不区分裸 agent、`--skill-path` 或未来的 knowledge prompt 注入。若 A 与 B0 用同一模型、
同一输出目录顺序运行，B0 会覆盖 A 的 summary；若 A 留有错误进度，B0 使用 `--resume` 还可能
错误地跳过 A 已完成的 task。A / B0 必须使用不同的 `OUTPUT_SUBDIR`，例如：

```bash
cd ~/DataClaw

# A：裸 agent；runner 参数中的 suite 在实际 split 生成后替换。
OUTPUT_SUBDIR=output_tkboost_dataclaw_A \
python dataclaw/eval/run_batch.py \
  --model "$MODEL" --suite "$TEST_SUITE" --runs 1

# B0：知识注入代码完成后，用独立输出目录。
OUTPUT_SUBDIR=output_tkboost_dataclaw_B0 \
python dataclaw/eval/run_batch.py \
  --model "$MODEL" --suite "$TEST_SUITE" --runs 1 \
  <knowledge-injection-argument>
```

`<knowledge-injection-argument>` 是 5.5 节要求新增的 agent-only prompt injection 参数，
当前 upstream CLI 尚未实现；不能临时改用 `--skill-path` 代替。

### 7.4 常用命令

```bash
cd ~/DataClaw

# image 已在本机，确认 tag 与 .env 的 DOCKER_IMAGE 一致
docker images | grep dataclaw

# 单 task
python dataclaw/eval/run_batch.py --model <model_id> \
  --task tasks/task_001_comprehensive_decision_easy_easy001.md

# 带 skill 注入
python dataclaw/eval/run_batch.py --model <model_id> \
  --suite task_001,task_002 --skill-path <host>/SKILL.md

# 对已有轨迹离线重算 process 分（不重跑 agent）
python dataclaw/eval/regrade_process.py --help
```

已有 run 的 process 分重算示例：

```bash
cd ~/DataClaw
python dataclaw/eval/regrade_process.py \
  --task-ids-file <test-task-ids.txt> \
  --output-dir output_tkboost_dataclaw_B0 \
  --skill-mode all \
  --force
```

该命令只重算 `process_score.json`；要先确认 B0 的 `score.json` 已存在。`--force` 会覆盖已有
process 分；只想补缺失项时改为 `--only-missing-gpr`。

历史轨迹只读路径：

```bash
ls ~/archive/DA_Workflow/output | head
ls ~/archive/DA_Workflow/output/task_054_comprehensive_decision_medium_medium029/
```

---

## 8. 本次调研的验证方式

没有跑 agent、没有调 LLM、没有起容器，全部结论来自读代码与读产物：
`Dockerfile`、`script/run.sh`、`script/docker_save_image.sh`、`dataclaw/utils/docker_utils.py`、
`dataclaw/eval/run_batch.py`、`README.md`，加上对 `~/archive/DA_Workflow/output` 下 362 个
run 目录的统计（文件覆盖率、run 前缀分布、`score.json` 聚合、日期跨度均为脚本实测）。
`dataclaw:0.1.0` 的存在与大小来自 `docker images` 实测。第 6 节的 skill 读取率来自对
`~/baseline/results/metrics.md` 所列 7 个评测批次（各 20 份 `chat.jsonl`）的脚本统计，
OpenClaw 的 skill 装载方式来自读宿主机 OpenClaw 2026.5.28 的 `dist/` 代码。本节属于调研
记录，没有产生生产代码改动，因此没有新增测试。
