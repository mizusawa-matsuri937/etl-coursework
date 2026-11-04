# 任务 9（字段级数据血缘）运行与交付报告

状态：2026-11-04 编写。**任务 9 完成**：血缘版本 `lv0001`（规则 `t9r1`，`verified`）已经 Airflow 生成并核对；60 项血缘链抽查由 AI 预填草稿、付林轩审核后按草稿原样采纳导入（2026-11-04），血缘链正确率 1.000（47/47，`unsure` 13 不进分母；L1 14/14、L2 10/10、L3 8/8、L4 10/10、L5 5/5）。规则、AC 与设计以 [spec](spec.md)、[design](design.md) 为准，逐项复核命令见 [checklist](checklist.md)，运行与审核方法见 [runbook](runbook.md)。数字来自 2026-11-04 对正式库 `etl_movies` 的只读查询与 `outputs/task9/<抽取>/lv0001/t9_full_1/summary.json`；没有验证的内容在文中明确说明。

## 1. 概要

- 输入限定为抽取 `f1e8b845…`、任务 3 `t3r2`（解析 `t3p1`）、任务 4 `map0002`、任务 5 `pmap0001`、任务 6 `rv0001`、任务 7 `cv0003`、任务 8 `rd0001` 与 IMDb 快照 `imdb_2026-10-29`（R01）。任务 9 只读这些上游，结果写入 schema `task9`（迁移 `801`）。
- 血缘四层：源记录 429,323 条 → 字段断言 1,548,754 条 → 派生边 2,106,585 条（另有字段选择的 select 边 493,144 条）→ 字段选择 531,371 个，影响闭包 887,035 行；评论层（7,911,696 条映射）用视图，不复制。
- **覆盖率**：范围内非空最终值 469,671 个，**全部可追溯到原始证据（100%）**；规划时现状试算为 85.6%（严格口径，411,673 个中 352,311 个）。试算缺口 59,362 个（类型页面成员 10,320、版本多值特征 46,027、评论上界 3,015）全部由只读重放补出定位，标 `relocated`，重放与上游不一致 0。
- 运行：Airflow `t9_full_1` 10 个任务 success（触发到结束 449 秒），`t9_full_2` 为 `reused`；`etl_task9.check` 退出码 0、`problems` 为空；`verify-sources` 快照 9,590、IMDb 文件 5 个、评论文件全部一致；上游行数运行前后不变。
- 评价：60 项分层样本 `ev_lv0001` 由 AI 预填、付林轩审核后按草稿原样采纳导入（`correct` 47、`unsure` 13），血缘链正确率 1.000（47/47，`unsure` 不进分母），只代表分层样本。

## 2. 目标与方法概述

目标见 spec“目标”：不重算任务 1–8，为目标层每个非空最终字段建立“是什么值、从哪些原始记录来、经过哪些处理、为何采用、未采用什么”的可查询血缘，并能在规则或来源改变后定位受影响字段。

| 方面 | 做法 | 规则 |
|---|---|---|
| 原始层 | 四类源记录：页面快照（ASIN、最终地址、快照路径、正文 SHA-256，构建时重算核验）、IMDb 文件行（文件、行号、文件 SHA-256）、评论文件记录（记录号、起止行、字节偏移、记录哈希）、人工修订；每条带获取时间与来源列 | R06–R09 |
| 断言 | 各上游表逐行翻译为抽取级、候选级、派生级断言，保留上游表、键、状态与原因（含未采用候选） | R11–R16 |
| 派生边 | 12 种操作闭集（extract、normalize、vocab_map、split、align、link、merge、select、union、infer、aggregate、relocate），每条边带规则、上游任务、运行与时间，并按 OpenLineage 标 `DIRECT`/`INDIRECT` | R17–R20 |
| 断链补定位 | 用上游同一规则函数只读重放：类型词面、版本多值特征、评论上界的第二早评论；推断上界把全部依赖评论登记为 `aggregate` 父节点 | R21、R22 |
| 字段选择 | 每个字段（多值按成员）一行：最终值、状态、采用与考虑过的断言、决策规则与理由、`trace_status`；与目标层逐行比对 | R23–R27 |
| 查询与影响 | `field`、`product`、`review`、`source`、`person`、`impact`；`verify-sources`；`report diff`；新上游版本全量重建（H09） | R28–R35 |
| 编排 | 五个阶段键、分批与恢复、咨询锁、`verify` 通过才 `succeeded`；DAG `amazon_movies_task9_lineage` 10 个节点 | R36–R40 |

