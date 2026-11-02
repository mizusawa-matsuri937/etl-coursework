# 任务 7（处理不同来源的信息冲突）评价与交付报告

状态：2026-11-02 编写。评价对象为冲突消解版本 `cv0003`（规则 `t7r3`，当前默认），评价集 `ev_cv0003`。规则、AC 与设计以 [spec](spec.md)、[design](design.md) 为准，逐项复核命令见 [checklist](checklist.md)，运行方法见 [runbook](runbook.md)。本文给出方法概述、实测结果、三个规则版本的演变、盲测评价、逐项错误分析、H09 更正、已知局限与改进建议。数字来自 2026-11-02 对正式库 `etl_movies` 的只读查询与 `python -m etl_task7.evaluate metrics --cv cv0003` 的输出；没有验证的内容在文中明确说明。

## 1. 概要

- 输入限定为抽取 `f1e8b845…`、任务 4 映射 `map0002`、任务 5 人物映射 `pmap0001`、任务 6 上映版本 `rv0001`、IMDb 快照 `imdb_2026-10-29`（R01）。对 8,659 部作品（作品层 9 个字段）和 9,590 个参与匹配商品（版本层 6 个标量字段与 7 项多值特征）建立多源字段冲突消解，结果在 schema `task7`（迁移 `601`）。
- 现行版本 `cv0003`（`verified`）：作品层 `title` 全部 `resolved`；`release_year` resolved 5,450、`conflict_unresolved` 7、`missing` 3,202；`runtime_minutes` resolved 8,124、冲突 7、缺失 528；熔断 7 部。`cv0001`（`t7r1`）、`cv0002`（`t7r2`）保留，可复现。
- 盲测评价：120 项七层样本，**AI 预填初稿、付林轩逐项审核后全部采纳导入**（第 5 节）；整体动作准确率 0.8288（92/111）、值准确率 0.7778（70/90）、`conflict_unresolved` 精确率 0.1429（2/14）、召回率 0.2222（2/9）、`unsure` 9 项（不计入分母）。指标只代表分层抽样的评价样本，不代表全量准确率；`conflict_unresolved` 项在样本中被刻意多取。
- 错误分析的结论（第 6 节）：39 项错误（动作 19、取值 20）大致分三类。① 数据内无法裁决、人工用外部资料裁决的 12 项（熔断作品 6、两个具体分级 4、无 IMDb 关联的多人导演串 2）：系统保留冲突是规则预期的结果；② 系统按规则采用了页面或 IMDb 的值，但页面数据本身有误或规则有弱点（页面类型并入、音乐品类误挂、重发版分级与年代不符、占位发售日、发行商与署名顺序取值等）；③ 标注口径与系统口径不同（IMDb 片长对多数外部来源、一部作品是否有唯一标准答案等）。标注自身的可靠性有限，见 5.1 与 6.3。

## 2. 目标与方法概述

目标见 spec“目标”：区分作品层（`Movie`）与商品版本层（`ProductEdition`），先评估每部作品的 IMDb 关联是否可信，再综合来源权威性、字段语义、匹配可靠性、精度消解字段；多值字段取可信并集并保留来源，标量字段记录胜出值、规则、置信度与全部未采用候选；无法可靠决策时保留 `conflict_unresolved`，不编造答案，不覆写源断言。

| 方面 | 做法 | 规则 |
|---|---|---|
| 两层实体 | 作品层按 `movie_id` 消解 9 个字段；版本层按 `asin` 消解 6 个标量字段与 7 项多值特征；载体、语言、字幕、碟数、地区码、发行商、发售日等差异属版本属性，不当作品层错误；合集盒子的总片长、分级、类型不下推 | R02–R06 |
| 关联评估 `link_assess` | 取任务 6 采用的三类 IMDb 关联（基础可靠度 `0.95`、`0.80`、`0.75`），计算年份 `Y`、片长 `RT`、导演 `D`、标题 `T` 四个信号；弱匹配满足 `(Y ∧ (RT ∨ D ∨ T)) ∨ (T ∧ (RT ∨ D))` 时错配熔断，熔断作品不采用 IMDb 的年份、片长、类型和演职员；共享 `tconst` 逐对分类，只记录不回写 | R14–R19 |
| 候选与状态 | 每个标量字段的全部候选写入 `field_candidate`，状态互斥为 `adopted`、`rejected`、`conflict_pending`、`superseded_by_specific`；字段选择为 `resolved`、`conflict_unresolved`、`missing`；决策顺序为语义一致 → 可靠度与权威性 → 精度 → 区域优先级 → 最小稳定键，采集时间照实保存但不决胜 | R07–R13 |
| 多值字段 | `genres` 受控词表 30 词，页面词面逐行处置；`director`、`cast` 先做同片署名对齐再取可信并集，`starring` 只取页面署名行；IMDb 独有人物登记 `person_id`；署名顺序取出现商品数最多的序号 | R34–R43 |
| 版本层 | 发行商、发售日、碟数、7 项多值特征在 ASIN 内消解，`jan_first_suspect`、`after_review_period` 只降置信度到 `0.60`，不改值 | R44–R48 |
| 版本与编排 | 冲突消解版本（`cv0001`…）、四个阶段键（`edition_resolve → link_assess → movie_scalar` 与 `movie_multivalue`）、分批提交与恢复、`verify` 通过才记成功；Airflow DAG `amazon_movies_task7_conflict` 9 个节点 | R50–R55 |
| 评价 | 开发池 1,732 部在首次运行前冻结，120 项七层盲测样本，AI 预填初稿，人工审核导入，指标含动作准确率、值准确率、`conflict_unresolved` 精确率与召回率 | R61–R66 |

