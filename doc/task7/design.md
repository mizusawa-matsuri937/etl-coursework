# 任务 7 设计思路（处理不同来源的信息冲突）

状态：2026-10-30 编写计划；2026-11-02 经复核修订，用户同意按复核结论修订（见 [spec](spec.md) 状态行与 H02–H05、H09）。需求、可观察行为与 AC 以 spec 为准；本文说明架构、表结构、算法、阶段依赖、编排、评价设计和正式库基线。阈值与置信度是**规划默认值**（规则版本 `t7r1`），只在开发池上调整，评价表导出前冻结；正文里标注“实测”的数字来自 2026-11-02 正式库 `cv0001` 的只读查询；标注“规划试算”的是 2026-11-02 规划阶段简化脚本的数字，仅保留作对照。

## 整体思路与架构边界

任务 7 把任务 3–6 的结果收敛到目标层，解决三类冲突：跨数据源（IMDb 与 Amazon 页面）、跨商品版本、单页面不同区域（`byline`、`detail_bullets`、`product_overview`、`breadcrumb`）。

设计顺序是**先分层，再判关联，后消解**：

1. 先分清作品层与版本层，版本物理差异不进入作品层冲突。
2. 再判断每部作品的 IMDb 关联是否可信。IMDb 值的权威性建立在“这确实是同一部片”之上，所以关联评估必须排在字段消解之前，并作为独立阶段持久化。
3. 最后按字段基数处理：标量择优，多值取可信并集，证据不足保留 `conflict_unresolved`。

| 层次 | 字段 | 策略 | 来源与优先序 |
|---|---|---|---|
| 作品层 | `title`、`original_title` | 标量择优，别名单独保存 | 未熔断的 IMDb `primary_title`/`original_title` > 页面清洁标题 |
| 作品层 | `release_year` | 决策表（第 4 节） | `imdb_t4_link` > `imdb_t5_hint` > `imdb_t6_cooccur` > `amazon_title_year`；推断上界独立保留 |
| 作品层 | `runtime_minutes` | 标量择优 + 逐版本差异表 | 未熔断的 IMDb 片长 > `edition` 商品标准版众数 |
| 作品层 | `mpaa_rating` | 标量择优 | `edition` 商品分级聚合；单一具体分级优于 `NR/Unrated` |
| 作品层 | `genres` | 受控词表并集，词面逐行保留 | IMDb ∪ 页面 `genre`/`breadcrumb` 映射结果 |
| 作品层 | `director`、`starring`、`cast` | 同片署名对齐后取并集 | `starring` 只取页面署名行 |
| 版本层 | `edition_title`、`runtime_minutes`、`mpaa_rating`、`publisher`、`product_release_date`、`discs` | 版本内标量择优 | `detail_bullets` > `product_overview` > `product_details` > `byline` |
| 版本层 | 7 项多值特征 | 版本内规范化并集 | `task4.field_assertion` ∪ `task3.page_field` |

## 正式库只读探查基线

2026-10-30 全量探查与 2026-11-02 复核（抽取 `f1e8b845…`、`map0002`、`pmap0001`、`rv0001`、`imdb_2026-10-29`）：

| 探查维度 | 实测 | 设计应对 |
|---|---|---|
| IMDb 关联 | 8,659 部中 `verified` 4,473、`hint_matched` 293、`cooccur_matched` 641，合计 5,407；`page_reported` 50、`missing` 3,202 | R14；弱匹配先经 `link_assess` |
| 版本层范围 | `matched` 商品 9,590；`task4.product_movie` 只含 8,866（`edition` 8,730、`contains` 136），724 个合集无关联作品 | R06：范围取 `task4.product_edition` |
| 标题差异 | 页面键命中 `primary` 4,515、仅 `original` 108、仅 `aka` 291、仅实词子集 493；`primary ≠ original` 871 部；跨商品规范键不同 18 部 | R20–R23 |
| 年份分歧 | 13 部跨源分歧：差 1 年 8 部，差 ≥2 年 5 部（全在 `cooccur_matched`，差 2、5、18、19、21 年）；`bound_before_release_year` 62 部 | R24–R27 |
| 片长覆盖 | 双方都有 4,883 部（5,111 对）、仅 IMDb 480、仅页面 2,768、都无 528；单 ASIN 多区域冲突 0 | R28–R32 |
| 片长差异（实测 2026-11-02 `cv0001`，按对 / 按最严重版本计部） | `exact` 3,064 / 2,880；`tolerance_1_2` 888 / 863；`variance_3_5` 476 / 465；`pal_evidence` 20 / 20；`cut_or_multidisc` 44 / 41；`unexplained_6_15` 370 / 367；`unexplained_gt15` 243 / 241；合计 5,105 对、4,877 部（规划试算 5,111 对，`tolerance_1_2` 889、`unexplained_gt15` 248） | 公式吻合 PAL 的 293 对里只有 20 对有 PAL 证据，其余 270 对归 `variance_3_5` 或 `unexplained_6_15` 并记 `pal_formula_match`（R30）；241 部存在 `unexplained_gt15`，写质量问题 `severe_runtime_conflict` |
| 演职员 | `task5.movie_person` 50,027 行（`director` 6,833、`starring` 13,151、`cast` 30,043），`order_conflict` 117 行；`starring` 同时写入 `cast`（`etl_task5.movie_person` 第 28 行） | R37–R42 |
| 署名对齐（实测 `cv0001`） | 对齐成功 3,358 条（`cast` 2,748、`director` 610），等级 `L1` 2,727、`L2` 29、`L3` 35、`LI` 20、`L4` 547；同等级并列未对齐 1 条，无兼容成员 2,826 条（`cast` 2,680、`director` 146）；多人串逐段行（`unsplit_multi_person`）`cast` 448、`director` 114。规划试算 3,335 条（`L1` 2,706、`L4` 545），多名 IMDb 成员同时命中时先取最高等级，仍并列才不对齐 | 弱匹配作品的页面人物大多无 `nconst`，不对齐会把同一人登记两次（R38） |
| 对齐后 IMDb 新增（实测） | `cast` 30,182、`director` 869；`imdb_person_ext` 22,157 人（规划试算 30,089、865、22,191） | R40 |
| 多人串 | 含 `;` 的署名 `director` 29（4 条有 IMDb 关联）、`cast` 51、`starring` 50（各 29 条有关联）；` and `/`&` 25 条，含 `Black and Blue Productions` | R39：只在有 IMDb 时逐段对齐，不新造人物 |
| 类型词面 | 页面规范词面 291 种 5,311 行，面包屑 220 种 30,146 行；扩充映射后未映射 276 行（5.2%）与 223 行（0.7%） | R34–R36 |
| 分级 | 原始值 16 种全部可规范化；7,854 个 ASIN、7,002 部有分级；跨版本不同 31 部（26 部具体 vs 未评级，5 部双具体分级） | R33 |
| 版本层 | `publisher` 98 个 ASIN 同页异写；`product_release_date` 9,558 行（`media_release` 7,834 行 / 7,723 ASIN，`listing_first_available` 1,724），两类都有 1,507 ASIN；`after_review_period` 490 行、`jan_first_suspect` 349 行 | R44–R48；R46 带标记采用 |
| 共享 `tconst` | 126 条问题对应 105 对：`candidate_shared` 40 + 共现 86（`unique` 85、`resolved_by_review_year` 1），其中 21 对两类重复；全部已被任务 6 采用；采用结果里 117 个 `tconst` 被 240 部作品共用 | R19：在 `link_assess` 分类，先于字段消解 |
| 采集时间 | 页面 `source_record.acquired_at` 全为 2026-10-28，`task2.product_manifest.fetched_at` 跨约 10.5 小时；IMDb 的 `acquired_at` 为空，取 `imdb.snapshot.downloaded_at` | R10：照实保存，不参与决胜 |