## 3. `lv0001` 实测结果

### 3.1 源记录、断言与派生边

| 项目 | 数量 |
|---|---|
| 页面快照源记录 | 9,590（构建时重算正文 SHA-256，全部 `ok`；`snapshot_missing`、`snapshot_hash_mismatch` 0） |
| IMDb 行源记录 | 202,298（作品 28,714、别名 77,229、title.crew 5,276、主要演职员 55,655、人名 35,424） |
| 评论记录源记录 | 217,435（3,015 部评论上界作品的全部依赖评论） |
| 人工修订 | 0（任务 4、5 的 `human_decision` 均为 0 行，H07） |
| 断言 | 1,548,754：extract 1,062,036、candidate 156,568、derived 330,150 |
| 派生边 | extract 1,062,036；link 276,300；aggregate 217,435；union 132,045；normalize 125,979；relocate 121,283；select 74,913；merge 53,250；align 17,213；vocab_map 13,158；split 9,010；infer 3,963；悬空 0、缺父 0、环 0 |
| 字段选择 | 531,371：resolved 462,241、missing 61,674、inferred 6,978、conflict_unresolved 478；select 边 493,144 |
| 影响闭包 | 887,035 行，涉及源记录 399,451、选择 469,672，最短路径最长 5 |
| 质量问题 | 1：`nm17757708` 出现在 `imdb.title_principal` 与任务 7 IMDb 独有人物中，但 `name.basics` 没有这一行，路径改连演职员行（`dangling_edge`） |

### 3.2 覆盖率前后对照

| 类别 | 非空最终值 | 规划试算（现状，严格口径） | `lv0001` 实测 |
|---|---|---|---|
| 作品标量 6 个字段（含有效 `tconst`） | 40,037 | 可追溯 | 全部 traced |
| 作品多值成员（cast、starring、director、genres） | 99,622 | 类型页面成员 10,320 断链 | traced 89,302、relocated 10,320 |
| 作品别名 | 90,433 | 可追溯 | 全部 traced |
| 推断上界 | 6,978 | 评论证据 3,015 部缺第二早评论与全部依赖评论 | traced 3,963、relocated 3,015（217,435 条依赖评论入边） |
| 版本标量 6 个字段与类别 | 59,570 | 可追溯 | 全部 traced |
| 版本多值特征 7 项 | 46,027 | 只有“值 → 区域”，断链 | 全部 relocated |
| 人物（规范名、别名、外部 ID） | 118,044 | 可追溯（试算未计外部 ID 与 IMDb 独有人物） | 全部 traced |
| 商品所属作品 | 8,960 | 可追溯（singleton 需显式写成边） | 全部 traced |
| **合计** | **469,671** | 试算口径 411,673 个中可追溯 352,311，**85.6%** | **469,671 / 469,671 = 100%**（traced 410,309、relocated 59,362、manual 0） |

非空值比试算多 57,998 个，来自试算没有计入的范围（人物外部 ID 35,390、任务 7 IMDb 独有人物的规范名 22,157、商品类别 9,590 等）与多值字段按成员拆分的口径差异；试算各类可比数字（作品标量 34,630、版本标量 49,980、类型成员 18,558、版本特征 46,027、别名 90,433、有效 `tconst` 5,407、上界 6,978）逐一对上。空值选择 61,700 个都记录状态与缺失原因，其中 61,623 个没有任何候选（`untraced`），不计入缺口（R25 只要求非空值可追溯）。按字段的覆盖率见导出的 `report.md`。

