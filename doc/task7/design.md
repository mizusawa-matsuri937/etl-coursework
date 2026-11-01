# 任务 7 设计思路（处理不同来源的信息冲突）

状态：2026-10-30 在分支 `task7-conflict-resolution`（从 `main` 的 `649ac06` 建立）编写计划；同日用户确认作品层与版本层字段边界、IMDb 有效关联范围（5,407 部）、演职员分角色权威与可信并集、标量字段容差与保留 `conflict_unresolved` 边界、以及 120 项七层评价样本与非循环 AI 预标注方案（H01–H05）。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明架构、表结构、冲突分类与消解算法、阶段键与幂等恢复、Airflow 编排、盲测评价分层及正式库只读探查的真实基线数字。阈值与置信度分档为**规划默认值**（规则版本 `t7r1`），只在开发池上调整、评价表导出前冻结；资源数字为**规划估计**，实测在 T7-07 补记。

## 整体思路与架构边界

任务 7 是集成层向目标层收敛的核心环节：任务 3 抽取了页面原始字段（`task3.page_field`），任务 4 完成了电影作品聚类与规范化断言（`task4.movie`、`task4.field_assertion`），任务 5 完成了页面人物消歧与演职员关系（`task5.person`、`task5.movie_person`），任务 6 完成了首次公开发行年份补全、人物共现反查与条件性推断上界（`task6.release_selection`、`task6.inferred_release_bound`）。在此基础上，任务 7 解决**跨数据源（IMDb vs Amazon 页面）**、**跨商品版本（同一作品下多个 DVD/Blu-ray/VHS 版本）**以及**单商品页面内部不同区域（`byline` vs `detail_bullets` vs `product_overview` vs `breadcrumb`）**的字段冲突。

核心设计原则是**先分清实体粒度（作品层 vs 版本层），再分清字段基数（标量择优 vs 多值可信并集），最后划定可消解容差与保留冲突（`conflict_unresolved`）的硬边界**：

| 层次 | 实体粒度 | 消解字段 | 基数与策略 | 核心来源与优先序 |
|---|---|---|---|---|
| **作品层（`Movie`）** | `movie_id`（8,659 部：含 `edition` 作品 8,472 部、仅 `contains` 成员 187 部） | `title`（主标题）、`original_title`（原名） | 标量择优（保留别名变体证据） | IMDb `primary_title` / `original_title`（无错配熔断时） > 页面清洁标题众数（`edition` 读 `entity_id = asin`，`contains` 成员读 `entity_type = 'collection_member' AND entity_id = asin || '#' || member_ordinal`，平票取最早 ASIN）；无 IMDb 且多版本标题键实质不同标 `conflict_unresolved` |
| **作品层（`Movie`）** | `movie_id` | `release_year`（首次公开发行年份） | 标量择优 + 跨字段核验（任务 6 推断上界独立保留） | 衔接任务 6 四类候选（`imdb_t4_link` > `imdb_t5_hint` > `imdb_t6_cooccur` > `amazon_title_year`）；相差 1 年记 `minor_tolerance`；弱匹配（`hint`/`cooccur`）若年份差 ≥2 年（或 `task6.bound_evidence`/`quality_issue` 中上界早于 IMDb 年份 ≥2 年）且伴随片长差 >15 分钟或导演不重合，触发**错配熔断**降为 `conflict_unresolved` |
| **作品层（`Movie`）** | `movie_id` | `runtime_minutes`（作品标准片长） | 标量择优 + 差异成因分类 | IMDb `runtime_minutes`（无错配熔断时） > `edition` 商品（排除 `contains` 合集总片长）标准版片长众数；无 IMDb 且跨版本极差 >5 分钟又无剪辑版区分时标 `conflict_unresolved` |
| **作品层（`Movie`）** | `movie_id` | `mpaa_rating`（作品内容分级） | 标量择优（具体分级优于未评级） | 页面各 `edition` 商品（读取 `task3.page_field(field_key='mpaa_rating')`，排除 `contains` 合集整体分级）规范化分级聚合：单一具体分级（`G`/`PG`/`PG-13`/`R`/`NC-17`/`X`）胜过 `NR/Unrated`；跨版本出现**两种及以上不同具体分级**（正式库 5 部）标 `conflict_unresolved` |
| **作品层（`Movie`）** | `movie_id` | `genres`（电影类型） | 多值受控词表映射 + 并集 | IMDb `title.genres` ∪ Amazon `edition` 页面 `genre`/`breadcrumb` 映射后的 30 词受控词表（过滤 `DVD`、`Movies & TV` 等载体与导航噪声） |
| **作品层（`Movie`）** | `movie_id` | `director`、`starring`、`cast`（演职员） | 多值可信并集 + 角色边界保护 + 署名顺序消解 | `director` 与 `cast` 取 `task5.movie_person` ∪ IMDb `title_director`/`title_principal`（IMDb 独有人物在 `task7.imdb_person_ext` 生成稳定 `person_id`）；`starring` **只取页面 `byline` 署名行 Actor**；分号 `;` 多导演串拆分匹配；署名顺序冲突（117 项）取众数 |
| **版本层（`ProductEdition`）** | `asin`（9,590 个参与匹配商品） | `edition_title`、`runtime_minutes`、`mpaa_rating`、`publisher`、`product_release_date` 与标量 `discs` | 版本内标量择优（跨版本差异属正常物理属性，不跨版本压平） | 同 ASIN 内 `detail_bullets` > `product_overview` > `product_details` > `byline`；发售日期优先 `media_release` 并同时保留 `listing_first_available`；`discs` 优先取 `edition.discs` 断言并回退 `edition.packaging` 中的 `(\d+)-disc` |
| **版本层（`ProductEdition`）** | `asin` | `media_format`、`language`、`subtitles`、`aspect_ratio`、`region`、`cut_type`、`video_system`（7 项多值特征） | 版本内多区域规范化并集 | 同 ASIN 内 `task4.field_assertion`（含 `edition.dubbed` 与 `edition.packaging` 画幅词）∪ `task3.page_field`（含 `format_swatch` 与 `media_format` 原始串画幅词），每项保留区域定位 |

## 正式库只读探查基线（2026-10-30 实测）

在规划阶段对正式库 `etl_movies`（`SET default_transaction_read_only = on;`，抽取 `f1e8b845…`、`map0002`、`pmap0001`、`rv0001`、`imdb_2026-10-29`）执行了全量只读探查，真实冲突分布如下：

