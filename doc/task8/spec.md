# 任务 8 规格（评论清洗与重复识别）

状态：2026-11-03 在分支 `task8-review-dedup`（从 `main` 的 `d03e3e3` 建立）完成正式库只读探查与计划编写；用户确认 H01–H04（去重层次、近似评论、合集口径、重复作品的统计口径）。T8-00（计划）已完成；T8-01 至 T8-06 同日实现，T8-07 业务库副本全链路演练通过；T8-08 正式库运行完成（`rd0001` `verified`，AC16、AC18 通过），评价表与 AI 草稿已导出，AC17 等待付林轩审核标注。实现中的工程修正（评价 `V5` 按作品对去重、建副本后执行 `CHECKPOINT`）不改变本文规则与 H01–H04，见 design“正式库实测（T8-08）”。设计见 [design](design.md)，执行顺序见 [tasks](tasks.md)，完成情况见 [checklist](checklist.md)。

## 目标

为任务 1 的全量 7,911,696 条原始评论记录建立可复核的清洗与重复识别流程：判定 helpfulness 等字段异常；识别 Amazon 把同一条评论展示在同一商品家族各版本上的平台复制，以及同一商品上的重复记录，把它们映射到同一条逻辑评论；对“同一用户在不同商品上贴相同文字”和“同一用户对同一作品写的相似评论”只标候选、不合并。原始记录全部保留，逻辑评论通过 `ReviewMapping` 关联原始记录。在此基础上按商品、作品和评分统计组统计评论数量与评分，明确是否去重、是否包含合集、分母是什么，使一条评论不在多个版本或作品间重复计数。

## 范围

- 包含：抽取 `f1e8b845…` 的全部原始记录（有效 7,849,565、隔离 62,131）；helpfulness、评分、时间、正文缺失等清洗状态；评论家族（按评论集合重叠识别的 Amazon 商品家族）；同 ASIN 重复、家族内跨版本复制的判定与核验；跨家族同内容与近似评论候选；`Reviewer`、逻辑评论、`ReviewMapping`；评论到商品、作品（经 `cv0003`）与评分统计组的归属；按商品、作品、统计组的评论数与评分（去重前后对照）；去重版本、阶段依赖、分批恢复与幂等重跑；Airflow DAG `amazon_reviews_task8_dedup`；报告、实例与只读查询；业务库副本演练；120 项分层评价（AI 预填、人工审核）。
- 不包含：修改任务 1–7 的表、规则、数据与测试（`public`、`task2`–`task7`、`imdb` 只读）；重新解码隔离记录或改变任务 1 的有效与隔离划分；在目标层合并 `movie_id` 或改写任务 4 映射、任务 7 的 `likely_duplicate_movie` 分类；跨用户的抄袭或多账号识别；评论正文语义分析与情感打分；字段级血缘引擎（任务 9）；联网抓取。
- 约束：新表全部建在 schema `task8`（迁移 `701`）；每次查询显式限定抽取、任务 3 规则版本、任务 7 冲突消解版本与阶段键；沿用 Python 3.12、PostgreSQL 18、Airflow 3.3.2、Docker Compose；测试只用合成夹具并连接 `etl_test`，夹具与上游真实表同形；每个实现阶段先在业务库副本演练再进正式库；运行导出、评价标注表不入 Git；导出不含评论正文全文。
- 依据：[作业说明](../../../作业/数据存储与管理_ETL作业说明.docx)第七部分任务 8、第五部分评论字段定义（`source_review_id`、`reviewer_id`、`profile_name`、`helpfulness`）、第八部分（处理层次、`Reviewer` 与 `Review`、`ReviewMapping`、可重复运行与恢复）、第十部分（报告有效、异常及重复评论数量；第 5 个查询“按产品或作品统计评论数量与评分”）；[任务 7 交接文档](../handoffs/2026-11-02-task7-to-task8.md)。

## 规则

“作业要求”来自任务书，“用户确认”来自 2026-11-03 规划确认，“工程默认”是本轮实现基线。分组：R01–R05 输入、分层与保留；R06–R10 清洗与比较键；R11–R19 评论家族与去重层次；R20–R22 不合并的候选；R23–R25 目标层；R26–R32 归属与统计口径；R33–R41 运行与编排；R42–R44 报告与查询；R45–R49 评价。