### 3.3 断链补定位（R21）

| 重放对象 | 调用的上游函数 | 重放数 | 一致 | 补出的定位 |
|---|---|---|---|---|
| 类型页面词面 | `etl_task7.sources.genre_terms`、`etl_task7.rules.classify_genre_term` | 34,531 | 34,531 | 任务 4 `genre` 断言 ID 与面包屑区域、序号；覆盖页面侧成员 10,320 个（试算按原值前缀粗配只配上 6,485/13,505 个 ASIN-词面对） |
| 版本多值特征 | `etl_task7.editions.feature_values` | 46,027 | 46,027 | 每个特征值的来源断言（9,575 个商品整商品结果与 `attributes_detail` 完全相等） |
| 评论上界第二早评论 | 按任务 6 排序重写，`etl_task6.rules` 复核 B3 | 3,015 | 3,015 | 第二早评论的源评论 ID（最早评论 ID、两个时间与 B3 都与任务 6 一致） |

`replay_mismatch` 为 0，没有 `partial` 字段。

## 4. 演示查询实例（正式库，2026-11-04）

命令见 design“演示查询”（正式库不加 `ETL_DB_NAME`）。`field`、`product`、`review`、`source`（ASIN 与 tconst）、`person`、`impact` 的正式库输出与业务库副本演练的 JSON 完全相同。

1. **`field`：Legend（`mv_24a89e3536e837e6`）`release_year`**：最终值 `1985`、`resolved`、`traced`，决策 `cv0003` 的 `year.table:YR2`（“IMDb 与页面标题年份相差 1 年，采用 IMDb”）；采用候选落到 IMDb `title.basics.tsv.gz` 第 87,476 行（文件 SHA-256 `ecc495f7…`，获取 2026-10-29 07:43:16Z，来源列 `imdb.snapshot.downloaded_at`），路径长度 4；两条未采用的页面标题年份 `1986`（`rejected`，`minor_tolerance`）分别落到 B000063UR2、B004QSQMKE 的快照；另列推断上界 `1999-08-23`（`relocated`）。同一作品的 `director` 成员 `ps_75871356bebf3f24`（`credit_both`）同时有两个页面快照与 IMDb `title.crew` 第 87,476 行、`name.basics` 第 632 行（`nm0000631`）证据。
2. **`product` 与 `review`**：B000063UR2 为 `collected`/`success`、类别 `movie`（`MV1`）、版本属性（标题 `Legend (1986)`、片长 204、2 碟、`DVD`、`NTSC` 等）、所属作品 Legend（`edition`、`auto_decision`）、匹配状态 `merged`。Legend 最早评论 `8f17b116…` 为第 6,450,929 条记录（行 58,058,313–58,058,320，偏移 7,609,597,242），`rd0001` 下 `D3_family_copy`、逻辑评论 `lr_a206995a2a4ec3f45864`、家族 `rf_f4c0c1854b2f4130`、归属 Legend，并标出它是 Legend 推断上界的 `aggregate` 直接依赖。
3. **`source`**：快照 B000063UR2（`sr_c90a9a2904d071ce`）影响 9 个实体的 41 个选择，变化时按任务 3 → 4 → 5 → 6 → 7 → 8 → 9 重跑；`tt0089469` 的 IMDb 行影响 12 个实体的 56 个选择（作品 title、original_title、release_year、runtime、有效 `tconst`、23 个别名、演职员与类型成员、9 个人物外部 ID、2 个商品所属作品），重跑从 IMDb 装载开始。每行带最短路径长度。
4. **`person`：Olivia de Havilland（`ps_fb262c7cedc84d5e`）**：规范名来源 IMDb `name.basics.tsv.gz` 第 15 行（`nm0000014`）；6 条页面署名（4 个商品的 byline 与 detail_bullets，含大小写变体 `Olivia De Havilland`）及各自的归并依据；外部 ID `imdb:nm0000014`；两个页面别名；参与 4 部作品 6 个角色成员；任务 5 映射事件 0（如实返回空）。