| 探查维度 | 正式库实测数字 | 设计应对（对应规则） |
|---|---|---|
| **有效 IMDb 关联作品数** | 8,659 部作品中，任务 4 `verified` **4,473 部**、任务 5 线索 `hint_matched` **293 部**、任务 6 共现 `cooccur_matched` **641 部**，合计 **5,407 部**有对应 `tconst`；仅页面标题年份 `page_reported` **50 部**，无年份 `missing` **3,202 部** | R04：5,407 部均引入 IMDb 候选，但按 `0.95 / 0.80 / 0.75` 区分可靠度，并对 `hint`/`cooccur` 设跨字段错配熔断（R11） |
| **标题与原名差异** | 5,407 部有 `tconst` 的作品中：页面标题键命中 IMDb `primary_title` **4,515 部**、仅命中 `original_title` **108 部**、仅命中 `aka` **291 部**、仅实词子集吻合（页面带载体/系列尾巴）**493 部**；IMDb `primary_title ≠ original_title` 共 **871 部**（`verified` 700、`cooccur` 111、`hint` 60）；多版本作品中跨商品原始标题不同 **199 部**、规范化标题键不同 **18 部** | R09–R10：`title` 与 `original_title` 分列消解；命中 `primary`/`original`/`aka` 或实词子集的页面标题记为 `formatting_or_alias` |
| **上映年份分歧与上界矛盾** | 286 部有多条年份候选的作品中，**13 部**存在跨源年份分歧：相差 1 年（`year_diff_1`）**8 部**（`verified` 6、`cooccur` 2），相差 ≥2 年（`year_disagreement`）**5 部**（均在 `cooccur_matched`，差值 2、5、18、19、21 年）；`tconst_disagreement` **0 部**；推断上界早于采用上映年（`bound_before_release_year`）**62 部**（`review` 37 + `media_release` 25） | R11–R12：相差 1 年记 `minor_tolerance`；相差 ≥2 年或上界早 ≥2 年结合片长/导演交叉验证，弱匹配且多字段矛盾时熔断为 `conflict_unresolved` |
| **片长（`runtime_minutes`）覆盖与冲突** | 8,659 部作品中：IMDb 与页面均有 **4,883 部**（5,111 个版本对）、仅 IMDb **480 部**、仅页面 **2,768 部**、均无 **528 部**。单 ASIN 内多区域片长冲突 **0 例**；240 部多版本作品中跨版本片长不同 **74 部**。在双方均有的 4,883 部（5,111 对）中：<br>• 完全一致：**2,880 部**（3,064 对）<br>• 相差 1–2 分钟：**864 部**（889 对）<br>• 相差 3–5 分钟：**484 部**（其中符合 PAL 24/25 帧率差约 4% 的有 **251 对**，其余 3–5 分钟微差 **264 对**）<br>• 相差 >5 分钟且带 `edition.cut_type`（22 对）或多碟含花絮（`discs > 1` 且 ≥1.5 倍，23 对）：**45 对**<br>• 未解释差异 6–15 分钟：**371 对**（379 部）<br>• 未解释差异 >15 分钟：**248 对**（276 部） | R13–R14：作品层取标准片长，版本层保留各版本实际片长；精确识别 PAL 24/25 帧率转换、剪辑版与多碟花絮；无 IMDb 且跨版本差 >5 分钟无剪辑版标记时标 `conflict_unresolved` |
| **演职员（`director` / `starring` / `cast`）** | `task5.movie_person` 共 50,027 行（`director` 6,833、`starring` 13,151、`cast` 30,043），跨版本署名顺序冲突 `order_conflict` **117 行**（`director` 23 行/21 部、`starring` 47 行/35 部、`cast` 47 行/35 部）。<br>• **导演**（5,407 部有 `tconst` 作品）：双方均有 **5,035 部**、仅 IMDb 有导演 **364 部**（`verified` 215、`hint` 108、`cooccur` 41）、仅页面有 **4 部**、均无 **4 部**；在 `verified` 4,473 部中，集合完全一致 4,038 部，IMDb 多出联合导演 131 部，页面含未关联 `nconst` 导演 79 部（含分号 `;` 多人串）。<br>• **演员**（`verified` 4,473 部）：双方均有 **4,166 部**、仅 IMDb **133 部**、仅页面 **134 部**、均无 **40 部**；页面 `cast` 19,316 行（已关联 `nconst` 16,124 行、仅页面 3,192 行），IMDb `title_principal`（`actor`/`actress`）41,400 行，IMDb 可补充约 **25,276 条**主要演员关系。 | R19–R22：`director` 与 `cast` 取可信并集，IMDb 独有人物在 `task7.imdb_person_ext` 生成稳定 `person_id`；`starring` 仅取页面署名行；分号串拆分匹配；署名顺序冲突取众数消解 |
| **电影类型（`genres`）** | IMDb 有类型 **5,397 部**（25 个标准词），页面 `genre` 断言有 **3,489 部**（双方共有 2,285、仅 IMDb 3,112、仅页面 1,204、均无 2,058），另 **9,538 个 ASIN** 有 `breadcrumb` 分类 | R17–R18：定义 30 词受控词表，拆分页面复合词、过滤 `DVD`/`Movies & TV` 等噪声词后取多源并集 |
| **内容分级（`mpaa_rating`）** | 7,854 个 ASIN（7,002 部作品）有页面分级，`byline` 写简称、`detail_bullets` 写全称；规范化为 6 档具体分级 + `NR/Unrated` 后，**单 ASIN 内冲突为 0**；跨版本分级不同的 **31 部作品**中，**26 部**为“单一具体分级 vs `NR/Unrated`”，**5 部**存在两种不同具体分级（`Phenomenon` G/PG、`Veer Zaara` G/PG、`Forbidden Zone` R/NC-17、`Hard Target` R/G、`The Five Heartbeats` R/PG） | R15–R16：同 ASIN 简称与全称归一；作品层具体分级胜过 `NR/Unrated`（26 部），两种不同具体分级（5 部）保留 `conflict_unresolved` |
| **版本层属性（`edition.*`）** | 单 ASIN 内多值/差异：`publisher` **98 个 ASIN** 在 `detail_bullets` 与 `product_overview` 写法不同；`product_release_date` **1,507 个 ASIN** 同时含 `media_release` 与 `listing_first_available`；240 部多版本作品跨版本差异：`media_format` 240 部、`language` 148 部、`publisher` 140 部、`runtime_min` 74 部、`subtitles` 49 部、`aspect_ratio` 32 部、`discs` 26 部、`video_system` 18 部、`title` 14 部、`region` 3 部、`cut_type` 1 部 | R03、R23–R24：版本层按 ASIN 独立消解并保留全部版本特征，不把跨版本物理差异当成作品层错误 |
| **跨作品共享 `tconst`** | `task6.quality_issue` 中 `shared_tconst_with_movie` **126 项**（任务 5 线索 40 项 + 任务 6 共现 86 项） | R26：在 `task7.shared_tconst_conflict` 逐对对比并分类为 `likely_duplicate_movie` 或 `false_positive_link` |

## 开源参考

按 `CLAUDE.md` 要求，设计前对比了数据融合（Data Fusion）与真值发现（Truth Discovery）领域的经典方案与开源实现：