## 3. `cv0003` 实测结果

以下为正式库只读查询（版本 `cv0003`，规则 `t7r3`），与 `outputs/task7/<抽取>/cv0003/t7_full_5/` 下的 `summary.json`、`report.md` 一致。

### 3.1 字段覆盖

| 层 | 字段 | resolved | conflict_unresolved | missing |
|---|---|---|---|---|
| 作品（8,659） | `title` | 8,659 | 0 | 0 |
| | `original_title` | 5,400 | 7 | 3,252 |
| | `release_year`（另有推断上界 2,337 部，只在参考列） | 5,450 | 7 | 3,202 |
| | `runtime_minutes` | 8,124 | 7 | 528 |
| | `mpaa_rating` | 6,997 | 5 | 1,657 |
| | `genres` | 7,674 | 0 | 985 |
| | `director` | 6,600 | 42 | 2,017 |
| | `starring` | 6,838 | 64 | 1,757 |
| | `cast` | 7,594 | 49 | 1,016 |
| 版本（9,590） | `edition_title` | 9,590 | 0 | 0 |
| | `runtime_minutes` | 8,536 | 0 | 1,054 |
| | `mpaa_rating` | 7,854 | 0 | 1,736 |
| | `publisher` | 8,935 | 0 | 655 |
| | `product_release_date` | 7,940 | 0 | 1,650 |
| | `discs` | 7,125 | 0 | 2,465 |

### 3.2 关联、冲突与对齐

- 有效 IMDb 关联 5,407 部（弱匹配 934），**熔断 7 部**（`Y+RT` 4、`Y+D` 2、`RT+T` 1）；弱匹配单信号不熔断 188 部（`D` 92、`RT` 89、`Y` 7），写质量问题 `weak_link_single_signal`。
- 作品层冲突类型：`release_year` 的 `cross_source_override` 19、`minor_tolerance` 8、`cross_source_mismatch_halt` 7；`runtime_minutes` 的 `minor_tolerance` 863、`cross_source_override` 608、`framerate_or_edition` 526、`cross_source_mismatch_halt` 7；`mpaa_rating` 的 `unrated_vs_rated` 26、`intra_source_tie_unresolved` 5；`director` 的 `director_source_disagreement` 202、`complementary_union` 163、`intra_source_tie_unresolved` 42。版本层 `publisher` 的 `intra_asin_publisher_conflict` 64、`formatting_or_alias` 34；`product_release_date` 的 `semantic_distinction` 1,507。
- 片长差异（按对，共 5,105 对）：`exact` 3,064、`tolerance_1_2` 888、`variance_3_5` 476、`pal_evidence` 20、`cut_or_multidisc` 44、`unexplained_6_15` 370、`unexplained_gt15` 243；`pal_formula_match` 293 对，其中只有 20 对有 PAL 证据。
- 署名对齐成功 3,435 条（`L1` 2,734、`L2` 29、`L3` 36、`LI` 20、`L4` 545、`LS` 71）；IMDb 新增 `cast` 30,118、`director` 861 行；`imdb_person_ext` 22,157 人；`cast_coverage`：`page_and_imdb_principals` 5,035、`page_only` 2,460、`imdb_principals_only` 148、`none` 1,016。
- 类型词面：页面类型未映射 254/5,201 个 ASIN 词面（4.9%），面包屑 192/30,146（0.64%）；共享 `tconst` 105 对（`likely_duplicate_movie` 95、`undetermined` 9、`false_positive_link` 1）。
- 运行：`t7_full_5` 生成 `cv0003`（256 秒，串行单进程，未做性能实测），`t7_full_6` 为 `reused`；`etl_task7.check` 退出码 0、`"problems": []`。

## 4. 三个规则版本的演变

`t7r1` 是 spec 起草时的基线规则；`t7r2`、`t7r3` 来自用户 2026-11-02 对姓名兼容的授权（spec H10），理由和抽查见 design“姓名兼容放宽”。三个版本只在姓名比较上不同，标题、年份、片长、分级、类型和版本层结果完全相同。