- R01 [作业要求]：输入限定为抽取 `f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`（任务 1 规则 `task1-rules-1`）、任务 3 分类规则 `t3r2`、任务 7 冲突消解版本 `cv0003`（`edition_resolved` 与其 `link_stage_key` 下的 `shared_tconst_result`）；所有读取都带这些条件。
- R02 [作业要求]：按第八部分分层：原始层为 `movies.txt.gz` 与 `public.review_record` 的定位、哈希及 `quarantine_record` 原始字节；暂存层为任务 1 的 `review_staging`、`quality_issue` 与任务 8 的清洗键 `review_clean`；集成层为评论家族、去重候选与核验结果；目标层为 `Reviewer`、逻辑评论、`ReviewMapping`、归属与统计；各层保留 `source_review_id`、批次与阶段键。
- R03 [作业要求]：原始记录全部保留：任务 8 不删除、不改写任何原始或暂存行；每个去重版本中 7,911,696 条原始记录在 `review_mapping` 各有且只有一行。
- R04 [作业要求]：逻辑评论 ID 由任务 8 生成（`lr_` 前缀），与 `source_review_id` 区分；ASIN 不当作品 ID。
- R05 [工程默认]：隔离记录保留任务 1 的分类，不重新解码、不形成逻辑评论，`mapping_status = 'quarantined'`，计入异常评论数，不进评分。
- R06 [作业要求]：每条有效记录判定 `helpfulness_status`：`ok`、`zero_zero`、`numerator_exceeds_denominator`、`negative`、`unparseable`、`missing`；后四类为异常，写 `task8.review_issue`；原值保留，票数与比例沿用任务 1 的规范值（异常时为 `NULL`）。
- R07 [作业要求]：`0/0` 不是异常：票数保留 0，比例为 `NULL`，在报告中单独计数。
- R08 [工程默认]：评论日 `review_day` 取 `review_time_utc` 的 UTC 日期；`score`、`review_time` 或 `review_text` 缺失的有效记录 `key_complete = false`，写 `incomplete_review` 问题，`mapping_status = 'incomplete'`，不形成逻辑评论，不参与合并与评分。
- R09 [工程默认]：内容键只用原值：`reviewer_id`、`review_day`、`score`（规范 Decimal）、`summary_raw`、`review_text_raw`；不做大小写、空白或 HTML 规范化；哈希只用于分组，合并前逐项核验完整原值相等。
- R10 [作业要求]：`Reviewer` 以 `reviewer_id` 为键，显示名不作用户标识；同一用户的显示名变体逐个保存并计数，不因同名合并用户。
- R11 [作业要求]：去重目的：一条用户撰写的评论在计数与评分中只计一次，同时保留它在各商品版本上的全部原始记录；去重键为 R09 的内容键，限定在同一 ASIN 或同一评论家族内（R12）。
- R12 [工程默认]：评论家族：每个 ASIN 的评论集合为其键完整记录的内容键集合；评论集合相同的 ASIN 先合为一组，两组共享内容键的 Jaccard ≥ 0.5 时相连，连通分量为一个评论家族；没有相连的 ASIN 自成家族。家族只表示“平台把同一批评论展示在这些商品上”，不代表同一作品。
- R13 [工程默认]：家族内最大评论集合不超过 2 条时，家族标 `weak_support`，照常参与 R16，报告单列其家族数、ASIN 数与复制记录数。
- R14 [作业要求]：`D1`（同 ASIN 完全相同）：同 ASIN、内容键相同，且 helpfulness 与显示名原值也相同的记录归同一逻辑评论。
- R15 [作业要求]：`D2`（同 ASIN 核验变体）：同 ASIN、内容键相同，helpfulness 或显示名不同的记录归同一逻辑评论，`diff_fields` 记录差异字段。
- R16 [工程默认]：`D3`（家族内跨版本复制）：不同 ASIN、同一评论家族、内容键相同的记录归同一逻辑评论，差异按 R15 记录。
- R17 [作业要求]：核验只允许 helpfulness 与显示名不同；用户、评论日、评分、摘要或正文任一不同即不合并（同正文不同摘要视为不同评论）。
- R18 [工程默认]：逻辑评论的代表记录取 `record_no` 最小的原始记录；逻辑评论的 helpfulness 取 `helpfulness_status` 为 `ok` 或 `zero_zero` 的副本中 `total_votes` 最大者（并列取较小 `record_no`），没有合格副本时为 `NULL`。
- R19 [工程默认]：`review_mapping.match_level` 互斥为 `representative`、`D1_exact_same_asin`、`D2_same_asin_variant`、`D3_family_copy`；`mapping_status` 为 `quarantined` 或 `incomplete` 时 `match_level` 与 `logical_review_id` 为 `NULL`；“重复评论数”定义为 `D1`、`D2`、`D3` 三类原始记录之和。
- R20 [作业要求]：`C1`（跨家族同内容）：内容键相同但分属不同评论家族的逻辑评论各自保留，在 `dup_candidate` 记一组 `cross_family_same_content`，不合并、不删除。
- R21 [用户确认]：`C2`（近似评论）：同一用户在同一评论家族或同一评分统计组（R29）内、正文不同的两条逻辑评论，按小写词集 Jaccard 计相似度，≥ 0.9 记 `near_duplicate` 候选；不合并，各自计数。
- R22 [工程默认]：候选只记录，不改变逻辑评论与映射；候选的人工结论只经评价导入记录（R49），不自动回写。
- R23 [作业要求]：`logical_review` 记录内容键哈希、评论家族、代表记录、副本数、涉及 ASIN 数、所选 helpfulness 及其来源记录；`review_mapping` 记录原始记录、ASIN、逻辑评论、`match_level`、`mapping_status`、`diff_fields` 与规则版本。
- R24 [工程默认]：`family_id = 'rf_' || substr(sha256(家族内最小 ASIN), 1, 16)`；`logical_review_id = 'lr_' || substr(sha256(内容键哈希 || '|' || family_id), 1, 20)`；同输入同规则重跑时 ID 不变。
- R25 [作业要求]：`Reviewer` 每个 `reviewer_id` 一行：显示名变体与次数、有效记录数、逻辑评论数、首末评论日。
- R26 [作业要求]：商品层归属：逻辑评论在它每个副本所在的 ASIN 上各算一条商品评论；按商品统计时同一逻辑评论在同一 ASIN 只计一次；各 ASIN 计数相加不等于逻辑评论总数，报告须说明。
- R27 [用户确认]：作品层归属只经 `edition` 关系：逻辑评论任一副本位于 `cv0003` 中 `product_kind = 'movie'` 的 ASIN 时，归属该 ASIN 的 `movie_ids[1]`；合集商品的评论只在商品层统计，不复制给成员作品；同一逻辑评论同时位于合集与单品时，按单品归属作品。
- R28 [作业要求]：剧集、演唱会或其他、非电影、待确认与未采集商品上的逻辑评论只在商品层与商品类别层统计，不进作品评分。
- R29 [用户确认]：评分统计组：两部作品在 `cv0003` 为 `likely_duplicate_movie`，或至少一条逻辑评论同时归属两者时相连，连通分量为统计组，单部作品自成一组；目标层 `movie_id` 不变，任务 4、7 的表不改。
- R30 [用户确认]：作品评分口径：去重后（逻辑评论）、不含合集、分母为归属该作品或统计组且 `score` 非空的逻辑评论数；统计组内每条逻辑评论只计一次；按 `movie_id` 统计时另列与同组其他作品共享的逻辑评论数。
- R31 [用户确认]：近似评论候选（`C2`）在评分中各计一次；报告给出把每组近似评论只计一次时的敏感性数字。
- R32 [作业要求]：报告同时给出去重前（原始有效记录）与去重后的评论数与平均分，并按平台复制、同 ASIN 重复、跨作品共享说明差异来源。
- R33 [作业要求]：每次运行生成去重版本（`rd0001`…），记录输入键、四个阶段键、规则版本（`t8r1`）与统计；旧版本保留可查；`report diff` 列出两个版本的计数与归属差异。
- R34 [工程默认]：阶段依赖为 `review_clean → review_family → review_dedup → review_attribution`，下游阶段键包含上游阶段键；只改评分口径或 `cv` 版本时只重算 `review_attribution`。
- R35 [作业要求]：同一输入、同一规则版本重跑不新增、不修改任何目标行，只记一条 `reused` 审计；只有通过 `verify` 的运行可被复用。
- R36 [作业要求]：`review_clean` 按 `record_no` 范围分批（默认 500,000 条），`review_family` 的签名与重叠计数、`review_dedup` 按 `reviewer_id` 哈希分区分批（默认 32 个分区）；结果与 `entity_stage`、`run_batch` 同事务提交，中断后跳过已完成批次；家族连通分量在该阶段最后一个事务中一次写入；每个步骤持抽取级咨询锁，并发运行确定性失败。
- R37 [作业要求]：DAG `amazon_reviews_task8_dedup` 为 `check_inputs → review_clean → review_family → review_dedup → review_attribution → finalize_version → verify → finalize_report → gate`；确定性错误不重试，暂时错误最多重试 2 次，收尾与门禁 `all_done` 且不掩盖失败。
- R38 [工程默认]：`finalize_version` 只把版本标为 `unverified`；`verify` 通过才标 `verified` 并把运行记为 `succeeded`；核对失败时运行为 `failed`，同输入重跑在原版本上重写。
- R39 [工程默认]：表结构为迁移 `701`；正式运行只写 `task8` 与 `public.schema_migration`。
- R40 [工程默认]：测试只用 `tests/fixtures/task8/` 合成夹具，夹具与上游真实输出同形（任务 1 的 `review_record`、`review_staging` 全部列与约束，`cv0003` 的 `edition_resolved`、`conflict_version`、`shared_tconst_result` 与阶段键）；任务 1–7 现有 1,121 项测试保持通过。
- R41 [用户确认]：每个实现阶段先在业务库副本（`CREATE DATABASE … TEMPLATE etl_movies OWNER etl_app`）上全链路演练并核对，通过后才在 `etl_movies` 运行；演练库用完删除。
- R42 [作业要求]：每次运行导出到 `outputs/task8/<抽取>/<去重版本>/<run_key>/` 并记录 SHA-256：读取、有效、隔离数；helpfulness 各状态；按层次的重复记录数；逻辑评论数；候选数；按商品类别的原始与逻辑评论数；家族统计；作品与统计组评分（去重前后）；口径说明。导出不含评论正文全文，实例只显示正文前 80 个字符。
- R43 [作业要求]：`python -m etl_task8.query` 提供只读接口：`review`（原始记录 ID → 逻辑评论、层次、家族、商品、作品、统计组与匹配状态）、`logical`（逻辑评论 → 全部原始记录与差异）、`stats --by product|movie|group`（评论数与评分，输出附口径说明）、`reviewer`（用户 → 显示名变体与评论）。
- R44 [作业要求]：`examples.md` 每类至少 3 例并附 `source_review_id` 定位：`D1`、`D2`、`D3`、`weak_support` 家族、`C1`、`C2`、helpfulness 异常、不完整记录、合集评论、跨作品共享与统计组。
- R45 [工程默认]：开发池按 `md5(reviewer_id)` 排序取前 20% 用户，在首次全量运行前冻结到 `task8.dev_pool`，只用于调整 R12 与 R21 的阈值；报告声明规划探查读过全量，阈值并非只由开发池得出。
- R46 [工程默认]：评价样本 120 项，排除开发池用户：`V1` 同 ASIN 变体（`D2`）15、`V2` 家族复制（`D3`，其中 `weak_support` 至少 10）25、`V3` 跨家族同内容（`C1`）20、`V4` 近似评论（`C2`）20、`V5` 统计组内作品对 20、`V6` 未成候选的同用户同 ASIN 不同评论 20；不足时按 `V2 → V3 → V4` 补足。
- R47 [作业要求]：盲测表按 `md5(item_key)` 全局打乱编号，只含两侧原始字段（用户、日期、评分、摘要、正文前 500 字符、ASIN 与商品标题、商品类别），不含系统层次、候选类型或层号；标注值 `same`、`different`、`unsure`（`V5` 判断是否同一部作品）。
- R48 [作业要求]：指标为合并精确率（`V1`、`V2` 中标 `same` 的比例）、不合并正确率（`V3`、`V6` 中标 `different` 的比例）、近似评论同一性比例（`V4` 中标 `same` 的比例，只描述，不作为错误）、统计组同片率（`V5`）；`unsure` 单独计数不进分母，分母为 0 记“无定义”；声明指标只代表分层样本。
- R49 [作业要求]：AI 预标注草稿只读盲测表，不读系统层次；须由付林轩逐项审核后导入，报告如实写“AI 预填、付林轩审核”；存疑或改判的项必须填外部依据，否则导入拒绝，除非标为 `unsure`。

