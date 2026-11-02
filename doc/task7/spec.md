# 任务 7 规格（处理不同来源的信息冲突）

状态：2026-10-30 在分支 `task7-conflict-resolution`（从 `main` 的 `649ac06` 建立）完成正式库只读探查与计划编写，用户确认 H01–H05。2026-11-02 经计划复核后修订，用户同意按复核结论修订：错配熔断改为先评估关联（`link_assess`）、补年份决策表、增加同片署名对齐、逐版本片长差异、类型词面处置、别名与演员覆盖范围，长规则拆分并重新编号为 R01–R66、AC00–AC22。T7-01 至 T7-08 于 2026-11-02 实现并在正式库运行（`cv0001`），T7-09（标注导入与指标）待用户审核。2026-11-02 用户授权放宽姓名兼容（R16、R38，新增规则版本 `t7r2`，`t7r1` 保留）；`t7r2` 的 `LS` 双向，抽查发现页面拼接串会误对齐，同日改为方向性并新增 `t7r3`（`t7r2` 保留，`cv0002` 可复现），并确认 H09、开发池外未映射音乐子类作为已知局限、多人导演串按 R39 不变；正式库先后在 `t7r2`、`t7r3` 下重跑出 `cv0002`、`cv0003`（当前默认）。设计见 [design](design.md)，执行顺序见 [tasks](tasks.md)，完成情况见 [checklist](checklist.md)。

## 目标

为任务 4 映射版本 `map0002` 的 8,659 部作品与 9,590 个参与匹配商品建立可复核的多源、多版本字段冲突消解流程。区分作品层（`Movie`）与商品版本层（`ProductEdition`），先评估每部作品的 IMDb 关联是否可信，再综合来源权威性、字段语义、匹配可靠性、精度与采集时间消解字段。多值字段取可信并集，每个成员保留来源与角色；标量字段记录胜出值、规则、置信度和全部未采用候选；无法可靠决策时保留 `conflict_unresolved`，不编造答案，也不覆写源断言。最后用排除开发池后的 120 项七层盲测样本报告消解质量。

## 范围

- 包含：`map0002` 全部 8,659 部作品（含仅经 `contains` 出现的 187 部）的 9 个作品层字段；9,590 个参与匹配商品的版本层字段；IMDb 快照 `imdb_2026-10-29` 的 5,407 部关联作品的关联评估与跨源融合；Amazon 页面内部与跨商品版本的分歧处理；演职员可信并集、同片署名对齐、IMDb 独有人物的 `person_id` 登记、多人串处理与署名顺序消解；类型受控词表映射与词面处置；作品别名保存；共享 `tconst` 的逐对分类；消解版本、版本差异、分批恢复与幂等重跑；Airflow DAG `amazon_movies_task7_conflict`；统计报告、实例、只读查询；开发池冻结、盲测评价与人工审核导入。
- 不包含：修改任务 1–6 的源表与断言（`public`、`task2`–`task6`、`imdb` 全部只读）；联网抓取或重新请求页面；`match_scope <> 'matched'` 的商品；把版本物理差异当作品层错误；评论逻辑去重与合并评分（任务 8）；通用血缘引擎（任务 9）；用 AI 或黑盒模型决定胜出值。
- 约束：不修改任务 1–6 的 spec、表结构、数据与测试；新表全部建在 schema `task7`（迁移 `601`）；每次查询显式限定抽取、上游版本和阶段键；沿用 Python 3.12、PostgreSQL 18、Airflow 3.3.2、Docker Compose；测试只用合成夹具并连接 `etl_test`；IMDb 文件、运行导出与评价标注表不入 Git；导出不含评论正文与整页 HTML。
- 依据：[作业说明](../../../作业/数据存储与管理_ETL作业说明.docx)的任务 7、第五部分（字段定义、受控词表、主演判定、覆盖范围）、第八部分（`FieldAssertion`、`FieldSelection`、`QualityIssue`、可重复运行）、第十部分（覆盖率、字段冲突查询）；[任务 6 交接文档](../handoffs/2026-10-30-task6-to-task7.md)。

## 规则

“作业要求”来自任务书，“用户确认”来自规划确认，“工程默认”是本轮实现基线。分组：R01–R06 输入与实体边界；R07–R13 候选与通用状态；R14–R19 关联评估；R20–R33 作品层标量；R34–R43 作品层多值；R44–R49 版本层；R50–R57 运行与编排；R58–R60 报告与查询；R61–R66 盲测评价。