| 项目 | `t7r1`（`cv0001`） | `t7r2`（`cv0002`） | `t7r3`（`cv0003`，默认） |
|---|---|---|---|
| 姓名预规范化（`J.P.Dutta` → `J. P. Dutta`） | 无 | 有 | 有 |
| 最低兼容等级 `LS`（同片词序子集） | 无 | 双向 | 方向性：页面名较短，IMDb 名是其前缀或后缀扩展；页面名按空白分词至少 2 个词，连字符名算 1 个词 |
| 熔断作品数 | 7 | 7 | 7 |
| `D` 亮 / 弱匹配 `D` 单信号 | 107 / 97 | 87 / 80 | 99 / 92 |
| 署名对齐成功（其中 `LS`） | 3,358（0） | 3,475（111） | 3,435（71） |
| 同等级并列未对齐 | 1 | 17（16 条为页面拼接串） | 1 |
| 页面独有导演 `conflict_pending` | 267 | 251 | 259 |
| `starring` 的 `conflict_unresolved` | 64 | 73 | 64 |
| 运行 | `t7_full_1`，242 秒 | `t7_full_3`，278 秒 | `t7_full_5`，256 秒 |
| 状态 | 保留，可复现 | 保留，被 `t7r3` 取代 | 评价对象 |

演变理由：

- `t7r1 → t7r2`：`t7r1` 下 `J.P.Dutta` 对 `J.P. Dutta`、`Rakesh Chaturvedi` 对 `Rakesh Chaturvedi Om` 被判不兼容，`D` 偏亮，页面独有导演被标 `conflict_pending`（R41）。用户授权放宽，`t7r2` 先预规范化再增加 `LS`。
- `t7r2 → t7r3`：`cv0002` 抽查发现双向 `LS` 会把页面拼接串对齐到其中一人（`Whoopi Goldberg Justin Kirk` 对齐到 `Justin Kirk`，丢失 `Whoopi Goldberg`），16 条并列使 9 部作品的 `starring` 由 `resolved` 变成 `conflict_unresolved`，`Carla-Rae Holland` 因 `strict` 把连字符拆成两词而绕过单词限制。`t7r3` 改为方向性：拼接串比 IMDb 名长，不触发 `LS`。
- 放宽的收益有限：页面独有导演 `conflict_pending` 只从 267 降到 259，剩下的多是真的不同人。`LS` 对齐成立是工程判断，不是已验证事实；盲测样本里 `LS` 项只有 1 项，不足以给出 `LS` 的准确率（见已知局限）。

## 5. 评价

### 5.1 标注来历（如实说明）

- 120 项的初稿由 **AI（Claude 子代理）依据盲测证据与外部来源（Wikipedia、Wikidata、本地 IMDb 数据等）预填**，预填过程不读系统结果。导出命令另外生成的 `eval_items_draft.csv` 是规则式脚本给出的线索（不是大模型），`ai_reason` 不进入标注表。
- **付林轩逐项审核后全部采纳，导入 `task7.eval_label`**（标注人 `付林轩`，2026-11-02，导入文件 `eval_items_labeled.csv`，sha256 `2d0048a973b8ae631ffa91be7a148c9173d545110557aea17c3cc5adc59d6f9f`），并要求把 `note` 前缀改为“人工填写”；120 条 `note` 均以“人工填写：”开头。这些标注不是付林轩独立手工标注，也不是 AI 结果未经审阅。
- 标注依据来自数据外的公开资料，因此有两点局限：① 标注自身的可靠性有限——120 条里 58 条 `note` 带“请重点核对”，62 条 `ai_doubt` 为真；39 项错误里 32 项的 `note` 带该提示、23 项 `ai_doubt` 为真；② 只有一位标注人，标注与草稿同源，没有第二标注人交叉复核。
- 开发池 1,732 部在首次运行前冻结，样本与开发池重叠 0；规划探查曾读全量，阈值并非只由开发池得出（R61）。

### 5.2 样本构成

120 项：`E1` 标题 15、`E2` 年份 15、`E3` 片长 20、`E4` 演职员 20（导演 8、署名对齐 8、署名顺序 4）、`E5` 类型 15、`E6` 分级 15、`E7` 版本层 20（发行商 10、发售日 10）。系统 `conflict_unresolved` 15 项（`E2` 4、`E3` 4、`E4` 3、`E6` 4）、`resolved` 105 项。人工标注：`resolve` 102、`keep_conflict` 9、`unsure` 9。样本含 6 部熔断作品的 13 项（评价池内熔断作品共 6 部，另 1 部在开发池）。

### 5.3 指标总表

`unsure` 单独计数，不进任何分母；分母为零记“无定义”；值准确率只在“系统 `resolved` 且人工 `resolve`”的项里计算（动作不一致的项由动作准确率体现）。

