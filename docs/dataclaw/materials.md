# DataClaw reflector 与规则样例：组会备选材料

本文保留两个用途不同的材料：

1. `task_185` 的一次真实 reflector 试跑，展示「失败轨迹 → 探查 → divergence → harness 打回 → 修正后接受」；
2. 适配设计中的人工规则样例，供后续讨论规则生成、合并与 B0 注入质量时参考。

这些材料不是正式 knowledge store，也不是对 test 的评测结果。真实试跑来自开发集，人工规则样例不进入
任何 store。

---

## 1. 真实 reflector 试跑：英文公司名到中文档案名

### 1.1 运行对象与结果

| 项 | 值 |
| --- | --- |
| task | `task_185_enterprise_industry_analysis_easy_easy117` |
| 历史 run | `glm-5.2_20260710_1634_850743` |
| 题目 | `Is Zhongke Zhiyun Data Services Co., Ltd. registered in Guangdong Province?` |
| 原 agent 答案 | `No relevant data found` |
| gold 答案 | `Yes` |
| 反思模型 | `openai/glm-5.2`（OpenAI-compatible provider 前缀；实际模型为 glm-5.2） |
| 用时 | 约 110 秒 |
| probe 数 | 3 |
| `<final>` 数 | 2 |
| 最终结果 | 1 条 `data` divergence 被接受；无 rejected / logged divergence |

完整、可回看记录（包含原始 prompt、完整 probe、两次 `<final>` 与 harness 回复）在 git 忽略的：

```text
tmp/dataclaw_dev/reflect/
  task_185_enterprise_industry_analysis_easy_easy117__
  glm-5.2_20260710_1634_850743.json
```

### 1.2 原 agent 的失败轨迹

原 agent 没有读中英对照 JSON。它先从网页搜索 `Zhongke Zhiyun`，随后在 CSV 里依次搜索英文名、`Zhongke`、
`中科`、`Zhiyun`，都没有结果，于是给出 `No relevant data found`。

关键错误不是「搜索命令写错」，而是没有理解这份数据的实体对齐方式：题目里的英文公司名不能直接在
`company_profile.csv` 里查，必须先用 `bilingual_translation_english_chinese.json` 找出数据中的中文名。

### 1.3 反思 agent 的探查过程

| probe | 做了什么 | 关键观察 |
| --- | --- | --- |
| #1 | 读取翻译 JSON 的顶层结构 | 文件是一个 dict，含 `company_items` 和 `policy_items`；这只是结构探查，输出过大 |
| #2 | 遍历 `company_items`，匹配英文值中的 `Zhiyun` / `Zhongke` | 找到 `众课智云数据服务公司` 对应 `Zhongke Zhiyun Data Services Co., Ltd.` |
| #3 | 在 `company_profile.csv` 中按中文名读取档案 | `bmCompanyName: 众课智云数据服务公司`，`province: 广东省` |

probe #2 的关键输出：

```text
'众课智云数据服务公司' -> ['Zhongke Zhiyun Data Services Co., Ltd.',
 'Zhong Ke Zhi Yun Shu Ju Fu Wu Co., Ltd.',
 'Zhongke Zhiyun Data Services Company']
```

probe #3 的关键输出：

```text
bmCompanyName: 众课智云数据服务公司
province: 广东省
```

### 1.4 第一次 `<final>` 被打回

模型的第一次结论方向正确，但有两个格式/证据错误：

1. 它把 JSON 的 `company_items` 写成了 `COLUMNS:
   bilingual_translation_english_chinese.json.company_items`。JSON 在 catalog 中是文件级对象，不存在名为
   `company_items` 的 CSV 列，所以文件/列存在性闸门拒绝了它。
2. 它写了 `REPRODUCED: milestone "Whether registered in Guangdong Province" = "Yes" FROM probe#3`。
   probe #3 只打印了 `广东省`，并没有打印 `Yes`；「是」是从省份与问题条件推出来的，不能伪装成 probe
   直接输出。因此「复现值必须出现在所引 probe 输出中」闸门拒绝了它。

harness 的反馈是：

```text
divergence 1: REJECTED
  - column company_items does not exist in bilingual_translation_english_chinese.json
  - REPRODUCED "Whether registered in Guangdong Province": Yes does not appear
    in the output of probe#3; copy the value exactly as the probe printed it
```

### 1.5 被接受的 divergence

模型依据反馈提交第二个 `<final>`：把 JSON 只留在 `TABLES`，删去不能由 probe #3 直接证明的 `Yes`
milestone，只保留注册省份 milestone。以下内容是记录中的被接受版本。