- R01 [作业要求]：输入限定为一个抽取、任务 4 映射版本、任务 5 人物映射版本、任务 6 上映版本和 IMDb 快照；默认为 `f1e8b845…`、`map0002`（解析 `t3p1`、规范化 `t4n1`）、`pmap0001`、`rv0001`、`imdb_2026-10-29`；所有读取都带这些条件。
- R02 [用户确认]：作品层粒度为 `movie_id`，消解 5 个标量字段（`title`、`original_title`、`release_year`、`runtime_minutes`、`mpaa_rating`）与 4 个多值字段（`genres`、`director`、`starring`、`cast`）。
- R03 [用户确认]：版本层粒度为 `asin`，消解 6 个标量字段（`edition_title`、`runtime_minutes`、`mpaa_rating`、`publisher`、`product_release_date`、`discs`）与 7 项多值特征（`media_format`、`language`、`subtitles`、`aspect_ratio`、`region`、`cut_type`、`video_system`）。
- R04 [作业要求]：不同版本在载体、语言、字幕、碟数、地区码、制式、画幅、剪辑版、发行商与发售日上的差异属于版本属性，不作为作品层冲突归一或丢弃。
- R05 [作业要求]：合集商品的盒子总片长、合集分级与混合类型属于合集版本自身，不下推给成员作品。
- R06 [工程默认]：版本层范围取 `task4.product_edition` 中 `match_scope = 'matched'` 的 9,590 个商品，不取自 `task4.product_movie`（后者只含 8,866 个）；没有关联作品的 724 个合集照常消解版本层，`movie_ids` 为空数组。
- R07 [作业要求]：每个标量字段的全部候选都保存在 `task7.field_candidate`，记录实体、字段、原始值、规范值、精度、来源定位、获取时间、匹配可靠度、候选状态与原因；绝不覆写或删除上游记录。
- R08 [作业要求]：候选状态互斥为 `adopted`、`rejected`、`conflict_pending`、`superseded_by_specific`；规范值与胜出值相同的候选都标 `adopted`；同作品的别名或更泛化写法标 `superseded_by_specific`；被规则排除的异值标 `rejected`。
- R09 [作业要求]：消解按“字段语义一致性 → 匹配可靠度与来源权威性 → 精度与非占位具体性 → 同源区域优先级 → 最小稳定键”依次决策；不得无理由采用最后抓到的值或简单多数票；每条选择写明规则、冲突类型、理由与置信度。
- R10 [工程默认]：采集时间照实保存但不参与决胜：页面在 2026-10-28 同日内采集、IMDb 为单一快照，时间先后不代表信息更新，用它决胜等于“最后抓到的值”；页面取 `task4.source_record.acquired_at`，IMDb 取 `imdb.snapshot.downloaded_at`。
- R11 [作业要求]：每个作品层 9 个字段与版本层 6 个标量字段各写一条选择记录，状态互斥为 `resolved`、`conflict_unresolved`、`missing`。
- R12 [工程默认]：`missing` 只表示没有候选；存在候选但无法采用时一律为 `conflict_unresolved`，胜出值为 `NULL`，候选标 `conflict_pending`。
- R13 [工程默认]：冲突类型为闭集：`none`、`formatting_or_alias`、`unrated_vs_rated`、`minor_tolerance`、`framerate_or_edition`、`cross_source_override`、`complementary_union`、`director_source_disagreement`、`billing_conflict_resolved`、`semantic_distinction`、`intra_asin_publisher_conflict`、`intra_edition_rating_conflict`、`intra_asin_runtime_conflict`、`cross_source_mismatch_halt`、`severe_mismatch_unresolved`、`intra_source_tie_unresolved`；细分原因写入 `disagreement_detail`。
- R14 [用户确认]：作品的有效 `tconst` 取任务 6 采用候选的三类来源（`imdb_t4_link`、`imdb_t5_hint`、`imdb_t6_cooccur`，正式库 5,407 部），基础可靠度 `0.95`、`0.80`、`0.75`；后两类称弱匹配。
- R15 [工程默认]：每部有效关联作品在字段消解前计算四个信号：年份 `Y`（页面标题年份与 IMDb 差 ≥2 年，或评论、发售日证据早于 IMDb 年 ≥2 年）；片长 `RT`（存在无剪辑版、无多碟花絮标记的 `edition` 商品与 IMDb 差 >15 分钟）；导演 `D`（双方都有导演且无一人兼容）；标题 `T`（去载体词与首尾冠词后，页面标题键与 IMDb 全部标题键既不相等也无双向实词子集）。
- R16 [工程默认；t7r2、t7r3 经用户授权放宽]：导演比较先按 `nconst`，再按分隔符拆分后用任务 5 的姓名兼容等级 `etl_task5.names.level`；不得只比 `nconst`，弱匹配作品的页面人物大多没有 `nconst`。`t7r2` 起在调用 `level` 前对两侧姓名预规范化（粘连缩写拆开：`J.P.Dutta` → `J. P. Dutta`；句点后缺空格、多余空白），并增加最低兼容等级 `LS`（同片词序子集）。最终规则（`t7r3`，当前默认）：`LS` 有方向，只在页面名是较短一方、IMDb 名是页面名的连续前缀或后缀扩展时成立（大小写与重音折叠后比较），例如页面 `Rakesh Chaturvedi` 对 IMDb `Rakesh Chaturvedi Om`；页面名按原始写法的空白分词至少 2 个词，连字符连接的名字算 1 个词（`Carla-Rae` 不触发）；单个词的名字不触发，中间多出词（`Michael Jordan` 与 `Michael B. Jordan`）不算，交给 `level` 原有规则；页面拼接串（`Whoopi Goldberg Justin Kirk`）比 IMDb 名长，不得以 `LS` 对齐到其中一人。`t7r2` 的 `LS` 是双向的，因页面拼接串会误对齐到其中一人而被 `t7r3` 取代；`t7r1` 没有预规范化也没有 `LS`。`LS` 只用于同一作品、同一角色内的比较（D 信号、R38 署名对齐、R41 页面独有导演）。
- R17 [用户确认]：弱匹配且满足 `(Y ∧ (RT ∨ D ∨ T)) ∨ (T ∧ (RT ∨ D))` 时判定错配熔断 `cross_source_mismatch_halt = true`；强关联不熔断。
- R18 [工程默认]：弱匹配只亮单一信号时不熔断，继续采用 IMDb，写质量问题 `weak_link_single_signal` 并在 `disagreement_detail` 记录信号。
- R19 [用户确认]：共享 `tconst` 按 `(tconst, movie_id)` 去重后逐对分类（任务 6 的 126 条问题对应 105 对）：已熔断为 `false_positive_link`；未熔断且标题相等或子集、导演不矛盾为 `likely_duplicate_movie`；其余为 `undetermined`；只记录，不回写任务 4。
- R20 [用户确认]：作品有未熔断的 IMDb 关联时，`title` 采用 `primary_title`；页面清洁标题规范值相同的标 `adopted`，命中 `primary`、`original`、`aka` 或实词子集的标 `superseded_by_specific`（`formatting_or_alias`），其余标 `rejected`。
- R21 [用户确认]：无 IMDb 关联或已熔断时，`title` 只用页面候选：`edition` 读 `entity_id = asin`，`contains` 读 `entity_type = 'collection_member' AND entity_id = asin || '#' || member_ordinal`；规范标题键一致或互为副标题、卷号包含时取频次最高的清洁标题，平票取最小 `asin`；实质不同则 `conflict_unresolved`。
- R22 [用户确认]：`original_title` 有未熔断 IMDb 关联时采用 `imdb.title.original_title`，与 `primary_title` 不同时标记 `distinct_from_primary`；无 IMDb 候选为 `missing`；已熔断为 `conflict_unresolved`。
- R23 [作业要求]：作品别名保存在 `task7.movie_title_alias`：IMDb `primary`、`original`、`aka`（含区域与语言，来源缺失时为 `NULL`）与页面清洁标题变体；已熔断作品不收录 IMDb 别名。
- R24 [用户确认]：`release_year` 候选衔接任务 6 的四类来源；候选一致或单一来源时采用（`none`），IMDb 与页面相差 1 年时采用 IMDb（`minor_tolerance`）；只有年份，`precision = 'year'`，不补成日期。
- R25 [用户确认]：强关联且 `Y` 成立时，只要导演或片长不同时矛盾就采用 IMDb（`cross_source_override`，写质量问题）；`D ∧ RT` 同时成立才为 `conflict_unresolved`。
- R26 [用户确认]：弱匹配已熔断时 `release_year` 为 `conflict_unresolved`（`cross_source_mismatch_halt`）；未熔断且 `Y` 成立时，导演兼容或存在差 ≤15 分钟的片长即为正面佐证，采用 IMDb（`cross_source_override`）；没有佐证为 `conflict_unresolved`。
- R27 [作业要求]：无 IMDb 候选且页面标题年份不一致时为 `conflict_unresolved`；无任何年份候选时为 `missing` 并记录任务 6 缺失原因；推断上界只放独立参考列，绝不成为 `release_year` 候选。
- R28 [用户确认]：作品层 `runtime_minutes` 只聚合 `relation_type = 'edition'` 商品；有未熔断 IMDb 片长时采用 IMDb；已熔断时为 `conflict_unresolved`。
- R29 [工程默认]：有 IMDb 片长时逐 `edition` 商品写一行 `task7.runtime_diff`，`diff_class` 取 `exact`（0）、`tolerance_1_2`（1–2）、`pal_evidence`、`variance_3_5`、`cut_or_multidisc`（>5 且有 `cut_type`，或 `discs ≥ 2` 且 ≥1.5 倍）、`unexplained_6_15`、`unexplained_gt15`。
- R30 [工程默认]：`pal_evidence` 必须同时有 `video_system` 含 PAL 的断言和 `|页面 − round(IMDb × 24/25)| ≤ 1`；只有公式吻合时不归为 PAL，改记 `pal_formula_match = true`。
- R31 [工程默认]：作品层 `conflict_type` 取该作品差异最严重版本：`exact`→`none`，`tolerance_1_2`→`minor_tolerance`，`pal_evidence`/`variance_3_5`/`cut_or_multidisc`→`framerate_or_edition`，`unexplained_*`→`cross_source_override`；存在 `unexplained_gt15` 时写质量问题 `severe_runtime_conflict`。
- R32 [用户确认]：无 IMDb 片长时，单版本直接采用；多版本极差 ≤5 取众数，平票取较小值（`minor_tolerance`，理由为差值在舍入误差内）；极差 >5 时采用无剪辑标记版本的众数，无法区分则 `conflict_unresolved`；仅经 `contains` 出现的作品为 `missing`。
- R33 [用户确认]：MPAA 读 `task3.page_field(field_key = 'mpaa_rating')`，规范化为 `G`、`PG`、`PG-13`、`R`、`NC-17`、`X`、`NR/Unrated`；未知值写 `unknown_mpaa_rating`；同 ASIN 不一致优先 `detail_bullets`（`intra_edition_rating_conflict`）。作品层只聚合 `edition` 商品：一致则采用；单一具体分级与 `NR/Unrated` 并存时采用具体分级（`unrated_vs_rated`）；两种以上具体分级为 `conflict_unresolved`。
- R34 [作业要求]：`genres` 受控词表为 IMDb 25 个标准词加 `Anime`、`Foreign`、`Performing Arts`、`LGBT`、`Silent`；页面词先去 ` see more` 后缀、把 `_` 还原为空格，再按映射表转换，复合词拆成多个受控词。
- R35 [作业要求]：每个页面词面在 `task7.movie_genre_term` 按作品逐行保存处置结果：`mapped`、`noise`（载体、导航、年龄段词）或 `unmapped`；`unmapped` 不进入 `genres`，写质量问题 `unmapped_genre_term`。
- R36 [作业要求]：`genres` 合并未熔断的 IMDb `title.genres` 与 `edition` 商品页面的映射结果，每个成员一行记录 `both`/`imdb_only`/`page_only`、原始词面与来源定位；集合差异属互补描述（`complementary_union`），不算互斥冲突；合集类型不下推。
- R37 [用户确认]：作品未熔断时 `director` 取页面与 `imdb.title_director` 的并集，`cast` 取页面与 `title_principal(category IN ('actor','actress'))` 的并集；`starring` 只取页面署名行，绝不从 IMDb 生成。
- R38 [工程默认]：并集前做同片署名对齐：同作品同角色的页面未关联 `nconst` 人物与 IMDb 人物用 R16 比较（等级序 `L1 > L2 > L3 > LI > L4`，`t7r2`、`t7r3` 末尾加 `LS`，`t7r3` 的 `LS` 只在页面名较短时成立），唯一最高等级命中时记为同一人（`both`），写入 `task7.credit_alignment`，保留原始署名与等级；同等级多命中（包括两人都与页面名构成 `LS`；页面拼接串不构成 `LS`，见 R16）不对齐，页面行标 `conflict_pending`。
- R39 [工程默认]：含分隔符（`;`、` and `、`&`）的页面署名只在有未熔断 IMDb 关联时按 R38 逐段对齐；全部成段命中才取代原行，部分命中或无 IMDb 时保留原行标 `conflict_pending` 并写 `unsplit_multi_person`；不为分段新造 `person_id`。
- R40 [用户确认]：对齐后的 IMDb 人物若已在 `task5.person_external_id` 则复用其 `person_id`；否则按任务 5 公式 `'ps_' || substr(sha256('imdb:' || nconst), 1, 16)` 登记到 `task7.imdb_person_ext`，已有 `task5.person_identity` 锚点时直接取用。
- R41 [用户确认]：对齐后仍同时存在页面独有导演与 IMDb 导演时，IMDb 导演标 `adopted`，页面导演标 `conflict_pending`，字段为 `resolved`，`conflict_type = 'director_source_disagreement'`，并写同名质量问题；多值字段没有任何 `adopted` 成员而有 `conflict_pending` 成员时为 `conflict_unresolved`。
- R42 [作业要求]：同作品同人同角色跨版本署名顺序不一致时取出现商品数最多的序号（理由：多数版本印刷一致，少数为重排），平票取最小序号，`resolution_note` 保留各商品分布（`billing_conflict_resolved`）；IMDb 独有成员排在页面成员之后并保留 `imdb_ordering`。
- R43 [作业要求]：`cast` 覆盖范围必须明确：`movie_resolved.cast_coverage` 取 `page_only`、`imdb_principals_only`、`page_and_imdb_principals`、`none`；报告声明 IMDb `title_principal` 只含主要演职员，不是完整演员表。
- R44 [用户确认]：版本 `edition_title` 取页面原始标题与清洁规范名；版本 `runtime_minutes` 取 `runtime_min`，同 ASIN 多值时按 `detail_bullets` > `product_overview` > `product_details` 并记 `intra_asin_runtime_conflict`。
- R45 [用户确认]：版本 `publisher` 在去标点、大小写折叠与厂牌后缀后相同或存在前缀包含时，取信息更完整的值（`formatting_or_alias`）；完全不同时按区域优先级取值并标 `intra_asin_publisher_conflict`，未采用值标 `rejected`。
- R46 [作业要求]：版本同时保存 `media_release` 与 `listing_first_available`；`product_release_date` 优先 `media_release`，无则回退上架日并在理由注明；两者都有且不同时记 `semantic_distinction`；任务 6 的 `after_review_period`、`jan_first_suspect` 标记写入 `date_flags` 并把置信度降到 `0.60`，值不改动。
- R47 [作业要求]：版本 `discs` 优先 `edition.discs` 断言（同 ASIN 多值按 R44 区域优先级），否则从 `edition.packaging` 提取整数；全部原始值写入候选表。
- R48 [作业要求]：7 项多值版本特征结合 `task4.field_assertion` 与 `task3.page_field` 规范化去重取并集，每个值保留来源区域。
- R49 [作业要求]：合并与消解后，原始评论、版本属性和来源记录仍可关联查询。
- R50 [作业要求]：每次运行生成冲突消解版本（`cv0001`…），记录输入键、四个阶段键、规则版本与统计；旧版本保留可查；`report diff` 用 `FULL OUTER JOIN` 列出两版本差异。
- R51 [工程默认]：阶段依赖链为 `edition_resolve → link_assess → movie_scalar` 与 `link_assess → movie_multivalue`，下游阶段键包含上游阶段键；输入或规则变化时只重算受影响阶段及其下游。
- R52 [作业要求]：同一输入、同一规则版本重跑时不新增、不修改任何目标行，只记一条 `reused` 审计；只有通过 `verify` 的运行才可被复用。
- R53 [作业要求]：`edition_resolve` 按 `asin`、`link_assess` 与两个作品阶段按 `movie_id` 分批（默认 1,000），结果、`entity_stage` 与 `run_batch` 同事务提交；中断后跳过已完成批次；每个步骤持抽取级咨询锁，并发运行确定性失败。
- R54 [作业要求]：DAG `amazon_movies_task7_conflict` 为 `check_inputs → edition_resolve → link_assess → movie_scalar_resolve → movie_multivalue_resolve → finalize_version → verify → finalize_report → gate`；确定性错误不重试，暂时错误最多重试 2 次，收尾与门禁 `all_done` 且不掩盖失败。
- R55 [工程默认]：`finalize_version` 只把版本标为 `unverified`；`verify` 通过才把版本标为 `verified`、运行标为 `succeeded`；核对失败时运行为 `failed`，同输入重跑在单事务中重写该未核实版本。
- R56 [工程默认]：表结构为迁移 `601`；正式运行只写 `task7` 与 `public.schema_migration`。
- R57 [工程默认]：测试只用 `tests/fixtures/task7/` 合成夹具；任务 1–6 现有 792 项测试保持通过。
- R58 [作业要求]：每次运行导出到 `outputs/task7/<抽取>/<冲突消解版本>/<run_key>/` 并记录 SHA-256：分字段覆盖率（分母 8,659、9,590、253,059；`resolved`、`conflict_unresolved`、`missing` 与 `inferred` 分列）、冲突类型与候选状态分布、片长 `diff_class` 分布、关联信号与熔断统计、署名对齐等级分布、类型词面处置统计、版本层冲突统计、共享 `tconst` 分类。
- R59 [作业要求]：`python -m etl_task7.query` 提供三个只读接口：`movie`（字段值、状态、规则理由、全部候选与来源、版本摘要、关联评估）、`edition`（版本属性、内部冲突、所属作品、评论统计）、`conflicts`（按冲突类型或字段列出冲突及理由）。
- R60 [作业要求]：`examples.md` 每类至少 3 例并附可定位证据：标题冲突、年份分歧与错配熔断、片长差异类别、演职员并集与署名对齐、多人串、署名顺序、类型映射、分级、版本层发行商与发售日、共享 `tconst`。
- R61 [作业要求]：开发池按 `md5(movie_id)` 排序取前 20%（1,732 部及其商品），在首次全量运行前冻结到 `task7.dev_pool`，只用于调阈值；版本层抽样时，任一关联作品在开发池的 ASIN 都排除；报告声明规划探查曾读全量，阈值并非只由开发池得出。
- R62 [用户确认]：评价样本 120 项，七层为 `E1` 标题 15、`E2` 年份 15、`E3` 片长 20、`E4` 演职员 20（导演 8、署名对齐 8、署名顺序 4）、`E5` 类型 15、`E6` 分级 15、`E7` 版本层 20；不足时按 `E3 → E4 → E1 → E7` 补足。
- R63 [工程默认]：`E1`、`E2`、`E3`、`E6` 每层优先纳入系统判为 `conflict_unresolved` 的项，至少 `min(可用数, 4)` 项；`E4` 的署名对齐项至少一半取 `L4` 等级；实际分布写入 `eval_set.strata`。
- R64 [作业要求]：盲测表按 `md5(entity_id || ':' || target_field)` 全局打乱编号，只含实体、目标字段与原始候选证据；候选证据由上游原始表直接生成并按来源键排序，不读 `task7` 候选状态或选择表，不泄露胜出值、状态、冲突类型或层号。
- R65 [作业要求]：用导入标注计算决策动作准确率、消解值准确率、`conflict_unresolved` 精确率与召回率（总体、按层、按字段）；`unsure` 单独计数不进分母；分母为零记“无定义”；声明指标只代表分层样本。
- R66 [作业要求]：AI 预标注草稿不得读 `task7` 选择结果，`ai_reason` 不进入正式标注；`ai_doubt = true` 或标注人改判的项必须填 `human_evidence`（可核实的外部依据），否则导入拒绝，除非标为 `unsure`；报告如实记录工具、用途、人工验证方式与局限。