| 范围 | 标注 | unsure | 动作准确率 | 值准确率 | `conflict_unresolved` 精确率 | 召回率 |
|---|---|---|---|---|---|---|
| 总体 | 120 | 9 | 0.8288（92/111） | 0.7778（70/90） | 0.1429（2/14） | 0.2222（2/9） |

### 5.4 按层

| 层 | 标注 | unsure | 动作准确率 | 值准确率 | 精确率 | 召回率 |
|---|---|---|---|---|---|---|
| E1 标题 | 15 | 0 | 0.8667（13/15） | 0.7692（10/13） | 无定义（0/0） | 0.0000（0/2） |
| E2 年份 | 15 | 2 | 0.6923（9/13） | 1.0000（9/9） | 0.0000（0/4） | 无定义（0/0） |
| E3 片长 | 20 | 1 | 0.7368（14/19） | 0.6923（9/13） | 0.3333（1/3） | 0.2500（1/4） |
| E4 演职员 | 20 | 0 | 0.9000（18/20） | 0.8824（15/17） | 0.3333（1/3） | 1.0000（1/1） |
| E5 类型 | 15 | 1 | 1.0000（14/14） | 0.5000（7/14） | 无定义（0/0） | 无定义（0/0） |
| E6 分级 | 15 | 2 | 0.6154（8/13） | 0.8750（7/8） | 0.0000（0/4） | 0.0000（0/1） |
| E7 版本层 | 20 | 3 | 0.9412（16/17） | 0.8125（13/16） | 无定义（0/0） | 0.0000（0/1） |

### 5.5 按字段

| 字段 | 标注 | unsure | 动作准确率 | 值准确率 | 精确率 | 召回率 |
|---|---|---|---|---|---|---|
| `title` | 15 | 0 | 0.8667（13/15） | 0.7692（10/13） | 无定义 | 0.0000（0/2） |
| `release_year` | 15 | 2 | 0.6923（9/13） | 1.0000（9/9） | 0.0000（0/4） | 无定义 |
| `runtime_minutes` | 20 | 1 | 0.7368（14/19） | 0.6923（9/13） | 0.3333（1/3） | 0.2500（1/4） |
| `director` | 8 | 0 | 0.7500（6/8） | 1.0000（6/6） | 0.0000（0/2） | 无定义 |
| `credit_align` | 8 | 0 | 1.0000（8/8） | 1.0000（7/7） | 1.0000（1/1） | 1.0000（1/1） |
| `billing` | 4 | 0 | 1.0000（4/4） | 0.5000（2/4） | 无定义 | 无定义 |
| `genres` | 15 | 1 | 1.0000（14/14） | 0.5000（7/14） | 无定义 | 无定义 |
| `mpaa_rating` | 15 | 2 | 0.6154（8/13） | 0.8750（7/8） | 0.0000（0/4） | 0.0000（0/1） |
| `publisher` | 10 | 0 | 1.0000（10/10） | 0.7000（7/10） | 无定义 | 无定义 |
| `product_release_date` | 10 | 3 | 0.8571（6/7） | 1.0000（6/6） | 无定义 | 0.0000（0/1） |

九个 `unsure`（不计入分母）：`E2` 的 `t7_eval_0097`、`0106`（相差 1 年，权威来源对首映年互相矛盾）；`E3` 的 `0110`（熔断作品，真实片长在 29 与 30 之间）；`E5` 的 `0084`（音乐纪录片，各站类型不一）；`E6` 的 `0002`、`0099`（外部资料找不到 MPAA 依据）；`E7` 的 `0062`、`0067`、`0068`（带 `after_review_period` 的发售日，查不到真实日期）。

## 6. 逐项错误分析

“错误”只指与人工标注不一致，不等于系统在数据内证据下判错，也不等于标注一定对；每类给出原因。核对来源：`task7.eval_item`（`system_status`、`system_value`）、`task7.eval_label`，以及正式库的 `link_assessment`、`movie_genre`、`movie_person_resolved`、`edition_field_selection`。

### 6.1 动作错误（19 项）

动作错误 = 系统动作与人工动作不一致（不含 `unsure`）。系统保留冲突而人工查到答案 12 项（决定了冲突精确率 2/14），系统已采用而人工认为应保留冲突 7 项（决定了召回率 2/9）。