| 项目 / 规范 | 借鉴什么 | 不直接照搬的原因 |
|---|---|---|
| **Bleiholder & Naumann (ACM Computing Surveys, *Data Fusion*)** 的冲突处理分类体系（Conflict Ignorance / Conflict Avoidance / Conflict Resolution: Deciding vs Mediating） | 严格采用 **Deciding（在真实候选中择优并保留来源，绝不凭空捏造折中值）** 策略处理标量字段，采用 **Set Union with Controlled Vocabulary** 处理多值字段，采用 **Explicit Uncertainty (`conflict_unresolved`)** 处理证据对等且无法裁决的硬冲突 | 文献中的 Mediating（如对两个冲突年份或片长取算术平均）会制造任何源都不存在的虚假值，违反作业“不得编造答案”与“每个最终字段须追溯到原始来源”的要求 |
| **[Schema.org `Movie` vs `Product` / `PublicationEvent`](https://schema.org/Movie)** 与 **FRBR（书目记录功能需求：Work / Expression / Manifestation）** | 将创作作品（`Work / Movie`：标题、原名、首映年、标准片长、类型、导演与演员）与载体发行版本（`Manifestation / ProductEdition`：DVD/VHS 载体、碟数、配音字幕、发行商、商品发售日、版本实际时长）严格分层建模 | 避免把 DVD 与 VHS 的载体差异、重发商差异或花絮时长差异误判为作品层数据冲突 |
| **[IMDb Non-Commercial Datasets](https://developer.imdb.com/non-commercial-datasets) & MPAA Film Rating System** | 采用 IMDb 25 个标准 `genres` 作为受控词表骨架；采用 MPAA 标准分级体系区分“已获得具体分级（`G`/`PG`/`PG-13`/`R`/`NC-17`/`X`）”与“未送审/未评级版本（`NR`/`Unrated`）” | Amazon 页面分类树混杂了商品导航词（`Featured Categories`、`DVD`），必须经显式映射表清洗后才能并入作品类型 |

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/601_task7_schema.sql` | 创建 schema `task7` 下的运行批次、阶段结果持久化表、消解版本、候选表、作品层/版本层选择表、多值类型与演职员表、共享 `tconst` 审计表与评价表；登记迁移 `601` |
| `src/etl_task7/rules.py` | 纯函数与规则集定义：`RuleSet`、`RULESETS`、`CURRENT_RULE_VERSION = "t7r1"`；标题清洁化、跨字段错配熔断判定、片长差值分类（含 PAL 24/25 公式）、MPAA 分级规范化与消解、30 词类型映射表、分号多人串拆分、署名顺序众数决胜、出版商同义归一 |
| `src/etl_task7/editions.py` | 商品版本层（`ProductEdition`，按 `asin` 分批）候选收集、标量消解（`edition_title`、`runtime_minutes`、`mpaa_rating`、`publisher`、`product_release_date` 与标量 `discs`）、7 项多值特征并集及阶段结果表 `task7.edition_stage_result` 落库（R23–R24） |
| `src/etl_task7/movies_scalar.py` | 作品层（`Movie`，按 `movie_id` 分批）5 项标量属性（`title`、`original_title`、`release_year`、`runtime_minutes`、`mpaa_rating`）的跨源候选收集、错配熔断评估、消解选择及阶段结果表 `task7.movie_scalar_stage_result` 落库（R09–R16） |
| `src/etl_task7/movies_multivalue.py` | 作品层多值属性（按 `movie_id` 分批，读取标量阶段已落库的 `cross_source_mismatch_halt`）：`genres` 受控词表映射与并集（R17–R18）；演职员（`director`/`starring`/`cast`）跨源并集、`task7.imdb_person_ext` 稳定 `person_id` 登记、分号串拆分匹配、署名顺序冲突消解及阶段结果表 `task7.movie_multivalue_stage_result` 落库（R19–R22） |
| `src/etl_task7/shared_tconst.py` | 跨作品共享 `tconst`（126 项）的双作品字段对比与分类（`likely_duplicate_movie` vs `false_positive_link`，R26）；从已落库阶段表无状态物化生成冲突消解版本 `cv0001`… 及跨版本 `diff`（R27） |
| `src/etl_task7/run.py`、`runner.py` | 迁移应用、输入键与阶段键计算、运行审计、PostgreSQL 咨询锁、分批提交与中断恢复编排（R28–R29） |
| `src/etl_task7/report.py`、`query.py`、`check.py` | 统计报告与实例导出（R33、R35）；三个只读查询 `movie`、`edition`、`conflicts`（R34）；正式库只读核对 `check`（AC18） |
| `src/etl_task7/evaluate.py` | 开发池冻结、120 项七层盲测表导出（MD5 全局打乱）、非循环 AI 预标注草稿生成、人工审核标注导入与指标计算（R36–R39） |
| `src/etl_task7/pipeline.py`、`dags/amazon_movies_task7_conflict.py` | Airflow DAG 步骤包装与 9 节点 DAG 定义（R30） |
| `scripts/seed_task7_e2e.py` | 为 `compose.e2e.yaml` 真实 scheduler 端到端验证准备 `etl_test` 合成数据 |
| `tests/fixtures/task7/`、`tests/unit/test_task7_rules.py`、`tests/integration/test_task7_*.py` | 单元测试与集成测试夹具及测试套件 |

## 数据模型（schema `task7`，迁移 `601`）

作业第八部分逻辑模型与 `task7` 表的对应关系：

| 作业逻辑实体 | `task7` 表名 | 说明 |
|---|---|---|
| `Movie`（消解后目标视图） | `task7.movie_resolved` | 每部作品一行（主键 `(conflict_version_id, movie_id)`）：消解后的标量字段、各字段状态、关联 `tconst` 与可靠度、任务 6 推断上界参考、类型数与演职员数 |
| `ProductEdition`（消解后目标视图） | `task7.edition_resolved` | 每个参与匹配商品一行（主键 `(conflict_version_id, asin)`）：消解后的版本标题、版本片长、版本分级、发行商、实体发售日与上架日、标量 `discs`、7 项多值版本特征（数组 + JSONB 详情） |
| `FieldAssertion`（冲突消解候选） | `task7.field_candidate` | 作品层与版本层所有参与比较的标量字段候选值、规范值、来源定位、可靠度、候选状态（`adopted`/`rejected`/`conflict_pending`/`superseded_by_specific`）与理由（按 `(extraction_id, stage_key)` 分阶段持久化） |
| 阶段决策持久化表（支撑增量重算与断点恢复） | `task7.edition_stage_result`、`task7.movie_scalar_stage_result`、`task7.movie_multivalue_stage_result` | 按 `(extraction_id, stage_key, entity_id)` 持久化各分批阶段的消解决策、胜出值、冲突类型、理由、置信度、错配熔断标记及版本多值特征，供 `finalize_version` 无内存状态依赖地物化版本表 |
| `FieldSelection`（字段选择决策） | `task7.movie_field_selection`、`task7.edition_field_selection` | 每个冲突消解版本 × 每个实体 × 每个字段一行：状态（`resolved`/`conflict_unresolved`/`missing`）、胜出值、采用的候选 ID、参与候选 ID 列表、冲突类型、规则编号、选择理由与置信度 |
| `Movie` 多值类型 | `task7.movie_genre` | 主键 `(extraction_id, stage_key, movie_id, genre)`（通过 `conflict_version.multivalue_stage_key` 关联）：来源集合（`both`/`imdb_only`/`page_only`）、原始词面、来源定位与置信度 |
| `Person`（IMDb 扩展登记） | `task7.imdb_person_ext` | 主键 `(extraction_id, imdb_snapshot_id, person_id)`：为 IMDb 独有演职员按 `imdb:<nconst>` 锚点生成的稳定 `person_id`、规范名、生卒年与源记录行号（与 `task5.person` 无缝互补，不改 `task5`） |
| `MoviePerson`（消解后演职员） | `task7.movie_person_resolved` | 主键 `(extraction_id, stage_key, movie_id, person_id, role)`（通过 `conflict_version.multivalue_stage_key` 关联）：来源集合（`both`/`imdb_only`/`page_only`）、消解后 `credit_order`、`imdb_ordering`、状态（`adopted`/`conflict_pending`）、署名顺序冲突消解详情与源定位 |
| `MergeDecision` / 跨作品冲突线索 | `task7.shared_tconst_conflict` | 主键 `(conflict_version_id, tconst, candidate_movie_id)`：126 项共享 `tconst` 的作品对对比摘要与分类（`likely_duplicate_movie` / `false_positive_link`） |
| `ETLRun` 与 `QualityIssue` | `task7.run`、`task7.run_batch`、`task7.entity_stage`、`task7.conflict_version`、`task7.quality_issue` | 运行审计、分批恢复、版本管理与质量问题（`quality_issue` 按 `(extraction_id, stage_key)` 记录阶段问题并在版本查询中汇总） |

### 关键表结构设计

1. **运行与版本管理（`task7.run`、`task7.conflict_version`、`task7.entity_stage`）及阶段结果持久化表**
   - **输入键（`input_key`）**：`extraction_id | t4_mapping_version_id | t5_person_mapping_version_id | t6_release_version_id | parser_version | norm_version | imdb_snapshot_id | rule_version`。
   - **阶段键（`stage_key`）**：
     - `edition_resolve`（对应 `edition_stage_key`）：`extraction_id | t4_mapping_version_id | t6_release_version_id | parser_version | norm_version | edition_rule_signature`（按 ASIN 分批）；
     - `movie_scalar`（对应 `scalar_stage_key`）：`extraction_id | t4_mapping_version_id | t5_person_mapping_version_id | t6_release_version_id | imdb_snapshot_id | halt_rule_signature | scalar_rule_signature`（按 `movie_id` 分批）；
     - `movie_multivalue`（对应 `multivalue_stage_key`）：`extraction_id | t4_mapping_version_id | t5_person_mapping_version_id | t6_release_version_id | imdb_snapshot_id | halt_rule_signature | multivalue_rule_signature`（按 `movie_id` 分批）。
     - **跨阶段依赖与增量复用**：由于 `movie_multivalue` 依赖 `movie_scalar` 判定的 `cross_source_mismatch_halt`（R18、R19 规定触发错配熔断时不并入 IMDb 类型与演职员），两个阶段的签名均包含 `halt_rule_signature`，且 `movie_multivalue_resolve` 直接从数据库表 `task7.movie_scalar_stage_result`（按当前 `scalar_stage_key`）读取每部作品已持久化的 `effective_tconst`、`tconst_source` 与 `cross_source_mismatch_halt`。当只修改作品层片长容差（不影响错配熔断阈值与多值规则）时，`edition_stage_key` 与 `multivalue_stage_key` 不变，直接复用已落库阶段结果，仅重算 `movie_scalar` 并汇总生成新的 `conflict_version_id`（如 `cv0002`）。
   - **三张阶段结果持久化表（解决分批中断恢复 AC12 与跨版本阶段复用 AC11 的无状态物化）**：
     - `task7.edition_stage_result`（主键 `(extraction_id, stage_key, asin)`）：持久化每个 ASIN 的 `movie_ids`、`product_kind`、`edition_title`、`edition_title_norm`、`runtime_minutes`、`mpaa_rating`、`publisher`、`media_release_date`、`listing_first_available_date`、标量 `discs integer`、7 项多值特征数组（`media_formats`、`languages`、`subtitles`、`aspect_ratios`、`regions`、`cut_types`、`video_systems`）、`attributes_detail jsonb` 及 5 个版本标量字段的决策明细 `field_selections jsonb`（每个字段的 `status`、`winning_value`、`selected_candidate_ids`、`considered_candidate_ids`、`conflict_type`、`selection_rule`、`reason`、`confidence`、`disagreement_detail`）。
     - `task7.movie_scalar_stage_result`（主键 `(extraction_id, stage_key, movie_id)`）：持久化每部作品的 `effective_tconst`、`tconst_source`、`cross_source_mismatch_halt boolean`、`halt_detail jsonb`、`inferred_bound_time`、`inferred_bound_precision` 及 5 个作品标量字段（`title`、`original_title`、`release_year`、`runtime_minutes`、`mpaa_rating`）的决策明细 `field_selections jsonb`。
     - `task7.movie_multivalue_stage_result`（主键 `(extraction_id, stage_key, movie_id)`）：持久化每部作品的 `genres text[]`、`director_count`、`starring_count`、`cast_count` 及 4 个作品多值字段（`genres`、`director`、`starring`、`cast`）的决策明细 `field_selections jsonb`。
     - 在第 6 步 `finalize_version` 中，生成 `conflict_version_id`（如 `cv0001`，记录其绑定的 `edition_stage_key`、`scalar_stage_key`、`multivalue_stage_key`），**完全从上述三张数据库阶段结果表读取并物化写入** `task7.movie_resolved`、`task7.edition_resolved`、`task7.movie_field_selection` 与 `task7.edition_field_selection`，不依赖任何进程内存态；因此无论中途哪一批崩溃重启（AC12）还是跨版本复用旧阶段（AC11），都能完整、无损地生成全量 8,659 部作品与 9,590 个商品记录。

2. **候选表（`task7.field_candidate`）**
   - 主键：`candidate_id bigint GENERATED ALWAYS AS IDENTITY`，唯一键：`(extraction_id, stage_key, entity_type, entity_id, field_name, source_kind, source_key)`。
   - 列：`entity_type`（`movie` / `product_edition`）、`entity_id`（`movie_id` 或 `asin`）、`field_name`、`source_kind`（`imdb_t4_link`、`imdb_t5_hint`、`imdb_t6_cooccur`、`page_byline`、`page_detail_bullets`、`page_product_overview`、`page_product_details`、`page_title`、`page_breadcrumb`、`page_format_swatch`、`task6_candidate`）、`source_key`、`raw_value`、`normalized_value`、`value_precision`、`source_reliability numeric(4,2)`、`source_acquired_at timestamptz`、`source_ref jsonb`（含 ASIN、section、ordinal、assertion_id、`source_record_id` 或 IMDb 文件名与行号）、`candidate_status`（`adopted`/`rejected`/`conflict_pending`/`superseded_by_specific`）、`status_reason text`。

3. **选择决策表（`task7.movie_field_selection` 与 `task7.edition_field_selection`）**
   - `task7.movie_field_selection` 主键：`(conflict_version_id, movie_id, field_name)`，其中作品层 9 个字段名统一为单数 `'director'`：`field_name IN ('title', 'original_title', 'release_year', 'runtime_minutes', 'mpaa_rating', 'genres', 'director', 'starring', 'cast')`。
   - `task7.edition_field_selection` 主键：`(conflict_version_id, asin, field_name)`，其中 `field_name IN ('edition_title', 'runtime_minutes', 'mpaa_rating', 'publisher', 'product_release_date')`。
   - 列：`status`（`resolved`/`conflict_unresolved`/`missing`）、`winning_value text`（多值字段存排序后的 JSON 数组摘要，标量字段存胜出值；`status <> 'resolved'` 时为 `NULL`）、`selected_candidate_ids bigint[]`、`considered_candidate_ids bigint[]`、`conflict_type text`、`selection_rule text`、`reason text`、`confidence numeric(4,2)`、`disagreement_detail jsonb`。
   - **CHECK 约束与标量/多值边界**：
     - 通用约束：`(status = 'resolved') = (winning_value IS NOT NULL)`；`status <> 'resolved'` 时 `cardinality(selected_candidate_ids) = 0`。
     - 标量字段（`field_name IN ('title', 'original_title', 'release_year', 'runtime_minutes', 'mpaa_rating')` 及版本层 5 个字段）：候选行存于 `task7.field_candidate`。仅当完全无候选（`cardinality(considered_candidate_ids) = 0`）时 `status = 'missing'`；当存在候选但因冲突或错配熔断未能选出胜出值时（如任务 6 为 `title_years_disagree` 且有 2 个不同的 `amazon_title_year` 候选，或弱匹配触发 `cross_source_mismatch_halt` 且拒绝 IMDb 唯一 `original_title` 候选），`cardinality(considered_candidate_ids) >= 1` 且 `cardinality(selected_candidate_ids) = 0`（对 `title_years_disagree` 等多候选冲突标 `status = 'conflict_unresolved'`；对唯一候选因跨字段错配熔断被标 `rejected` 的 `original_title` 允许 `status = 'missing'` 且保留该被拒候选在 `considered_candidate_ids` 中备查）；当 `status = 'resolved'` 时 `cardinality(selected_candidate_ids) >= 1` 且 `selected_candidate_ids <@ considered_candidate_ids`。
     - 多值字段（`field_name IN ('genres', 'director', 'starring', 'cast')`）：明细行存于 `task7.movie_genre` 与 `task7.movie_person_resolved`，故 `selected_candidate_ids` 与 `considered_candidate_ids` 均为空数组 `'{}'`；只要该字段有至少 1 个 `status = 'adopted'` 的成员（含 R21 `director_source_disagreement` 下 IMDb 导演标 `adopted`、页面未关联导演标 `conflict_pending` 的情形），`movie_field_selection.status` 即为 `'resolved'`（并在 `conflict_type` 中标 `'director_source_disagreement'`）。

4. **作品与版本汇总目标表（`task7.movie_resolved` 与 `task7.edition_resolved`）**
   - `task7.movie_resolved` 主键：`(conflict_version_id, movie_id)`：直接提供查询友好的宽表列（`title`、`title_status`、`original_title`、`original_title_status`、`release_year`、`release_year_status`、`inferred_bound_time`、`inferred_bound_precision`、`runtime_minutes`、`runtime_status`、`mpaa_rating`、`mpaa_status`、`genres text[]`、`director_count`、`starring_count`、`cast_count`、`effective_tconst`、`tconst_source`、`cross_source_mismatch_halt boolean`）。
   - `task7.edition_resolved` 主键：`(conflict_version_id, asin)`：提供版本层完整消解后属性（`movie_ids text[]`、`product_kind`、`edition_title`、`edition_title_norm`、`runtime_minutes`、`mpaa_rating`、`publisher`、`publisher_status`、`media_release_date`、`listing_first_available_date`、标量 `discs integer` 及 7 项多值特征数组 `media_formats text[]`、`languages text[]`、`subtitles text[]`、`aspect_ratios text[]`、`regions text[]`、`cut_types text[]`、`video_systems text[]` 与 `attributes_detail jsonb`）。

## 各字段冲突分类与消解算法

### 1. 跨字段弱匹配错配熔断（Cross-Field Mismatch Halt，R04、R11、R13）

任务 6 盲测评价在 `E3`（`imdb_t5_hint`）中发现了 2 例异片错配（因同名人物参演了同标题键的另一部电影）。为了防止弱匹配（`imdb_t5_hint` 或 `imdb_t6_cooccur`）把错误 IMDb 作品的年份、片长、类型和演职员灌入当前作品，任务 7 在作品层消解前先执行**跨字段一致性校验**：
- 对 `tconst_source IN ('imdb_t5_hint', 'imdb_t6_cooccur')` 的作品，检查以下三项强矛盾信号：
  1. **年份与上界严重矛盾**：IMDb `start_year` 与页面标题年份（`amazon_title_year`）相差 ≥2 年，**或**任务 6 上界评估证据早于 IMDb `start_year` ≥2 年——此处不仅读取 `task6.inferred_release_bound`，还**显式关联查询 `task6.bound_evidence`（`failed_checks` 含 `'B4'` 或 `'M4'`，即因早于采用上映年而在任务 6 被拦下的评论时间或介质发售日证据）与 `task6.quality_issue(issue_code = 'bound_before_release_year')`（正式库共 62 部）**，只要该作品最早有效评论年份或 `edition` 介质发售年份 $\le \text{imdb\_start\_year} - 2$，即触发信号 1；
  2. **片长严重矛盾**：该作品 `relation_type = 'edition'` 商品存在 `runtime_min` 且无剪辑版/多碟花絮标记，但与 IMDb `runtime_minutes` 相差 >15 分钟；
  3. **导演完全不重合**：页面有导演署名且 IMDb 有导演，但页面导演（含分号拆分后）与 IMDb 导演无一人重合。
- **熔断条件与各字段降级行为**：当弱匹配作品同时触发“信号 1 + 信号 2”或“信号 1 + 信号 3”或“信号 2 + 信号 3”（至少两项独立字段严重矛盾）时，判定 `cross_source_mismatch_halt = true`，持久化写入 `task7.movie_scalar_stage_result`，并写质量问题 `weak_link_cross_field_conflict`。此时：
  - 不将该 `tconst` 的 IMDb 属性作为自动压倒页面的权威值；
  - `title` 与 `original_title`：`original_title` 拒绝错配 IMDb 候选并置为 `status = 'missing'`（`winning_value = NULL`，被拒候选保留在 `considered_candidate_ids`）；`title` 不采用错配 IMDb `primary_title`——若页面标题键与该错配 IMDb 标题键不一致则标为 `status = 'conflict_unresolved'`（`conflict_type = 'cross_source_mismatch_halt'`），若页面标题键一致则退回按 R10 页面清洁标题规则消解；
  - `release_year` 与 `runtime_minutes`：跨源冲突的标量字段状态置为 `conflict_unresolved`（候选双方标 `conflict_pending`），保留两边的原始候选供人工复核；
  - `genres` 与演职员（`director`/`cast`）：在 `movie_multivalue_resolve` 阶段读取 `cross_source_mismatch_halt = true` 后，不并入该错配 `tconst` 的 IMDb 类型与演职员。

### 2. 187 部仅经 `contains` 出现的合集成员作品的候选归属边界（R02、R03、R09–R10、R13–R18）

在 `src/etl_task4/editions.py` 中，合集商品（`product_kind = 'collection'`）拆出的成员在 `task4.field_assertion` 中的 `entity_type = 'collection_member'`、`entity_id = '<ASIN>#<member_ordinal>'`（存放单部成员标题、括号年份与单部修饰词），而 `entity_type = 'product_edition'`、`entity_id = '<ASIN>'` 存放的是整个合集盒子的总标题（如 `"4 Movie Marathon: ..."`）、合集多片总时长（如 400 分钟）与合集整体分级。为严防合集盒子属性污染单部作品，候选收集严格按 `task4.product_movie.relation_type` 分流：

| 作品层字段 | `relation_type = 'edition'`（`member_ordinal = 0`） | `relation_type = 'contains'`（`member_ordinal > 0`） |
|---|---|---|
| **`title` 与 `title_year`** | 读取 `task4.field_assertion` 中 `entity_type = 'product_edition' AND entity_id = asin` 的 `title` / `title_year` | **严格读取 `entity_type = 'collection_member' AND entity_id = asin || '#' || member_ordinal`** 的成员 `title` / `title_year`（严禁读取 `entity_id = asin` 的合集盒子总标题） |
| **`runtime_minutes`** | 读取 `entity_type = 'product_edition' AND entity_id = asin` 的 `runtime_min` | **不读取页面合集总时长**（合集盒子总时长仅归属合集 `ProductEdition` 自身；若该成员作品无独立 `edition` 商品且无 IMDb 片长，作品层 `runtime_minutes` 保持 `missing`） |
| **`mpaa_rating`** | 读取 `task3.page_field` 中 `asin` 的 `field_key = 'mpaa_rating'` | **不读取合集盒子整体分级**（合集外盒分级可能是最严成员分级或 `NR`，不直接下推给单个成员作品） |
| **`genres`（页面部分）** | 读取 `edition` 商品的 `genre` 断言与 `breadcrumb` | **不将合集盒子混合类型下推给单个成员**（仅当该成员关联了 IMDb `tconst` 或有独立 `edition` 商品时获得对应类型） |

### 3. 作品标题与原名（`title` / `original_title`，R09–R10）

- **候选收集**：
  - IMDb 候选：`imdb.title.primary_title`（作为 `title` 候选）、`imdb.title.original_title`（作为 `original_title` 候选）；同时读取 `task4.imdb_title_key`（`primary`、`original`、`aka`）用于判断页面标题是否为已知别名。
  - 页面候选：按第 2 节边界读取该作品所有 `edition` 商品（`entity_id = asin`）及 `contains` 成员（`entity_id = asin || '#' || member_ordinal`）的 `task4.field_assertion(field_name='title')`。通过清洁函数 `clean_page_display_title(raw_title)` 剥离末尾的 `[VHS]`、`[DVD]`、`[Blu-ray]`、`(Widescreen Edition)`、`(Full Screen)`、`(Collector's Edition)`、`(Bilingual)` 及已提取为 `title_year` 的括号年份，保留作品核心名称与卷号/副标题。
- **消解规则**：
  - **`title`**：
    1. 有可靠 IMDb `primary_title`（未触发错配熔断）时：采用 `imdb.title.primary_title`（`status = 'resolved'`）。若页面清洁标题与 `primary_title` 完全一致，冲突类型为 `none`；若页面标题键命中 `original`/`aka` 或仅差载体后缀，页面候选标为 `adopted`（同义支撑）或 `superseded_by_specific`，冲突类型记 `formatting_or_alias`；若某商品标题键完全不在 IMDb 标题键中（如合集捆绑标题残余），该商品候选标 `rejected`。
    2. 无 IMDb（或错配熔断后页面标题键一致回退）时：比较各商品/成员的规范化标题键（`normalized_value`）。若全部一致，或一方为另一方加副标题/卷号（如 `"The Kid"` 与 `"Charlie Chaplin's The Kid"`），采用出现频次最高（平票取字典序最小 `asin`）的清洁标题（`status = 'resolved'`，多写法时记 `formatting_or_alias`）；若多商品标题键实质不同且无包含关系，标 `status = 'conflict_unresolved'`（`winning_value = NULL`，候选标 `conflict_pending`）。
  - **`original_title`**：有可靠 IMDb `original_title`（未触发错配熔断）时采用该值（`status = 'resolved'`，若与 `primary_title` 不同则在 `disagreement_detail` 标记 `distinct_from_primary: true`，正式库共 871 部）；无 IMDb 时记 `status = 'missing'`；触发错配熔断时拒绝错配 IMDb `original_title` 候选并记 `status = 'missing'`。

### 4. 上映年份（`release_year`，R11–R12）

- 读取 `task6.release_candidate` 与 `task6.release_selection`（`rv0001`）以及 `task6.inferred_release_bound`、`task6.bound_evidence`、`task6.quality_issue`：
  1. 若触发了跨字段错配熔断（`cross_source_mismatch_halt = true`），或存在 `year_disagreement`（差 ≥2 年）且属弱匹配无法佐证，状态置为 `conflict_unresolved`（`winning_value = NULL`），冲突候选标 `conflict_pending`。
  2. 若无 IMDb 候选但页面存在多个不一致的 `amazon_title_year` 候选（任务 6 中 `missing_reason = 'title_years_disagree'`），在任务 7 中保留该 2+ 个页面年份候选于 `considered_candidate_ids`（标 `conflict_pending`），并将 `movie_field_selection(field_name='release_year')` 的状态标为 **`conflict_unresolved`**（而非 `missing`）。
  3. 若属 `imdb_t4_link` 强关联，即使与页面某重映商品标题年份相差 ≥2 年（或存在 `bound_before_release_year`），只要导演/片长支持该经典作品（如老片重映版或早期评论混入），采用 IMDb 年份，冲突类型记 `cross_source_override` 并保留质量问题记录。
  4. 候选间相差 1 年（`year_diff_1`，正式库 8 部）采用优先级高的 IMDb 候选，冲突类型记 `minor_tolerance`。
  5. 单一来源或完全一致记 `none`；完全无候选（如任务 6 `no_candidate`、`cooccur_ambiguous`、`hint_ambiguous`）记 `missing`（保留 `inferred_release_bound` 于独立列，绝不混入 `release_year`）。

### 5. 片长（`runtime_minutes`，R13–R14、R23）

- **版本层（`edition_resolved.runtime_minutes`）**：每个 ASIN 取其页面 `runtime_min` 断言（正式库 8,536 个 ASIN 有值，单 ASIN 内 `detail_bullets` 与 `product_overview` 0 冲突；若测试夹具中同 ASIN 多区域冲突，按 `detail_bullets` > `product_overview` > `product_details` 择优并记 `intra_asin_runtime_conflict`）。
- **作品层（`movie_resolved.runtime_minutes`，仅聚合 `relation_type = 'edition'` 商品页面片长）**：
  1. **有可靠 IMDb `runtime_minutes` 且未触发错配熔断**（正式库 5,363 部：双方共有 4,883 部 + 仅 IMDb 480 部）：作品层标准片长采用 IMDb `runtime_minutes`（`status = 'resolved'`）。对每个关联 `edition` 商品的版本片长 $R_{\text{page}}$ 与 $R_{\text{imdb}}$ 计算差值 $\Delta = |R_{\text{page}} - R_{\text{imdb}}|$ 并分类：
     - $\Delta = 0$：`none`（完全一致，正式库 2,880 部）；
     - $1 \le \Delta \le 2$：`minor_tolerance`（四舍五入或片头发行商片标微差，正式库 864 部）；
     - $\Delta > 2$ 且 $|R_{\text{page}} - \text{round}(R_{\text{imdb}} \times 24 / 25)| \le 1$：`framerate_or_edition`（PAL 25fps 与 NTSC 24fps 的 4% 帧率转换差，正式库 251 对）；
     - 其余 $3 \le \Delta \le 5$：`framerate_or_edition`（片尾字幕或版本微差，正式库 264 对）；
     - $\Delta > 5$ 且商品带 `edition.cut_type`（导演剪辑版/加长版/未分级版）或（`discs >= 2` 且 $R_{\text{page}} \ge 1.5 \times R_{\text{imdb}}$，即多碟含花絮总时长）：`framerate_or_edition`（合法版本变体，正式库 45 对）；
     - 其余 $6 \le \Delta \le 15$：`cross_source_override`（以 IMDb 院线标准片长为准，页面版本片长保留在版本层，正式库 371 对）；
     - 其余 $\Delta > 15$：写质量问题 `severe_runtime_conflict`（正式库 248 对）；若属弱匹配且伴随年份/导演矛盾则由熔断机制置为 `conflict_unresolved`。
  2. **无 IMDb `runtime_minutes` 但 `edition` 页面有片长**（正式库 2,768 部）：
     - 单版本作品或各版本片长相同：直接采用（`resolved`，`none`）；
     - 多版本作品且跨版本极差 $\max(R_{\text{page}}) - \min(R_{\text{page}}) \le 5$：取众数（平票取较小中位数），记 `minor_tolerance`；
     - 多版本作品且跨版本极差 $> 5$：若部分版本显式标有 `cut_type`（如 Extended/Director's Cut）或多碟合集，而另一部分无特殊剪辑标记，采用无特殊剪辑标记版本的众数（记 `framerate_or_edition`）；若均无剪辑标记且极差 $> 5$，无法判断哪个是标准片长，作品层标 `conflict_unresolved`（`winning_value = NULL`，各版本在版本层保留各自时长）。

### 6. 内容分级（`mpaa_rating`，R15–R16、R23）

- **上游读取路径**：由于 `task4.field_assertion` 未单独生成 `mpaa_rating` 行，任务 7 直接从 `task3.page_field` 读取 `field_key = 'mpaa_rating'`（涵盖 `section = 'byline'` 与 `section = 'detail_bullets'` 等区域，并关联 `task4.source_record` 获取采集时间与源记录 ID）。
- **受控分级词表与规范化**：
  - 具体分级（6 档）：`G`、`PG`、`PG-13`、`R`、`NC-17`、`X`；
  - 未评级占位（1 档）：`NR/Unrated`（匹配 `NR`、`Not Rated`、`Unrated`、`NR (Not Rated)`、`Unrated (Not Rated)`）；
  - 其他无法识别的值：不产生有效分级候选，写质量问题 `unknown_mpaa_rating`。
- **版本层消解（按 ASIN）**：同一 ASIN 的 `byline`（如 `PG`）与 `detail_bullets`（如 `PG (Parental Guidance Suggested)`）规范化后相同 → 采用该分级码（若原始字符串不同记 `formatting_or_alias`）；若同一 ASIN 内规范化分级码不同，优先采用 `detail_bullets` 并记 `intra_edition_rating_conflict`。
- **作品层消解（按 `movie_id`，仅汇总 `relation_type = 'edition'` 商品的版本分级）**：
  1. 全部 `edition` 商品规范化分级码一致（共 6,971 部）：采用该分级（`resolved`，`none`）；
  2. 仅存在**唯一一种具体分级**与 `NR/Unrated` 并存（正式库 **26 部**，如 `Casablanca` 的 `PG` 与 `NR/Unrated`、`Kill Bill: Vol. 2` 的 `R` 与 `NR/Unrated`）：因为经典老片或重发版/未删减版 DVD 常标 `NR`/`Unrated`，而院线版有明确 MPAA 分级，作品层采用该具体分级，`NR/Unrated` 候选标为 `superseded_by_specific`，冲突类型记 `unrated_vs_rated`；
  3. 存在**两种及以上不同的具体分级**（正式库 **5 部**：`Phenomenon` G vs PG、`Veer Zaara` G vs PG、`Forbidden Zone` R vs NC-17、`Hard Target` R vs G、`The Five Heartbeats` R vs PG）：由于 IMDb 基础表不含 MPAA 分级，仅凭页面无法可靠裁决哪一个是院线原始分级、哪一个是重审或电视剪辑版分级，作品层 `mpaa_rating` 状态严格置为 **`conflict_unresolved`**（`winning_value = NULL`，冲突候选标 `conflict_pending`，冲突类型记 `intra_source_tie_unresolved`），各版本在 `edition_resolved` 中保留各自分级。

### 7. 电影类型受控词表映射与并集（`genres`，R17–R18）

- **30 词受控词表**：
  - IMDb 25 个标准词：`Action`, `Adult`, `Adventure`, `Animation`, `Biography`, `Comedy`, `Crime`, `Documentary`, `Drama`, `Family`, `Fantasy`, `Film-Noir`, `History`, `Horror`, `Music`, `Musical`, `Mystery`, `News`, `Romance`, `Sci-Fi`, `Short`, `Sport`, `Thriller`, `War`, `Western`。
  - Amazon 页面扩展受控词（5 个）：`Anime`, `Foreign`, `LGBT`, `Performing Arts`, `Silent`。
- **Amazon 页面词面映射表（`GENRE_MAP`）**：
  - 单一映射：`drama → [Drama]`, `comedy → [Comedy]`, `horror → [Horror]`, `romance → [Romance]`, `documentary → [Documentary]`, `action → [Action]`, `westerns / western → [Western]`, `animation → [Animation]`, `science fiction / sci-fi → [Sci-Fi]`, `musicals / musical → [Musical]`, `thrillers / thriller → [Thriller]`, `crime → [Crime]`, `fantasy → [Fantasy]`, `adventure → [Adventure]`, `kids & family / children / family → [Family]`, `military & war / war → [War]`, `foreign / foreign films → [Foreign]`, `lgbt → [LGBT]`, `silent / silent films → [Silent]`, `biography → [Biography]`, `history → [History]`, `sports / sport → [Sport]`, `music / classical / international music / indie & alternative → [Music]`。
  - 复合词拆分映射：`action & adventure → [Action, Adventure]`, `mystery & thrillers / mystery & suspense → [Mystery, Thriller]`, `science fiction & fantasy → [Sci-Fi, Fantasy]`, `anime & manga / anime → [Animation, Anime]`, `musicals & performing arts / performing arts → [Musical, Performing Arts]`。
  - 显式噪声过滤（`GENRE_NOISE`）：`movies & tv`, `featured categories`, `dvd`, `blu-ray`, `dvd movie`, `blu-ray movie`, `cds & vinyl`, `classic`, `feature`, `europe`, `art house & international`（记录在过滤统计中，不进入 `movie_genre`）。
- **并集与来源标记**：对每部作品取 IMDb `title.genres`（未熔断时）与关联 `edition` 商品页面映射后受控词的并集，按字母序排列写入 `task7.movie_resolved.genres`，并在 `task7.movie_genre` 中逐词记录 `source_coverage`（`both` / `imdb_only` / `page_only`）及原始断言/页面字段 ID。

### 8. 演职员并集、角色边界、分号拆分与署名顺序消解（`movie_person_resolved`，R19–R22）

- **角色边界硬约束**：
  - `director`：`task5.movie_person(role='director')` ∪ `imdb.title_director`（未熔断时）；
  - `starring`：**仅限** `task5.movie_person(role='starring')`（来自 Amazon 页面 `byline` 署名行 Actor），绝不从 IMDb `title_principal` 生成 `starring`；
  - `cast`：`task5.movie_person(role='cast')` ∪ `imdb.title_principal(category IN ('actor','actress'))`（未熔断时）。
- **分号 `;` 与 `and` 多人串补救匹配（R21）**：
  - 针对交接文档指出的 Amazon `detail_bullets` 用分号 `;` 连接多位导演导致任务 5 未关联 `nconst` 的情况（如 `B000NDIB4G`：`"Tomas Guiterrez Alea; Carlos Garcia Agraz; and Jose Luis Garcia Agraz"`），当作品有对应 `tconst` 且存在 `page_only` 未关联 `nconst` 的导演/演员时，按 `;` 及 `; and ` 拆分子串并计算 `strict_name`：若子串严格键命中该作品 IMDb 演职员的 `strict_name`，则将其合并到该 IMDb 演职员的 `person_id` 下（标记 `semicolon_split_matched`），不再重复保留未拆分的脏串行。
- **导演跨源不一致检测（R21）**：
  - 若经分号拆分后，作品仍同时存在 `page_only`（无 `nconst`）的页面导演与 `imdb_only` 的 IMDb 导演，保留双方行：IMDb 导演标 `status = 'adopted'`，页面未关联导演标 `status = 'conflict_pending'`；在 `movie_field_selection(field_name='director')` 中，由于已有已确认的 `adopted` 导演，字段汇总 `status = 'resolved'`、`winning_value` 为已采用导演列表 JSON，但 `conflict_type` 显式标为 `'director_source_disagreement'`（`disagreement_detail` 记录待确认页面导演列表），并写质量问题 `director_source_disagreement`（正式库 `verified` 作品中预计约 70 余部）。
- **IMDb 独有人物的稳定 `person_id`（R20）**：
  - 若 IMDb 演职员的 `nconst` 已在 `task5.person_external_id` 中存在，直接复用其 `task5.person.person_id`；
  - 若不在 `task5` 中（即页面未出现过的 IMDb 独有导演或主要演员），按任务 5 完全相同的哈希公式 `person_id = "ps_" + sha256(f"imdb:{nconst}")[:16]` 写入 `task7.imdb_person_ext`（若 `task5.person_identity` 已有该锚点则直接取用），保证全库 `person_id` 命名空间统一且无需改动 `task5` 表。
- **署名顺序（`credit_order`）冲突消解（R22）**：
  - 对页面演职员（`task5.movie_person`），若 `order_conflict = false`，直接继承 `credit_order`；
  - 若 `order_conflict = true`（正式库共 117 项），读取 `task5.movie_person_source` 中各商品版本的 `byline_rank`：取出现商品数最多的序号（众数）；若票数并列，取页面采集时间 `fetched_at` 较晚商品的序号（再并列取最小序号），并在 `resolution_note` 中记录各 ASIN 的原始序号分布与 `billing_conflict_resolved`；
  - 对 `imdb_only` 的补充导演或演员，保留 `imdb_ordering`（`title_director.ordinal` 或 `title_principal.ordering`），其 `credit_order` 排在同作品同角色页面有署名顺序的人员之后（即 `max(page_credit_order) + dense_rank(imdb_ordering)`）。

### 9. 版本层内部消解与上游源表映射（`ProductEdition`，R23–R24）

- **版本层各字段的上游提取与归并路径**：
  - `edition_title`：读取 `task4.field_assertion(entity_type='product_edition', field_name='title')`；
  - `runtime_minutes`：读取 `task4.field_assertion(entity_type='product_edition', field_name='runtime_min')`；
  - `mpaa_rating`：读取 `task3.page_field(field_key='mpaa_rating')`（规范化为 6 档具体分级 + `NR/Unrated`，同 ASIN 内 `detail_bullets` > `byline`）；
  - `publisher`（工作室/发行商）：读取 `task4.field_assertion(field_name='edition.publisher')`（正式库 8,935 个 ASIN 有值，其中 **98 个 ASIN** 在同一页面 `detail_bullets`、`product_overview` 或 `product_details` 有多个不同规范值）。消解时先比较去常见后缀（`Home Video`、`Home Entertainment`、`Pictures`、`Studios`、`Entertainment`、`Inc.`、`Corp.`）后的核心词根：若词根相同或存在前缀包含（如 `"Warner Home Video"` 与 `"Warner Bros."`），选来自 `detail_bullets` 且字面信息更完整的值（冲突类型 `formatting_or_alias`）；若词根完全不同（如发行代理商与制作厂牌并列），主值取 `detail_bullets`（主详情区），其余候选标 `rejected` 并记 `intra_asin_publisher_conflict`；
  - `product_release_date`（商品发售日 vs 上架日）：读取 `task6.product_release_date`，每个 ASIN 在 `edition_resolved` 中同时提供 `media_release_date`（实体发售日）与 `listing_first_available_date`（上架日）；版本层主发售日字段 `product_release_date` 优先采用 `media_release_date`（无 `media_release_date` 时回退 `listing_first_available_date` 并注明 `fallback_to_listing_date`）；当同一 ASIN 同时存在两类日期且不同时（正式库 1,507 个 ASIN），记 `semantic_distinction`；
  - **标量 `discs integer`**：优先读取 `task4.field_assertion(field_name='edition.discs')` 解析出的正整数（同 ASIN 出现多个值时优先 `detail_bullets` > `product_overview` > `product_details`）；若无 `edition.discs` 断言，则从 `edition.packaging` 断言（如 `"2-Disc Set"`、`"3 Disc Collector's Edition"`）正则提取 `(\d+)\s*-?\s*discs?`；原始字符串与多区域差异保存在 `attributes_detail->'discs'` 中；
  - **7 项多值版本特征（`text[]`）**：
    1. `media_formats`：合并 `task4.field_assertion(field_name='edition.media_format')` 与 `task3.page_field(section='format_swatch', field_key='media_format')`（规范化为 `DVD`、`VHS`、`Blu-ray`、`LaserDisc`、`UMD`、`HD DVD`、`VCD` 等受控载体词）；
    2. `languages`：合并 `task4.field_assertion` 中 `field_name IN ('edition.language', 'edition.dubbed')`；
    3. `subtitles`：读取 `task4.field_assertion(field_name='edition.subtitles')`；
    4. `aspect_ratios`：合并 `task4.field_assertion(field_name='edition.aspect_ratio')`（如 `1.85:1`、`16:9`）以及从 `task4.field_assertion(field_name='edition.packaging')` 与 `task3.page_field(field_key='media_format')` 原始文本中提取的画幅词（`Widescreen`、`Full Screen`、`Anamorphic`、`Letterboxed`、`Pan & Scan`）；
    5. `regions`：读取 `task4.field_assertion(field_name='edition.region')`；
    6. `cut_types`：读取 `task4.field_assertion(field_name='edition.cut_type')`；
    7. `video_systems`：读取 `task4.field_assertion(field_name='edition.video_system')`。

## Airflow DAG 编排与幂等恢复（R27–R30）

**DAG `amazon_movies_task7_conflict`**：
`check_inputs → edition_resolve → movie_scalar_resolve → movie_multivalue_resolve → shared_tconst_audit → finalize_version → verify → finalize_report → gate`

1. **`check_inputs`**：校验抽取 ID、任务 4 `map0002`、任务 5 `pmap0001`、任务 6 `rv0001` 与 IMDb 快照 `imdb_2026-10-29` 均存在且状态成功；检查并幂等应用迁移 `601`；获取抽取级 PostgreSQL 咨询锁；若相同 `input_key` 已有 `succeeded` 运行，则登记一条 `reused` 运行并跳过重算。
2. **`edition_resolve`**：按 ASIN 分批（默认 1,000 个/批）处理 9,590 个参与匹配商品，写入版本层候选（`task7.field_candidate`）与版本阶段决策（`task7.edition_stage_result`），每批同事务写入 `task7.entity_stage(stage='edition_resolve')` 与 `task7.run_batch`。
3. **`movie_scalar_resolve`**：按 `movie_id` 分批（默认 1,000 部/批）处理 8,659 部作品的标量字段候选与跨字段错配熔断，写入 `task7.field_candidate` 与 `task7.movie_scalar_stage_result`（持久化 `cross_source_mismatch_halt` 及 5 项标量决策），同事务记录 `entity_stage(stage='movie_scalar')`。
4. **`movie_multivalue_resolve`**：按 `movie_id` 分批（默认 1,000 部/批），从 `task7.movie_scalar_stage_result` 读取 `cross_source_mismatch_halt` 后处理作品层 `genres`（`task7.movie_genre`）与演职员（`task7.movie_person_resolved`，含 `imdb_person_ext` 登记、分号拆分与署名顺序冲突消解），并写入 `task7.movie_multivalue_stage_result`，同事务记录 `entity_stage(stage='movie_multivalue')`。
5. **`shared_tconst_audit`**：审计任务 6 的 126 项 `shared_tconst_with_movie`，生成双作品对比记录。
6. **`finalize_version`**：在单事务中生成 `conflict_version`（`cv0001`…，绑定三个阶段的 `stage_key`），**纯粹从数据库已落库的阶段结果表（`edition_stage_result`、`movie_scalar_stage_result`、`movie_multivalue_stage_result`）物化写入** `task7.movie_resolved`、`task7.edition_resolved`、`task7.movie_field_selection`、`task7.edition_field_selection`、`task7.shared_tconst_conflict` 与版本汇总计数，将 `task7.run.status` 置为 `succeeded`。
7. **`verify`**：运行只读核对 `etl_task7.check`，校验行数、互斥状态、外键引用、无孤儿候选及任务 1–6 表零修改。
8. **`finalize_report`（`trigger_rule="all_done"`）**：若上游成功则导出 `summary.json`、`report.md`、`examples.md` 与 CSV 清单；若上游失败则写入 `_failures/<run_id>.json`。
9. **`gate`（`trigger_rule="all_done"`）**：上游任一节点失败时抛出 `AirflowFailException`（从 `airflow.sdk.exceptions` 优先导入，无弃用警告），确保 DAG 最终状态如实反映成败。

## 盲测评价设计与非循环 AI 预标注（R36–R39）

吸取任务 6 独立审查（`doc/task6/review.md`）的全部经验，任务 7 的评价流程从设计上强制执行以下四项防泄漏、防循环措施：

1. **先冻结开发池，后正式运行，严格排除关联商品**：
   - 在正式库首次运行前，先执行 `python -m etl_task7.evaluate freeze`，按 `md5(movie_id)` 排序将前 20% 的作品（**1,732 部**，与任务 6 开发池完全对齐）冻结在 `task7.dev_pool`（记录 `frozen_at` 时间戳，须早于正式运行 `started_at`）。
   - 所有评价样本严格从剩余 80% 评价池（**6,927 部作品**）中抽取；对于按 `asin` 抽样的版本层 `E7`，**只要某个 `asin` 在 `task4.product_movie`（无论 `edition` 还是 `contains`）关联的任一 `movie_id` 属于 `task7.dev_pool`，该 `asin` 即被严格排除**，杜绝合集或多关联商品跨池泄露。

2. **120 项七层分层抽样（`E1`–`E7`）**：

| 层号 | 目标字段与冲突场景 | 抽样粒度与 `target_field` 编码 | 样本数 | 主要检验目标 |
|---|---|---|---|---|
| **E1** | 作品层 `title` / `original_title`：跨源差异（IMDb `primary ≠ original` 或仅命中 `aka`/子集）或跨版本标题不同 | `movie_id` × `'title'` | 15 | 清洁标题抽取、原名分离与跨版本标题冲突裁决是否正确 |
| **E2** | 作品层 `release_year`：存在跨源年份候选分歧（`year_diff_1` / `year_disagreement`）或推断上界早于上映年（`bound_before_release_year`） | `movie_id` × `'release_year'` | 15 | 1 年容差、重映年覆盖与弱匹配错配熔断是否准确 |
| **E3** | 作品层 `runtime_minutes`：跨源或跨版本片长不一致（覆盖 1–2 分钟微差、PAL 帧率差、剪辑版/多碟与 >15 分钟严重差异） | `movie_id` × `'runtime_minutes'` | 20 | 标准片长选择、帧率/剪辑版识别与无 IMDb 跨版冲突保留是否合理 |
| **E4** | 作品层演职员：含导演跨源补充/未关联页面导演冲突/分号多人串（12 项，编码为 `'director'`）与跨版本署名顺序冲突（8 项，编码为 `'billing:<role>:<person_id>'`） | `movie_id` × (`'director'` 或 `'billing:<role>:<person_id>'`) | 20 | 可信并集、主演边界保护、分号拆分与署名顺序众数消解是否正确 |
| **E5** | 作品层 `genres`：同时具有 IMDb 类型与 Amazon 页面 `genre`/`breadcrumb` 复合词或噪声词的作品 | `movie_id` × `'genres'` | 15 | 受控词表映射、噪声词过滤与多源并集是否准确 |
| **E6** | 作品层 `mpaa_rating`：跨版本存在 `NR/Unrated` vs 具体分级或两种不同具体分级冲突，余量补多区域分级 | `movie_id` × `'mpaa_rating'` | 15 | 具体分级胜过 `NR` 及双具体分级保留 `conflict_unresolved` 是否准确 |
| **E7** | 版本层 `publisher` / `product_release_date`：排除关联任何 `dev_pool` 作品的 ASIN 后，单 ASIN 内发行商写法冲突（10 项，`'publisher'`）或同时存在 `media_release` 与 `listing_first_available` 不同（10 项，`'product_release_date'`） | `asin` × (`'publisher'` 或 `'product_release_date'`) | 20 | 版本层内部区域优先级与发售日语义区分是否准确 |

   - 若某层在评价池中数量不足配额，按 `E3 → E4 → E1 → E7` 顺序从余量充足的冲突层补足，实际抽样分布记入 `task7.eval_set.strata`。

3. **盲测导出、全局 MD5 打乱与多值字段比对规范**：
   - 抽样得到 120 个评价项后，**不按 `E1 → E7` 顺序编号**，而是按 `md5(entity_id || ':' || target_field)` 全局确定性排序后再分配 `t7_eval_0001`–`t7_eval_0120`。
   - 导出的 `eval_items.csv` 只包含：`item_id`、`entity_type`（`movie`/`edition`）、`entity_id`、`target_field`、`entity_context`（作品标题/ASIN 链接/关联 `tconst` 链接）、`raw_candidates_json`（各来源的原始候选值、标签与来源说明），以及待填空列 `human_action`（`resolve` / `keep_conflict` / `unsure`）、`human_winning_value`、`note`。绝不导出系统的 `status`、`winning_value`、`conflict_type` 或 `stratum`。
   - **标量与多值字段的 `human_winning_value` 比对规范**：
     - 标量字段（`title`、`release_year`、`runtime_minutes`、`mpaa_rating`、`publisher`、`product_release_date` 及 `billing:<role>:<person_id>` 的胜出序号）：去首尾空白后精确比对（年份与片长/序号按整数值比对，标题与发行商按大小写折叠并规范化空白比对）；
     - 多值字段（`E4` 的 `'director'` 与 `E5` 的 `'genres'`）：标注约定用 `|` 分隔多个成员（如导演 `"Joel Coen|Ethan Coen"`，类型 `"Action|Adventure|Sci-Fi"`）；计算消解值准确率时，将 `human_winning_value` 与系统已采用（`status = 'adopted'`）成员集合分别按 `|` 切分并规范化（导演名按 `strict_name` 规范化，类型词按受控词表标准大小写规范化）后作**无序集合等价比较**（`set(norm_human) == set(norm_system)`），避免因姓名排列顺序或连字符微差产生误判。

4. **非循环 AI 预标注草稿（防自证循环）**：
   - `_generate_ai_draft` 函数以只读方式接收 `eval_items.csv` 的公开行与原始 `imdb`/`task3` 证据，**在代码层面禁止查询或传入 `task7.movie_field_selection`、`task7.edition_field_selection`、`task7.movie_resolved`、`task7.edition_resolved`、`task7.edition_stage_result`、`task7.movie_scalar_stage_result`、`task7.movie_multivalue_stage_result` 的任何系统判定结果**。
   - AI 草稿独立比对各来源原始证据后写入 `eval_items_draft.csv`（含 `ai_action`、`ai_winning_value`、`ai_doubt`、`ai_reason`），由标注人付林轩逐条复核修改后作为正式标注文件导入。

## 资源预算与估算

| 项目 | 规划估计 | 依据说明 |
|---|---|---|
| `task7.field_candidate` 行数 | 约 18 万–22 万行 | 8,659 部作品 × 5 个标量字段多源候选 + 9,590 个商品 × 版本层标量候选 |
| `task7.movie_field_selection` 行数 | 77,931 行 | 8,659 部作品 × 9 个作品层字段（5 标量 + 4 多值汇总） |
| `task7.edition_field_selection` 行数 | 47,950 行 | 9,590 个商品 × 5 个版本层标量字段 |
| `task7.movie_genre` 行数 | 约 1.8 万–2.2 万行 | 约 6,600 部有类型的作品 × 平均约 3 个受控类型词 |
| `task7.imdb_person_ext` 行数 | 约 1.8 万–2.2 万行 | IMDb 独有导演与主要演员去重后的 `nconst` 数 |
| `task7.movie_person_resolved` 行数 | 约 7.5 万–7.8 万行 | 任务 5 的 50,027 行 + IMDb 补充的约 400 条导演与约 2.5 万条 `cast` |
| 全量运行耗时 | 2 分钟以内 | 本地 PostgreSQL 批量查询与分批事务写入（1,000 实体/批），无外部网络 I/O |
| schema `task7` 磁盘占用 | 200 MB 以内 | 含候选 JSONB 证据与索引 |
| 二次同参重跑（`reused`） | 1 秒以内 | `input_key` 命中已成功运行，直接写 `reused` 审计行返回 |

## 异常与恢复处理

| 异常场景 | 处理行为 |
|---|---|
| 上游版本（`map0002`、`pmap0001`、`rv0001`、`imdb_2026-10-29`）不存在或未完成 | `check_inputs` 抛出确定性错误，不重试，下游标记 `upstream_failed`，门禁阻断 |
| 同一抽取已有另一个任务 7 进程持有咨询锁 | 抛出确定性 `RunBusy` 错误，不重试，防止并发写冲突 |
| 分批阶段（`edition_resolve` / `movie_scalar` / `movie_multivalue`）中途崩溃或注入故障 | 当前未提交批次自动回滚；重跑时根据 `task7.entity_stage` 跳过已提交批次，从断点继续处理，无重复无遗漏 |
| 全局收尾阶段（`finalize_version`）提交前中断 | 单事务整体回滚，重跑时复用已完成的三个分批阶段，直接重新执行 `finalize_version` |
| 数据库瞬时连接中断或死锁 | 识别为暂时错误（`is_transient`），Airflow 自动重试（最多 2 次） |
| 页面出现未知的 MPAA 分级字符串或未知日期标签 | 记入 `task7.quality_issue`（`unknown_mpaa_rating`），不崩溃、不污染受控词表 |