## AC测试

以下为验收定义，实际完成情况只记入 checklist。AC00 为计划文件检查；AC01–AC14 用合成夹具在 `etl_test` 验证；AC15 在业务库副本演练；AC16–AC18 针对正式库与交付。合成夹具放在 `tests/fixtures/task8/`，与上游真实表同形；每个案例独立初始化 `etl_test`；正式库核对只用只读查询。

| AC | Rule | Input | Expected result |
|---|---|---|---|
| AC00 | 计划文件规范 | 作业任务 8 及第五、八、十部分；任务 7 交接文档；CLAUDE.md 规范。 | `spec.md` 五个二级标题顺序正确；R01–R49 均有任务与 AC 覆盖；`spec.md` 与 `checklist.md` 的 AC00–AC18 一一对应；未执行条目保持未勾选并标注 `（计划）`。 |
| AC01 | R01–R05, R19 | 夹具含两个抽取（样本与全量）、两个 `cv` 版本；全量抽取含有效、隔离（`invalid_utf8`、`unknown_line`）与缺 5 个字段的记录。 | 只读指定抽取与 `cv0003`；每条原始记录在 `review_mapping` 恰好一行；隔离记录 `mapping_status = 'quarantined'`、不完整记录为 `incomplete`，二者 `logical_review_id` 为空；`logical_review_id` 均以 `lr_` 开头且不等于任何 `source_review_id`；`public` 各表行数与摘要不变。 |
| AC02 | R06–R08 | helpfulness 为 `3/5`、`0/0`、`8/1`、`-1/2`、`abc`、缺失；另有缺评分与时间的记录。 | 依次为 `ok`、`zero_zero`、`numerator_exceeds_denominator`、`negative`、`unparseable`、`missing`；后四类各写一条 `review_issue`，`0/0` 不写；缺字段记录 `key_complete = false` 并写 `incomplete_review`。 |
| AC03 | R09, R10, R17, R25 | K1：两条记录正文只差大小写；K2：只差末尾空格；K3：同正文不同摘要；K4：注入哈希碰撞（两条不同正文得到相同分组哈希）；用户 U1 有显示名 `Ann` 与 `Ann "A"`，另一用户也叫 `Ann`。 | K1–K4 都不合并；K4 在核验时被拆开并写 `hash_collision` 问题；`reviewer` 中 U1 一行含两个显示名及次数，同名的另一用户单独一行。 |
| AC04 | R12, R13 | ASIN A、B 评论集合相同（10 条）；C 与 A 共享 6 条（Jaccard 0.6）；D 与 A 只共享 1 条；E 无共享；F、G 各只有 1 条且相同。 | A、B、C 同一家族；D、E 各自成家族；F、G 同一家族且 `weak_support`；`family_id` 按 R24 由家族内最小 ASIN 生成；阈值改为 0.7 时 C 自成家族。 |
| AC05 | R11, R14–R19, R23, R24 | 同 ASIN 两条八字段全同；同 ASIN 两条只 helpfulness 不同（`1/2`、`3/4`）；同家族 3 个 ASIN 上同内容；跨家族同内容；第二次运行同输入。 | 分别得到 `D1_exact_same_asin`、`D2_same_asin_variant`（`diff_fields = {helpfulness}`）、`D3_family_copy`；代表记录为最小 `record_no`；`D2` 的逻辑评论 helpfulness 取 `3/4`；跨家族同内容是两条逻辑评论；重跑 `logical_review_id` 不变；重复评论数 = `D1` + `D2` + `D3`。 |
| AC06 | R20–R22 | X1：同一用户同内容分别在两个不相连家族；X2：同一用户在同一家族内两条正文词集 Jaccard 0.93；X3：同上但 0.6；X4：同一用户在同一统计组的两部作品上各一条 Jaccard 0.95。 | X1 一组 `cross_family_same_content`；X2、X4 各一对 `near_duplicate`；X3 无候选；四例的逻辑评论数与映射与无候选时相同。 |
| AC07 | R26–R28 | 作品 M1 有单品 A1、A2（同家族）；合集 C1 含 M1 与 M2；逻辑评论 L1 在 A1、A2；L2 只在 C1；L3 在 C1 与 A1；L4 在剧集商品；L5 在未采集商品。 | 商品层 A1、A2 各 1 条 L1；作品 M1 归属 L1、L3 各一次；L2 不归属任何作品，只在 C1 商品计数；M2 无归属；L4、L5 只在商品与类别统计。 |
| AC08 | R29–R32 | M1、M2 为 `likely_duplicate_movie`；M3、M4 共享 1 条逻辑评论；M5 独立；各作品评论评分已知，含一对近似评论候选。 | 统计组 `{M1,M2}`、`{M3,M4}`、`{M5}`；组内共享评论只计一次；按 `movie_id` 统计时列出共享数；去重前后平均分与手算一致；近似候选各计一次，敏感性数字与手算一致；任务 4、7 的表未改。 |
| AC09 | R33–R35 | 同输入运行两次；改家族阈值再运行；改 `cv` 版本再运行。 | 第二次为 `reused` 且业务行不变；改阈值重算 `review_family` 及下游；改 `cv` 只重算 `review_attribution`；各生成新版本；`report diff` 列出变化；旧版本可查。 |
| AC10 | R36 | 批大小与分区数缩小；分别在 `review_clean`、`review_family`、`review_dedup` 第 2 批提交前注入故障后重跑；另启动并发运行。 | 故障后只有已提交批次落库；重跑跳过已完成批次，结果与一次跑完一致；家族表没有半写入状态；并发运行因拿不到咨询锁确定性失败。 |
| AC11 | R37, R38 | 检查 DAG 拓扑与参数；成功、重跑复用、上游 `cv` 缺失、`verify` 注入失败后重跑；用 `compose.e2e.yaml` 经真实 scheduler 验证。 | 9 个节点按序连接，`finalize_report`、`gate` 为 `all_done`，重试上限 2；上游缺失时 `check_inputs` 失败且只尝试 1 次；`verify` 失败时运行 `failed`、版本 `unverified`，重跑在原版本上重写后成功。 |
| AC12 | R42–R44 | 夹具运行后执行 `report export` 与四个 `query` 子命令。 | `summary.json` 计数与数据库一致；`examples.md` 覆盖 R44 全部类别并附定位；`query review` 对有效、隔离、不完整记录都返回状态；`stats` 输出口径说明；导出无正文全文，哈希一致。 |
| AC13 | R45–R49 | 夹具含开发池与评价池；执行 `evaluate freeze`、`export`、`draft`、`import`、`metrics`；导入含缺外部依据的改判项。 | 开发池用户被排除；六层配额与补足顺序正确；盲测表不泄露层次与候选类型；草稿不读 `task8` 结论表；缺依据的改判被拒；指标与手算一致，分母为 0 记“无定义”。 |
| AC14 | R09, R12, R21, R24 | 单元测试：内容键构造与核验、签名合并与 Jaccard 连通分量、词集 Jaccard、ID 生成。 | 全部纯函数与边界案例通过（空正文、单条评论家族、并列、Unicode 正文）。 |
| AC15 | R41, R01–R38 | 在业务库副本经全链路运行 `rd0001`，执行 `etl_task8.check` 并与 design 试算基线对照。 | 运行 `succeeded`，`check` 退出码 0；逻辑评论数、重复数、家族数与试算的差异逐项解释；演练库用完删除，`etl_movies` 未写入。 |
| AC16 | R01–R44 | 正式库先冻结开发池，再经 Airflow 全量运行生成 `rd0001`，同参重跑，执行 `python -m etl_task8.check`。 | 7,911,696 条原始记录各有一行映射；有效 + 隔离 = 读取；重复数 = 有效键完整记录数 − 逻辑评论数；作品评分不含合集、统计组内不重复计数；任务 1–7 主表行数不变；`check` 退出码 0、`"problems": []`；重跑为 `reused`。 |
| AC17 | R45–R49 | 正式库导出 120 项盲测表与 AI 草稿，经付林轩审核后导入并计算指标。 | 配额符合 R46；导入 120 条标注；输出总体与分层指标；报告说明工具、人工审核方式与局限。 |
| AC18 | R39, R40 | 检查 `public.schema_migration`、全量测试、`git ls-files` 与 `src/etl_task8` 写语句。 | 迁移为 `001,101,102,103,201,301,302,401,501,601,701`；全量测试通过且 1,121 项不减少；写语句目标全部在 `task8`；仓库不含 `.env`、`outputs/`、评价表或原始数据。 |