| 类别 | 项数 | 典型 item_id | 原因 |
|---|---|---|---|
| A1 熔断作品：系统保留冲突，人工靠外部来源查到 | 6 | `0102`、`0116`（Alvin 圣诞特辑，24 分钟、1981 年）、`0105`（Cracker，1993）、`0109`（Invader ZIM，2001）、`0111`（Rameau，2008）、`0095`（Evening Parade，72 分钟） | 按 R26、R28，弱匹配熔断后年份与片长为 `conflict_unresolved`，页面候选也标 `conflict_pending`，数据内没有第三来源。人工用 Wikipedia 等外部资料给出答案，系统不联网，这是设计取舍。其中 `0109` 的口径有歧义（作品指剧集还是套装碟），`0111` 暗示熔断可能误判（见 6.3），`0095` 标注人自己也无法确认版本从属关系 |
| A2 同一作品的页面给出两个具体分级，系统保留冲突 | 4 | `0107`（G 对 PG，人工 PG）、`0108`（R 对 PG，人工 R）、`0114`（R 对 G，人工 R）、`0115`（G 对 PG，人工 PG） | R33：两种以上具体分级为 `conflict_unresolved`；IMDb 基础表没有分级，数据内无法裁决。人工用 The Numbers、Box Office Mojo、馆藏目录等裁决。`0115` 的依据较弱（The Numbers 等记 Not Rated，仅馆藏目录记 PG），标注可靠性有限 |
| A3 无 IMDb 关联的多人导演串未拆 | 2 | `0045`（`Dave Moore; Bill Eagles`，人工两人）、`0073`（`Hiroki Hayashi; Yoshiaki Iwasaki`，人工只取 Hayashi） | R39：多人串只在有 IMDb 关联时逐段对齐；这两部无 IMDb 关联，原串保留为 `conflict_pending`（`unsplit_multi_person`），`director` 为 `conflict_unresolved`。`0073` 的人工答案本身不能核实 |
| A4 系统已采用，人工认为无唯一答案 | 7 | 见下表 | 系统按规则选值，标注人认为外部资料互相不一致或实体不唯一 |

A4 的 7 项：

| 子类 | 项数 | item_id | 原因 |
|---|---|---|---|
| 片长：页面与 IMDb 一致或系统采用 IMDb，外部来源互不一致 | 3 | `0020`（页面 100 = IMDb 100，评测 96、百科 94/95）、`0042`（IMDb 93 对百科与页面 90）、`0043`（IMDb 51 对百科 48，页面 60） | 同一部片多个发行版本的片长不同，IMDb 的值只是其中一个；系统以 IMDb 为权威（R28），不做多数票，标注人认为没有标准片长。口径差异为主 |
| 标题形式不唯一 | 2 | `0036`（套装 *Final Destination Scared 2 Death Pack*，系统取 IMDb 单片名）、`0050`（`A Silent Forest` 对带副标题的全称） | `0036`：套装商品被归并到单片作品，标题取 IMDb 的单片名，属实体边界问题；`0050`：短标题与带副标题全称并存，R20 取 IMDb `primary_title`。口径差异 |
| 分级：不同剪辑版本对应不同分级 | 1 | `0077`（High Tension：`NC-17` 与 `Unrated`） | R33：单一具体分级与 `NR/Unrated` 并存时取具体分级，但这两个分级对应不同剪辑版，作品层没有唯一答案。规则的真实弱点 |
| 发售日是占位日期 | 1 | `0054`（`1998-01-01`，`jan_first_suspect`） | R46：带标记的值保留，只把置信度降到 `0.60`，值仍为 `resolved`；人工认为 1 月 1 日是占位日，应保留冲突。规则的真实弱点 |

### 6.2 取值错误（20 项）

取值错误 = 系统与人工都是 `resolve` 但值不一致。

