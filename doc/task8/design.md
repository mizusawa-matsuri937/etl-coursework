# 任务 8 设计思路（评论清洗与重复识别）

状态：2026-11-03 编写计划（T8-00），用户确认 H01–H04（见 [spec](spec.md)）。需求、可观察行为与 AC 以 spec 为准；本文说明分层、表结构、算法、阶段依赖、编排、演练、评价设计与资源估算。阈值是**规划默认值**（规则版本 `t8r1`），只在开发池上调整，评价表导出前冻结。正文数字除特别注明外都是 **2026-11-02 至 03 规划试算**：正式库只读查询加仓库外的简化脚本（只比较 MD5、未排除 12 条不完整记录），不是实测，实现后以实测替换（H08）。

## 整体思路与分层

任务 1 只做了“每条源记录一行”的装载，明确不做逻辑去重（任务 1 R12）。探查发现评论文件里的重复主要不是用户重复提交，而是 **Amazon 把同一条评论展示在同一商品家族的各个版本上**（同一部片的 VHS、DVD、蓝光等），SNAP 抓取时每个 ASIN 各存了一份。McAuley 维护的 Amazon 数据集说明也指出，重复来自 Amazon 把近乎相同的商品（例如同一部电影的 VHS 与 DVD）的评论合并展示。所以设计顺序是**先识别家族，再在家族内去重，最后按口径归属与统计**：

1. **清洗**：判定 helpfulness 状态、键是否完整，生成内容键哈希（暂存层）。
2. **家族**：按 ASIN 之间评论集合的重叠识别评论家族（集成层）。只有在同一 ASIN 或同一家族内，“同用户、同日、同评分、同摘要、同正文”才说明是同一条评论被复制；跨家族的同文本多是同一用户在不同商品上贴的泛泛评价（例如同一人在 324 个商品上贴的 “BLU-RAY VERSION IS WORTH BUYING…”），不能合并。
3. **去重**：在 ASIN 或家族内按内容键分组、逐项核验原值，形成逻辑评论与 `ReviewMapping`；跨家族同内容与近似评论只记候选（集成层到目标层）。
4. **归属与统计**：逻辑评论 → 商品（全部副本）→ 作品（只经 `edition` 单品）→ 评分统计组（目标层），给出去重前后两套数字。

| 层次（第八部分） | 任务 8 的对象 | 来源或产物 |
|---|---|---|
| 原始层 | 源文件、记录定位与原始字节 | `movies.txt.gz`；`public.review_record`（`source_review_id`、`record_no`、行号、字节区间、`raw_sha256`）、`public.quarantine_record` |
| 暂存层 | 结构化字段、异常、清洗键 | `public.review_staging`、`public.quality_issue`（任务 1，只读）；`task8.review_clean`、`task8.review_issue` |
| 集成层 | 评论家族、去重候选与核验 | `task8.asin_signature`、`task8.signature_overlap`、`task8.review_family`、`task8.family_member`、`task8.dup_candidate` |
| 目标层 | `Reviewer`、`Review`（逻辑评论）、`ReviewMapping`、归属与统计 | `task8.reviewer`、`task8.logical_review`、`task8.review_mapping`、`task8.logical_review_work`、`task8.stat_group`、`task8.stat_group_member`、`task8.product_review_stats`、`task8.movie_review_stats`、`task8.stat_group_review_stats` |

## 正式库只读探查基线（规划试算）

探查限定抽取 `f1e8b845…`、`t3r2`、`cv0003`；每条 `psql` 命令都带 `SET default_transaction_read_only = on;`。脚本和中间文件在仓库外的临时目录，不入 Git。

### 记录与清洗

| 维度 | 试算 | 设计应对 |
|---|---|---|
| 原始记录 | 7,911,696 条：有效 7,849,565、隔离 62,131（`invalid_utf8` 62,085、`unknown_line` 34、`unknown_line` + 缺产品 ID 12）；可识别 ASIN 的 7,911,684 条 | R03、R05：每条一行映射；隔离不进入去重 |
| helpfulness | `ok` 5,869,509；`0/0` 1,980,001；分子大于分母 43；负数 0；缺失 12 | R06、R07；负数为 0 仍保留规则与测试 |
| 不完整记录 | 评分、时间、正文、摘要、helpfulness 同时缺失 12 条（任务 1 的 `missing_field`） | R08：`incomplete`，不形成逻辑评论 |
| 时间 | 全部是 UTC 零点（日粒度），1997-08-20 至 2012-10-25 | R09：比较用评论日；“同一时间”即同一天 |
| 用户 | 有效记录中用户 886,308 个，没有空用户和 `unknown` 字面量；1,294 个用户有多个显示名 | R10、R25 |

### 不同键组合的重复规模（有效记录，按 MD5 分组）

“组”指键相同且至少 2 行的组，“多余行”为组内行数减 1 之和。

| 键 | 组 | 组内行 | 多余行 | 最大组 |
|---|---|---|---|---|
| 八字段全同（ASIN、用户、时间、评分、摘要、正文、显示名、helpfulness） | 4,465 | 9,499 | 5,034 | 7 |
| ASIN + 用户 + 时间 + 评分 + 摘要 + 正文 | 12,281 | 26,214 | 13,933 | 15 |
| ASIN + 用户 + 时间 + 评分 + 正文 | 13,656 | 29,154 | 15,498 | 15 |
| ASIN + 用户 + 时间 + 正文 | 13,812 | 29,534 | 15,722 | 15 |
| ASIN + 用户 + 正文 | 18,978 | 40,930 | 21,952 | 15 |
| ASIN + 用户（不看正文） | 66,682 | 145,493 | 78,811 | 117 |
| 用户 + 时间 + 评分 + 正文（不看 ASIN） | 1,230,803 | 7,182,172 | 5,951,369 | 341 |
| 用户 + 正文（不看 ASIN 与时间） | 1,229,909 | 7,183,398 | 5,953,489 | 341 |