作业血缘示例（导出 `examples.md`，四类各 1 例）：

| 示例 | 实例 |
|---|---|
| 商品发售日期不进作品年份 | `mv_03cfdd09f2d5c995` 年份 1935 来自 IMDb `title.basics` 第 26,129 行；其商品 B004BR1RGY 的发售日期 2010-11-11 只在版本字段 `product_release_date`，不在作品年份的采用路径上 |
| 第三方记录（IMDb）被采用 | `mv_2741af47e2fe8f3a` 年份 1960 采用 IMDb（`year.table:YR5`，弱匹配未熔断）；页面标题年份 1958 为 `rejected`（`cross_source_override`），证据落到 B000QJMDBY 快照 |
| 署名别名归并到同一 `person_id` | `ps_d08bde679a468df4` George 'Gabby' Hayes：`George Hayes`、`George "Gabby" Hayes` 等 6 种写法共 28 次提及归并到同一人，外部 ID `nm0371025` |
| 上映年份缺失，只有条件性上界 | `mv_9faa0696bada4b8b` 年份 `missing`（没有任何年份候选），只有发售日期证据的上界 `not_later_than 1994-10-21`（商品 6303312551 快照，`infer:media_release:M1-M4`）；另一例 `mv_cbefb17430301a33` 只有评论上界 2005-04-13（`bound:review`，`relocated`） |

## 5. 变更定位示例（R32–R35）

- **来源变化**：`verify-sources` 对 `lv0001` 引用的源记录重算哈希：快照 9,590 与 5 个 IMDb 文件在 `crawler` 容器里全部一致（12 秒），评论文件在 Airflow 容器里一致（217,435 条引用）；退出码均为 0。若某快照不一致，会列出该源记录、经 `field_impact` 得到的受影响字段与任务 3 起的重跑步骤（夹具 AC15 验证）。
- **规则变化**：`query impact --rule rating:MR2` 只经 26 条 select 边影响 26 个作品的 `mpaa_rating`。上游以新规则版本生成新版本后，任务 9 以新输入键**全量重建**新血缘版本（用户 2026-11-04 选定方案 C，H09），再用 `report diff` 核对：夹具中只改片长容差的第二个 `cv`，事前 `impact --rule RT1 --rule-version t7r3` 定位的字段集合 `{movie.runtime_minutes}` 与 `diff` 报出的完全一致，其余字段值与证据签名不变（AC15）。正式库只有 `lv0001`，没有生成第二个血缘版本。

## 6. 运行、只读验证、体量与耗时

| 项目 | 结果 |
|---|---|
| 业务库副本演练（T9-07，`etl_t9_rehearsal`） | 清出空 `task9` 后全链路 440 秒，`lv0001` `verified`、重跑 `reused`、`check` 退出码 0；五个阶段键与计数和正式库完全相同；演练库已删除并 `CHECKPOINT` |
| `t9_full_1` | 10 个任务 success，触发到结束 449 秒；按任务开始时间推算 `build_sources` 约 62 秒、`build_assertions` 约 85 秒、`build_derivations` 约 152 秒、`build_selections` 约 83 秒、`build_impact` 约 28 秒、`verify` 约 17 秒；`task9.run` 记 02:26:44 至 02:33:52（UTC，`succeeded`） |
| `t9_full_2` | 10 个任务 success（31 秒），`task9.run` 记 `reused`，指向 `lv0001`，业务表不变 |
| `etl_task9.check` | 退出码 0、`"problems": []`：阶段登记与完成标记、派生图（悬空、缺父、环 0）、与目标层不一致 0、非空值缺口 0、计数与上游行数一致 |
| 上游 | 运行前后行数不变（`public.review_record` 7,931,696、`review_staging` 7,869,446、`task2.product_manifest` 253,059、`task4.field_assertion` 156,845、`task5.person` 29,635、`task7.movie_resolved` 25,977（三个 cv）、`task8.review_mapping` 7,911,696、`imdb.title` 1,250,408）；`task7` 仍有 `cv0001`、`cv0002`、`cv0003` |
| 迁移 | `001,101,102,103,201,301,302,401,501,601,701,801`；`src/etl_task9` 与 `sql/801_task9_*.sql` 的写语句目标全部在 `task9`，唯一的 `public` 写入是迁移 801 的登记行 |
| `task9` 体量 | 4,222 MB（`assertion` 1,412、`derivation` 1,246、`field_selection` 615、`source_record` 583、`field_impact` 212、`entity_stage` 96、`relocation` 54 MB）；`etl_movies` 36 → 40 GB；postgres 卷空闲 104 GB |