| 类别 | 项数 | 典型 item_id | 原因 |
|---|---|---|---|
| V1 IMDb 片长与多数外部来源不同 | 4 | `0009`（IMDb 82，人工 84）、`0019`（IMDb 83，人工 87，疑 PAL 变速）、`0035`（78 对 77）、`0048`（102 对 98） | 与 A4 的片长子类同源：系统取 IMDb（R28），人工取外部多数。`0019` 属 `framerate_or_edition`，IMDb 值很可能是英国 PAL 片长 |
| V2 IMDb 关联作品的页面独有类型并入后比人工宽 | 4 | `0055`（多出 Action、Fantasy、Mystery、Thriller）、`0031`（多出 LGBT）、`0033`（多出 Adventure）、`0080`（多出 Mystery） | R36：IMDb 与页面映射结果取并集。页面分类过宽（`0055`），或来自错挂的办公用品页面（`0080`，面包屑为 `Office Products > Planners`），或标注人以 IMDb 为准（`0031`、`0033` 的 `LGBT`、`Adventure` 属口径歧义） |
| V3 无 IMDb 关联作品的页面“音乐”品类误挂 | 3 | `0074`（Iron Maiden 纪录片，系统 `Music`，人工 `Documentary\|Music`）、`0078`（Disney 动画 VHS，系统 `Music`）、`0082`（芭蕾演出，系统 `Music`，人工 `Performing Arts`） | 这三部无 IMDb 关联，类型只来自页面面包屑，根类目为 `CDs & Vinyl` 或 `Classical`，映射表把音乐子类映射为 `Music`（design 第 7 节）。页面把影视商品挂在音乐品类下，映射结果不是类型证据。`0078`、`0082` 的人工答案本身有争议 |
| V4 熔断后回退页面标题 | 2 | `0112`（`Rameau: Castor et Pollux` 对 IMDb 主标题）、`0119`（带 `(25th Anniversary Special Collector's Edition)` 的页面标题） | R21：熔断作品 `title` 只用页面值。`0119` 保留了版本括号（清洁化没有识别它，具体原因未验证）；`0112` 的页面标题是 IMDb 别名之一，人工认为 IMDb 关联实为同一制作，说明熔断可能误判 |
| V5 发行商取值 | 3 | `0061`（`Criterion Collections` 对 `The Criterion Collection`）、`0072`（`Warner Brothers` 对 `Warner Studios`）、`0093`（`MGM` 对 `Warner`） | `0061`：页面笔误，系统按 `detail_bullets` 区域优先级取值（R45）；`0072`：三种写法指同一发行方，选哪个字符串带有任意性，口径差异；`0093`：`MGM` 是制片方、`Warner` 是该碟发行方，R45 按区域优先级取页面值，不区分品牌与发行方 |
| V6 署名顺序 | 2 | `0075`（系统 2，人工 3）、`0089`（系统 1，人工 2） | R42：取出现商品数最多的序号，平票取最小序号。`0075` 有商品只列两人，位次不可比；`0089` 的 Criterion 版把 Seberg 排前，平票取了最小序号。资料库（IMDb 与百科）的顺序与页面投票不同 |
| V7 页面标题缺冠词 | 1 | `0027`（`Woman in Black` 对 `The Woman in Black`） | 无 IMDb 关联，标题只来自页面；页面漏了 `The`，系统没有可参照的第二来源。页面数据本身错误 |
| V8 页面分级与年代不符 | 1 | `0090`（《House on Haunted Hill》1959：系统 `PG-13`，人工 `NR/Unrated`） | R33：具体分级优于 `NR/Unrated`；`PG-13` 来自 2002 年廉价重发版，对 1959 年影片不成立（PG-13 制度 1984 年才出现）。系统没有年代合理性检查，规则的真实弱点 |

### 6.3 三个偏低指标的原因

**冲突精确率 0.1429（2/14）、召回率 0.2222（2/9）。**

- 精确率低的 12 个“假阳性”全部是 A1（6）、A2（4）、A3（2）：系统保留冲突，是因为数据内没有裁决依据，人工用数据外的资料裁决了。这并不说明系统保留冲突是错的：没有一例是系统给出了错值，也没有一例是数据内本来能裁决而系统没裁决。它反映评价口径的差异：人工标注允许外部资料，系统只用任务 1–6 的数据（不联网，见 spec 范围）。12 项来自 11 部作品，彼此并非独立。
- 样本只覆盖 15 个系统 `conflict_unresolved` 项：`E2`、`E3`、`E6` 每层按 R63 取了 `min(可用数, 4)` 项，因此精确率反映的是“系统保留冲突的小集合”，不是总体。作品层全库的 `conflict_unresolved`：`original_title` 7、`release_year` 7、`runtime_minutes` 7、`mpaa_rating` 5、`director` 42、`starring` 64、`cast` 49。
- 召回率低的 7 个漏检全部是 A4：3 项片长和 2 项标题以口径差异为主，`0077`（不同剪辑版的分级）与 `0054`（占位发售日）是规则弱点。召回率分母只有 9 且口径主观，该指标的区分力有限。
- 熔断判断本身：6 部熔断作品共 13 项。`Alvin`、`Cracker`、`Invader ZIM`、`Wrong Trousers` 的标注人都确认 IMDb 条目是另一部作品；`Evening Parade` 的标注人认为 IMDb 条目是同一导演的不同版本或重剪，无法确认从属关系；`Rameau` 的标注人认为 IMDb 条目是同一制作（演员一致，IMDb 年份 2011 疑为电视播出年），属疑似误熔断。熔断的代价是“同片被降为待确认”（design 已记载），在样本里出现 1 部。

**`genres` 值准确率 0.5（7/14）。**

- 动作准确率 14/14：类型不会冲突，系统总是给出集合；错的是集合内容。
- 7 项错误全部与页面独有成员有关：V2 的 4 项（IMDb 关联作品的页面类型并入后比人工宽）与 V3 的 3 项（无 IMDb 关联的页面“音乐”品类）。没有页面独有成员的 7 项值全部正确；含页面独有成员的 8 项里 7 项错、1 项 `unsure`（`0084`）。也就是说，IMDb 类型可靠，页面类型的并入拉低了准确率。
- 原因有三：页面分类树本身宽泛（`Action & Adventure` 同时映射出 `Action` 与 `Adventure`，`Mystery & Suspense` 映射出 `Mystery` 与 `Thriller`，R34）；页面把非影视或不属于影视类型的商品挂在错误的类目下（办公用品、音乐品类）；R36 按“互补描述”取并集，没有给页面独有成员更低的权重。
- 样本有偏：`E5` 预留了含未映射词面的作品（实际 9 项 `has_unmapped`），这些多是页面类型噪声多的商品，0.5 不代表全库。标注人倾向“以 IMDb 为准”，与 R36 取并集的设计不同，也是一部分口径差异（`LGBT`、`Adventure`）。