含 ASIN 的键只解释约 1.4 万多余行；去掉 ASIN 后多余行达 595 万，是重复的主体。

### 评论家族

| 维度 | 试算 | 设计应对 |
|---|---|---|
| 评论集合完全相同的 ASIN | 46,011 组、156,915 个 ASIN，覆盖 7,053,232 行（有效记录的 89.9%），组内最大 46 个 ASIN | R12：先按签名合组 |
| 组间 Jaccard（共享内容键的签名组对；这一行的内容键尚未含摘要） | `[0, 0.1)` 15,473 对（14,081 对只共享 1 组）、`[0.1, 0.5)` 2,392 对、`[0.5, 0.9)` 446 对、`[0.9, 1]` 825 对（共享 191,916 组）；双峰 | H05：阈值 0.5 落在两峰之间 |
| 内容键含摘要后，不同阈值的跨家族同内容 | 只按相同集合：113,082 组、1,069,328 行；Jaccard ≥ 0.9：20,728 组；≥ 0.8：11,669；**≥ 0.5：4,204 组、33,982 行**；≥ 0.2：3,172 | R12、R20：取 0.5 |
| 家族数（阈值 0.5） | 140,587 个，其中多 ASIN 家族 45,956 个 | R24 |
| `weak_support` | 评论集合相同且只有 1 条评论的组 7,359 个（18,182 个 ASIN），2 条的 4,679 个（11,979 个 ASIN） | R13、H07 |
| 逻辑评论数 | 阈值 0.5：约 1,908,873 条；不看家族：约 1,902,607 条；只合并同 ASIN：约 783 万条 | H01 |
| 跨家族同内容实例 | 同一用户在不同季的剧集、不同电影上贴 “Order for a friend”、“We love it!” 等；324 个商品上的 “BLU-RAY VERSION…” | R20：不合并 |
| 摘要的作用 | 同一用户同正文不同摘要（`KHAN!!!`、`Star Trek IV`、`Star Trek VI`）分别对应不同电影 | R09、R17：摘要进内容键 |
| 核验差异（用户 + 时间 + 评分 + 正文分组） | 跨 ASIN 且每个 ASIN 一条的组 1,228,116 个中，摘要不同 2,611、helpfulness 不同 5,736、显示名不同 1,299，三项全同 1,220,270；只在同一 ASIN 上重复的组 486 个中 helpfulness 不同 321 | R15–R17：只允许这两项不同 |

### 归属与评分

| 维度 | 试算 | 设计应对 |
|---|---|---|
| 商品类别（`t3r2` 与 `cv0003`）的原始记录（有效 / 隔离） | 电影 8,730 个 ASIN：298,769 / 2,557；有作品的合集 136 个：1,759 / 16；无作品的合集 724 个：14,482 / 110；剧集 835 个：29,298 / 135；演唱会或其他 3,256 个：39,055 / 251；非电影 17 个：564 / 5；已采集但待确认 15,428 个：493,088 / 3,892；未采集 223,933 个：6,972,550 / 55,153 | R26–R28 |
| 电影单品上的逻辑评论 | 原始 298,769 行 → 归属作品的逻辑评论 257,733 条；542 部作品的评论数因去重变化，平均分变化 ≥ 0.1 的 29 部、最大 0.667 | R30、R32 |
| 跨作品共享 | 233 个家族的单品映射到 2–4 个 `movie_id`（58 个全是 `likely_duplicate_movie`）；15,015 条逻辑评论归属 2 部以上作品，按 `movie_id` 求和为 274,208（多计 16,475）；抽查几乎都是任务 4 没合并的同一部片（`Amadeus`、`My Fair Lady`、`Mission: Impossible II`） | R29 |
| `likely_duplicate_movie` | 95 对：两边都有评论 89 对，其中 66 对共享 3,859 条逻辑评论；这些作品上的逻辑评论 9,050 条 | R29 |
| 评分统计组 | 由 95 对与 273 个“共享评论的作品对”（其中 66 对两者都是）连通：多作品组 259 个、涉及 541 部作品，最大 4 部；按组计数时每条逻辑评论只计一次（257,733） | R29、R30 |
| 合集 | 只在合集上的逻辑评论 13,994 条；同时在合集与单品上的 1,827 条 | R27、H03 |
| 近似评论 | 1,811 个“用户 × 作品”组合有多条不同正文；2,374 对中 Jaccard ≥ 0.9 的 756 对（750 对在同一 ASIN 上），0.7–0.9 的 318 对；全库同用户同 ASIN 不同正文 50,282 组（110,627 行，含家族复制） | R21、H02 |

## 开源参考

按 `CLAUDE.md` 要求，设计前对比了已有做法：