与 design 预算对照（预算是规划估计，不是实测）：

| 项目 | 预算 | 实测 | 说明 |
|---|---|---|---|
| 运行时间 | 20–40 分钟 | 约 7.5 分钟 | 快照核验用线程池流式解压（约 10 秒），其余是库内连接 |
| 源记录 | 约 45 万 | 429,323 | IMDb 被引用行估高 |
| 断言 | 80–100 万 | 1,548,754 | 未计任务 3 页面字段、IMDb 按列拆分与集成层断言 |
| 派生边 | 约 150 万 | 2,106,585 + 493,144 | 未计抽取边与选择边 |
| 字段选择 | 约 45 万 | 531,371 | 比试算多计人物外部 ID 等 |
| 影响闭包 | 200–300 万 | 887,035 | 每个字段的采用路径多只到 1 条源记录 |
| `task9` 体量 | 1.5–2.5 GB | 4.2 GB | **超预算**：抽取边物化（约 0.5 GB）、集成层断言 36 万条、长文本键与索引；没有为凑预算删减范围（H02、H04） |

耗时是同机记录，不是性能实测。

工程层面的修正（不改变 spec 规则）：正式库导出审核表时发现，推断上界这类**选择项**的依赖评论证据只有定位、没有正文前 80 个字符（R47 要求；L5 的评论归属项原本就有）。补测试 `test_ac18_bound_review_evidence_has_text_head`（修正前失败、修正后通过），`evaluate.item_rows` 从 `public.review_staging` 只读补上正文前 80 个字符；尚未导入标注，用 `evaluate export --force` 重新导出、`draft` 重新生成，60 项样本不变（按 `md5(选择键)` 抽样），只有该类项的证据摘录变化。

## 7. 评价

### 7.1 样本与草稿

- 评价集 `ev_lv0001`：60 项，`L1` 作品标量 15、`L2` 作品多值成员 15、`L3` 版本字段 10、`L4` 人物 10、`L5` 评论与商品归属 10（商品归属 5、评论归属 5），均无补位（R46）。审核表 `eval_items.csv` 每项有实体、字段、最终值、处理路径与原始证据摘录，不含 `trace_status` 等系统状态列（R47）。
- **AI 草稿**：`evaluate draft` 只读审核表与原始证据文件（解压快照核对正文哈希并检索定位原文、按行号读 IMDb 原行、取评论正文前 80 个字符）生成 `eval_items_draft.csv`：`correct` 47、`unsure` 13（`ai_doubt` 13 项，全部是 `unsure`）；分层为 `L1` 14/1、`L2` 10/5、`L3` 8/2、`L4` 10/0、`L5` 5/5（`correct`/`unsure`）。`unsure` 的原因：最终值经规范化、对齐或受控词映射，没有原样出现在证据里（11 项，如演职员成员是 `person_id`、商品所属作品是 `movie_id`、上界是由评论时间推出的日期），或快照里没找到定位原文（2 项：B000QRIKOA 的类别、B000OSLBZQ 的所属作品）。草稿不会给出 `incorrect`，错判只能由人工给出。
- **审核与导入**：2026-11-04 付林轩审核草稿后确认按草稿原样采纳（`correct` 47、`unsure` 13，没有改判，因此不需要 `human_evidence`），另存为 `eval_labels.csv` 后以 `--annotator 付林轩` 导入，`task9.eval_label` 60 行。这是“AI 预填、人工审核采纳”，不是付林轩独立逐项手工标注。