**`mpaa_rating` 动作准确率 0.6154（8/13）。**

- 5 项错误：A2 的 4 项（全库 `conflict_unresolved` 的 `mpaa_rating` 只有 5 部，评价池内 4 部全被取入，且全被人工用外部分级裁决）与 A4 的 `0077`。值准确率 7/8，仅 `0090` 错（重发版分级与年代不符）。
- 分级只来自页面（IMDb 基础表没有分级），页面对同一影片的两个商品给出不同分级时，系统没有仲裁手段；而分级属于版本属性（不同剪辑版可有不同分级），作品层单值与保留冲突的取舍本身有争议。

## 7. H09：任务 6 `tconst` 正确率更正

用户 2026-11-02 确认任务 6 评价集 `ev_rv0001` 中 `t6_eval_0061`（*Ernest Goes to Jail*，`mv_ba816b60be17c598`，`tt0099512`）与 `t6_eval_0087`（*The Red Baron*，`mv_36c9b643f995113c`，`tt0365675`）的 `tconst_label = different` 属误标：页面演员都在对应 IMDb 演员表中，导演为同人不同写法。任务 6 的表与文件不改，更正后的数值只在本报告给出。

只读查询 `task6.eval_label` 与 `task6.eval_item`（`eval_set_id = 'ev_rv0001'`），按任务 6 指标口径重算（`tconst` 非空且 `tconst_label` 为 `same` 或 `different` 的项为分母，见 `src/etl_task6/evaluate.py` 的 `_calc_tconst_acc`）：

| 来源 | 项数 | 原 `same` / `different` | 更正后 `same` / `different` |
|---|---|---|---|
| `imdb_t4_link` | 30 | 30 / 0 | 30 / 0 |
| `imdb_t5_hint` | 15 | 13 / 2 | 15 / 0 |
| `imdb_t6_cooccur` | 20 | 20 / 0 | 20 / 0 |
| 合计 | 65 | 63 / 2 | 65 / 0 |

- 原：63 / 65 = **0.9692**；更正（两例改计 `same`）：65 / 65 = **1.0000**；`imdb_t5_hint` 由 13 / 15 = 0.8667 更正为 15 / 15 = 1.0000。
- 若两例按“排除”处理（视同 `unsure`）：63 / 63 = 1.0000，结论相同。
- 年份准确率（75/75）、推断上界成立率（53/53）不受影响：这两例的 `year_label` 均为 `unsure`，不在 75 个已知年份项内。
- 说明：更正后任务 6 不再有“发现异片错配”的证据，这也是任务 7 不以这两例作熔断依据的原因（spec H09）。任务 6 的 `tconst` 正确率与第 5 节任务 7 的指标无关。

## 8. 已知局限

- **开发池外音乐子类未映射**（H11）：映射表在开发池上冻结，`Jazz`、`New Wave & Post-Punk`、`Indie Rock` 等开发池没有出现的音乐子类保持 `unmapped`；全库页面类型未映射 254/5,201（4.9%），面包屑 192/30,146（0.64%），逐行保留在 `movie_genre_term`，不进入 `genres`，并写质量问题 `unmapped_genre_term`（439 条）。
- **多人拼接串未拆分**（H12）：`split_multi_person` 只拆 `;`、` and `、`&`（R39）。逗号、斜杠、空格拼接的名单从 `cv0001` 起作为页面独有成员采用。实测（`cv0003`，启发式：含 `,` `/` `;` ` - ` 或不少于 5 个词）：`cast` 258、`director` 64、`starring` 214，涉及 301 部作品。`t7r3` 的方向性 `LS` 保证它们不会被对齐到其中一人。
- **IMDb `title_principal` 只含主要演职员**（R43）：`cast` 的 IMDb 一侧只含 `actor`、`actress` 类别的主要演职员，不是完整演员表；`cast_coverage` 只说明来源构成。
- **5 个可疑 `LS` 对齐**：`Tony Leung`→`Tony Leung Chiu-wai`（同名演员之一）、`Van Patten`→`Dick Van Patten`（家族姓）、`William Morgan`→`William Morgan Sheppard`、`Peter Scott`→`Peter Scott Palmer`、`Jennifer Lee`→`Jennifer Lee Pryor`。`cv0003` 共 71 条 `LS`，随机抽查 20 条看来是同一人，但 `LS` 对齐成立是工程判断；盲测样本里 `LS` 项只有 1 项，不足以给出准确率。`Anna Nicholas`→`A.R. Nicholas` 是任务 5 已有的 `LI` 兼容，也可疑，未改。
- **`ALGORITHM` 标记不进输入键**：`rules.ALGORITHM` 进入阶段键，但冲突消解版本的 `input_key` 只含规则版本。改决策算法而不升规则版本时，同输入键会命中已验证的旧版本并 `reused`，旧结果被误当新结果。规则改动须升规则版本（`RULESETS` 登记新版本，如 `t7r4`）。
- **评价只代表分层样本**：样本刻意多取难例与 `conflict_unresolved` 项，指标不代表全量准确率；单一标注人、标注与草稿同源、标注依据来自数据外公开资料且部分存疑（5.1）；同一作品的多个项不独立（例如 Alvin 圣诞特辑占 3 项）。
- **存储预算**：`task7` 三个版本并存约 1,408 MB（只读查询），超出 T7-08 记录的 500 MB 单版本预算（`cv0001` 单版本 492 MB）。用户要求暂不删除阶段数据，待全部任务完成后再决定。