## AC测试

以下为验收定义，实际完成情况只记入 checklist。AC00 为计划文件检查；AC01–AC17、AC21、AC22 用合成夹具在 `etl_test` 验证；AC18–AC20 针对正式库与交付。合成夹具放在 `tests/fixtures/task7/`，覆盖 9 个作品层字段与版本层字段的各类冲突；每个案例独立初始化 `etl_test`；正式库核对只用只读查询。

| AC | Rule | Input | Expected result |
|---|---|---|---|
| AC00 | 计划文件规范 | 作业任务 7 及第五、八、十部分；任务 6 交接文档；CLAUDE.md 规范。 | `spec.md` 五个二级标题顺序正确；R01–R66 均有任务与 AC 覆盖；`spec.md` 与 `checklist.md` 的 AC00–AC22 一一对应；未执行条目保持未勾选并标注 `（计划）`。 |
| AC01 | R01–R03, R06, R14 | 夹具含多个上游版本，当前版本含五类上映状态、只经 `contains` 出现的成员作品、一个无关联作品的合集和一个 `tv_series` 商品。 | 只读指定版本；每部作品在 `movie_resolved`、每个 `matched` 商品在 `edition_resolved` 恰好一行；无关联合集 `movie_ids = '{}'`；`tv_series` 不进入 `task7`；三类关联均按各自可靠度引入 IMDb 候选。 |
| AC02 | R04, R05, R07–R13, R49 | 同一作品的 DVD 宽屏版与 VHS 全屏版属性不同，另有合集含该作品，作品层字段存在跨源候选。 | 全部候选带完整定位与互斥状态；胜出值相同者都为 `adopted`；`missing` 记录 `considered_candidate_ids` 为空；`conflict_type` 全部落在闭集内；版本差异保留在版本层；合集总片长、分级与类型不下推；上游行不变。 |
| AC03 | R20–R23 | M1：IMDb `Seven Samurai` / `Shichinin no samurai`，页面 `Seven Samurai (Criterion Collection) [DVD]`，有 2 条 aka；M2：无 IMDb，`The Kid [VHS]` 与 `The Kid (1921) [DVD]`；M3：无 IMDb，键 `red shadow` 与 `crimson ninja`；M4：已熔断，页面 `Wrong Trousers`，IMDb `Curse of the Were-Rabbit`。 | M1 `title`、`original_title` 为 IMDb 值，页面候选 `superseded_by_specific`，别名表含 3 条 IMDb 标题及语言；M2 `title = "The Kid"`、`original_title` 为 `missing`；M3 `title` 为 `conflict_unresolved`；M4 `title` 取页面值，IMDb 候选 `rejected`，`original_title` 为 `conflict_unresolved`，别名表无 IMDb 行。 |
| AC04 | R24–R27 | Y1：强关联，IMDb 1985、页面 1986；Y2：强关联，IMDb 1954、页面 1998，导演兼容；Y3：强关联，`Y`、`D`、`RT` 同时成立；Y4：弱匹配已熔断；Y5：弱匹配，IMDb 1991、页面 2010，导演兼容，未熔断；Y6：弱匹配，年份差 5，无导演、无片长；Y7：无 IMDb，页面 1971 与 1975；Y8：无候选，有上界 `2001-05-01`。 | Y1 1985（`minor_tolerance`）；Y2 1954（`cross_source_override`）；Y3、Y4、Y6、Y7 为 `conflict_unresolved`（值 `NULL`）；Y5 1991（`cross_source_override`）；Y8 为 `missing`，上界只在参考列。 |
| AC05 | R28–R32 | R1：IMDb 100，版本 A 101、B 96（PAL 断言）、B2 96（无 PAL 断言）、C 125（`cut_type`）；R2：IMDb 90，单碟无 `cut_type`、135；R3：无 IMDb，92 与 94；R4：无 IMDb，85 与 118 无标记；R5：已熔断。 | R1 作品层 100，`runtime_diff` 分别为 `tolerance_1_2`、`pal_evidence`、`variance_3_5`（`pal_formula_match = true`）、`cut_or_multidisc`，作品层 `framerate_or_edition`；R2 取 90、`unexplained_gt15`、写 `severe_runtime_conflict`；R3 取 92（`minor_tolerance`）；R4、R5 为 `conflict_unresolved`，版本层保留各自片长。 |
| AC06 | R33 | 商品 A1：`byline` `PG`、`detail_bullets` `PG (Parental Guidance Suggested)`；P1：`R`、`NR (Not Rated)`、`Unrated`；P2：`G` 与 `PG`；商品 A2：未知分级串。 | A1 版本层 `PG`（`formatting_or_alias`）；P1 作品层 `R`（`unrated_vs_rated`），未评级候选 `superseded_by_specific`；P2 为 `conflict_unresolved`，版本层各自保留；A2 写 `unknown_mpaa_rating`。 |
| AC07 | R34–R36 | G1：IMDb `{Action, Sci-Fi}`，页面 `Action & Adventure`、`Science Fiction & Fantasy see more`、`faith_&_spirituality`，面包屑含 `Movies & TV`、`DVD`、`Anime & Manga`；G2：已熔断。 | G1 `genres = {Action, Adventure, Animation, Anime, Fantasy, Sci-Fi}`，`Action`、`Sci-Fi` 为 `both`；`see more` 被去除；`Movies & TV`、`DVD` 记 `noise`，`faith & spirituality` 记 `unmapped` 并写质量问题，三者都在 `movie_genre_term`；G2 不含 IMDb 类型。 |
| AC08 | R37, R40–R43 | C1：页面导演 D1、主演 S1（三版本序号 1、2、1），IMDb 另有导演 D2、演员 A2；C3：页面导演 `John Doe` 未关联，IMDb 导演 `Jane Roe`；C4：只有 IMDb 演员。 | C1 `director` 含 D1（`both`）与 D2（`imdb_only`，有稳定 `person_id`），`cast` 含 S1 与 A2，`starring` 只含 S1，S1 序号 1 且 `billing_conflict_resolved`；C3 `John Doe` 为 `conflict_pending`，字段 `resolved`、类型 `director_source_disagreement`；`cast_coverage` 按 R43 取值；`task5` 不变。 |
| AC09 | R44–R48 | E1：发行商 `Warner Home Video`（`detail_bullets`）与 `Warner Bros.`，发售日 `2003-05-20`、上架日 `2001-09-10`；E2：`Sony Pictures` 与 `Universal Studios`；E3：多区域 `DVD`、`Widescreen`、`NTSC`、`English`、`French`；E4：发售日带 `after_review_period`；E5：无 `edition.discs`，包装 `2-Disc Set`。 | E1 `publisher` 为前者（`formatting_or_alias`），发售日取 `media_release` 并保留上架日（`semantic_distinction`）；E2 取 `Sony Pictures`（`intra_asin_publisher_conflict`），另一候选 `rejected`；E3 多值并集保留区域；E4 `date_flags` 含标记、置信度 `0.60`；E5 `discs = 2`，有选择记录。 |
| AC10 | R19 | 夹具含同一对作品的两条共享 `tconst` 问题，另有已熔断、标题相等且导演兼容、标题相等但导演不兼容的共享对。 | 重复问题只生成一对；三类分别为 `false_positive_link`、`likely_duplicate_movie`、`undetermined`；任务 4 未被修改。 |
| AC11 | R50–R52 | 同一输入运行两次；修改片长容差再运行；修改发行商规则再运行；切换任务 4 映射版本再运行。 | 第二次为 `reused` 且业务行不变；改片长容差只重算 `movie_scalar`；改版本规则重算 `edition_resolve`、`link_assess` 与两个作品阶段；改输入生成新版本；`report diff` 列出变化；旧版本可查询。 |
| AC12 | R53 | 批大小 2；分别在 `edition_resolve`、`link_assess`、`movie_scalar`、`movie_multivalue` 第 2 批提交前注入故障后重跑；另启动并发运行。 | 故障后只有第 1 批落库；重跑跳过已完成批次，最终结果与一次跑完一致；并发运行因拿不到咨询锁确定性失败。 |
| AC13 | R54, R55 | 检查 DAG 拓扑与参数；按 DAG 顺序运行成功、重跑复用、上游版本不存在、`verify` 注入失败后重跑；用 `compose.e2e.yaml` 经真实 scheduler 验证。 | 9 个节点按序连接，`finalize_report`、`gate` 为 `all_done`，重试上限 2，无弃用警告；上游缺失时 `check_inputs` 失败且只尝试 1 次；`verify` 失败时运行为 `failed`、版本 `unverified`，重跑不是 `reused`，在原版本上重写后成功。 |
| AC14 | R58–R60 | 夹具运行后执行 `report export` 与 `query movie|edition|conflicts`。 | `summary.json` 全部计数与数据库一致；`examples.md` 覆盖 R60 全部类别并附定位；三个查询返回含胜出值、未采用候选、理由与关联评估的 JSON；导出无评论正文与整页 HTML，哈希一致。 |
| AC15 | R61–R66 | 夹具含开发池与评价池；执行 `evaluate freeze`、`export`、`import`、`metrics`；导入含缺少 `human_evidence` 的存疑项。 | 开发池严格排除；七层配额与 `conflict_unresolved` 保底正确；盲测表不泄露系统结果，候选顺序与状态无关；草稿不读 `task7` 选择表，`ai_reason` 不入标注；缺依据的存疑项被拒绝；指标与手算一致，分母为 0 为“无定义”。 |
| AC16 | R15, R16, R20, R21, R29, R30, R33, R34, R39 | 单元测试：单实词与括号年份标题、载体词与冠词归一、标题信号、PAL 公式与证据、多人串拆分、`NR/Unrated` 写法、未知 MPAA、类型词面处置。 | 全部纯函数与边界案例通过，无假阳性合并或静默吞掉冲突。 |
| AC17 | R50, R58, R59 | 多次运行（`succeeded` 与 `reused`）与多阶段键共存时，测试报告、`--run-id` 校验与 `etl_task7.check`。 | 不存在的 `--run-id` 被拒绝；有 `reused` 时仍读 `succeeded` 计数；阶段表查询不串版本；`check` 能检出注入的孤儿候选、状态不一致与未核实版本。 |
| AC18 | R01–R60 | 正式库先冻结开发池 1,732 部，再经 Airflow 全量运行生成 `cv0001`，同参重跑，执行 `python -m etl_task7.check`。 | 8,659 部作品、9,590 个商品各有完整记录且状态互斥；每个 `resolved` 标量字段指向有效采用候选；`conflict_unresolved` 胜出值为 `NULL`；多值字段无角色混淆且成员有来源；任务 1–6 全部表行数不变；`check` 退出码 0、`"problems": []`；重跑为 `reused`。 |
| AC19 | R61–R66 | 正式库导出盲测表与 AI 草稿，经付林轩核查后署名导入并计算指标。 | 配额符合 R62–R63；导入 120 条标注；输出总体、按层、按字段指标；报告说明工具、人工核查方式与局限。 |
| AC20 | R56, R57 | 检查 `public.schema_migration`、全量测试、`git ls-files` 与 `src/etl_task7` 写语句。 | 迁移为 `001,101,102,103,201,301,302,401,501,601`；全量测试通过且 792 项不减少；写语句目标全部在 `task7`；仓库不含 `.env`、`outputs/`、评价表或 IMDb 数据。 |
| AC21 | R14–R18 | L1：弱匹配，`Y ∧ RT`；L2：弱匹配，`Y ∧ D`；L3：弱匹配，`T ∧ RT`；L4：弱匹配只亮 `D`（`John Cherry` / `John R. Cherry III` 兼容，不亮）；L5：弱匹配只亮 `RT`；L6：强关联 `Y ∧ RT`；L7：页面人物无 `nconst` 但姓名严格相同；L8：页面 `Emma VHS`、`Departed, The`；`t7r2`、`t7r3` 另有 L11：弱匹配 `Y`，页面导演 `J.P.Dutta`、IMDb `J.P. Dutta`；L12：弱匹配 `Y`，页面 `Rakesh Chaturvedi`、IMDb `Rakesh Chaturvedi Om`；L13：弱匹配 `Y`，页面 `Madonna`、IMDb `Madonna Louise`；L14：弱匹配 `Y`，页面 `Michael Jordan`、IMDb `Michael B. Jordan`。 | L1–L3 熔断，写 `task7.link_assessment`；L4、L7 不亮 `D`；L5 不熔断且写 `weak_link_single_signal`；L6 不熔断；L8 不亮 `T`；信号、原因与证据完整持久化，`movie_scalar` 与 `movie_multivalue` 读取同一判定。`t7r1` 下 L11、L12 的 `D` 亮并因 `Y+D` 熔断；`t7r3` 下 L11（预规范化后 `L1`）、L12（`LS`）、L14（`L3`）不亮 `D`，L11、L12 只剩 `Y` 单信号，不熔断且写 `weak_link_single_signal`；L13 单个词不触发 `LS`，`D` 仍亮，各版都熔断。 |
| AC22 | R38, R39 | C5：页面未关联 `Garey Busey`，IMDb `Gary Busey`；C6：页面 `John Cusack`，IMDb `John Cusack`、`Joan Cusack`；C7：两个同等级命中；C8：页面导演 `Tomas Guiterrez Alea; Carlos Garcia Agraz`，IMDb 为两人；C9：部分命中的多人串；C10：无 IMDb 的多人串；C11：`Black and Blue Productions`；`t7r2`、`t7r3` 另有 C12：页面导演 `J.P.Dutta`，IMDb `J.P. Dutta`；C13：页面导演 `Rakesh Chaturvedi`，IMDb `Rakesh Chaturvedi Om`；C14：页面演员 `Madonna`，IMDb `Madonna Louise`；C15：页面演员 `Rakesh Chaturvedi`，IMDb 有 `Rakesh Chaturvedi Om` 与 `Rakesh Chaturvedi Shah`；C16：页面 `Michael Jordan`，IMDb `Michael B. Jordan`；C17：页面导演串 `J.P.Dutta; Rakesh Chaturvedi`，IMDb 为 `J.P. Dutta` 与 `Rakesh Chaturvedi Om`；C18：页面演员 `Whoopi Goldberg Justin Kirk`，IMDb `Justin Kirk`；C19：页面演员 `George Meeker Trevor Bardette`，IMDb 有 `George Meeker` 与 `Trevor Bardette`；C20：页面演员 `Carla-Rae`，IMDb `Carla-Rae Holland`；C21：页面演员 `Wai-Man Chan`，IMDb `Michael Wai-Man Chan`。 | C5 对齐为一人（`both`，等级 `L4`，保留原署名）；C6 取严格命中；C7 不对齐且 `conflict_pending`；C8 两段都对齐，原行被取代且保留在 `credit_alignment`；C9、C10、C11 保留原行、`conflict_pending` 并写 `unsplit_multi_person`；未新造 `person_id`。`t7r1` 下 C12、C13 的页面导演为 `conflict_pending` 且字段类型 `director_source_disagreement`；`t7r3` 下 C12 对齐为 `L1`、C13 对齐为 `LS`，都是 `both`、无 R41 分歧；C14 单个词不对齐；C15 两人并列 `LS`，不对齐且页面行 `conflict_pending`；C16 等级 `L3`（不是 `LS`，两版相同）；C17 两段分别 `L1`、`LS` 全部命中，原行被取代；C18、C19 在 `t7r3` 下无兼容成员（页面拼接串不得以 `LS` 对齐到其中一人），整串作为页面独有成员保留，而 `t7r2` 下 C18 误对齐到 `Justin Kirk`、C19 两人并列 `LS`；C20 连字符名按 1 个词计，不触发 `LS`（`t7r2` 会）；C21 空白分词 2 个词，对齐为 `LS`。 |