```text
DIVERGENCE: At CALL #4–#8 the agent searched company_profile.csv and other
enterprise files directly for "Zhongke", "中科", "Zhiyun" etc., found nothing,
and gave up.

NEEDED: It should have consulted bilingual_translation_english_chinese.json to
obtain the Chinese company name for "Zhongke Zhiyun Data Services Co., Ltd.",
then looked up that name in company_profile.csv.

MISSING_DATA_UNDERSTANDING:
  SCOPE: cross_table
  TABLES: bilingual_translation_english_chinese.json,
          enterprise/company_profile.csv
  COLUMNS: enterprise/company_profile.csv.bmCompanyName,
           enterprise/company_profile.csv.province
  FACT: The bilingual translation JSON maps the English name
        "Zhongke Zhiyun Data Services Co., Ltd." to the Chinese company name
        "众课智云数据服务公司"; the pinyin "Zhongke" maps to the characters
        "众课" (not "中科"), so grepping the database for "中科" or "Zhongke"
        finds nothing. Looking up "众课智云数据服务公司" in company_profile.csv
        yields province "广东省".

CATEGORY: 7. Entity names that do not match

EVIDENCE: probe#2 → '众课智云数据服务公司' -> ['Zhongke Zhiyun Data Services Co.,
          Ltd.', 'Zhong Ke Zhi Yun Shu Ju Fu Wu Co., Ltd.',
          'Zhongke Zhiyun Data Services Company']
EVIDENCE: probe#3 → bmCompanyName: 众课智云数据服务公司  province: 广东省

REPRODUCED: milestone "Registered province of Zhongke Zhiyun Data Services Co.,
            Ltd." = "广东省" FROM probe#3
SEMANTIC_MATCH: "广东省" is the Chinese name for "Guangdong Province"
KIND: data
```

### 1.6 这个例子说明什么

- **真实的 data divergence。** 原 agent 的失败来自忽略中英对照文件，不是算术、格式或提前停止等
  `non_data` 错误。
- **cross-table scope 合理。** 数据事实由 JSON 的英文到中文映射，以及 `company_profile.csv` 的
  `bmCompanyName`/`province` 共同构成。
- **闸门没有替模型补全推理。** harness 不接受「广东省，所以 Yes」被写成「probe 输出 Yes」；它只接受
  probe 实际打印的注册省份。
- **闸门能修正 schema 幻觉。** `company_items` 是 JSON 内部 key，不是 catalog 可引用的列。模型收到错误
  后删掉该字段，保留文件级引用。
- **仍可改进。** probe #1 把完整 JSON 内容打印出来，明显过宽；prompt 虽要求聚合输出，模型仍先做了昂贵
  的结构探查。后续可在 prompt 中加一个反例：探查 JSON 时不要打印嵌套字典，先只打印顶层 key、类型和
  长度。

---

## 2. 人工规则样例

以下 12 条是从失败轨迹、gold steps 和数据统计中人工写出的对照样例，来自
`docs/dataclaw/TK-Boost-adapt.md` 第 11.5 节。它们用于说明规则的目标形态，不是 reflector 的真实输出，
也不进入正式 store。

规则正文的约束：

- `ENSURE`、`WHEN_TO_CHECK`、`CONTEXT` 锚在文件/列和可复用的数据性质上；
- 具体实体、指标取值、数量、年份只在 `EXAMPLE_USAGE`；
- `TRIGGER` 只记录来源问法，后续进入 provenance，不渲染给被测 agent；
- 规则描述数据特性，不绑定 `grep`、`awk`、`pandas` 等具体工具。

### R1 多表关联关系

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

### R2 找错表：有汇总表却从明细聚合

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

### R3 缺失值与异常值

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

### R4 有更好的列作为指标（政策计数）

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

### R5 同一指标有多个名字

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

### R6 同一指标每行单位不同

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

### R7 实体名称对不上

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

### R8 统计范围与纳入口径

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

### R9 文件名暗示的含义与内容不符

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

### R10 结构化字段优先于全文搜索

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

### R11 数值格式与工具的相互作用

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

### R11b 数值格式与工具的相互作用（文件级）

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

## 3. 复用这些材料时的限制

- R4 的 agent 错误来源未核实；R9 中「Tokyo 来自合并分洲文件」只是推断；R10 的 `task_054` 是
  `gold_suspect`，所以其数据路径可以讨论，不能作为复现验证的样本。
- R5 的 `WHEN_TO_CHECK` 很宽，R7 很窄；它们是人工例子，之后仍需通过 `TRIGGER` 和同组规则合并来重写。
- `task_185` 的真实 divergence 当前也不能直接当作最终规则：阶段 C 仍要从 divergence 单独生成
  `DATA_RULE`，再执行正文不含单元格取值等检查。