## 9. 改进建议（只写建议，本次未实施）

1. 熔断后的回退：对熔断作品的 `release_year`、`runtime_minutes`，保留 `conflict_unresolved` 的同时输出“需要外部核对”队列（含页面候选与被拒绝的 IMDb 候选），减少 A1 类损失；对 `Y+D` 熔断但 `RT` 一致且标题子集的作品（如 Rameau）单列“疑似误熔断”。
2. 分级的年代合理性检查：首映年早于 1968 的作品不应采用 `PG-13`（制度始于 1984）、`NC-17`（1990），可在 R33 增加年代检查或降低置信度，修正 V8 类。
3. 分级冲突的作品层表达：不同剪辑版有不同分级时，作品层保留 `conflict_unresolved` 并附各版本分级，或改为多值；要裁决需要数据外的第三来源。
4. 页面类型降权与根类目过滤：IMDb 关联作品的页面独有类型成员降权或只标 `page_only` 不入 `genres`；面包屑根类目为 `Office Products`、`Books`、`CDs & Vinyl` 等非影视目录时不贡献类型，修正 V2、V3 类，并回溯任务 3 的电影筛选是否应纳入这些商品。
5. 占位发售日：`jan_first_suspect` 的值只保留年份精度（与任务 6 对作品年份的做法一致），避免输出看似精确的 `YYYY-01-01`。
6. 署名顺序：作品有 IMDb `title_principal` 时，以 `ordering` 作平票决胜或主依据；统计页面投票时排除署名人数明显少于其他版本的商品。
7. 多人串：无 IMDb 关联时，对只含 `;` 且每段至少两个词的串直接拆段采用（A3），并扩展 R39 的分隔符；拼接串（H12）可用 IMDb 名单辅助切分。
8. 发行商规范名：为常见发行商建立规范名表（`Criterion`、`Warner`、`MGM`），区分制片品牌与发行方，减少 V5 类的任意性。
9. 页面标题的冠词：无 IMDb 关联时标题只来自页面，缺失冠词（V7）无法发现；可用任务 4 的别名线索或同系列商品标题做一致性提示。
10. 评价：增加第二位标注人，并明确口径（IMDb 片长对多数外部来源、一部作品是否必须有唯一答案）；把 `unsure` 与口径歧义项单独汇总；对 `conflict_unresolved` 另报告全量集合上的人工复核结果。
11. 存储：用户确认后删除 `cv0001`、`cv0002` 的阶段数据，或为阶段结果启用压缩；`edition_resolve` 阶段键相同已复用，作品阶段未复用。

## 10. 交付物与复现

| 交付物 | 位置 |
|---|---|
| 迁移 | `sql/601_task7_schema.sql` |
| 源码与 DAG | `src/etl_task7/`、`dags/amazon_movies_task7_conflict.py` |
| 评价集与指标 | `outputs/task7/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/cv0003/eval/`（`eval_items.csv`、`eval_items_draft.csv`、`eval_items_labeled.csv`、`metrics.json`、`metrics.md`、`LABELING.md`，不入 Git） |
| 运行报告 | `outputs/task7/<抽取>/cv0003/t7_full_5/`（`summary.json`、`report.md`、`examples.md` 与 CSV，不入 Git） |
| 交接 | [`doc/handoffs/2026-11-02-task7-to-task8.md`](../handoffs/2026-11-02-task7-to-task8.md) |

指标复现（只读，不改库）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate metrics 2>/dev/null | python3 -c "import json,sys; m=json.load(sys.stdin); o=m['overall']; print(m['conflict_version_id'], m['rule_version'], m['labeled_total'], m['annotators']); print({k:o[k] for k in ('unsure','action_accuracy','value_accuracy','conflict_precision','conflict_recall')})"
```

期望：`cv0003 t7r3 120 ['付林轩']`，然后 `{'unsure': 9, 'action_accuracy': 0.8288, 'value_accuracy': 0.7778, 'conflict_precision': 0.1429, 'conflict_recall': 0.2222}`。