## 假设

| 编号 | 当前选择或假设 | 状态与理由 | 相关规则 |
|---|---|---|---|
| H01 | **[用户确认，2026-11-03]** 去重分层：同 ASIN 重复（`D1`、`D2`）与评论家族内跨版本复制（`D3`）合并；不同家族的同内容只标候选（`C1`）。 | 试算：评论集合完全相同的 ASIN 156,915 个、覆盖 705.3 万行；家族 Jaccard 双峰分布；按家族合并约 190.9 万条逻辑评论，不看家族约 190.3 万条（会把 4,204 组跨家族同内容并掉）。 | R11–R20 |
| H02 | **[用户确认，2026-11-03]** 同一用户对同一作品的相似评论不合并，Jaccard ≥ 0.9 只标 `near_duplicate`，评分各计一次并给敏感性数字。 | 试算：电影类商品上 1,811 个“用户 × 作品”组合有多条不同正文，2,374 对中 756 对 ≥ 0.9。 | R21、R22、R31 |
| H03 | **[用户确认，2026-11-03]** 作品评分不包含合集评论；合集评论只在商品层统计并单列；同时在合集与单品上的逻辑评论按单品归属。 | 沿用任务 4“合集评论不复制给每部电影”。试算：只在合集上的逻辑评论 13,994 条，合集与单品都有 1,827 条。 | R27、R28、R30 |
| H04 | **[用户确认，2026-11-03]** `likely_duplicate_movie` 与“同一逻辑评论归属多部作品”只调整统计口径（评分统计组），目标层 `movie_id` 不合并。 | 试算：95 对 `likely_duplicate_movie` 中 66 对共享 3,859 条逻辑评论；15,015 条逻辑评论归属 2 部以上作品，按 `movie_id` 直接求和多计 16,475 次；统计组 259 个、涉及 541 部作品。 | R29、R30 |
| H05 | **[工程默认]** 家族阈值 Jaccard 0.5、近似阈值 0.9；只在开发池上调整，评价表导出前冻结。 | 试算家族对 Jaccard 分布双峰：≥ 0.9 的 825 对共享 19.2 万组，< 0.1 的 15,473 对多数只共享 1 组。 | R12、R21、R45 |
| H06 | **[工程默认]** 隔离记录不重新解码、不进入去重与评分，只作为异常评论计数。 | 隔离 62,131 条（`invalid_utf8` 62,085），约占读取数 0.79%；重新解码会改变任务 1 的划分，超出本任务范围。 | R05 |
| H07 | **[工程默认]** `weak_support` 家族（最多 2 条评论）照常按家族合并，单列报告并在评价中抽查。 | 试算：评论集合相同且只有 1 条评论的组 7,359 个（18,182 个 ASIN）、2 条的 4,679 个（11,979 个 ASIN）；无法从评论集合区分平台复制与转贴。 | R13、R46 |
| H08 | **[待验证]** design 中的探查数字是 2026-11-02 至 03 的规划试算（简化脚本），实现后以正式库实测替换并注明差异。 | 试算未逐项核验完整原值，也未排除不完整记录。 | R42、AC15、AC16 |