任务 6 交接文档中“`t6_eval_0061`、`t6_eval_0087` 为异片错配”不作为本设计依据：两例页面演员都在 IMDb 演员表中，导演为同人不同写法（H09）。熔断的依据是下述规划试算与正式库实测中直接观察到的错配。

## 开源参考

按 `CLAUDE.md` 要求，设计前对比了数据融合领域方案：

| 项目 / 规范 | 借鉴 | 不照搬的原因 |
|---|---|---|
| Bleiholder & Naumann, *Data Fusion*（ACM Computing Surveys）的冲突处理分类 | 标量用 Deciding（在真实候选中择优），多值用受控词表并集，无法裁决时显式保留不确定 | Mediating（如年份取平均）会制造任何来源都没有的值 |
| [Schema.org `Movie`](https://schema.org/Movie) 与 FRBR（Work / Manifestation） | 创作作品与载体版本分层 | 避免把载体差异误判为作品冲突 |
| [IMDb Non-Commercial Datasets](https://developer.imdb.com/non-commercial-datasets) 与 MPAA 分级体系 | 25 个 `genres` 作词表骨架；区分具体分级与未评级 | 页面分类树混有导航词，须经映射清洗 |

姓名比较不从零实现，复用 `etl_task5.names.parse`、`level`，保持任务 5–7 的兼容等级一致。

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/601_task7_schema.sql` | schema `task7` 全部表与迁移登记 |
| `src/etl_task7/rules.py` | 纯函数与 `RULESETS`（`t7r1`）：标题清洁化、标题信号、片长差异分类、MPAA 规范化、类型映射、多人串拆分、发行商归一、署名顺序决胜 |
| `src/etl_task7/editions.py` | 版本层候选与消解（R44–R48），写 `edition_stage_result` |
| `src/etl_task7/link_assess.py` | 有效 `tconst`、四信号、错配熔断、共享 `tconst` 分类（R14–R19），写 `link_assessment`、`shared_tconst_result` |
| `src/etl_task7/movies_scalar.py` | 作品层 5 个标量字段（R20–R33），写 `movie_scalar_stage_result`、`runtime_diff`、`movie_title_alias` |
| `src/etl_task7/movies_multivalue.py` | 类型与演职员（R34–R43），写 `movie_genre`、`movie_genre_term`、`credit_alignment`、`movie_person_resolved`、`imdb_person_ext`、`movie_multivalue_stage_result` |
| `src/etl_task7/versions.py` | 从阶段表物化 `cv0001`…，`report diff` 的差异计算（R50、R55） |
| `src/etl_task7/run.py`、`runner.py` | 迁移、输入键与阶段键、审计、咨询锁、分批提交与恢复 |
| `src/etl_task7/report.py`、`query.py`、`check.py` | 报告与实例（R58、R60）、三个查询（R59）、只读核对 |
| `src/etl_task7/evaluate.py` | 开发池、盲测、草稿、导入、指标（R61–R66） |
| `src/etl_task7/pipeline.py`、`dags/amazon_movies_task7_conflict.py` | DAG 步骤与 9 节点定义（R54） |
| `scripts/seed_task7_e2e.py` | 真实 scheduler 验证所需的 `etl_test` 合成数据 |
| `tests/fixtures/task7/`、`tests/unit/test_task7_rules.py`、`tests/integration/test_task7_*.py` | 夹具与测试 |

## 数据模型（schema `task7`，迁移 `601`）

| 作业逻辑实体 | 表 | 说明 |
|---|---|---|
| `Movie` | `movie_resolved` | 主键 `(conflict_version_id, movie_id)`；标量值与状态、`release_precision`、`effective_tconst`、`tconst_source`、`cross_source_mismatch_halt`、`cast_coverage`、推断上界参考列、类型数与演职员数 |
| `ProductEdition` | `edition_resolved` | 主键 `(conflict_version_id, asin)`；版本标量、`media_release_date`、`listing_first_available_date`、`date_flags`、7 项多值数组、`attributes_detail` |
| `FieldAssertion` | `field_candidate` | 标量候选，按 `(extraction_id, stage_key)` 持久化 |
| 关联评估（新增） | `link_assessment` | 主键 `(extraction_id, stage_key, movie_id)`：`effective_tconst`、`tconst_source`、`base_reliability`、`sig_year`、`sig_runtime`、`sig_director`（`ok`/`bad`/`na`）、`sig_title`（`ok`/`sub`/`bad`/`na`）、`cross_source_mismatch_halt`、`halt_reason`、`signal_detail jsonb` |
| 跨作品线索 | `shared_tconst_result`（阶段）、`shared_tconst_conflict`（版本） | 主键 `(extraction_id, stage_key, tconst, movie_id)`；两作品对比摘要、`link_class`、依据、来源问题 ID 列表 |
| 片长差异（新增） | `runtime_diff` | 主键 `(extraction_id, stage_key, movie_id, asin)`：`page_runtime`、`imdb_runtime`、`delta`、`diff_class`、`pal_formula_match`、`has_pal_evidence` |
| 别名（新增） | `movie_title_alias` | 主键 `(extraction_id, stage_key, movie_id, alias_kind, alias_ordinal)`：别名、区域、语言、来源定位 |
| 类型 | `movie_genre`、`movie_genre_term` | 前者每成员一行；后者每个页面词面一行，含 `disposition`（`mapped`/`noise`/`unmapped`） |
| `Person` 扩展 | `imdb_person_ext` | 主键 `(extraction_id, imdb_snapshot_id, person_id)` |
| `MoviePerson` | `movie_person_resolved`、`credit_alignment` | 前者每个最终人物角色一行，后者保存页面原始署名到最终人物的对齐证据 |
| `FieldSelection` | `movie_field_selection`、`edition_field_selection` | 每版本 × 实体 × 字段一行 |
| 阶段结果 | `edition_stage_result`、`movie_scalar_stage_result`、`movie_multivalue_stage_result` | 含决策明细 `field_selections jsonb`，供物化使用 |
| `ETLRun`、`QualityIssue` | `run`、`run_batch`、`entity_stage`、`conflict_version`、`quality_issue` | 审计、恢复、版本与质量问题 |
| 评价 | `dev_pool`、`eval_set`、`eval_item`、`eval_label` | R61–R66 |

### 输入键与阶段键

- `input_key`：抽取 | 任务 4 映射版本 | 任务 5 人物版本 | 任务 6 上映版本 | 解析版本 | 规范化版本 | IMDb 快照 | 规则版本。
- 阶段键（R51），下游包含上游：
  - `edition_stage_key`：抽取 | t4 | t6 | parser | norm | `edition_rule_signature`
  - `link_stage_key`：`edition_stage_key` | t5 | imdb | `link_rule_signature`
  - `scalar_stage_key`：`link_stage_key` | `scalar_rule_signature`
  - `multivalue_stage_key`：`link_stage_key` | `multivalue_rule_signature`

代价是版本层规则变化时下游都要重算；全量重算在规划上只需数分钟，换来依赖不漏。片长容差属于 `scalar_rule_signature`，修改时只重算 `movie_scalar`。信号阈值（15 分钟、2 年）属于 `link_rule_signature`。

### 选择记录约束

`field_selection` 两表列：`status`、`winning_value`、`selected_candidate_ids`、`considered_candidate_ids`、`conflict_type`、`selection_rule`、`reason`、`confidence`、`disagreement_detail`。

- `(status = 'resolved') = (winning_value IS NOT NULL)`；`status <> 'resolved'` 时 `selected_candidate_ids` 为空。
- 标量字段：`status = 'missing'` 等价于 `considered_candidate_ids` 为空；`resolved` 时 `selected_candidate_ids <@ considered_candidate_ids` 且非空。
- 多值字段：明细在 `movie_genre` 与 `movie_person_resolved`，两个候选数组为空；有 `adopted` 成员为 `resolved`；只有 `conflict_pending` 成员为 `conflict_unresolved`；无成员为 `missing`。
- `conflict_type` 用 CHECK 限定为 R13 的闭集。

## 各字段算法

### 1. 关联评估（`link_assess`，R14–R19）

有效 `tconst` 取 `task6.release_selection.selected_candidate_id` 对应候选的 `source_key`，来源以 `imdb_` 开头时生效。四个信号：

| 信号 | 定义 |
|---|---|
| `Y` | `abs(amazon_title_year − start_year) ≥ 2`；或 `task6.bound_evidence` 任一证据年 ≤ `start_year − 2`（含 `failed_checks` 里被 `B4`/`M4` 拦下的） |
| `RT` | 存在 `edition` 商品：`|页面 − IMDb| > 15`，且无 `cut_type`，且不满足 `discs ≥ 2 ∧ 页面 ≥ 1.5 × IMDb` |
| `D` | 双方都有导演，且页面导演（按 `;`、` and `、`&` 拆段后）与 IMDb 导演无一人 `nconst` 相同或 `level` 兼容 |
| `T` | 页面键与 IMDb 全部标题键（`task4.imdb_title_key`）去载体词（`vhs`、`dvd`、`bluray` 等）和首尾冠词后，既不相等，也无 ≥2 实词的双向子集 |

熔断：`weak ∧ ((Y ∧ (RT ∨ D ∨ T)) ∨ (T ∧ (RT ∨ D)))`。

三版口径的规划试算对比（934 部弱匹配）；实现口径的实测见表后：

| 口径 | 命中 | 观察 |
|---|---|---|
| 原 design：三信号任二，导演比 `nconst` | 大量误伤 | `nconst` 口径下 679 部亮 `D`，其中 460 部只是页面人物无 `nconst` |
| 原 design：三信号任二，导演比姓名 | 22 部 | 16 部只因片长差加姓名写法差命中（`Alred Hitchcock`、`Jess Franco`/`Jesús Franco`） |
| 修订口径 | 8 部 | Wrong Trousers→Were-Rabbit、Invader ZIM→Enter the Florpus、Alvin 圣诞片→2007 版等明显错配；Evening Parade、Love for Three Oranges 疑似同片但被保守拦下 |

实测（2026-11-02 `cv0001`，934 部弱匹配）熔断 7 部：`Y+RT` 4、`Y+D` 2、`RT+T` 1，与规划试算的 8 部相比少 1 部（`The Love for Three Oranges`，见实现补充约定“T 信号的‘实词’”）。7 部里只有 1 部（`Saturday Night Live - The Best of Chris Rock` 对 `Best of Saturday Night Live: Special Edition`）在开发池，其余 6 部在评价池之外的规划探查中直接观察到是错配（如 `Wallace and Gromit: The Wrong Trousers` 对 2005 年的 `The Curse of the Were-Rabbit`）。开发池上还看到两部未熔断的弱匹配（`Umrao Jaan`、`Bolo Raam`）亮了 `RT` 与 `D`，但标题键相同、只是导演写法不同（`J.P.Dutta` 对 `J.P. Dutta`、`Rakesh Chaturvedi` 对 `Rakesh Chaturvedi Om`），不熔断是对的；熔断规则没有调整。
熔断的代价是极少数同片被降为待确认，但不写错值；未熔断的弱匹配单信号照常采用 IMDb 并写 `weak_link_single_signal`（实测 190 部：`D` 97、`RT` 86、`Y` 7，没有 `T` 单信号）；另有 8 部同时亮 `RT` 与 `D` 但无 `Y` 或 `T`，不满足熔断式，也采用 IMDb，不写该质量问题。

熔断的后果：

| 字段 | 处理 |
|---|---|
| `title` | 按 R21 回退页面值，IMDb 候选 `rejected`，`conflict_type = 'cross_source_mismatch_halt'` |
| `original_title`、`release_year`、`runtime_minutes` | `conflict_unresolved`，IMDb 候选（及页面候选）标 `conflict_pending` |
| `mpaa_rating` | 不受影响，只来自页面 |
| `genres`、`director`、`cast` | 不并入该 IMDb 的值 |
| 别名 | 不收录 IMDb 别名 |

共享 `tconst`（R19）用同一批信号分类，不另设阈值：已熔断为 `false_positive_link`；`T ∈ {ok, sub}` 且 `D ∈ {ok, na}` 为 `likely_duplicate_movie`；其余为 `undetermined`。因此误挂自动走熔断后果，不存在“分类了但错值已入库”的情况。实测：105 对（涉及 100 个 `tconst`）里 `likely_duplicate_movie` 94、`undetermined` 10、`false_positive_link` 1，与规划试算相同。

### 2. 合集成员的候选边界（R05、R21、R28、R33、R36）

| 作品层字段 | `relation_type = 'edition'` | `relation_type = 'contains'` |
|---|---|---|
| `title`、`title_year` | `entity_type = 'product_edition' AND entity_id = asin` | `entity_type = 'collection_member' AND entity_id = asin || '#' || member_ordinal`，严禁读盒子总标题 |
| `runtime_minutes` | 读 `runtime_min` | 不读；无 IMDb 且无独立 `edition` 商品时 `missing` |
| `mpaa_rating` | 读 `task3.page_field` | 不读 |
| `genres`（页面部分） | 读 `genre` 与 `breadcrumb` | 不读 |

### 3. 标题与别名（R20–R23）

`clean_page_display_title` 去掉末尾 `[VHS]`、`[DVD]`、`[Blu-ray]`、`(Widescreen Edition)`、`(Full Screen)`、`(Collector's Edition)`、`(Bilingual)` 及已提取的括号年份，保留卷号与副标题。

- 有未熔断 IMDb：`primary_title` 胜出；页面清洁标题规范值相同标 `adopted`，别名或子集标 `superseded_by_specific`，其余标 `rejected`。
- 无 IMDb 或已熔断：按 R21 比较规范键；一致或互相包含时取频次最高的清洁写法，这里只在同键写法间选展示形式，不是跨值投票；实质不同为 `conflict_unresolved`。
- 别名：从 `imdb.title_aka` 取 `region`、`language`、`is_original`，来源为空时保存 `NULL`；规划估计最多约 14.2 万行，按实测调整索引。

### 4. 上映年份决策表（R24–R27）

| 关联 | 条件 | 结果 | `conflict_type` |
|---|---|---|---|
| 任意 | 候选一致或单一来源，无 `Y` | 采用 | `none` |
| IMDb | 与页面差 1 年 | 采用 IMDb | `minor_tolerance` |
| 强 | `Y` 且不同时 `D ∧ RT` | 采用 IMDb，写质量问题 | `cross_source_override` |
| 强 | `Y ∧ D ∧ RT` | `conflict_unresolved` | `severe_mismatch_unresolved` |
| 弱 | 已熔断 | `conflict_unresolved` | `cross_source_mismatch_halt` |
| 弱 | 未熔断，`Y`，导演兼容或有差 ≤15 分钟的片长 | 采用 IMDb，写质量问题 | `cross_source_override` |
| 弱 | 未熔断，`Y`，无佐证 | `conflict_unresolved` | `severe_mismatch_unresolved` |
| 无 | 页面年份不一致 | `conflict_unresolved` | `intra_source_tie_unresolved` |
| 无 | 无候选 | `missing`，记任务 6 原因 | `none` |

实测：`release_year` 为 `cross_source_override` 的 19 部（弱匹配未熔断且 `Y` 的 7 部、强关联 12 部）都有正面佐证，无 `severe_mismatch_unresolved`；`minor_tolerance` 8 部；`conflict_unresolved` 的 7 部全是熔断作品；例如 Beauty and the Beast 取 1991（页面标题年份为 2010），属于 `cross_source_override`。推断上界只写 `movie_resolved` 的参考列。

### 5. 片长（R28–R32、R44）

版本层在 `edition_resolve` 取 `runtime_min`，不依赖 IMDb。作品层在 `movie_scalar` 阶段逐商品写 `runtime_diff`，这样版本阶段的键不需要包含 IMDb 快照。

分类顺序：`exact` → `tolerance_1_2` → `pal_evidence` → `variance_3_5` → `cut_or_multidisc` → `unexplained_6_15` → `unexplained_gt15`。`pal_formula_match` 独立记录，公式吻合但无证据的对子按 Δ 落入后续类别。

无 IMDb 时按 R32；众数只在极差 ≤5 分钟内使用，理由是这个范围内差异可视为舍入误差，不是跨值投票。

### 6. 内容分级（R33）

规范化后同 ASIN 无冲突（正式库 0 例）。作品层聚合已在 R33 明确；5 部双具体分级（`Phenomenon`、`Veer Zaara`、`Forbidden Zone`、`Hard Target`、`The Five Heartbeats`）保留 `conflict_unresolved`（`intra_source_tie_unresolved`），因为 IMDb 基础表没有分级。页面字段不含 `source_record_id`，用 `(asin → task4.source_record.locator->>'asin', source_type = 'page')` 取定位。

### 7. 类型映射（R34–R36）

受控词表：IMDb 25 词（`Action`, `Adult`, `Adventure`, `Animation`, `Biography`, `Comedy`, `Crime`, `Documentary`, `Drama`, `Family`, `Fantasy`, `Film-Noir`, `History`, `Horror`, `Music`, `Musical`, `Mystery`, `News`, `Romance`, `Sci-Fi`, `Short`, `Sport`, `Thriller`, `War`, `Western`）加 `Anime`, `Foreign`, `LGBT`, `Performing Arts`, `Silent`。

读 `task4.field_assertion(field_name = 'genre')` 的 `normalized_value`：同一页标签串已按 `,` 和 `/` 拆成多行，`raw_value` 是整串，不能直接映射。

映射表（小写词面 → 受控词）：

- 单一：`drama`/`tv drama`/`drama classic`→Drama；`comedy`/`classic comedies`/`stand-up`→Comedy；`horror`/`slasher flicks`→Horror；`romance`/`love & romance`→Romance；`documentary`→Documentary；`action`/`martial arts`→Action；`western(s)`→Western；`animation`/`animated`→Animation；`science fiction`/`sci-fi`→Sci-Fi；`musical(s)`→Musical；`thriller(s)`/`suspense`→Thriller；`mystery`→Mystery；`film noir`→Film-Noir；`crime`→Crime；`fantasy`→Fantasy；`adventure`→Adventure；`kids & family`/`children`/`children's`/`family`/`tv family`→Family；`military & war`/`war`/`world war ii`→War；`foreign`/`foreign films`/`international`/`art house & international`/`bollywood`→Foreign；`lgbt`/`gay & lesbian`/`gay & lesbian video`→LGBT；`silent`/`silent films`→Silent；`biography`→Biography；`history`→History；`sport(s)`→Sport。
- 音乐类页面：`music`、`classical`、`international music`、`indie & alternative`、`rock`、`classic rock`、`progressive rock`、`pop`、`vocal pop`、`rap & hip-hop`、`latin music`、`children's music`、`r&b`→Music。
- 复合：`action & adventure`→Action, Adventure；`mystery & thrillers`/`mystery & suspense`→Mystery, Thriller；`science fiction & fantasy`→Sci-Fi, Fantasy；`romantic comedy`/`romantic comedies`→Comedy, Romance；`anime`/`anime & manga`→Animation, Anime；`musicals & performing arts`/`performing arts`→Musical, Performing Arts。
- 噪声：`movies & tv`、`featured categories`、`dvd`、`blu-ray`、`dvd movie`、`blu-ray movie`、`vhs`、`movies`、`cds & vinyl`、`classic`、`classics`、`feature`、`general`、`live`、`performance`、`cult movies`、`award winning`、`europe`、`britain`、`britain & ireland`、`continental europe`、`digital movie`、`buy movie`、`rent movie`、`prime video`、年龄段（`3-6 years`、`7-9 years`、`10-12 years`）及非影视目录词（`books`、`electronics`、`accessories`、`camera & photo`、`video games` 等）。
- 其余为 `unmapped`，如 `spielberg`、`alien movie`。不猜测，逐行保留并汇总质量问题，开发池观察后才可入表。

### 8. 演职员（R37–R43）

处理顺序：

1. 读页面成员（`task5.movie_person`）。
2. 未熔断时读 IMDb 导演与主要演员。
3. 署名对齐：对页面无 `nconst` 的人物，与同作品同角色 IMDb 成员比较 `level`；取唯一最高等级命中，等级序 `L1 > L2 > L3 > LI > L4`。`John Cusack` 同时近似 `Joan Cusack` 时严格命中胜出。
4. 多人串：按 `;`、`; and `、` and `、`&` 拆段，逐段走第 3 步；全部成段命中才取代原行。`Black and Blue Productions` 不命中，保留原行。
5. 确定 `person_id`：已在 `task5.person_external_id` 的复用；否则写 `imdb_person_ext`。
6. 写 `movie_person_resolved`（`source_coverage`、`status`、`credit_order`、`imdb_ordering`、`resolution_note`）与 `credit_alignment`（`page_person_id`、`raw_name`、`segment`、`resolved_person_id`、`level`）。
7. 导演不一致按 R41；`cast_coverage` 按 R43 计算。

`task5.movie_person_source` 没有采集时间列，署名顺序平票直接取最小序号（R42），不再引用 `fetched_at`。

对齐只在同作品内生效，不修改任务 5 的人物。同一人在别的作品仍可能是页面 `person_id`；`credit_alignment` 提供这条线索，任务 9 查询人物时可据此联结。

### 9. 版本层（R44–R48）

- 上游：`edition_title`、`runtime_min`、`edition.publisher`、`edition.discs`、`edition.packaging`、`edition.media_format`、`edition.language`、`edition.dubbed`、`edition.subtitles`、`edition.aspect_ratio`、`edition.region`、`edition.cut_type`、`edition.video_system` 来自 `task4.field_assertion`；分级与 `format_swatch` 来自 `task3.page_field`；日期来自 `task6.product_release_date`。
- 发行商：去后缀 `Home Video`、`Home Entertainment`、`Pictures`、`Studios`、`Entertainment`、`Inc.`、`Corp.` 后比较词根。实例：`Warner Archive`/`Warner`、`Criterion Collection`/`The Criterion Collection` 归 `formatting_or_alias`；`MGM`/`Warner`、`Allied Vaughn`/`MGM` 归 `intra_asin_publisher_conflict`，主值按区域优先级取值，这类多为品牌与发行方并列，报告说明不等于前者一定正确。
- 日期：每 ASIN 的同类日期去重后唯一（正式库实测）；`product_release_date` 的选择记录含 `date_flags`。
- `discs`：候选进 `field_candidate`，有选择记录。
- 画幅词从 `edition.packaging` 与 `media_format` 原始串提取（`Widescreen`、`Full Screen`、`Anamorphic`、`Letterboxed`、`Pan & Scan`）。

## Airflow 编排与幂等恢复（R50–R55）

`check_inputs → edition_resolve → link_assess → movie_scalar_resolve → movie_multivalue_resolve → finalize_version → verify → finalize_report → gate`

1. `check_inputs`：校验上游版本存在且成功，幂等应用迁移；同输入键有 `succeeded` 运行时登记 `reused`，后续步骤跳过。
2. `edition_resolve`：按 `asin` 分批，写候选与 `edition_stage_result`。
3. `link_assess`：按 `movie_id` 分批，读版本阶段结果，写 `link_assessment`；全部批次完成后在一个事务内写 `shared_tconst_result`。
4. `movie_scalar_resolve`：按 `movie_id` 分批。
5. `movie_multivalue_resolve`：按 `movie_id` 分批。
6. `finalize_version`：单事务从阶段表物化版本表与 `shared_tconst_conflict`，版本状态 `unverified`。
7. `verify`：运行 `etl_task7.check`；通过后版本 `verified`、运行 `succeeded`。
8. `finalize_report`（`all_done`）：导出报告或写 `_failures/<run_id>.json`。
9. `gate`（`all_done`）：上游失败时抛 `AirflowFailException`。

沿用任务 6 的模式：每个步骤各持一次抽取级咨询锁，多个步骤共用一条 `task7.run`，靠 `airflow_run_id` 找回。与任务 6 的差别是运行状态在 `verify` 之后才成功，避免核对失败的版本被 `reused` 复用。

## 盲测评价设计（R61–R66）

1. **开发池**：`evaluate freeze` 在首次运行前执行，须先应用迁移；1,732 部，与任务 6 口径一致，`frozen_at` 早于运行 `started_at`。规划探查读过全量，规则形状并非只由开发池得出，报告据实说明。
2. **七层抽样**：

| 层 | 目标 | `target_field` | 样本 | 保底 |
|---|---|---|---|---|
| E1 | 标题跨源、跨版本差异 | `title` | 15 | `conflict_unresolved` ≥ min(可用, 4) |
| E2 | 年份分歧、上界矛盾、熔断 | `release_year` | 15 | 同上 |
| E3 | 片长各 `diff_class` | `runtime_minutes` | 20 | 同上 |
| E4 | 导演 8、署名对齐 8、署名顺序 4 | `director`、`credit_align:<role>:<page_person_id>`、`billing:<role>:<person_id>` | 20 | 署名对齐至少一半取 `L4` |
| E5 | 类型映射与并集 | `genres` | 15 | 含 `unmapped` 词面的作品优先 |
| E6 | 分级跨版本冲突 | `mpaa_rating` | 15 | `conflict_unresolved` ≥ min(可用, 4) |
| E7 | 发行商 10、发售日 10 | `publisher`、`product_release_date` | 20 | 带 `date_flags` 的优先 |

3. **盲测导出**：按 `md5(entity_id || ':' || target_field)` 打乱编号。抽样可以读系统状态，但 `raw_candidates_json` 由上游原始表重新生成，按 `(source, source_key)` 排序，不读 `field_candidate`，避免候选顺序或状态泄露结果。列：`item_id`、`entity_type`、`entity_id`、`target_field`、`entity_context`、`raw_candidates_json`、`human_action`（`resolve`/`keep_conflict`/`unsure`）、`human_winning_value`、`human_evidence`、`note`。
4. **比对规范**：标量去空白后比较，年份、片长、序号按整数，标题与发行商大小写折叠；多值字段用 `|` 分隔，导演按严格键、类型按受控词作无序集合比较；`credit_align` 比较“是否同一人”。
5. **AI 草稿与标注**：`_generate_ai_draft` 只收公开行与 `imdb`/`task3` 原始证据，不接受选择表。任务 6 的教训是代码生成的存疑备注被原样采纳，所以 `ai_reason` 只在草稿里出现，不进入 `eval_label.note`；`ai_doubt = true` 或改判的项必须填 `human_evidence`。标注人可以查公开网页，属人工核查，不进入 ETL。

## 资源预算与估算

| 项目 | 规划估计 | 依据 |
|---|---|---|
| `field_candidate` | 18 万–23 万行 | 作品 5 字段多源候选 + 商品 6 字段候选 |
| `movie_field_selection` | 77,931 行 | 8,659 × 9 |
| `edition_field_selection` | 57,540 行 | 9,590 × 6 |
| `link_assessment` | 5,407 行 | 每个有效关联一行 |
| `runtime_diff` | 约 5,111 行（实测 5,105） | 试算对数 |
| `movie_title_alias` | 约 15 万行 | IMDb 别名 142,236 行加页面变体 |
| `movie_genre` / `movie_genre_term` | 约 2 万 / 约 3.5 万行 | 平均约 3 个受控词；词面按作品去重 |
| `imdb_person_ext` | 约 2.2 万行（实测 22,157） | 试算 22,191 |
| `movie_person_resolved` | 约 8.1 万行 | 50,027 + 30,954（试算） |
| `credit_alignment` | 约 3,500 行（实测 6,747 行：对齐成功 3,358、无兼容成员 2,826、多人串分段 562、并列 1，另有 `movie_title_alias` 90,433 行、`field_candidate` 114,483 行） | 试算 3,335 加多人串分段；未对齐的行与别名、候选数低于估计的原因是不少作品没有 IMDb 关联 |
| 全量运行 | 5 分钟以内 | 无外部 I/O，分批事务 |
| schema `task7` 磁盘 | 500 MB 以内 | 含别名与 JSONB 证据 |
| 同参重跑 | 1 秒以内 | 输入键命中 |

## 异常与恢复处理

| 异常 | 处理 |
|---|---|
| 上游版本不存在或未完成 | `check_inputs` 确定性失败，不重试 |
| 另一运行持有咨询锁 | `RunBusy`，不重试 |
| 分批阶段中途失败 | 未提交批次回滚；重跑按 `entity_stage` 跳过已完成批次 |
| `finalize_version` 提交前中断 | 整体回滚，重跑只重做物化 |
| `verify` 失败 | 运行 `failed`，版本保持 `unverified`，不可被 `reused`；重跑在单事务中重写该版本 |
| 瞬时连接中断或死锁 | 判为暂时错误，最多重试 2 次 |
| 未知 MPAA、未映射类型词面 | 写 `quality_issue`，不崩溃，不污染受控词表 |

## 实现补充约定（T7-01 至 T7-06，2026-11-02）

实现时对 spec 与上文没有明确规定之处采用的做法，均不改变规则含义与 AC 预期；若与用户的期望不同，只需改对应函数与测试。

| 事项 | 做法 | 理由 |
|---|---|---|
| 阶段键里的抽取 | 键串以抽取 ID 开头（`edition_stage_key` 按上文 `抽取 \| t4 \| t6 \| parser \| norm \| 签名`），表里仍有独立的 `extraction_id` 列 | 与上文一致；冗余但无害 |
| 规则签名 | `RuleSet.signature(group)` 取该组字段值与算法标记 `ALGORITHM[group]` 的 SHA-256 前 12 位 | 只改片长容差只让 `movie_scalar` 的键变化；算法改动但数值不变时靠升算法标记 |
| 候选 ID | 批内成块从序列 `task7.candidate_id_seq` 取号，先决策后一次写入，候选状态已定 | 选择记录可直接引用候选 ID；用不完的号留空 |
| 阶段结果与版本表 | 阶段结果保存标量值与 `field_selections` 的 jsonb；`movie_field_selection`、`edition_field_selection` 等版本表由 T7-05 物化，本阶段只建表 | 沿用上文“阶段结果供物化使用” |
| 未知 MPAA | 候选保留（规范值 `NULL`）；有已知分级时标 `rejected`，没有时版本层为 `conflict_unresolved`（`intra_source_tie_unresolved`），作品层无可用候选为 `missing` | R12：存在候选但无法采用不得写成 `missing` |
| 同 ASIN 碟数不一致 | 按区域优先级取值，冲突类型借用 `intra_asin_runtime_conflict`，`disagreement_detail.field = 'discs'` | R13 闭集没有碟数专用类型；正式库此类为 0 |
| 已熔断作品的片长 | 不写 `runtime_diff`，各版本片长保留在版本层 | 熔断后 IMDb 片长不可信，写差异类别会污染统计 |
| 别名去重 | `movie_title_alias` 内同名（大小写折叠）别名只保留先出现者，顺序 `primary`、`original`、`aka`、页面变体 | 别名是名称集合；`aka` 里与主标题相同的写法不重复登记 |
| 页面候选的匹配可靠度 | `singleton`、`human` 为 `1.00`；`auto_decision` 取任务 4 决策置信度，缺失时 `0.90`；版本层候选来自本 ASIN 页面，为 `1.00` | 作品层候选还要经过任务 4 的商品归并 |
| 作品层分级、片长候选 | 每个 `edition` 商品一条，引用版本层选中的候选 ID 与原始值，而不是重新读页面 | 版本层同 ASIN 内的冲突已先消解，作品层不再重复处理 |
| 质量问题代码 | `cross_source_mismatch_halt`（熔断，`link_assess`）、`weak_link_single_signal`、`cross_source_override`、`severe_mismatch_unresolved`、`severe_runtime_conflict`、`unknown_mpaa_rating` | R18、R25、R26、R31、R33 点名了其中一部分，其余按冲突类型命名 |
| T 信号的页面标题键 | 读 `task4.field_assertion` 的 `normalized_value`（任务 4 规范键，括号说明已去掉），`normalized_value` 为空时才回退到 `title_key(clean_page_display_title(raw_value))`；`title_match_class` 同样接收该键，命中 IMDb 任一标题键时至少为 `alias` | `clean_page_display_title` 为保留副标题会留下没见过的括号（如 `300 (Limited Collector's Edition + Digital Copy)`），重算的键带说明词，使 T 误亮、《300》类正确关联被熔断；试算读的就是规范键，也是 R15 的“页面标题键” |
| T 信号的“实词” | `content_words` 不计虚词（冠词与 `and`、`of`、`in`、`on`、`at`、`to`、`for`、`with`、`from`、`by`、`or`、`is`、`as`），实词子集与 `subset_min_words` 都按实词计 | 因此规划试算（简化实现）的 8 部熔断在实现中为 7 部：`The Love for Three Oranges` 因 `of`、`for` 不再被当作无关而不熔断，`Fishbone` 不再是 T 单信号；design 正文与 spec H08 的数字已于 T7-08 用 `cv0001` 实测替换 |
| 署名对齐的对象与候选（T7-04） | 对象是页面未关联 `nconst` 的人物（含只在 `starring` 的人物），候选是尚未被 `nconst` 关联占用的同角色 IMDb 成员；页面人物取规范名与页面别名中最严的兼容等级；`starring` 复用 `cast` 的对齐结果（任务 5 把署名行演员同时写入 `cast`），所以 `credit_alignment` 的角色只有 `director` 与 `cast`；多个页面人物对齐到同一个 IMDb 人物时合并为一行 | `starring` 在对齐成功时使用对齐后的 `person_id`（仍标 `page_only`），避免同一人在同一作品的 `starring` 与 `cast` 中有两个 ID |
| 对齐记录与多人串（T7-04） | 没有可比较的 IMDb 成员时，单人不写 `credit_alignment`（没有发生比较）；多人串无论有无 IMDb 都逐段写行。全部成段命中且各段命中不同人物才取代原行（各段 `aligned`）；否则原行保留，各段 `outcome = unsplit_multi_person`，命中的段保留 `nconst`、等级作证据，`resolved_person_id` 为空 | R39：不为分段新造 `person_id`；`Black and Blue Productions` 因此原样保留 |
| 页面独有导演（T7-04） | 当前仍是 `adopted` 的页面独有导演，在作品有未熔断 IMDb 且 IMDb 有导演时标 `conflict_pending`，包括页面已关联 `nconst` 但不在 IMDb 导演中的；没有可用 IMDb 导演时页面导演照常采用 | R41 按字面执行；实测 267 位、涉及 210 部作品，见 T7-08 的统计 |
| 署名顺序与成员顺序（T7-04） | 只统计该作品 `edition` 商品的署名行序号（合集盒子的署名行不是该作品的署名顺序），没有时回退任务 5 的 `credit_order`；IMDb 独有成员的 `credit_order` 在页面最大序号之后顺延；多值字段的 `winning_value` 为采用成员的 `person_id`（类型为受控词）按署名顺序用 `|` 连接 | R42；候选数组为空，成员明细在 `movie_person_resolved` 与 `movie_genre` |
| 多值字段的冲突类型与置信度（T7-04） | 字段级 `conflict_type` 按优先级取：`director_source_disagreement` > `billing_conflict_resolved` > `complementary_union`（页面与 IMDb 成员并存）> `formatting_or_alias`（有对齐）> `none`；全部成员无法采用时为 `conflict_unresolved` 与 `intra_source_tie_unresolved`；置信度取 `conf_union`（0.90），只有 IMDb 独有成员时取 `conf_imdb_only`（0.80），含 IMDb 成员时不超过关联基础可靠度 | R13 闭集没有“多人串”专用类型，沿用 `intra_source_tie_unresolved` |
| 类型词面行（T7-04） | `movie_genre_term` 按（作品、区域、小写词面）一行，`asins` 去重；`genre` 区取任务 4 断言的 `normalized_value`，`breadcrumb` 区取任务 3 字段的 `value_raw`；IMDb 类型不在受控词表内时不入 `genres`，写 `unmapped_genre_term`（`section = imdb`） | 同一页面标签串里重复的标签（如两个年龄段词都带 `Kids & Family`）按 ASIN 去重，所以逐行统计略少于按断言行统计（实测 5,311 行断言对应 5,201 行去重后的页面词面） |
| IMDb 人物登记（T7-04） | `imdb_person_ext` 以 `nconst` 为全局键，`ON CONFLICT DO NOTHING`；任务 5 的 `person_external_id` 已有的人物复用其 `person_id` 不登记；`person_identity` 已有 `imdb:<nconst>` 锚点时取其 `person_id` 并标 `in_task5_identity` | R40 |
| 类型词面的实测对比（T7-04） | 临时库（业务库完整副本）实测：页面类型词面按断言行统计未映射 276/5,311（5.2%，与试算一致）；面包屑未映射 331/30,146（1.1%），试算为 223（0.7%），差 108 行：331 行里 320 行位于非影视目录（`CDs & Vinyl` 169、`Books` 43、`Arts, Crafts & Sewing`、`Video Games`、`Office Products` 等），只有 11 行在 `Movies & TV` 下。差异来自试算时映射表比 design 列出的词表多收了一批音乐子类与非影视目录词 | 映射表按 design 列出的词表实现，没有为凑数字追加条目；是否追加音乐子类与非影视目录噪声词，已在 T7-08 的开发池核查后决定（见下一行） |
| 类型映射在开发池上冻结（T7-08，`t7r1`） | 开发池 1,732 部上，面包屑未映射 74 行（62 种词面）、页面类型 54 行（54 种）。只追加影视语义明确的词：① 音乐风格与体裁 → `Music`（`Hardcore & Punk`、`Progressive`、`Album-Oriented Rock (AOR)`、`Blues`、`Folk`、`Flamenco`、`Concertos`、`Symphonies`、`Opera & Classical Vocal` 等 30 个，与已有的 `rock`、`pop`、`classical` 同类）；② 同义写法（`kids and family`、`family entertainment` → `Family`；`comedy romance` → `Comedy`, `Romance`；`spy thriller` → `Thriller`；`military` → `War`；`short` → `Short`）；③ 明显的非影视零售目录词与地区、格式词 → `noise`（`Sphygmomanometers`、`Makeup`、`Clothing`、`Grill Racks`、`Eastern Europe`、`3D`、`Universal Blu-ray` 等 35 个）。含义不明确的词（`Soul`、`Country`、`Gospel`、`Halloween`、`Faith & Spirituality`、`Spielberg` 等）保持 `unmapped`，不猜测。因为映射表变化，`ALGORITHM['multivalue']` 由 `m1` 升到 `m2`（阶段键随之变化）。冻结后全库实测（按 ASIN 计）：页面类型未映射 254/5,201（4.9%）、面包屑 192/30,146（0.64%）；追加前按断言行为 276/5,311 与 331/30,146。开发池上面包屑未映射降到 6 行，其余在非开发池里的未映射多为开发池没有出现的音乐子类（`Jazz`、`New Wave & Post-Punk`、`Indie Rock` 等），按“只看开发池”的约定不追加，保留为已知局限 | 追加的是同类词，不是为凑 223 这个规划数字；全库面包屑未映射比例与规划试算接近属巧合，后一半没有追加的词说明了这一点 |
| 版本物化与核对（T7-05） | `finalize_version` 单事务写版本与五张物化表，版本状态 `unverified`；同输入键已有未核实版本时在同一事务里清掉它的物化行再重写，版本号不变；`verify` 通过后单事务把版本标 `verified`、运行标 `succeeded`，失败时运行为 `failed`、版本保持 `unverified`、不会被 `reused`。命令行 `etl_task7.check` 要求版本已 `verified`，DAG 的 `verify` 步骤在标记前调用，传 `expect_verified=False` | R55；阶段结果按阶段键保留，所以核对失败后的重跑不重算阶段 |
| 核对的口径（T7-05） | 标量字段的胜出值必须等于采用候选的 `normalized_value` 或 `raw_value`（版本层 `edition_title` 取原始标题、`publisher` 取原写法，候选的规范值是清洁标题与折叠键）；已熔断作品的原名、上映年份、片长不得为 `resolved`（有候选为 `conflict_unresolved`，没有候选如 IMDb 原名为空时为 `missing`）；孤儿候选指阶段键下不被任何选择记录考虑的候选 | 与 R12 一致：存在候选才是 `conflict_unresolved`，没有候选就是 `missing` |
| 报告与查询（T7-05） | 导出目录 `outputs/task7/<抽取>/<版本>/<run_key>/`：`summary.json`、`report.md`、`examples.md` 与 8 个 CSV（作品与版本层选择记录、关联评估、片长差异、署名对齐、类型词面、共享 `tconst`），不含评论正文与整页 HTML；`examples.md` 每类最多 3 例，没有实例的类别写“无”；`query conflicts` 的 `--limit` 限制两层合计条数；`query edition` 的评论统计按 `public.review_record` 的 `classification` 计数 | R58–R60 |
| DAG 与故障注入（T7-06） | DAG 任务名 `movie_scalar_resolve`、`movie_multivalue_resolve` 对应运行器阶段 `movie_scalar`、`movie_multivalue`；`fault_injection` 取 `before_commit:<阶段>:<批号>`、`transient_once:<标记文件>` 或 `verify_fail`，只在 `ETL_ENV=test` 下生效；`VerificationFailed` 是确定性错误，不重试；`finalize_report` 额外写 `run_summary.json`（任务状态、失败记录、各阶段运行），与 `report.export_files` 的 `summary.json`（统计）分开；`batch_size` 默认 1,000 | R54、R55 |

| 评价工具（T7-07） | `evaluate freeze` 先调用幂等的 `run.apply_task7_schema` 再冻结，不另写迁移命令；`export` 要求开发池已冻结，系统状态为 `missing` 的项不入池（没有候选，无从评价）；抽样位内先取 `conflict_unresolved` 保底，再取预留桶，再按桶轮流各取一项，桶内按 `md5(entity_id:target_field)` 稳定排序，不足用该层余项补，层不足时按 `E3 → E4 → E1 → E7` 补足；`eval_set.strata` 记录各层目标、实际、补足、可用数与各桶计数 | R62、R63 要求保底与补足顺序，桶轮流是工程默认，保证每层覆盖不同冲突类型而不是只取最容易的一类 |
| 盲测比较口径（T7-07） | `director` 的系统值取采用成员的姓名（IMDb 姓名优先），按严格姓名键无序比较；`credit_align` 的值是同一人的 `nconst`（没有同一人填 `none`），系统 `aligned` 为 `resolved`、同等级并列未对齐为 `conflict_unresolved`，只抽单人署名；`billing` 的值是署名序号，只抽跨版本序号不一致的人物；`genres` 按受控词无序集合比较；发售日用 ISO 日期 | R65 的“是否同一人”需要可核实的标识，姓名拼写差异不应让对齐判错 |
| 指标定义（T7-07） | 动作准确率 = 系统动作与人工一致 / 非 `unsure`；值准确率只在系统 `resolved` 且人工 `resolve` 的项里算（动作不一致已由动作准确率体现，避免重复扣分）；`conflict_unresolved` 精确率与召回率分别以系统保留、人工保留为分母；`unsure` 与分母为零均单独呈现 | R65 |
| AI 草稿（T7-07） | `generate_ai_draft(row)` 只接收盲测行，是规则式脚本而非大模型；`ai_reason` 只写入草稿；导入时 `ai_doubt` 为真或与草稿动作、值不同的项必须有 `human_evidence`，`note` 与 `ai_reason` 相同被拒绝，默认要求 120 项齐全才写入（全有或全无） | R66；任务 6 中代码生成的存疑备注被原样采纳的教训 |