### 7.2 指标

`evaluate metrics --version lv0001`（2026-11-04）：

| 层 | correct | incorrect | unsure | 正确率 |
|---|---|---|---|---|
| L1 作品标量 | 14 | 0 | 1 | 1.000 |
| L2 作品多值成员 | 10 | 0 | 5 | 1.000 |
| L3 版本字段 | 8 | 0 | 2 | 1.000 |
| L4 人物 | 10 | 0 | 0 | 1.000 |
| L5 评论与商品归属 | 5 | 0 | 5 | 1.000 |
| 合计 | 47 | 0 | 13 | 1.000 |

- 正确率只代表 60 条分层抽查样本，不代表全量；`unsure` 13 项（11 项最终值经规范化、对齐或受控词映射后不原样出现在证据里，2 项快照中未找到定位原文）不进分母，这些链条的正确性没有得到确认。
- 没有 `incorrect`，所以错误类型分布为空。指标文件 `outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/eval/metrics.md`（不入 Git）。

## 8. 已知局限

- **快照在库外**：页面快照留在 `outputs/task2/snapshots/`（28,913 个、4.7 GB），血缘只登记路径与正文哈希；清理 `outputs/` 时必须保留它，否则 R09 会报 `snapshot_missing`（H05）。
- **评论文件只挂载在 Airflow 容器**：`crawler` 容器的 `verify-sources` 把评论文件记为 `unavailable`（不算不一致），评论哈希要在 Airflow 容器里核验。
- **人工修订路径只在夹具验证**：正式库人工修订 0 条，`manual` 计数 0（H07）。
- **评分统计不逐条物化**：任务 8 三类评分统计只给口径与筛选条件，不列出每个统计依赖的评论（H01、R05）。
- **全量重建**：每个新血缘版本约 7.5 分钟、4.2 GB；旧版本按 R36 保留，需要时由用户清理（H09）。
- **体量超预算**：`task9` 4.2 GB，超出 design 的 1.5–2.5 GB 预算（第 6 节）。
- **`nm17757708` 缺人名行**：IMDb `name.basics` 没有该人，规范名为 `missing`，路径改连演职员行，质量问题 1 条（上游事实）。
- **评价样本有限**：60 项中 13 项为 `unsure` 不进分母；草稿的 `correct` 依据是最终值原样出现在原始证据里，审核为采纳草稿而非独立逐项手工标注。

## 9. 交付物与复现

| 交付物 | 位置 |
|---|---|
| 迁移与视图 | `sql/801_task9_schema.sql`、`sql/801_task9_views.sql` |
| 源码与 DAG | `src/etl_task9/`、`dags/amazon_movies_task9_lineage.py` |
| 运行报告 | `outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/t9_full_1/`（`summary.json`、`report.md`、`examples.md`、`quality_issues.csv`、`checksums.json`、`run_summary.json`）；同目录 `t9_full_2/`（重跑）、`manual/`（`report export` 手动导出，内容相同），均不入 Git |
| 审核表与 AI 草稿 | `outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/eval/`：`eval_items.csv`（sha256 `aef18344c641c27682dbb2484b29ac551b404565c0284873d6232683903d1791`）、`eval_items_draft.csv`（sha256 `fdaeaf2fa8be7c8a74904816e134ff178e65312cfd593f237eb5cee6a715ebb9`）、`LABELING.md`、`eval_labels.csv`（导入文件，sha256 `7539bb139d8487e3c40b0d4f4bff91cf53ce20ef48f462845573e1142dfaf2bf`）、`metrics.md`，不入 Git |
| 演练导出 | `outputs/rehearsal_t9/task9/<抽取>/lv0001/`（`rehearsal/`、`rehearsal_full/`，不入 Git） |

只读复现关键数字：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.check 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['lineage_version_id'], d['version_status'], d['ok'], d['impact']['rows'], d['problems'])"
```

期望：`lv0001 verified True 887035 []`。