## 假设

| 编号 | 当前选择或假设 | 状态与理由 | 相关规则 |
|---|---|---|---|
| H01 | **[用户确认]** 作品层与版本层字段边界：作品层 9 个字段，版本层 6 个标量字段与 7 项多值特征；版本物理差异留在版本层。`discs` 于 2026-11-02 补入标量选择记录。 | 2026-10-30 确认方案 A；符合第五、七、八部分。 | R02–R05 |
| H02 | **[用户确认，2026-11-02 修订]** IMDb 关联范围 5,407 部，可靠度 `0.95`/`0.80`/`0.75`；弱匹配按 R17 熔断。原 spec 与 design 定义不一致；用户同意按复核结论修订，公式与阈值为工程默认，开发池可调。 | 规划试跑：原 design 口径 22 部且多为同片；修订口径 8 部，多数明显错配。 | R14–R18 |
| H03 | **[用户确认，2026-11-02 修订]** `director`、`cast` 取可信并集，`starring` 只取页面署名行；并集前增加同片署名对齐，避免同一人出现两个 `person_id`。 | 规划试跑：页面未关联人物与 IMDb 兼容 3,335 条，其中严格相同 2,706 条，不对齐会重复。 | R37–R42 |
| H04 | **[用户确认，2026-11-02 补充]** 标量容差与保留冲突边界同原方案；补充年份决策表（R25–R26）；已熔断作品的 `title` 回退页面值，其余 IMDb 标量为 `conflict_unresolved`。 | 正式库 5 部 `year_disagreement` 原规则无分支；修订后均有正面佐证。 | R11、R12、R24–R33 |
| H05 | **[用户确认，2026-11-02 修订]** 开发池 1,732 部；120 项七层样本；非循环 AI 草稿经付林轩审核导入；新增 `conflict_unresolved` 保底、`E4` 署名对齐项与存疑项外部依据。 | 任务 6 的存疑备注被原样采纳，见 H09。 | R61–R66 |
| H06 | **[工程默认]** `genres` 受控词表 30 词；映射表在开发池上冻结后（T7-08），正式库实测页面类型词面未映射 254/5,201 个 ASIN 词面（4.9%），面包屑 192/30,146（0.64%）；剩余多为自由标签、含义不明确的词和开发池没有出现的音乐子类（已知局限，见 H11），逐行保留不进入 `genres`。 | 2026-11-02 `cv0001` 实测；追加的词见 design“类型映射在开发池上冻结”。 | R34–R36 |
| H07 | **[工程默认]** 共享 `tconst` 只分类不回写任务 4；`likely_duplicate_movie` 作品在目标层仍是两个 `movie_id`，字段值相同，任务 8 统计须知悉。 | `map0002` 是任务 5、6、7 的共同基线。 | R19、R56 |
| H08 | **[已验证]** 2026-11-02 正式库 `cv0001` 实测（规划试算数字见 design）：熔断 7 部（`Y+RT` 4、`Y+D` 2、`RT+T` 1）；弱匹配单信号 `D` 97、`RT` 86、`Y` 7（共 190，无 `T` 单信号）；片长 `diff_class` 按对 `exact` 3,064、`tolerance_1_2` 888、`variance_3_5` 476、`pal_evidence` 20、`cut_or_multidisc` 44、`unexplained_6_15` 370、`unexplained_gt15` 243，合计 5,105；共享对 105（`likely_duplicate_movie` 94、`undetermined` 10、`false_positive_link` 1）；署名对齐成功 3,358 条；IMDb 新增 `cast` 30,182、`director` 869 行；`imdb_person_ext` 22,157 人。姓名放宽后的 `cv0003`（`t7r3`，当前默认；`t7r2` 的 `cv0002` 见 design）：熔断仍为 7 部；弱匹配单信号 `D` 92、`RT` 89、`Y` 7；共享对 105（`likely_duplicate_movie` 95、`undetermined` 9、`false_positive_link` 1）；署名对齐成功 3,435 条（`LS` 71）；IMDb 新增 `cast` 30,118、`director` 861 行；页面独有导演 `conflict_pending` 259（R41 原因 211，涉及 202 部）。片长、年份、标题、分级、类型与版本层三个版本完全相同。 | 2026-11-02 正式库运行 `t7_full_1`（`cv0001`）、`t7_full_3`（`cv0002`）、`t7_full_5`（`cv0003`）。 | R15–R19、R29、R38 |
| H09 | **[用户确认，2026-11-02]** 任务 6 `t6_eval_0061`（Ernest Goes to Jail）与 `t6_eval_0087`（The Red Baron）的 `tconst_label = different` 确属误标：页面演员都在对应 IMDb 演员表中，导演为同人不同写法。不修改任务 6 的任何表与文件；任务 7 不以这两例为熔断依据；T7-09 报告会给出更正后的任务 6 `tconst` 正确率。 | 误标已由用户确认；更正后的正确率只在任务 7 报告里给出，任务 6 的记录保持原样。 | R17、R66 |
| H10 | **[用户确认，2026-11-02]** 姓名兼容放宽为规则版本 `t7r2`（R16、R38）：预规范化格式差异并增加最低等级 `LS`。真实例子 `J.P.Dutta` 与 `J.P. Dutta`、`Rakesh Chaturvedi` 与 `Rakesh Chaturvedi Om` 在 `t7r1` 下被判不兼容，使 `D` 偏亮，页面独有导演被标 `conflict_pending`（R41）。`t7r2` 的 `LS` 双向，正式库抽查发现页面拼接串会误对齐到其中一人，用户同日确认改为方向性（`t7r3`，当前默认）；`t7r1`、`t7r2` 保留，`cv0001`、`cv0002` 可复现；多人导演串（42 部）按现行 R39 不变。放宽后的新增 `LS` 对齐是否同一人，只能靠抽查与盲测样本检验，`LS` 对齐成立是工程判断，不是已验证的事实。 | `cv0001` 与 `cv0002` 的差异见 design“姓名兼容放宽（`t7r2`）”。 | R16、R38、R39、R41 |
| H11 | **[用户确认，2026-11-02]** 开发池之外未映射的音乐子类（`Jazz`、`New Wave` 等）作为已知局限，不追加映射（沿用 H06 的“只看开发池”约定）。 | 全库实测的未映射比例见 H06 与 design。 | R34–R35 |
| H12 | **[用户确认，2026-11-02，已知局限]** 页面拼接串（逗号、斜杠、空格拼接的人名，`split_multi_person` 不拆）是早已存在的数据问题，从 `cv0001` 起就作为页面独有成员被采用，这次不扩大 R39 的分隔符。实测（`cv0003`，启发式）页面独有成员中疑似拼接串 `cast` 258、`director` 64、`starring` 214，涉及 301 部作品。`t7r3` 的方向性 `LS` 保证它们不被对齐到其中一人。 | 数量与做法见 design“姓名兼容放宽”。 | R39 |