| 项目或资料 | 借鉴 | 不照搬的原因 |
|---|---|---|
| [McAuley 的 Amazon 评论数据集说明](https://cseweb.ucsd.edu/~jmcauley/datasets/amazon/links.html) | 确认重复来自 Amazon 合并近似商品的评论；提供“去重”与“激进去重”两种版本 | 只给出处理后的文件，没有公开判定方法；“激进去重”会把不同用户的相同文本也删掉，违背“原始记录全部保留”，也超出本任务的同用户范围 |
| [datasketch](https://github.com/ekzhu/datasketch)（MinHash、MinHash LSH） | 用词集 Jaccard 衡量近似文本；大规模时用 LSH 找候选 | 近似比较只发生在同一用户、同一家族或统计组内（试算约 2,400 对），直接算精确 Jaccard 更简单，结果也可复现；不引入新依赖 |
| [dedupe](https://github.com/dedupeio/dedupe) / recordlinkage | “分块（blocking）→ 比较 → 聚类”的流程 | 本任务的分块键就是内容键与家族，比较是逐项相等核验，不需要训练式模型 |

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/701_task8_schema.sql` | schema `task8` 全部表、约束与迁移登记 |
| `src/etl_task8/rules.py` | 纯函数与 `RULESETS`（`t8r1`）：helpfulness 状态、内容键与核验、签名、Jaccard 连通分量、词集 Jaccard、ID 生成 |
| `src/etl_task8/sources.py` | 上游只读取数（任务 1、`t3r2`、`cv0003`），全部带版本条件 |
| `src/etl_task8/clean.py` | `review_clean` 阶段：写 `review_clean`、`review_issue` |
| `src/etl_task8/family.py` | `review_family` 阶段：签名、签名组重叠、连通分量，写家族表 |
| `src/etl_task8/dedup.py` | `review_dedup` 阶段：分组、核验、逻辑评论、映射、`Reviewer`、候选 |
| `src/etl_task8/attribution.py` | `review_attribution` 阶段：作品归属、统计组、三类统计表 |
| `src/etl_task8/run.py`、`runner.py`、`versions.py` | 迁移、输入键与阶段键、审计、咨询锁、分批提交与恢复、版本物化与差异 |
| `src/etl_task8/report.py`、`query.py`、`check.py` | 报告与实例（R42、R44）、四个只读查询（R43）、只读核对 |
| `src/etl_task8/evaluate.py` | 开发池、盲测表、AI 草稿、导入、指标（R45–R49） |
| `src/etl_task8/pipeline.py`、`dags/amazon_reviews_task8_dedup.py` | DAG 步骤与 9 节点定义（R37） |
| `scripts/seed_task8_e2e.py` | 真实 scheduler 验证所需的 `etl_test` 合成数据 |
| `tests/fixtures/task8/`、`tests/unit/test_task8_rules.py`、`tests/integration/test_task8_*.py` | 夹具与测试 |

## 数据模型（schema `task8`，迁移 `701`）

大表按**阶段键**保存（同一阶段键的结果被多个版本复用），只有小表按**去重版本**物化，避免任务 7 那样每个版本整套复制（`task7` 三个版本约 1,408 MB）。

| 作业逻辑实体 | 表 | 主键与关键列 |
|---|---|---|
| 暂存（清洗键） | `review_clean` | `(extraction_id, clean_stage_key, record_no)`；`source_review_id`、`asin`、`reviewer_id`、`review_day`、`score`、`summary_md5`、`text_md5`、`content_hash`（`bytea`，对五项原值的 SHA-256）、`helpfulness_status`、`helpful_votes`、`total_votes`、`profile_md5`、`key_complete` |
| `QualityIssue` | `review_issue` | `(extraction_id, stage_key, issue_key)`；`record_no`、`code`（`numerator_exceeds_denominator`、`negative_votes`、`unparseable_helpfulness`、`missing_helpfulness`、`incomplete_review`、`hash_collision`）、`evidence`、`rule_version` |
| 家族（集成） | `asin_signature` | `(extraction_id, family_stage_key, asin)`；`signature`、`review_count` |
| 家族（集成） | `signature_overlap` | `(extraction_id, family_stage_key, partition_no, sig_a, sig_b)`；`shared`（分区内部分计数，按分区写入以便恢复） |
| 家族（集成） | `review_family`、`family_member` | 前者 `(extraction_id, family_stage_key, family_id)`：ASIN 数、最大评论集合、`weak_support`、合并依据（签名组对与 Jaccard）；后者每个 ASIN 一行 |
| `Review`（逻辑评论） | `logical_review` | `(extraction_id, dedup_stage_key, logical_review_id)`；`reviewer_id`、`review_day`、`score`、`content_hash`、`family_id`、`representative_record_no`、`copy_count`、`asin_count`、`helpful_votes`、`total_votes`、`helpfulness_record_no` |
| `ReviewMapping` | `review_mapping` | `(extraction_id, dedup_stage_key, record_no)`；`source_review_id`、`asin`、`logical_review_id`、`mapping_status`（`mapped`、`quarantined`、`incomplete`）、`match_level`、`diff_fields text[]`、`rule_version` |
| `Reviewer` | `reviewer` | `(extraction_id, dedup_stage_key, reviewer_id)`；`profile_names jsonb`（显示名 → 次数）、`record_count`、`logical_count`、`first_day`、`last_day` |
| 候选 | `dup_candidate` | `(extraction_id, dedup_stage_key, candidate_id)`；`candidate_type`（`cross_family_same_content`、`near_duplicate`）、`logical_review_ids text[]`、`similarity`、`basis jsonb` |
| 作品归属 | `logical_review_work` | `(attribution_stage_key, logical_review_id, movie_id)`；`via_asins text[]`、`also_on_collection` |
| 评分统计组 | `stat_group`、`stat_group_member` | 组 ID（`sg_` + 组内最小 `movie_id` 的哈希）、成员、连边依据（`likely_duplicate_movie` 或 `shared_logical_review` 及共享数） |
| 统计 | `product_review_stats`、`movie_review_stats`、`stat_group_review_stats` | 按 `(review_dedup_version_id, 实体)`：原始有效数、隔离数、逻辑评论数、有评分逻辑评论数、平均分（去重前后）、共享数、近似候选数 |
| `ETLRun` | `run`、`run_batch`、`entity_stage`、`dedup_version` | 审计、恢复、版本；`dedup_version` 记录输入键、四个阶段键、规则版本、状态（`unverified`/`verified`）与统计 |
| 评价 | `dev_pool`、`eval_set`、`eval_item`、`eval_label` | R45–R49 |

约束要点：`review_mapping` 中 `(mapping_status = 'mapped') = (logical_review_id IS NOT NULL)`，`mapping_status = 'mapped'` 时 `match_level` 非空；`match_level = 'representative'` 的行数等于逻辑评论数（`check` 核对）；`helpfulness_status` 与 `candidate_type` 用 CHECK 限定为闭集；`logical_review_work.movie_id` 只能来自 `cv0003` 的 `product_kind = 'movie'` 商品（`check` 核对）。

### 输入键与阶段键（R33、R34）

- `input_key`：抽取 | `t3r2` | `cv0003` | 规则版本。
- 阶段键（下游包含上游）：
  - `clean_stage_key`：抽取 | 任务 1 规则版本 | `clean_rule_signature`
  - `family_stage_key`：`clean_stage_key` | `family_rule_signature`（Jaccard 阈值、`weak_support` 上限）
  - `dedup_stage_key`：`family_stage_key` | `dedup_rule_signature`（核验允许字段、代表记录与 helpfulness 选择、近似阈值）
  - `attribution_stage_key`：`dedup_stage_key` | `t3r2` | `cv0003` | `cv0003.link_stage_key` | `attribution_rule_signature`
- 吸取任务 7 的教训（交接第 6 节第 4 条）：凡是改变结果的算法改动都升规则版本并进入对应签名，不另设不进键的 `ALGORITHM` 标记。

## 各阶段算法

### 1. `review_clean`（R06–R09）

按 `record_no` 范围每批 500,000 条读取 `review_record` 左连 `review_staging`：

- 有效记录：由 `helpfulness_raw` 与任务 1 规范值判定 `helpfulness_status`（`8/1` 这类分子大于分母在任务 1 已置 `NULL`，这里再按原值分类；负数由原值的 `-` 判定）；`key_complete = score、review_time_unix、review_text_raw 都非空`；`content_hash = sha256(reviewer_id ‖ review_day ‖ score ‖ summary_raw ‖ review_text_raw)`，各字段以长度前缀拼接，避免分隔符歧义。
- 隔离记录不进 `review_clean`，在 `review_dedup` 直接写 `quarantined` 映射。
- 异常写 `review_issue`；`0/0` 不写。批结果与 `entity_stage`、`run_batch` 同事务提交。

### 2. `review_family`（R12、R13）

1. **签名**：按 ASIN 哈希分区，对每个 ASIN 取其键完整记录的 `content_hash` 排序后求 SHA-256，写 `asin_signature`。
2. **重叠计数**：按 `reviewer_id` 哈希分区（内容键包含用户，同一内容键必在同一分区），对每个出现在 ≥ 2 个签名组的内容键，枚举签名组对并计数，写 `signature_overlap`（每个分区一个事务）。试算共约 1.9 万个签名组对。
3. **连通分量**：在 Python 中汇总各分区计数，`J = shared / (n_a + n_b − shared)`，`J ≥ 0.5` 时并查集合并；家族 ID 按 R24 由家族内最小 ASIN 生成；`review_family`、`family_member` 在一个事务里写入（全量约 25 万行），避免半写入状态。
4. 无键完整记录的 ASIN 不进家族；它们的记录都是隔离或不完整，不需要家族。

### 3. `review_dedup`（R10、R14–R25）

按 `reviewer_id` 哈希分 32 个分区，每个分区一个事务：

1. 读取分区内 `review_clean` 与 `family_member`，按 `(family_id, content_hash)` 分组。
2. **核验**：对每组读取原值，逐项比较用户、评论日、评分、摘要、正文；完整原值不同（哈希碰撞）时拆组并写 `hash_collision`。组内按 ASIN 与 helpfulness、显示名比较定层次：代表记录 `record_no` 最小；与代表同 ASIN 且八字段全同为 `D1`，同 ASIN 有差异为 `D2`，其他 ASIN 为 `D3`；`diff_fields` 只能是 `helpfulness`、`profile_name` 的子集。
3. 写 `logical_review`、`review_mapping`、按用户汇总的 `reviewer`。
4. **候选**：同一用户内，`content_hash` 相同但 `family_id` 不同的逻辑评论 → `cross_family_same_content`；同一用户、同一家族内正文不同的逻辑评论两两算词集 Jaccard（小写、`[a-z0-9']+` 分词），≥ 0.9 → `near_duplicate`。统计组内（跨家族）的近似对要用到作品归属，在 `review_attribution` 补算，同样写入 `dup_candidate`（以归属阶段键区分）。
5. 隔离与不完整记录的映射行在第 0 批写入。

### 4. `review_attribution`（R26–R32）

1. 作品归属：`review_mapping` 连 `cv0003.edition_resolved`（`product_kind = 'movie'`），每条逻辑评论按副本 ASIN 得到 `movie_ids[1]` 集合；副本同时在合集上时标 `also_on_collection`，合集本身不产生作品归属。
2. 统计组：作品对来源为 `shared_tconst_result` 中的 `likely_duplicate_movie`（限定 `cv0003.link_stage_key`）与“同一逻辑评论归属两部作品”；并查集求连通分量。
3. 统计表（按版本物化）：
   - `product_review_stats`：每个 ASIN 的原始有效数、隔离数、逻辑评论数（`COUNT(DISTINCT logical_review_id)`）、去重前后平均分、商品类别（`t3r2`）、是否有作品。
   - `movie_review_stats`：每部作品的逻辑评论数、平均分、与同组其他作品共享的逻辑评论数、近似候选数，以及去重前（单品原始行）计数与平均分。
   - `stat_group_review_stats`：组内 `COUNT(DISTINCT logical_review_id)` 与平均分。

### 评分查询口径

第十部分第 5 个查询由 `python -m etl_task8.query stats --by product|movie|group` 实现，核心语句如下（版本与阶段键由 `dedup_version` 给出）：

```sql
-- 按作品（去重、不含合集、分母 = 有评分的逻辑评论数）
SELECT w.movie_id,
       count(DISTINCT l.logical_review_id)                          AS logical_reviews,
       count(DISTINCT l.logical_review_id) FILTER (WHERE l.score IS NOT NULL) AS rated,
       avg(l.score)                                                 AS avg_score
FROM task8.logical_review_work w
JOIN task8.logical_review l USING (logical_review_id)
WHERE w.attribution_stage_key = :attr AND l.dedup_stage_key = :dedup
GROUP BY w.movie_id;
```

`logical_review_work` 的主键保证同一逻辑评论在同一作品只出现一次，`avg` 的分母就是有评分的逻辑评论数。按统计组时把 `w.movie_id` 换成组 ID 并对 `logical_review_id` 去重，同一条评论在组内只计一次。输出固定附三行口径：①去重后，每条用户评论在同一作品 / 组内只计一次；②不含合集商品上的评论（合集只在按商品统计中出现）；③按 `movie_id` 统计时，`shared_with_group` 列给出与同组其他作品共享的评论数，按 `movie_id` 求和不等于逻辑评论总数。

## Airflow 编排与幂等恢复（R33–R38）

```text
check_inputs → review_clean → review_family → review_dedup → review_attribution
            → finalize_version → verify → finalize_report → gate
```

- `check_inputs`：确认抽取 `complete`、`t3r2` 分类完整、`cv0003` 为 `verified`、迁移 `701` 已应用（未应用则应用）；上游缺失属确定性错误。
- 每个阶段先算阶段键：已完成且所属运行已 `verify` 的阶段直接复用；否则从第一个未完成批次继续（`entity_stage` 记录每批完成状态）。
- `finalize_version` 在一个事务里写 `dedup_version`（`unverified`）与按版本物化的统计表；`verify` 运行 `check` 的版本级检查，通过后标 `verified`、运行 `succeeded`。
- 同输入同规则：`input_key` 命中已 `verified` 的版本时只写 `reused` 审计，不碰业务表。
- 每个步骤持 `pg_try_advisory_lock(hashtext('task8:' || extraction_id))`，拿不到即确定性失败。
- 确定性错误映射为 `AirflowFailException`（不重试）；数据库断连等暂时错误最多重试 2 次；`finalize_report`、`gate` 用 `all_done`，`gate` 在任一上游失败时失败。

## 业务库副本演练（R41）

任务 7 的教训是合成夹具会掩盖真实数据缺陷（交接第 6 节第 1 条）。每个实现阶段（T8-01 至 T8-05）完成后、T8-08 正式运行前，都在业务库副本上全链路运行并核对：

1. 停止会连接 `etl_movies` 的 Airflow 服务（`CREATE DATABASE … TEMPLATE` 要求源库没有其他连接）：`docker compose stop airflow-scheduler airflow-dag-processor airflow-apiserver`。
2. `docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE etl_t8_rehearsal TEMPLATE etl_movies OWNER etl_app"`（约 30 GB，磁盘剩余 58 GB，副本建好后约剩 28 GB），随后恢复 Airflow。
3. 以环境变量 `ETL_DB_NAME=etl_t8_rehearsal` 覆盖 `crawler` 服务的库名（`docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler …`）运行 `python -m etl_task8.runner` 全链路与 `check`，对照本文试算基线逐项解释差异，并抽查各层次与候选实例。
4. 结束后 `DROP DATABASE etl_t8_rehearsal`。演练中发现的缺陷先补夹具与测试，再改实现。

## 夹具设计（R40）

- 夹具直接写上游表，形状与真实输出相同：`public.source_file`、`extraction`、`etl_run`、`etl_batch`、`review_record`、`review_staging`（全部原值与规范值列，满足任务 1 的 CHECK 与延迟触发器）、`quarantine_record`、`quality_issue`；`task2.product_manifest`、`task3.product_classification`（`t3r2`，`input_group` 与 `category` 组合取真实存在的组合）；`task7.conflict_version`、`edition_resolved`（`movie_ids` 为数组，合集可为空数组）、`shared_tconst_result`（带 `stage_key`）。
- 夹具的评论正文含真实数据里出现过的形态：HTML 链接、多行正文、只差摘要的同正文、Unicode 字符、`0/0` 与 `8/1`。
- 每类 AC 的场景（AC01–AC13）各自构造最小世界，独立初始化 `etl_test`。

## 盲测评价设计（R45–R49）

作业对评论去重没有要求 Precision/Recall，但合并决定会影响“重复评论数量”与评分，需要抽样核查。

- 开发池：`md5(reviewer_id)` 排序前 20% 的用户，首次全量运行前冻结；阈值只在开发池上调整。
- 120 项六层样本（R46），抽样时排除开发池用户；每项是一对原始记录或一对逻辑评论（`V5` 是一对作品）；同层内按 `md5(item_key)` 排序取前 N 项。
- 盲测表只含两侧原始字段，按 `md5(item_key)` 全局打乱；AI（Claude 子代理）只读盲测表出草稿，草稿不读 `task8` 结论表；付林轩逐项审核，存疑或改判须给外部依据；报告如实写“AI 预填、付林轩审核”，不写成独立手工标注。
- 指标见 R48；`V4` 的 `same` 比例只说明“近似评论里有多少是同一意见的重贴”，不当作错误率。

## 资源预算与估算

| 项目 | 估算或预算 | 依据 |
|---|---|---|
| 全量运行耗时 | 预算 ≤ 40 分钟 | 探查中整表按 MD5 分组每次约 1 分钟（`review_staging` 14 GB），签名与重叠计数约 1.5 分钟；按批写入另计 |
| `review_clean` | 约 7.85M 行 × 约 150 B ≈ 1.2 GB（含索引） | 每行约 12 列，正文只存哈希 |
| `review_mapping` | 约 7.91M 行 × 约 130 B ≈ 1.0 GB（含索引） | 每条原始记录一行 |
| `logical_review`、`reviewer`、家族表 | 约 1.91M + 0.89M + 0.5M 行 ≈ 0.6 GB | |
| 统计与归属表 | < 100 MB | 25 万 ASIN、8,659 部作品 |
| `task8` 合计 | 首个版本预算 ≤ 4 GB；只改归属口径的新版本增加 < 100 MB | 大表按阶段键复用 |
| 内存 | 进程 RSS 预算 1 GiB；并查集约 25 万节点 | 每个分区约 25 万行 |
| 磁盘 | 当前剩余 58 GB；演练库副本约 30 GB，用完删除 | `df` 2026-11-03 |

以上都是估算和预算，不是性能实测；实测写入 T8-07、T8-08 的完成情况。

## 异常与恢复处理

| 情况 | 处理 |
|---|---|
| 上游版本缺失或未完成（抽取、`t3r2`、`cv0003`） | `check_inputs` 确定性失败，不重试 |
| 哈希碰撞 | 核验拆组，写 `hash_collision`，不合并 |
| 批次中途故障 | 当前批事务回滚；重跑从第一个未完成批次继续 |
| 家族连通分量写入中断 | 家族表在单事务中写入，失败则全部回滚，重跑重新汇总已提交的分区计数 |
| `verify` 失败 | 运行 `failed`、版本 `unverified`；同输入重跑在原版本上重写，不 `reused` |
| 并发运行 | 咨询锁拿不到即失败 |
| 规则或阈值改动 | 升规则版本，进入对应阶段签名，只重算受影响阶段及下游 |

## 实现补充约定（T8-01 起）

以下是实现时对上文的工程层面补充，不改变 spec 的规则与用户确认的 H01–H04；与上文表述不同处以本节为准。

- **阶段键短键**：大表（`review_clean`、`review_issue`、`asin_signature`、`signature_overlap`、`review_family`、`family_member`、`logical_review`、`review_mapping`、`reviewer`、`dup_candidate`、`logical_review_work` 等）的 `*_stage_key` 列存短键：阶段首字母（`c`、`f`、`d`、`a`）+ 完整阶段串 SHA-256 前 15 位。完整串、所属抽取、上游短键与规则版本登记在 `task8.stage_key`。大表不再重复存 `extraction_id`（短键已由抽取派生），也不对短键建外键，避免 785 万行逐行外键检查；上文数据模型表里“`(extraction_id, clean_stage_key, …)`”的主键因此为“`(clean_stage_key, …)`”。读任务 1 的表时仍全部带 `extraction_id` 条件（R01）。
- **`review_clean` 的列**：按 R02 保留 `source_review_id` 与本阶段批号 `batch_no`；显示名与 helpfulness 存原值 `profile_name_raw`、`helpfulness_raw`（上文写 `profile_md5`），使 D1/D2 的核验按原值比较，`Reviewer` 的显示名统计不必再读 `review_staging`；另存分区桶 `asin_bucket`、`reviewer_bucket`（0–1023，`rules.bucket`）并各建索引，N 个分区时第 p 区取 `bucket % N = p` 的桶。
- **键完整**：`key_complete` 另要求 `reviewer_id` 非空（内容键第一项）；正式库有效记录没有空用户（design 探查），所以不改变计数。`review_day` 取 `review_time_utc` 的 UTC 日期，`review_time_utc` 为空即键不完整。
- **SQL 与 Python 两份判定**：helpfulness 状态、内容键哈希与分区桶在库内批量计算（`rules.helpfulness_status_sql`、`content_hash_sql`、`bucket_sql`），Python 版本供单元测试与哈希碰撞拆组；集成测试逐条核对两者一致（AC02、AC14）。内容键串为五项各自“字符数:值”（NULL 记 `-`）的拼接，评分用 `trim_scale(score)::text`（`3.0` → `3`）。
- **helpfulness 状态判定顺序**与任务 1 解析一致：格式 → 负数（`-0` 不算负数）→ 超出 int64（任务 1 的 `out_of_range`，归 `unparseable`）→ 分子大于分母 → `0/0`。表上 CHECK 保证 `ok`、`zero_zero` 恰好对应任务 1 规范票数非空。
- **批次单元与恢复**：`entity_stage` 每行是一个批次单元而不是一条记录：清洗为记录号区间 `r<首>-<末>`（中断后从最后一个已提交区间之后继续，批大小改变也能接上），家族为 `sig<p>/<N>`、`ovl<p>/<N>` 与 `components`，去重为 `unmapped` 与 `p<p>/<N>`；`__complete__` 与最后一个单元同事务写入。已按 N 个分区提交过部分单元时换分区数确定性失败（否则同一用户会落在两个分区重复写入）。
- **分阶段运行**：`python -m etl_task8.runner stages --through <阶段>` 记一条 `kind = 'stage'` 的运行，只读任务 1（`check_extraction`），用于测试与演练；任务 3 与任务 7 的检查在 `check_attribution_inputs`（T8-04 的归属阶段与 `check_inputs` 步骤使用）。`task8.run.conflict_version_id` 因此不建外键，`dedup_version.conflict_version_id` 有外键。
- **去重的核验单元**：`review_dedup` 的批次单元依次为 `unmapped`、`verify`、`p<p>/<N>`。`verify` 一次核验全部多行分组（`(family_id, content_hash)` 相同且多于一行）的完整原值：库内对 `review_staging` 的五项原值取 `min = max`（`COLLATE "C"`，摘要另要求同为空或同非空），原值不同的组把原值取进 Python 拆成子组，每条记录写一条 `hash_collision` 问题（`detail` 含家族、子组数与子组序号）；分区读这些问题得到拆组结果。上文“各阶段算法”第 3 节写的是在每个分区里核验，演练实测每个分区要随机读或顺序扫描一次 `review_staging`（单个分区 35–63 秒，32 个分区约 27 分钟），改为一次核验后为 91 秒。这条查询上规划器把多行分组估成几百行而选嵌套循环（演练中 10 分钟未完成），所以只在该事务里 `SET LOCAL enable_nestloop = off`、`work_mem = 256MB`。
- **D1 与 D2 的比较对象**：同一逻辑评论里，每个 ASIN 上 `record_no` 最小的记录是该 ASIN 的首条；代表记录以外的首条为 `D3`（差异相对代表记录）；同一 ASIN 的其余记录与该 ASIN 的首条比较，helpfulness 与显示名原值全同为 `D1`，否则为 `D2`。这样“同 ASIN 重复”（R14、R15）在家族复制的每个版本上都按同 ASIN 判定；上文“与代表同 ASIN”只覆盖代表记录所在的 ASIN。重复评论数（R19）不受影响。
- **哈希碰撞的 ID**：碰撞拆出的第 2 个起的子组在 R24 的哈希输入末尾加 `|v<序号>`（`logical_review.collision_variant` 记序号），避免两条不同评论同 ID；没有碰撞时 ID 与 R24 完全相同。碰撞组里的逻辑评论不参与 C1 分组（内容键相同不代表内容相同）。演练库碰撞 0。
- **近似评论**：词集用 `[a-z0-9']+`（小写后），非 ASCII 字符作分隔；两边都没有词时相似度为 0，不成候选；同一正文（正文 MD5 相同）的两条逻辑评论不比较（它们因摘要、日期或评分不同而分开，不是“正文不同”）。
- **Reviewer**：`profile_names` 只记显示名非空的记录（JSON 键不能表示缺失），`record_count` 含全部有效记录（含不完整记录）；只有不完整记录的用户 `logical_count = 0`。
- **写入**：分区内流式读取（服务器端游标，每次 20,000 行），按用户处理，写入每满 50,000 行 COPY 一次；测试用 `flush_rows = 2` 与 1 个分区核对结果与 4 个分区相同。
- **迁移 701 一次建好全部表**：归属、统计、版本与评价表（T8-04 至 T8-06 使用）已按上文定义建好；701 尚未在正式库应用，后续阶段如需调整列可直接改 701。

## 业务库副本演练实测（`etl_t8_rehearsal`）

演练库 2026-11-03 由 `CREATE DATABASE etl_t8_rehearsal TEMPLATE etl_movies OWNER etl_app` 建立（停 Airflow 三个服务后建库约 1 分 48 秒，建好后立即恢复；磁盘剩余 58 GB → 28 GB），用 `python -m etl_task8.runner stages` 逐阶段运行。耗时是同机记录，不是性能实测（清洗阶段运行时全量测试也在并行）。

### T8-01 `review_clean`

| 项目 | 试算 | 演练实测 | 说明 |
|---|---|---|---|
| 写入行（有效记录） | 7,849,565 | 7,849,565（16 批，581 秒） | 一致 |
| `ok` / `zero_zero` | 5,869,509 / 1,980,001 | 5,869,509 / 1,980,001 | 一致 |
| 分子大于分母 / 负数 / 无法解析 / 缺失 | 43 / 0 / — / 12 | 43 / 0 / 0 / 12 | 一致 |
| 不完整记录 | 12 | 12（键完整 7,849,553） | 一致；这 12 条同时缺 helpfulness，各有两条问题 |
| `review_issue` | — | 67（43 + 12 + 12） | |
| 用户数 | 886,308 | 886,308 | 一致 |
| 表大小 | 约 1.2 GB（含索引） | 2,778 MB（表 2,213 MB、主键 451 MB、两个桶索引各约 56 MB） | 超出估算：估算时没算 `source_review_id`（64 位十六进制）、两项 MD5、显示名原值与每行开销 |

查询计划：每批 `review_record` 连 `review_staging` 选择了哈希连接（顺序扫描 `review_staging` 约 8 秒）；强制嵌套循环索引查找实测更慢（单批 34.8 秒对 26.4 秒），所以保留默认计划。

### T8-02 `review_family`

| 项目 | 试算 | 演练实测 | 说明 |
|---|---|---|---|
| 有键完整记录的 ASIN | — | 252,603（32 分区，53 秒） | 清单 253,059 个 ASIN 中另 456 个只有隔离或不完整记录，不进家族（上文“各阶段算法”第 2 节第 4 条） |
| 评论集合完全相同的组（≥ 2 个 ASIN） | 46,011 组、156,915 个 ASIN | 45,951 组、156,746 个 ASIN | 试算按 MD5 的简化键分组且未排除 12 条不完整记录；实现按五项原值的内容键（含摘要）去重后求签名 |
| 其中只有 1 条 / 2 条评论 | 7,359 组（18,182）/ 4,679 组（11,979） | 7,326 组（18,085）/ 4,684 组（11,988） | 同上 |
| 共享内容键的签名组对 | 19,136 对（试算这一行的键不含摘要） | 12,230 对 | 摘要进入内容键后，同一用户不同摘要的同正文不再算共享，组对减少 |
| Jaccard ≥ 0.5 的组对 | 1,271 对（同上，不含摘要） | 1,419 条相连边 | 口径不同（试算不含摘要），不可直接比较；分布仍集中在 [0.9, 1]（849 条）与 [0.5, 0.6)（242 条） |
| 家族数 / 多 ASIN 家族 | 140,587 / 45,956 | 140,584 / 45,953 | 差 3 个，来自上面内容键口径的差别 |
| 最大家族 | 组内最大 46 个 ASIN | 57 个 ASIN | 家族由多个签名组按 Jaccard 连通而成，比单个签名组大 |
| `weak_support` | 只统计了多 ASIN 的签名组 | 61,054 个家族：多 ASIN 12,054 个（30,290 个 ASIN）、单 ASIN 49,000 个 | R13 按“家族内最大评论集合 ≤ 2”判定，单 ASIN 家族也会标记；单 ASIN 家族没有跨版本复制，报告只单列多 ASIN 的部分与其复制记录数（T8-05） |

### T8-03 `review_dedup`

演练第一次运行按分区核验（见“实现补充约定”），13 个分区后停止并改为 `verify` 单元；清掉该阶段的行后重跑，下表为重跑结果（34 个单元，388 秒）。

| 项目 | 试算 | 演练实测 | 说明 |
|---|---|---|---|
| 映射行 | 7,911,696 | 7,911,696（`mapped` 7,849,553、`quarantined` 62,131、`incomplete` 12），`record_no` 不重复 | R03 |
| 逻辑评论 | 约 1,908,873 | 1,908,386 | 少 487 条（0.03%）：试算按 MD5 的简化键分组、家族由不含摘要的键算出（上表“签名组对”），且未排除不完整记录；实现按含摘要的内容键在家族内分组并逐项核验原值 |
| `D1` + `D2` | 13,933（“ASIN + 用户 + 时间 + 评分 + 摘要 + 正文”的多余行） | 4,501 + 9,432 = 13,933 | 一致 |
| `D1` | 5,034（八字段全同的多余行） | 4,501 | 试算的八字段全同组按 ASIN 直接比较；实现按“同 ASIN 首条”比较，同一 ASIN 上三条记录 helpfulness 依次为 b、a、a 时，试算的八字段全同组（两条 a）记 1 条多余，实现把两条 a 都与该 ASIN 首条 b 比较而记为 `D2`；两者之和（`D1` + `D2`）一致 |
| `D2` 差异字段 | — | helpfulness 9,428、显示名 2、两者 2 | |
| `D3` | — | 5,927,234（差异：无 5,919,324、helpfulness 5,013、显示名 2,893、两者 4） | 跨版本复制是重复的主体 |
| 重复评论数（R19） | 约 594 万 | 5,941,167 = 7,849,553 − 1,908,386 | |
| 多 ASIN `weak_support` 家族上的复制记录 | — | 25,531 条 `D3` | R13 单列（T8-05 报告） |
| C1 跨家族同内容 | 4,204 组 | 4,202 组 | 差 2 组，原因同逻辑评论数；抽查为同一用户在同一剧集不同季的商品上贴同一段话 |
| C2 家族内近似评论 | 只试算了“用户 × 作品”的 2,374 对、≥ 0.9 的 756 对 | 3,470 对（< 0.95 的 688 对） | 口径不同：实现的家族内 C2 覆盖全部商品类别；作品与统计组内（跨家族）的 C2 由 T8-04 补算后再与 756 对比较 |
| 有多个显示名的用户 | 1,294 | 1,294 | 一致 |
| 逻辑评论没有合格 helpfulness | — | 8 | 全部副本都是分子大于分母 |
| 最大副本数 | — | 144 | |

抽查（只读查询，正文只看前 40 个字符）：`D1` 实例为同一用户同一天在 5 个版本上的同一评论，每个版本上又各重复一次（每个 ASIN 一条 `D3` 或代表、一条 `D1`）；`D2` 实例为同一 ASIN 上 helpfulness `13/15` 与 `2/4` 的两份，家族里另一版本同样两份；C1 实例为同一用户在 3 个剧集商品上的同一句话；C2 实例为同一用户在同一 ASIN 上摘要 `It'll make you stand…` 与 `It will make you sta…`、正文几乎相同的两条（0.9479）。

### 表大小与耗时（演练实测）

| 表 | 大小（含索引） |
|---|---|
| `review_clean` | 2,778 MB |
| `review_mapping` | 2,347 MB |
| `logical_review` | 631 MB |
| `reviewer` | 162 MB |
| 家族四表 | 约 150 MB |
| 合计 | 约 6.1 GB（上文预算 ≤ 4 GB） |

超出预算的原因：两张大表都按 R02 保留 64 位十六进制的 `source_review_id`，`review_clean` 另有两项 MD5 与显示名原值，`review_mapping` 有三个索引（主键、逻辑评论、ASIN）；上文按每行约 130–150 B 估算，实测约 300 B。正式库运行前磁盘剩余 58 GB，6.1 GB 可以接受；如需压缩，可去掉 `review_clean` 的 `summary_md5`（目前只用于诊断）或把 `source_review_id` 存为 `bytea`（与任务 1 的文本类型不一致，暂不做）。耗时：清洗 581 秒（同时有全量测试在跑）、家族 53 秒、去重 388 秒，合计约 17 分钟，在 40 分钟预算内（不是性能实测）。
