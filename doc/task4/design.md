# 任务 4 设计思路（定义并识别同一电影）

状态：2026-10-29 编写计划并实现 T4-01 至 T4-08 的程序与正式库运行；实现中的修订集中记在文末“实现中的修订”，开发池上的规则调整记在“规则调整记录”，实测资源写在“资源预算”。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明怎么实现、为什么这样选。权重、阈值的**默认值是规划值**，T4-07 之后正式使用的是 t4r2；资源数字里“估计”与“实测”分开写。AC15 需要用户标注，尚未完成。

## 整体思路

任务 4 是“暂存层 → 集成层”：把任务 3 抽取的页面字段（暂存层）和 IMDb 公开数据（第三方原始层）放在一起，判断哪些商品是同一部作品，生成作品实体和商品–作品映射（集成层）。整条链路只在下载 IMDb 文件时联网一次，下载由用户许可后单独执行；之后的装载与匹配都离线运行。

分五步，每一步的结果都入库、都可追溯：

1. **IMDb 参考装载**（R04、R05）：核对固定快照的五个文件，流式过滤后装进 schema `imdb`。这一步和匹配分开：IMDb 快照不常换，换快照或重跑匹配互不牵连。
2. **商品版本与标准化**（R02、R03、R07–R11、R17）：为全部 253,059 个 ASIN 建商品版本；参与匹配的商品从 `task3.page_field` 取标题、人物、片长、版本信息，写成带源记录的字段断言（原值与规范值分开），合集同时拆出成员。
3. **候选与判定**（R12–R15、R20–R25、R27）：用标题键分块，生成“商品对 IMDb 作品”和“商品对商品”两类候选；逐个计算特征、打分、按三档判定，所有候选都写决策行。
4. **聚类与映射**（R24–R26、R28–R30）：自动判定的关联连成作品簇，检查簇内硬冲突，生成稳定的 `movie_id`、映射版本和历史事件；人工决策在这一步叠加。
5. **核对、报告与评价**（R32–R37）：核对数量与约束，导出报告与实例；另有命令导出评价表和簇抽查表、导入标注、计算指标。

为什么把“商品对 IMDb 作品”当主要途径：页面上没有上映年份（只有 224 个标题带年份），作业又规定缺年份不能自动合并。如果只比较商品之间，几乎所有候选只能进人工复核。IMDb 作品带 `startYear`、导演和主要演员；商品经人物和片长验证、关联到同一个 `tconst`，就是有外部 ID、有年份依据的合并。没有关联上的商品不硬合并，单独成为一部 `page_only` 作品，同名商品对进入人工复核。代价是依赖第三方数据的覆盖：IMDb 缺少的冷门影片，或页面人物太少的旧 VHS，会留在 `page_only` 或复核里，如实计入待复核率。

## 输入依据与规划时快照

- 任务依据：作业任务 4 与第五、八、十部分；交接文档 `doc/handoffs/2026-10-29-task3-to-task4.md` 第 2–8 节。
- 任务 3 分类（2026-10-29 正式库只读查询，抽取 `f1e8b845…`、规则 `t3r2`）：`movie` 8,730（结构化 5,931、推断 2,799），`collection` 860（全部为推断级 CL2），`tv_series` 835，`concert_other` 3,256，`non_movie` 17，`pending` 239,361。
- 电影类商品的字段（同日只读查询，`t3p1`，按区域计 ASIN 数）：

  | 字段 | 区域与 ASIN 数 | 用法 |
  |---|---|---|
  | 标题 | `title` 8,730 | 标题键、标题年份、版本说明 |
  | 导演 | 署名行 6,356（每行一人）；详情要点 6,455（逗号串） | 导演重叠 |
  | 演员 | 署名行 7,214；详情要点 7,984 | 演员重叠 |
  | 片长 | 详情要点 7,858；概览 6,103 | 片长差 |
  | 商品发售日期 | 详情要点 7,299 | 只作版本属性 |
  | 语言、字幕、配音 | 概览语言 5,489、要点语言 2,539、字幕 2,820、配音 1,063 | 版本属性 |
  | 载体、碟数、发行商、UPC、画幅 | 载体 8,693、碟数 5,990、发行商 8,130、UPC 710、画幅 3,667 | 版本属性 |
  | 类型 | 概览 Genre 3,557 | 弱特征（对照 IMDb 类型） |

- 粗略同名分组（去括号、压缩空白、转小写）：385 组、818 个商品落在多商品组里，其中 82 组导演不止一种。例子：
  - “Breathless” 4 个商品，导演 Jean-Luc Godard（两版）、Jim McBride（1983 翻拍）、Jesse Baget（2012 年另一部同名片）。
  - “Cinderella” 4 个：Clyde Geronimi 动画版三个，Brandy 主演、无导演字段的 1997 年电视电影一个。
  - “Casablanca” 4 个，导演一致；其中 Snap Case 版片长 2 小时 19 分（含花絮或页面错误），所以片长不能当硬冲突。
- 人物写法：旧页面的署名行有 “Arlt, Lewis” 这类“姓, 名”写法；同一页面的详情要点是 “Arlt, Lewis, Brown, Blair, …”，按逗号拆分无法确定边界。所以署名行优先（R10）。
- 合集标题（规划时按 `md5(asin)` 看了前 30 个）：
  - 显式分隔可拆的约一半，例如 “Edward II / All Over Me / Twelfth Night (Triple Feature)”、“The Alien Legacy (Alien / Aliens / Alien 3 / Alien: Resurrection)”。
  - 其余拆不出成员，例如 “Hollywood Hoodlums Collection”、“Serial Killers 4 Movie Pack”。
  - 还有任务 3 误判的单片与歌剧，例如 “Million Dollar Baby (…) 2 DVD Special Box Set”、“Verdi - Nabucco / Bruson, …”。歌剧的 “ / ” 后面是演员表，按 R17 会拆出非作品成员，这类成员在 IMDb 匹配不上，留待确认。
- 以上都是规划参考；正式数字以 T4-07（AC14）为准。

## IMDb 参考数据

**来源**：IMDb Non-Commercial Datasets（作业参考资料 [4]），文件位于 `https://datasets.imdbws.com/`，每日更新。使用条款限个人、非商业用途，不再分发，所以文件和装载结果都不进仓库。

**文件与用途**（大小为规划估计，以下载记录为准）：

| 文件 | 估计压缩大小 | 装载内容 | 用途 |
|---|---|---|---|
| `title.basics.tsv.gz` | 约 200 MB | `titleType ∈ {movie, tvMovie, video}` 的作品 | 标题、年份、片长、类型、作品类型 |
| `title.akas.tsv.gz` | 约 450 MB | 上述作品的全部别名 | 跨语言标题键（R09） |
| `title.crew.tsv.gz` | 约 75 MB | 上述作品的导演 | 导演重叠 |
| `title.principals.tsv.gz` | 约 700 MB | 上述作品中 `category ∈ {actor, actress, director}` 的行 | 演员重叠（只有主要演职员，不是全部演员） |
| `name.basics.tsv.gz` | 约 280 MB | 被上面两表引用的人 | 姓名键 |

**下载**（计划脚本 `scripts/fetch_imdb.py`，在宿主机运行，运行前取得用户许可）：

- 把五个文件下到 `../imdb/<下载日期>/`，与 `movies.txt.gz` 一样放在仓库外。
- 同目录写 `download.json`，记录每个文件的地址、开始与结束时间、HTTP `Last-Modified`、字节数与 SHA-256。
- 快照编号为 `imdb_<下载日期>`。Compose 增加只读挂载 `IMDB_HOST_DIR → /data/imdb`。

**装载**（`etl_task4.imdb`，DAG `imdb_reference_task4_load`）：

1. 校验：逐个计算 SHA-256 与大小，与 `download.json` 对照；不一致时确定性失败，什么都不写（AC02）。
2. 过滤与写入：
   - 先流式读 `title.basics`，按作品类型过滤，把保留的 `tconst` 以整数（去掉 `tt` 前缀）放进内存集合，约 150 万个，估计 100 MB 以内。
   - 再流式读 `akas`、`crew`、`principals`，只保留集合内的作品，同时收集被引用的 `nconst` 集合。
   - 最后读 `name.basics`，只保留集合内的人。
   - 每个文件分批 `COPY`，每行带 `line_no`。
3. 核对：每个文件的读取行数、保留行数、过滤原因计数写入 `imdb.source_file`；保留行的外键（别名、导演、演员 → 作品；导演、演员 → 人）全部有效。
4. 幂等：快照以 `snapshot_id` 唯一；已装载成功的快照再次运行只记 `reused`（R38）。装载中断时，该快照的行在一个事务里全部回滚，或按文件分阶段提交、以文件为恢复单位。**选择（2026-10-29）：五个文件一个事务。** 先在单独的小事务里登记快照为 `loading`（失败遗留的行此时清掉），再在一个事务里写全部表与 `source_file` 并改为 `loaded`；失败时整体回滚并把快照标 `failed`。理由：失败后的状态只有“有或没有”，恢复就是重跑；代价是失败后要从头重读，按估计 15–40 分钟可以接受。是否改成按文件提交，等 T4-07 实测装载时间再定。

**为什么不只装与商品标题相关的作品**：那样装载最小，但标准化规则一改，标题键就变了，必须重装 IMDb。按作品类型全量装，改规则只需重算 `task4.imdb_title_key`。代价是库里多出约几 GB，估计见“资源预算”。

## 组件与职责

| 组件 | 职责 |
|---|---|
| `scripts/fetch_imdb.py` | 下载五个 IMDb 文件并写 `download.json`（宿主机，联网，需用户许可） |
| `sql/301_imdb_schema.sql` | schema `imdb` 的快照、文件、作品、别名、导演、主要演职员、人名表；登记迁移 `301` |
| `sql/302_task4_schema.sql` | schema `task4` 的下文各表、约束与索引；登记迁移 `302` |
| `src/etl_task4/imdb.py` | 校验、流式过滤与装载、核对 |
| `src/etl_task4/normalize.py` | 纯函数：标题键、说明分类与标题年份、姓名键、片长、合集成员拆分；`NORM_VERSION` 与词表集中在此 |
| `src/etl_task4/editions.py` | 商品版本、字段断言与源记录的写入 |
| `src/etl_task4/match.py` | 纯函数：候选特征、得分、硬冲突与三档判定；`RULE_VERSION`、权重与阈值集中在 `RuleSet` |
| `src/etl_task4/candidates.py` | IMDb 标题键、页面与 IMDb 事实取数、候选生成（G1–G4）与判定落库，分批、同事务标记实体 |
| `src/etl_task4/run.py` | 迁移 302 的应用、输入键、运行审计、抽取锁、批次与实体阶段标记 |
| `src/etl_task4/runner.py` | 一次匹配运行的编排：输入检查、锁、复用判断、各阶段顺序、运行审计 |
| `src/etl_task4/review.py` | 人工决策 CSV 的校验与导入，导入后重算映射 |
| `src/etl_task4/cluster.py` | 作品簇、一致性检查、`movie_id` 登记、映射版本与事件、人工决策叠加、版本差异 |
| `src/etl_task4/report.py` | 统计、`summary.json`、`report.md`、CSV 与实例导出 |
| `src/etl_task4/evaluate.py` | 开发池冻结、评价表与簇抽查表导出、标注导入、指标计算 |
| `src/etl_task4/pipeline.py`、`dags/imdb_reference_task4_load.py`、`dags/amazon_movies_task4_match.py` | DAG 步骤函数与编排（装载 DAG 已实现，匹配 DAG 在 T4-05） |
| `src/etl_task4/check.py` | 正式库只读核对命令（AC14） |
| `tests/fixtures/task4/` | 合成页面字段、合成 IMDb 小文件与预期值说明 |

复用而不复制：

- `etl_task1`：`config.load_settings`、`db.connect`、`db.advisory_key`、`reporting.write_atomic`、`reporting.file_digest`、`reporting.utc_now`、`pipeline.is_transient`、`pipeline.gate_decision`。
- `etl_task2.pipeline` 的 `run_step` 与失败文件写法。
- `etl_task3.rules.runtime_minutes`（片长解析）；`etl_task3.rules` 中的载体词表只作参考，任务 4 的说明词表单独定义、单独版本化（交接文档第 8 节）。

## 数据模型

作业第八部分建议的实体与本任务表的对应：

| 作业实体 | 本任务的表 | 说明 |
|---|---|---|
| ProductEdition | `task4.product_edition`，版本属性在 `task4.field_assertion` | 全部 ASIN；`product_id = edition_id = asin` |
| Movie | `task4.movie`（按映射版本），身份登记 `task4.movie_identity` | 只含身份、状态、显示标题（H08） |
| ProductMovie | `task4.product_movie` | `edition` 与 `contains` 两种关系 |
| MergeDecision | `task4.candidate` + `task4.merge_decision`，人工决策 `task4.human_decision` | 未采用的候选同样保存 |
| SourceRecord | `task4.source_record` | 页面与 IMDb 两种来源 |
| FieldAssertion | `task4.field_assertion` | 匹配所用字段的原值、规范值、来源与规则 |

### schema `imdb`（迁移 301）

- `imdb.snapshot`：`snapshot_id` 主键，`base_url`、`downloaded_at`、`status`（`loading`/`loaded`/`failed`）、`loaded_at`、`loader_version`。
- `imdb.source_file`：主键（`snapshot_id`, `file_name`）；`url`、`size_bytes`、`sha256`、`last_modified`、`rows_read`、`rows_kept`、`filter_counts`（jsonb）。
- `imdb.title`：主键（`snapshot_id`, `tconst`）；`title_type`、`primary_title`、`original_title`、`is_adult`、`start_year`、`end_year`、`runtime_minutes`、`genres text[]`、`line_no`。
- `imdb.title_other`：主键（`snapshot_id`, `tconst`）；`title_type`、`primary_title`、`original_title`、`start_year`、`line_no`；R15 用，见文末“实现中的修订”。
- `imdb.title_aka`：主键（`snapshot_id`, `tconst`, `ordering`）；`title`、`region`、`language`、`types`、`attributes`、`is_original`、`line_no`。
- `imdb.title_director`：主键（`snapshot_id`, `tconst`, `ordinal`）；`nconst`、`line_no`（来自 `title.crew` 的逗号串，`ordinal` 为串内序号）。
- `imdb.title_principal`：主键（`snapshot_id`, `tconst`, `ordering`）；`nconst`、`category`、`job`、`characters`、`line_no`。
- `imdb.name`：主键（`snapshot_id`, `nconst`）；`primary_name`、`birth_year`、`death_year`、`primary_profession`、`line_no`。

源记录定位就是（快照、文件名、`line_no`、`tconst`/`nconst`）。快照对应的文件哈希在 `imdb.source_file`，满足作业“数据文件版本与行号”的定位要求。

### schema `task4`（迁移 302）

**运行与版本**

- `task4.run`：`run_id`，`kind`（`match`/`human_import`/`evaluate`），`extraction_id`、`t3_rule_version`、`parser_version`、`norm_version`、`rule_version`、`imdb_snapshot_id`、`human_set_hash`、`mapping_version_id`，`status`（`running`/`succeeded`/`failed`/`reused`/`interrupted`），`params`（jsonb：`batch_size`、`limit`），计数、起止时间、`dag_id`、`airflow_run_id`、`failure_reason`。
- 输入键：（`extraction_id`, `t3_rule_version`, `parser_version`, `norm_version`, `rule_version`, `imdb_snapshot_id`, `human_set_hash`）。已有成功运行时，新运行记 `reused`（R38）。
- `task4.run_batch`：主键（`run_id`, `stage`, `batch_no`）；`first_asin`、`last_asin`、`rows_written`、`committed_at`。与该批数据在同一事务写入，是中断恢复的依据（R39）。

**商品版本、源记录与字段断言**

- `task4.product_edition`：主键（`extraction_id`, `asin`）。
  - `product_kind`：任务 3 类别，及其 `t3_rule_version`。
  - `match_scope`：`matched`/`not_applicable`/`no_page`。
  - 页面来源：`content_hash`、`final_url`、`fetched_at`；无页面时为 NULL。
  - 不随规则版本变化，同一抽取只建一次。
- `task4.source_record`：
  - 字段：`source_record_id`，`source_type`（`page`/`imdb`），`locator`（jsonb），`content_hash` 或文件 SHA-256，`acquired_at`。
  - 页面的定位为 ASIN、快照路径、最终地址；IMDb 的定位为快照、文件、行号、ID。
  - 唯一键为（`source_type`, 定位的规范化串），同一来源只登记一次。
  - IMDb 源记录只为进入候选的作品与人登记，不为几百万行逐一登记。
- `task4.field_assertion`：
  - 字段：`assertion_id`、`extraction_id`、`norm_version`、`entity_type`（`product_edition`/`collection_member`）、`entity_id`（ASIN，或 `ASIN#成员序号`）、`field_name`、`ordinal`、`raw_value`、`normalized_value`、`qualifier`（说明类别、人物角色、宽松键等）、`source_record_id`、`source_locator`（任务 3 字段的区域与序号）、`parent_assertion_id`、`rule_id`。
  - `field_name` 取值：`title`、`title_year`、`descriptor`、`director`、`actor`、`runtime_min`，以及版本属性 `edition.media_format`、`edition.region`、`edition.video_system`、`edition.language`、`edition.subtitles`、`edition.dubbed`、`edition.discs`、`edition.publisher`、`edition.publisher_series`、`edition.cut_type`、`edition.packaging`、`edition.product_release_date`、`edition.upc`、`edition.aspect_ratio`。
  - 派生值用 `parent_assertion_id` 指回来源断言。例如标题年份、说明都指回 `title`，合集成员指回合集的 `title`。这条父子链给任务 9 的血缘打底。
  - 索引（`norm_version`, `field_name`, `normalized_value`）供分块使用。
- `task4.imdb_title_key`：主键（`imdb_snapshot_id`, `norm_version`, `tconst`, `key_source`, `aka_ordering`）；`title_key`、`region`、`language`。`key_source` 为 `primary`/`original`/`aka`。索引（`norm_version`, `title_key`）。

**候选与决策**

- `task4.candidate`：
  - 字段：`candidate_id`，`norm_version`，左侧 `left_entity`（ASIN 或 `ASIN#成员`），右侧 `right_kind`（`imdb`/`product`）与 `right_id`（`tconst` 或 ASIN），`generator`（`G1` 标题键对 IMDb 主标题或原名，`G2` 对 IMDb 别名，`G3` 商品同标题键）。
  - 唯一键为（`norm_version`, `left_entity`, `right_kind`, `right_id`）。商品对商品只存 ASIN 较小者在左的一行。
- `task4.candidate_overflow`：每个超过候选上限的商品一行，记录候选总数与截断依据（R20）。
- `task4.merge_decision`：
  - 字段：`decision_id`、`candidate_id`、`rule_version`、`run_id`、`tier`（`auto`/`review`/`reject`）、`score`、`confidence`、`rank`、`margin`、`veto`（硬冲突原因数组）、`features`（jsonb，每个特征含取值、得分、所用断言或 IMDb 源记录）、`thresholds`（jsonb），`decided_by` 固定为 `system`。
  - 唯一键为（`candidate_id`, `rule_version`）。
- `task4.human_decision`：
  - 字段：`human_decision_id`、`set_hash`、`kind`（`must_link`/`cannot_link`/`detach`）、`left_entity`、`right_entity`（`detach` 时为原 `movie_id`）、`note`、`annotator`、`decided_at`、`import_file_sha256`。
  - 人工决策集是若干条决策的集合，其哈希进入运行的输入键。

**作品与映射**

- `task4.movie_identity`：`anchor_key` 唯一（`imdb:tt…` 或 `asin:…`）；`movie_id` 唯一，取 `mv_` 加 `sha256(anchor_key)` 前 16 位十六进制（R28 不含原始 ID）；`created_run`。同一锚点永远得到同一 `movie_id`；哈希碰撞由唯一约束发现，发现即确定性失败。
- `task4.mapping_version`：`mapping_version_id`，`parent_version_id`（上一个映射版本），`run_id`，输入键各项，`counts`（jsonb，R29 的五项数量），`created_at`。
- `task4.movie`：主键（`mapping_version_id`, `movie_id`）；`status`（`imdb_linked`/`page_only`），`anchor_key`，`tconst`，`display_title`，`display_title_source`（断言 ID 或 IMDb 源记录），`edition_count`，`contains_count`。
- `task4.product_movie`：主键（`mapping_version_id`, `asin`, `member_ordinal`, `movie_id`）；`relation_type`（`edition` 时成员序号为 0，`contains` 时为成员序号），`basis`（`auto_decision`/`singleton`/`human`），`decision_id` 或 `human_decision_id`。约束：电影类商品在每个映射版本恰好一条 `edition` 关系。
- `task4.product_match_status`：主键（`mapping_version_id`, `asin`）。`status` 取以下之一，`reason` 记原因：
  - 电影类：`merged`（多商品作品）、`singleton`、`review_pending`；
  - 合集：`collection_split`、`collection_unsplit`；
  - 其他：`not_applicable`、`no_page`。
- `task4.mapping_event`：`mapping_version_id`、`event`（`create`/`merge`/`split`/`move`/`retire`）、`asin`、`from_movie_id`、`to_movie_id`、`reason`、`decision_id`/`human_decision_id`。由新旧两个映射版本对比生成。
- `task4.cluster_check`：`mapping_version_id`、`movie_id`、`asin_a`、`asin_b`、`conflict`（`director_disjoint`/`year_conflict`）、`action`（`removed_b` 等）、`resolved_by`（`system`/`human`）。

**评价**

- `task4.dev_pool`：冻结的开发池标题键，以 `t4n1` 标题键为准，后续标准化版本也沿用同一份 ASIN 名单，避免分区随规则漂移。
- `task4.eval_set`、`task4.eval_pair`（层次、双方实体、导出时的映射版本与系统判定）、`task4.eval_label`（`same`/`different`/`unsure`、标注人、时间、备注）。
- `task4.cluster_sample`、`task4.cluster_label`（簇是否混入不同作品、问题商品）。

**质量问题**：`task4.quality_issue` 记录运行、实体、类型（如 `name_ambiguous`、`collection_member_unmatched`、`candidate_overflow`、`human_vs_veto`）与细节。

外键指向 `public.extraction`、`task2.product_manifest`、`task3.page_field`/`page_parse`、`imdb.*`，不在任务 1–3 的表上加列或约束（R41）。

## 标准化（`NORM_VERSION = t4n1`）

**标题键**（R07）。依次执行：

1. 先切出说明段（见下一节），剩下的作为标题主体。
2. 主体做 NFKC 规范化，去掉 `™®©` 与方向符；NFKD 后去掉重音；大小写折叠。
3. `&`、`+` 改为 `and`。
4. 整词罗马数字 II–XX 转为阿拉伯数字。只转标题主体中独立的词，以免把 “I” 当成代词改掉：只在词位于末尾，或后面紧跟冒号、连字符时才转换。
5. 去掉开头的冠词：英文 the/a/an，以及常见外文冠词 le/la/les/el/los/las/il/der/die/das。
6. 标点改为空格，压缩空白。
7. 冒号后的副标题、续集编号都保留。所以 “Rocky II” 的键为 `rocky 2`，与 `rocky` 不同块（AC05）。

**说明分类**（R08）。识别三类说明段：

- 标题里的 `(…)` 与 `[…]` 段；
- 尾部 “ - Widescreen Edition” 这类破折号说明；
- 前缀品牌词，例如 “Superbit”。

每段按词表归为一类，词表随 `t4n1` 记录：

| 类别 | 例子 | 写入 |
|---|---|---|
| 载体 | DVD、Blu-ray、VHS、HD DVD、Blu-ray + DVD、UMD | `edition.media_format` |
| 版本或剪辑 | Director's Cut、Extended、Unrated、Uncut、Remastered、Restored、Colorized、Special/Collector's/Anniversary/Diamond/Ultimate Edition | `edition.cut_type`（剪辑类）或 `edition.packaging`（纪念版类） |
| 地区与制式 | Region 0/1/2、PAL、NTSC、Import、UK | `edition.region`、`edition.video_system` |
| 语言版本 | Spanish Version、Subtitled、Dubbed、English Subtitles | `edition.language` |
| 包装 | Snap Case、Slim Case、Two-Disc、Widescreen、Full Screen、Steelbook | `edition.packaging` |
| 发行品牌 | The Criterion Collection、Superbit、Anchor Bay | `edition.publisher_series` |
| 年份 | `(1994)`、`(1986 VHS)` | `title_year`（取其中的四位年份，1888–2013） |
| 未知 | 不在词表中的括号段 | 记 `descriptor` 类别 `unknown`，仍从键中去掉 |

所有说明的原文都存为 `descriptor` 断言。未知类的去掉是有意的取舍：粗略分组显示，括号段绝大多数是商品描述。代价是少数正名里含括号的作品会和别的作品同键，这由人物与年份验证兜底。未知类按出现次数统计进报告，用来补词表。

**IMDb 标题键**（R09）：对 `primaryTitle`、`originalTitle` 与每个别名套用同一函数（不做说明切分，因为 IMDb 标题里没有载体说明），写入 `task4.imdb_title_key`。

**姓名键**（R10）：

- 来源优先级：署名行（`byline`，每行一人）优先。没有署名行时才用详情要点，按逗号拆分。两者都有时只取署名行；详情要点里多出的人记为 `qualifier = detail_only`，只算弱证据。
- “姓, 名”：署名行恰好含一个逗号、两侧都不含空格以外的分隔符时，倒序为“名 姓”，`qualifier` 记 `reversed`。
- 严格键：NFKD 去重音、大小写折叠、去掉标点、压缩空白。
- 宽松键：在严格键上再去掉单字母的中间名缩写，例如 “hamilton s luske” 变成 “hamilton luske”。
- 占位词表：Various、Artist Not Provided、n/a、Unknown、Not Available。命中的署名写断言但 `normalized_value` 为 NULL，`qualifier = placeholder`。
- IMDb 一侧对 `primary_name` 用同一函数。

**片长**（R11）：调用 `etl_task3.rules.runtime_minutes`。详情要点优先，没有时用概览；两者都有且不同时，两个值都存断言，特征取较小值，并记 `qualifier = conflict`。

**合集成员拆分**（R17），按顺序尝试：

1. 括号内含 “ / ” 的列表，例如 “The Alien Legacy (Alien / Aliens / …)”，成员取括号内。
2. 标题主体含 “ / ”，按它切分。
3. 标题带 Double/Triple Feature 标记且主体含 “ & ”，按 “ & ” 切分。

每个成员再做一次说明切分和标题键。成员数不在 2–6 之间时整个商品记 `collection_unsplit`。每个成员写成一个 `collection_member` 实体：原文、序号、拆分模式都在断言里，并通过 `parent_assertion_id` 指回合集标题。

## 同一电影的定义与例外

| 情况 | 归并层次 | 依据与实现 |
|---|---|---|
| 同一作品的 DVD、Blu-ray、VHS、组合装 | 同一 `movie_id`，不同 `edition_id` | R12；载体只进版本属性 |
| 不同语言版本、配音、字幕、地区码、制式、进口版 | 同一 `movie_id` | R12；语言与地区进版本属性，标题键里已去掉 |
| 不同发行商、再发行、纪念版、包装 | 同一 `movie_id` | R12 |
| 导演剪辑版、加长版、未分级版、修复版、上色版 | 同一 `movie_id`，版本属性记 `cut_type` | R13；IMDb 通常也只有一个 `tconst`；带剪辑标记时片长差不扣分 |
| 翻拍（Breathless 1960 / 1983） | 不同 `movie_id` | R14；导演不重叠，或年份差大于 1 年 |
| 续集（Rocky / Rocky II） | 不同 `movie_id` | R07 保留编号，两者不进同一分块 |
| 同名不同作品（Cinderella 动画版 / 1997 年电视电影） | 不同 `movie_id` | R14、R22；关联到不同 `tconst`；没有 IMDb 关联时最多进入复核 |
| 电视电影、直接发行录像 | 算电影 | H07；IMDb `tvMovie`、`video` 是候选 |
| 剧集商品 | 不建作品 | R16 |
| 合集 | 商品–作品多对多 `contains` | R17、R18 |

## 候选生成（R20）

- **G1/G2 商品对 IMDb 作品**：商品（或合集成员）的标题键等于某个 IMDb 作品的标题键时生成候选；等于主标题或原名的为 G1，只等于别名的为 G2。同一作品同时命中 G1 和 G2 时只保留 G1。
- **G3 商品对商品**：参与匹配的电影类商品中，标题键相同的两两成对；合集成员不参与 G3，成员与电影之间的关系通过 IMDb 关联建立。
- **上限**：每个商品的 IMDb 候选上限 50 个（常见短标题如 “Hamlet” 可能有几十个同名作品）。超过时先按 `startYear` 与标题年份的接近程度排序，再按 IMDb 作品类型（`movie` 优先）、`tconst` 排序截断。截断写入 `candidate_overflow` 和质量问题，被截断的商品最高只能判为复核。G3 的块大于 30 个商品时同样记溢出，块内只生成复核候选。
- **分块与召回的取舍**：只用精确标题键分块，候选少、可解释；代价是标题写法差异大的商品对（例如 “Dr. Strangelove” 与它的全称）不会成为候选。评价样本专设“标题相近但未被分块”一层（S5），用来估计这部分漏合并。

## 特征、得分与判定（规划默认值 `t4r1`；正式使用 `t4r2`，见“规则调整记录”）

**商品对 IMDb 作品的特征与默认分值**：

| 特征 | 取值 | 分值 |
|---|---|---|
| T 标题 | G1 / G2 | +2 / +1 |
| D 导演 | 严格键有重叠 / 只有宽松键重叠 / 两边都有导演但无重叠 / 一边缺 | +3 / +1 / 硬冲突 / 0 |
| A 演员（页面演员对 IMDb actor/actress） | 严格键重叠 ≥2 / =1 / 两边各 ≥3 人且无重叠 / 其他 | +3 / +2 / −2 / 0 |
| R 片长 | 差 ≤ max(5 分钟, 8%) / 差 > 25% 且无剪辑标记 / 其他或缺失 | +1 / −1 / 0 |
| Y 年份（标题年份对 `startYear`） | 差 ≤1 / 差 >1 / 一边缺 | +2 / 硬冲突 / 0 |
| K 类型（页面 Genre 对 IMDb genres） | 有交集 / 其他 | +0（只记录，不计分） |

- 满分为 2 + 3 + 3 + 1 + 2 = 11。
- 置信度 = 得分 / 11，下限 0，保留两位小数；有硬冲突时置信度为 0。
- K 只记录不计分：页面 Genre 只覆盖 3,557 个电影类商品，而且受控词表与 IMDb 不同，等任务 6 定好类型词表后再考虑。

**判定**：

1. 有硬冲突：`reject`。
2. 得分 ≥ 6，且 R22 条件满足（IMDb `startYear` 不为空；D 严格重叠，或 A 严格重叠 ≥1），且领先本商品第二名候选 ≥ 2 分：`auto`。
3. 得分 ≥ 3，或者本来满足 `auto` 但领先不足 2 分：`review`。
4. 其余：`reject`。

**样例推演**（用于检查默认值是否合理）：

| 情况 | 得分 | 判定 |
|---|---|---|
| 标题 G1 + 导演 + 1 位演员 | 2 + 3 + 2 = 7 | `auto` |
| 标题 + 导演 + 片长 | 6 | `auto`（导演满足 R22） |
| 没有导演字段，标题 + 2 位演员 + 片长 | 6 | `auto` |
| 标题 + 1 位演员 + 片长 | 5 | `review` |
| 标题 + 片长 | 3 | `review` |
| 只有标题 | 2 | `reject` |

**为什么关联 IMDb 作品不要求商品自己有年份**：R22 要的是“两边都有年份”。商品经人物验证关联到 IMDb 作品后，年份来自该作品的 `startYear`，并且这个年份已被人物重叠佐证。只靠标题、没有人物佐证的关联最高是复核，不会借 IMDb 的年份自动合并。商品自带标题年份时，必须与 `startYear` 相符（Y 特征）。

**商品对商品（G3）**：

- 特征：T（同键 +2）、D、A、R 同上。Y 用两边的标题年份：都有且差 ≤1 为 +2，差 >1 为硬冲突，缺一边为 0。
- 判定只在两边都没有 `auto` 的 IMDb 关联时生效：
  - 满足 R25（标题年份都有且相同、导演严格重叠）时为 `auto`；
  - 否则得分 ≥ 3 为 `review`，其余为 `reject`。
- 一边有 IMDb 关联、另一边没有时：G3 决策最高为 `review`，原因记 `suggest_join_imdb_cluster`。
- 两边关联到不同 `tconst` 时：直接 `reject`，原因 `different_external_id`，这是“同名不同作品”的主要证据。

**合集成员**：

- 用 G1/G2 找 IMDb 候选。D、A 两项用整个合集页面的人物，因为页面不区分成员。R 不计分，因为合集片长是总长。
- 自动阈值相同（≥6 且满足 R22，领先 ≥2）。
- 成员自动关联到 `tconst` T 时：
  - 若本映射版本已有锚点为 T 的作品，建立 `contains` 关系；
  - 若没有，按锚点 `imdb:T` 新建一部 `imdb_linked` 作品，它没有 `edition` 商品，`edition_count = 0`。
- 没有自动关联的成员不建作品，状态写入 `reason`（`member_review`/`member_unmatched`）。

**无标题年份、无 IMDb 的同名商品对**：只能是 `review` 或 `reject`，这正是 R22 的要求。估计这会是复核队列的大头，待复核率会偏高，如实报告。

## 规则调整记录（T4-07，t4r1 → t4r2）

**做法**：t4r1 用上文的默认值全量跑完（映射版本 `map0001`）后，立即冻结开发池（`evaluate freeze`：全部 9,835 个参与匹配的标题键按 `md5` 排序，前 20%，即 1,967 个键、2,105 个实体），之后只看开发池内实体的判定结果。这里是 AI 观察判定结果，**没有产生任何标注，也不算评价**；评价表在规则冻结后才导出。

**在开发池里观察到的**（t4r1，IMDb 候选）：

| 观察 | 数量（开发池内） | 判断 |
|---|---|---|
| 自动合并，得分 6–7 的抽样 30 个 | 30 | 未发现明显错误：标题、导演、片长、演员相符（例如“Chicago (Fullscreen Edition)”对 2002 年版、“House on Haunted Hill”对 1959 年版）；这是 AI 的目测，不是标注 |
| 标题 + 导演严格重叠（5 分），片长缺失或差异大，没有演员重叠 | G1 候选 25 个停在复核 | 抽样 20 个，几乎都像同一作品（Halo Legends、Pollyanna、Devdas 等）；原因只是片长差异或缺失 |
| 标题 + 片长接近、页面没有任何人物（3 分） | 172，复核 | 多数明显是同一作品（Chariots of Fire、Swamp Thing），但 R22 要求导演或演员重叠，不能放宽，保持复核 |
| 导演完全不重叠（硬冲突）却有演员重叠 | 59，拒绝 | 抽样 25 个，绝大多数是姓名写法问题：编码损坏的非 ASCII 字符（`Fran�ois Girard`）、拼写差异（`Johhnie To`）、中间名（`John Mallory Asher` 对 `John Asher`）、姓名顺序（`Yuen, Woo-Ping`）；少数是真正的翻拍或续集。这属于姓名标准化，不是权重问题，不在本轮调整范围（见已知局限） |

**调整**：只改一个权重，使上表第二行的一类达到自动阈值：导演姓名键严格重叠的权重 3 → 4（`w_director_strict`），满分相应 11 → 12（`max_score`，只影响置信度换算）。自动阈值 6、复核阈值 3、领先差 2、其余权重、R22 和 R14 的硬冲突都不变。这样“标题（2）+ 导演严格重叠（4）”= 6 自动合并，仍然必须有 IMDb 年份、仍然必须领先第二名至少 2 分（同名作品并列仍进复核），“别名（1）+ 导演（4）”= 5 仍是复核。

**不采用的方案**：把自动阈值降到 5。它会同时放行“标题 + 1 位演员 + 片长接近”（5 分），而一位演员重叠不足以区分同名翻拍；调权重只放行导演证据这一类，范围更窄。

**调整前后**（全量与开发池，`map0001` → `map0002`；同一 IMDb 快照与标准化版本，只重算判定）：

| 指标 | t4r1（map0001） | t4r2（map0002） |
|---|---|---|
| 判定档 auto / review / reject（全部候选） | 4,848 / 1,818 / 28,546 | 5,037 / 1,629 / 28,546 |
| 作品数（电影类商品 8,730 个） | 8,481 | 8,472 |
| 多商品作品 / 其中商品数 | 232 / 481 | 240 / 498 |
| 待复核商品（占比） | 1,013（11.60%） | 899（10.30%） |
| IMDb 关联商品（占比） | 4,430（50.74%） | 4,544（52.05%） |
| 簇内一致性检查移出 | 0 | 1（Halo Legends 两个版本，导演列表不同，见已知局限） |
| 开发池内 auto / review / reject（IMDb 候选） | 872 / 352 / 5,055 | 912 / 312 / 5,055 |
| 开发池内待复核商品 | 197 | 169 |

映射版本之间有 114 个商品的 `movie_id` 变化：107 个 `move`、7 个 `merge`，全部是 `review → auto`（得分 5 → 6），没有任何 `auto` 变成别的档。拒绝档数量不变，因为权重提高只增加分数。`t4r1` 保留在 `RULESETS` 里作为对照，不再用于正式输出。评价表导出时用的是 `map0002`（t4r2），导出后规则版本冻结；如果再改规则，必须在报告里说明“评价集已被看过，不再独立”。

## 聚类与一致性（R24–R26）

1. **IMDb 簇**：每个商品取它唯一的 `auto` IMDb 关联（按 R24 最多一个），同一 `tconst` 的商品归为一簇。
2. **页面簇**：没有 IMDb 关联的商品之间，用 G3 的 `auto` 边做并查集。由于 R25 要求标题年份相同且导演重叠，这类边很少。
3. **一致性检查**：
   - 对每个多商品簇枚举全部商品对，检查 R14 的硬冲突：导演严格键与宽松键都不重叠且两边都有导演，或者两边的标题年份差 >1。
   - 有冲突时，在冲突对里移出关联得分较低的商品（同分时移出 ASIN 较大者），改为 `review_pending`，原因 `cluster_conflict`，写 `cluster_check`。
   - 重复上述过程，直到簇内没有冲突。
   - 簇大小设上限（默认 30），超过时整簇写质量问题，供人工抽查。
4. **单商品作品**：剩下的电影类商品各成一部 `page_only` 作品，状态 `singleton`；有 `review` 候选的同时标 `review_pending`。
5. **复核边不连簇**：`review` 和 `reject` 的决策永远不进入并查集。A≈B、B≈C 都是复核时，三者各自独立，等人工决策（R26、AC07）。

## `movie_id`、映射版本与人工纠正（R28–R31）

- **锚点**：IMDb 簇取 `imdb:<tconst>`；页面簇和单商品作品取 `asin:<簇内最小 ASIN>`。`movie_identity` 按锚点登记，已登记的直接复用，所以同一输入重跑得到同一组 `movie_id`。
- **锚点变化**：页面簇拆开后，最小 ASIN 留下的那部分保持原 `movie_id`，拆出去的商品按新锚点得到新 `movie_id`。整个簇被并入 IMDb 簇时，旧 `movie_id` 在新版本中不再出现，记 `retire` 事件，`to_movie_id` 指向新作品，所以旧 ID 仍可追到去向。
- **映射版本**：每次非 `reused` 的匹配运行，或人工决策导入后的重算，都生成一个新的映射版本，并记录父版本。事件表由新旧版本逐商品对比生成：`merge`（并入已有作品）、`split`（离开原作品成为新作品）、`move`（从一部作品移到另一部）、`create`、`retire`。
- **人工决策叠加**，在自动聚类之后、一致性检查之前执行：
  - `must_link(a, b)`：合并 a、b 所在的作品。新簇的锚点取原两簇中优先级较高者：IMDb 锚点优先，其次 ASIN 较小。
  - `cannot_link(a, b)`：a、b 在同一簇时，把 b 移出成为新作品（`split`）。
  - `detach(a)`：把 a 移出原作品。
  - 人工 `must_link` 与硬冲突矛盾时仍执行，但写质量问题 `human_vs_veto`，并在 `cluster_check` 里记 `resolved_by = human`。
- **改规则**：`norm_version` 变化时从标准化开始重算；只有 `rule_version` 变化时，复用已有的断言与候选，只重算决策及以后的步骤（AC09）。差异命令列出两个映射版本间 `movie_id` 变化的商品，并给出新旧决策的判定档与得分。

## 评价设计（R32–R36）

**开发池与评价池**：

- 首次运行 `t4n1` 时，对全部参与匹配商品的标题键按 `md5(标题键)` 排序，前 20% 的标题键冻结为开发池，记入 `dev_pool`。按标题键而不是按 ASIN 划分，是为了让同一组版本整组落在同一侧，评价对不会和开发样本共享商品。
- 调整权重与阈值时，只看开发池的判定。这里由 AI 观察判定结果，不产生标注，也不算评价。
- 评价表导出前冻结规则版本。评价表导出后如果再改规则，必须在报告里说明“评价集已被看过，不再独立”（任务 3 的教训，见交接文档第 8 节）。

**120 对评价样本的分层**（只从评价池抽，每层按 `md5(pair_id)` 排序取前 N；某层不足时按顺序从 S1、S4 补足，实际层数记入评价集）：

| 层 | 内容 | 对数 | 主要检验 |
|---|---|---|---|
| S1 | 系统判为同一作品的商品对（经 IMDb 簇） | 30 | 误合并（Precision） |
| S2 | 系统判为同一作品的页面簇商品对（R25 途径） | 5 | 页面途径的误合并 |
| S3 | G3 判为 `review` 的商品对 | 20 | 复核档里的真实同一比例 |
| S4 | 标题键相同但判为不同作品（`reject`，或分属不同簇） | 25 | 同名、翻拍的漏合并与正确拒绝 |
| S5 | 标题键不同但字符三元组相似度 ≥ 0.6 的电影类商品对 | 15 | 分块造成的漏合并、续集 |
| S6 | 合集成员对电影类商品（成员标题键等于商品标题键） | 15 | 合集关系 |
| S7 | 一方标题键只命中另一方 IMDb 作品别名的商品对 | 10 | 跨语言标题 |

- **导出**：`outputs/task4/<抽取>/<映射版本>/eval/eval_pairs.csv`。每行含 `pair_id`、层、双方的 ASIN、原始标题、导演、演员、片长、标题年份、各自关联的 `tconst` 与 IMDb 页面地址、`https://www.amazon.com/dp/<ASIN>`；空列 `label`、`note` 留给用户填写。
- **不导出系统判定**：标注时看不到系统的判定，避免锚定效应。系统判定存在库里（`eval_pair`）。
- **标注口径**（写在导出文件的说明行和 runbook 里）：
  - 是否同一部创作作品，按本文“同一电影的定义与例外”一节判断；
  - 证据以页面与 IMDb 为准；
  - 无法判断时标 `unsure`，并写 `note`。
- **分歧处理**（H10）：只有用户一人标注。导入时自动检查明显矛盾，例如双方关联到同一 `tconst` 且导演一致，却被标为 `different`。矛盾项列入报告供用户复看，但不改动用户的标注。

**指标**：

- “预测同一”：在导出时的映射版本里，双方处于同一作品，且该关系完全来自系统判定（不含人工决策）。S6 的“同一”指成员的 `contains` 作品等于该商品的 `edition` 作品。
- TP、FP、FN 只在 `same`/`different` 标注上计算：
  - Precision = TP / (TP + FP)，Recall = TP / (TP + FN)，F1 = 2PR / (P + R)；
  - 任一分母为 0 时，该值记 NULL，报告写“无定义（分母为 0）”。
- 同时按层报告。分层抽样使样本里的难例比例远高于全量，所以总体数字只代表这个样本（R34）。
- 误合并实例取 FP 全部，漏合并实例取 FN 全部，并附特征。
- 人工复核覆盖率 = 有人工决策的复核项数 / 复核项总数；待复核率 = `review_pending` 商品数 / 参与匹配的电影类商品数。

**簇抽查**（R35）：

- 从评价池随机取 20 个多商品作品：商品数 ≥3 的取 10 个，2 个商品的取 10 个；不足时互补。
- 导出每个簇的全部商品与证据，用户逐簇标“全部同一作品 / 混入不同作品（列出 ASIN）”。
- 报告混入的簇数和例子。簇抽查不计入 Precision。

**实例**（R36）：

- 跨版本合并：从多商品作品里选，优先选已被用户标为 `same` 的；
- 易混淆未合并：优先选 S4、S5 中已标为 `different` 的；
- 不足 5 组时，用确定性排序补足并注明“未标注”。
- 每组导出双方的标题、判定档、关键特征和源记录定位。

## 统计与报告（R19、R37）

`summary.json` 与 `report.md` 包含：

- 商品版本数与匹配状态分布（分母 253,059）。
- 合并前后：参与匹配的电影类商品数 → 作品数，多商品作品数与它们包含的商品数，单商品作品数，待复核商品数。
- 作品来源：`imdb_linked` 与 `page_only` 分开；`imdb_linked` 中另计“只有合集成员、没有 `edition` 商品”的作品。第三方来源单独列出，不并入页面识别数（任务 3 R18 的延续）。
- 候选与判定：各生成器的候选数，各判定档数，硬冲突类型计数，溢出数，一致性检查移出数。
- 合集：拆分成功数、成员数、成员关联数、`collection_unsplit` 数。剧集商品数单列。
- 评论口径（来自 `task2.product_manifest.records_total`）：
  - 经 `edition` 关系可归到作品的记录数；
  - 合集商品的记录数（不分给成员）；
  - 剧集与其他类别的记录数；
  - 无页面商品的记录数；
  - 各口径之和等于 7,911,684。
- 未知说明段的出现次数（前 50 个），用来补词表。

导出文件：`summary.json`、`report.md`、`product_movie.csv`、`movies.csv`、`decisions_sample.csv`（每档按 `md5` 取前 200 个）、`examples.md`，写入 `outputs/task4/<抽取>/<映射版本>/<run_key>/` 并记录哈希。导出不含评论正文与整页 HTML。

## 运行、恢复与编排（R38–R40）

**分批**：

- 标准化与候选按商品分批，默认 500 个 ASIN 一批。每批的断言、源记录、候选、特征与决策在同一事务提交，并写批次完成标记 `task4.run_batch`。
- 聚类与映射是全局步骤（需要看到全部决策），在一个事务里生成整个映射版本。电影类商品不到 1 万个，规模允许一次提交。
- 中断后同一运行键重跑时，跳过已完成的批次；聚类步骤没有提交就从头重做（R39、AC10）。

**锁**：输入键做 advisory lock，同一输入同时只有一个运行；拿不到锁时确定性失败。

**两个 DAG**：

- `imdb_reference_task4_load`：`check_files → load → verify → gate`。参数为 `snapshot_id`。
- `amazon_movies_task4_match`：`check_inputs → normalize → candidates_decide → cluster_map → verify → finalize_report → gate`。参数为抽取、任务 3 规则版本、IMDb 快照、`norm_version`、`rule_version`、人工决策集、`batch_size`、`limit`。
  - `check_inputs` 确认输入存在：任务 3 分类存在且全量、IMDb 快照为 `loaded`、迁移版本齐全。
  - `verify` 只读核对 AC14 的约束。
  - `finalize_report` 与 `gate` 的触发规则为 `all_done`；门禁只在上游全部成功时通过。
  - 暂时错误（连接中断、死锁）最多重试 2 次，确定性错误（输入缺失、哈希不符、约束违反）不重试。

**人工决策与评价的命令**（不进 DAG，由人触发，结果写入库，再触发一次匹配 DAG 重算映射）：

- `python -m etl_task4.review import <csv>`
- `python -m etl_task4.evaluate export|import|metrics`
- `python -m etl_task4.evaluate clusters-export|clusters-import`

## 资源预算（估计与实测）

**估计**（规划时，2026-10-29）：

| 项目 | 估计 | 依据 |
|---|---|---|
| IMDb 压缩文件 | 1.5–1.8 GB，放仓库外 | 规划时的公开文件大小估计 |
| IMDb 装载后库内 | 2–4 GB | 作品约 150 万、别名数百万、主要演职员约千万行量级 |
| IMDb 装载时间 | 15–40 分钟 | 流式解压与 `COPY`，单线程 |
| 装载内存 | < 1 GB | `tconst`、`nconst` 整数集合与批缓冲 |
| 匹配全量（8,730 商品 + 合集成员） | 10 分钟以内 | 候选估计数万对，特征计算在内存中按批完成 |

**实测**（2026-10-29，本机 Colima 4 核 8 GB，经真实 Airflow scheduler 在正式库 `etl_movies` 运行；估计与实测的差异不是规划目标）：

| 项目 | 实测 | 说明 |
|---|---|---|
| IMDb 压缩文件 | 1,933,753,541 字节（五个文件 229 + 520 + 84 + 789 + 312 MB） | 见 `download.json`；下载约 9 分钟 |
| 文件校验（SHA-256） | 约 4 秒 | `check_files` 步骤 |
| IMDb 装载（单事务） | 287 秒（约 4.8 分钟） | 读 `basics` 12.9M、`akas` 60.0M、`crew` 12.9M、`principals` 102.6M、`name.basics` 15.8M 行；保留 1,250,408 部作品（`movie` 760,792、`video` 332,834、`tvMovie` 156,782）、别名 6,336,339、导演 1,171,260、主要演职员 7,244,854、人名 2,050,319；`title_other` 1,620,363 |
| IMDb 装载后库内 | schema `imdb` 4,350 MB | 超出估计上界；`title_principal` 与 `title_aka` 各约 1.0–1.2 GB |
| 装载内存 | scheduler 容器采样一次 595 MiB（装载中途） | 单点采样，不是峰值记录 |
| 标准化（9,590 个商品与合集） | 26 秒（20 批） | 匹配 DAG 的 `normalize` 步骤 |
| IMDb 标题键（1,250,408 部电影类作品 + 1,620,363 部 `title_other` 作品 = 2,870,771 部，共 12,077,881 个键） | 约 4.5 分钟（576 批） | Python 里算键，是全量匹配里最慢的一步；`task4.imdb_title_key` 占 2,744 MB |
| 候选与判定（t4r1，含上一项） | 317 秒 | 9,721 个实体 → 34,625 个 IMDb 候选，另有 587 个商品对候选 |
| 只改规则版本的重算（t4r2） | 48 秒 | 复用断言、标题键与候选，只新增决策 |
| 聚类与映射 | 1.1 秒 | 一个事务 |
| 只读核对（`verify`） | 0.7–21 秒 | 随规则版本的决策量变化 |
| 全量匹配总耗时（t4r1，首次） | 346 秒（约 5.8 分钟） | `task4.run` 的起止时间 |
| schema `task4` 总大小 | 3,149 MB | 其中 `imdb_title_key` 2,744 MB、`merge_decision` 142 MB |
| 磁盘 | `etl_movies` 总计 25 GB（含任务 1–3） | 宿主机剩余 168 GB |

两个估计偏低：IMDb 库内大小（实测 4.35 GB，估计 2–4 GB）与匹配耗时中的标题键部分（`imdb_title_key` 比估计更大，因为别名所在作品和 `title_other` 里的 162 万个作品也生成标题键）。装载时间估计偏高（实测 4.8 分钟）。换规则版本不重算标题键，所以调参成本只有 1 分钟左右。

## 异常与恢复

| 情况 | 处理 |
|---|---|
| IMDb 文件哈希或大小不符 | 装载确定性失败，库中不留部分行（AC02） |
| IMDb 快照未装载就运行匹配 | `check_inputs` 失败，下游 `upstream_failed` |
| 任务 3 分类不完整（行数不等于清单） | `check_inputs` 失败 |
| 页面字段缺标题 | 商品记质量问题，作为 `page_only` 单商品作品，`display_title` 为 NULL |
| 姓名无法拆分（详情要点 “姓, 名, 姓, 名”） | 只用署名行；没有署名行时按逗号拆分并记 `name_ambiguous`，这些人名只算弱证据 |
| 候选溢出 | 记溢出与质量问题，相关商品最高判为复核 |
| 簇过大 | 质量问题，簇抽查优先抽这类簇 |
| `movie_id` 哈希碰撞 | 唯一约束失败，确定性失败 |
| 人工决策引用不存在的 ASIN 或作品 | 导入整体失败，不写部分行 |
| 中断 | 已提交批次保留，重跑跳过；聚类步骤整体重做 |

## 已知局限与风险

- **IMDb 覆盖**：冷门影片、地区发行、教学片与误判为电影的商品在 IMDb 找不到，或标题写法不同，只能成为 `page_only`。合并率取决于 IMDb 关联率，规划时无法估计，以实测为准。
- **只用精确标题键分块**：标题写法差异大的同一作品会漏合并，用评价层 S5 估计。
- **人物只做姓名比较**：同名的不同人可能把翻拍误判为重叠。翻拍通常导演不同，导演重叠才是强证据；演员同名的情况在误合并实例里观察。任务 5 有了 `person_id` 后可升规则版本。
- **IMDb principals 只含主要演职员**：页面演员多、IMDb 只给前 10 位左右，A 特征只看交集，不把“页面有而 IMDb 无”当反证。只有两边各 ≥3 人且完全不重叠才扣分。
- **待复核率高**：没有标题年份、没有 IMDb 关联的同名商品对都会进入复核，用户不一定处理（H09）。报告如实给出待复核率，不为降低它而放宽 R22。
- **评价样本有偏**：分层抽样刻意多取难例，指标不代表全量准确率。

- **正式运行的实测局限**（t4r2，`map0002`）：
  - IMDb 关联率 52.05%（4,544 / 8,730 个电影类商品）；关联不上的 4,186 个成为 `page_only`，其中很多是 VHS、教学片、地区发行，在 IMDb 里找不到或写法差异大。合并率只有 2.96%（8,730 个电影类商品合并掉 258 个）：多数电影类商品只有一个版本，同名多版本里又有一部分被姓名键的硬冲突或复核拦下。
  - 姓名键太严：硬冲突 `director_disjoint` 有 19,112 个判定，其中相当一部分来自姓名写法（编码损坏的字符、拼写、中间名、姓名顺序）而不是真正不同的导演。这会造成漏合并，例如页面写 `Chris Matthew Miller`、IMDb 写 `Chris Miller`，或页面把 `Francois` 的变音字符存成了替换字符；`examples.md` 里的易混淆未合并实例都是硬冲突拒绝的，其中是否有名字写法造成的漏合并，要看评价层 S4 的标注。任务 5 有了 `person_id` 后可升规则版本；评价层 S4 会暴露这类漏合并。
  - 一致性检查移出的唯一一例是 Halo Legends 两个版本：页面的导演列表不同（选集片），按 R14 判为冲突，记入 `cluster_check`，等待人工。
  - 合集 860 个中 488 个拆不出成员（`collection_unsplit`），拆出的 991 个成员只有 230 个关联到作品；没有 `edition` 商品的作品 187 部。
  - 候选溢出 75 个实体（截断 1,641 个候选），这些商品最高只能判复核；非电影类型命中的 `non_movie_title_type` 有 129 个待复核商品。
  - 评价池里页面簇商品对（S2）为 0，S2 的 5 对由 S1 补足，评价集对页面途径的误合并没有专门的样本。

## 与后续任务的接口

- 任务 5（人物）：`field_assertion` 中的 `director`、`actor` 断言（含原值、署名顺序、来源），以及 `imdb.name`/`title_principal` 都是人物候选的输入。
- 任务 6（上映信息）：`imdb_linked` 作品的 `tconst` 与 IMDb `startYear` 源记录可直接用于补全；商品发售日期已经是 `edition.product_release_date`，不会和上映年份混淆。
- 任务 7、任务 9：`field_assertion` 的父子链与 `merge_decision` 的特征证据是字段选择与血缘的基础。
- 任务 8：评论通过 `task4.product_movie` 的 `edition` 关系归到作品；合集评论保持在商品层。

## 实现中的修订（T4-01 至 T4-04，2026-10-29）

实现时对上文做的改动；不一致处以本节为准。已同步 spec 的 R05、R15、AC02。

- **`imdb.title_other`（改 R05、R15、AC02）**：R15 要求“电影类商品只在 `tvSeries` 等作品里有匹配时标 `non_movie_title_type`”，但 R05 原本不装载这些作品，无从识别。改为：`tvSeries`、`tvMiniSeries`、`tvSpecial`、`short`、`tvShort` 五类只把 `tconst`、类型、标题、年份与行号写入 `imdb.title_other`（basics 文件读两遍，第二遍不重复计数），不装别名、导演、演员；`tvEpisode` 等其余类型只计数不装载（约 800 万行，不值得）。`imdb_title_key` 对它们也生成标题键，`title_class = 'other'`；商品只在这类键里命中时生成 G4 候选，判定 `review`，原因 `non_movie_title_type`。
- **人物来源（对齐 R10、AC04）**：署名行存在时完全不使用详情要点，删去原文“详情要点多出的人记为 `detail_only`”。没有署名行时才按逗号拆分，每项一个词且项数为偶数的逗号串（“Arlt, Lewis, Brown, Blair”）整串标 `name_ambiguous` 并记质量问题，只算弱证据。参与评分的来源限 `byline`、`reversed`、`detail`。
- **批次与恢复判据**：新增表 `task4.entity_stage`（抽取、阶段、阶段键、实体）。每批的断言、候选、决策、实体标记与 `run_batch` 在同一事务写入，重跑只处理没有标记的实体，与批大小无关；`run_batch` 只做批次审计。阶段键：标准化 = 解析版本+标准化版本；判定 = 任务 3 规则版本+解析版本+标准化版本+规则版本+IMDb 快照。因此只改规则版本时不会重新标准化、也不会重新生成候选，只新增该规则版本的决策。
- **候选表**：`task4.candidate` 的 `imdb_snapshot_id` 对商品对商品候选同样记录（G3 的判定依赖 IMDb 关联，换快照后要有独立的候选与决策）；`generator` 增加 `G4`。`task4.merge_decision` 增加 `reason` 列（`tie`、`r22_not_met`、`candidate_overflow`、`non_movie_title_type`、`suggest_join_imdb_cluster`、`different_external_id`、`same_imdb_work`、`page_cluster`、`no_title_year`、`block_overflow` 等）。
- **G3 的细则**：两边自动关联到同一 `tconst` 的商品对记 `auto`（原因 `same_imdb_work`，簇仍由 IMDb 关联形成）；标题键块超过 30 个商品时仍枚举全部商品对，但只能判 `review`，并写 `candidate_overflow`。一个标题键块是 `decide_pair` 阶段的一个实体（`block:<标题键>`）。
- **字段断言**：增加 `genre`（只记录，对应 K 特征）；说明段的原文存去掉括号后的内部文字，位置（paren、bracket、dash、prefix）和类别在 `qualifier`、`extra`；`field_assertion` 唯一键带 `parser_version`；`extra` 存宽松键、匹配片段、拆分方式。尾部说明（破折号、冒号、逗号之后）只在完全由词表解释时才去掉，避免 “Star Trek - The Motion Picture” 被截断。
- **`product_match_status`**：只写参与匹配的商品（电影与合集）。`not_applicable`、`no_page` 由 `product_edition.match_scope` 给出，不在每个映射版本里重复 25 万行。
- **映射版本**：编号 `map0001`、`map0002`…（按抽取计数）。输入键唯一，同一输入键只会有一个映射版本。人工决策是按抽取累积的集合，集合哈希（空集为 `none`）进入输入键；`cannot_link(a, b)` 把 b 移出，`detach(a)` 把 a 移出，移出后该商品不再使用它的自动 IMDb 关联。
- **事件语义**：与父版本逐商品对比。`split` = 商品离开仍存在的旧作品、去了新作品；`merge` = 商品并入已有作品、旧作品不再出现；`move` = 其余情形（两部已有作品之间，或锚点改变）；`retire` = 旧作品不再出现，`to_movie_id` 指向接收它成员最多的作品。
- **IMDb 源记录**：只为进入候选的作品登记 `source_record`（基于 `title.basics` 行号）；导演与演员的文件与行号放在决策 `features.evidence.imdb` 里，没有逐人登记。
- **纯函数与数据库代码的分工**：`match.py` 只含纯规则；取数与落库在 `candidates.py`；运行编排在 `runner.py`。运行锁按抽取加（比“同一输入”更严），拿不到锁抛 `RunBusy`，按 AC10 是确定性失败，不自动重试。
- **匹配 DAG 与 `limit`**：`runner.execute_match` 暂未提供 `limit`，试跑限定留给 T4-05 的 DAG 步骤。

## 实现中的修订（T4-05 至 T4-08，2026-10-29）

不一致处以本节为准；已同步 spec 的 R40、AC10–AC13 的说明与 tasks、checklist。

- **DAG 分三个步骤共用一条运行**：规划里 `normalize → candidates_decide → cluster_map` 是三个 Airflow 任务，而 `runner.execute_match` 是一次完整调用。新增 `runner.run_stage`：每个步骤各持一次抽取锁，靠 `task4.run.airflow_run_id` 找回同一条运行；步骤失败把运行记为 `failed`，同一 DAG run 里重试时改回 `running` 并接着用，已提交的批次不重做；`close_abandoned` 不关闭本次 DAG run 自己的 `running` 运行。输入键已有成功运行时，`normalize` 步骤记 `reused`，后两个步骤直接返回 `reused`。`execute_match` 保留，供测试与命令行直接使用。
- **DAG 参数**：规划里的“人工决策集”不是参数。决策集按抽取累积在 `task4.human_decision` 里，哈希进输入键，所以导入决策之后再触发一次 DAG 就是新的输入键。`limit` 只限制标准化的商品数，之后的 `candidates_decide` 因仍有未标准化的商品而确定性失败，不生成映射版本（和任务 3 的 `limit` 语义一致：试跑不冒充全量）。
- **故障注入**：`before_commit:<阶段>:<批号>`（阶段为 `normalize`、`imdb_keys`、`decide`、`decide_pair`）与 `transient_once:<标记文件>`，仅 `ETL_ENV=test`。
- **规则集登记**：`match.RULESETS` 登记全部规则版本，`match.ruleset(version)` 取用，未登记是确定性错误；`CURRENT_RULE_VERSION = t4r2` 是 DAG 参数默认值，代码里的 `RULE_VERSION`/`DEFAULT_RULES` 仍是 t4r1（单元与集成测试依赖它的数值）。
- **IMDb 引用缺失的容忍**（改 `imdb.problems`）：真实快照里导演有 86 行、主要演职员有 618 行引用了 `name.basics` 没有的 `nconst`（占比 0.001% 与 0.009%）。这是源数据瑕疵，不为通过核对而删行；占比不超过 0.1% 时记为 `warnings`（写入 `summary.json`），超过才算问题。比较时这些人没有姓名，被自然跳过。首次装载因此在 `verify_snapshot` 失败过一次（`t4_imdb_load_1`），修正后重新触发（`t4_imdb_load_2`，装载 `reused`）。
- **核对 `check.py`**：`verify` 在只读事务里核对一个映射版本，`problems` 给出问题列表；DAG 的 `verify` 步骤与命令行共用。核对的 R22 条件落在判定的 `features`：IMDb 自动关联要求 `evidence.start_year` 非空、导演严格重叠或演员严格重叠、没有硬冲突；商品对自动要求同一 IMDb 作品或“标题年份相同且导演严格重叠”。
- **报告 `report.py`**：`build_summary`（合并前后、作品来源、判定、合集、剧集、评论口径、未知说明段）、`examples`（确定性选择：已有用户标注的优先，其次 IMDb 关联的多商品作品，再按 `md5`）、`export_files`、命令行 `summary|examples|export`。
- **评价工具 `evaluate.py`**：
  - 开发池存的是实体编号（商品 ASIN 与合集成员 `ASIN#序号`），按标题键划分，所以同一标题键下的商品与成员整组落在同一侧。
  - 评价表的 `predicted_same` 在导出时写入 `eval_pair`，指标按导出时的预测计算，之后改规则不影响已导出的评价集；`metrics` 会提示导出后出现了新的映射版本。
  - 层 S2（页面簇商品对）在评价池里没有可抽的对（`available: 0`），按 design 的补足规则由 S1 补 5 对，实际层数写入 `eval_set.strata`。
  - S5（标题键相近）用字符三元组 Jaccard ≥ 0.6，倒排索引并跳过出现在超过 250 个标题键里的三元组；候选对按 `md5` 取前若干个，保证确定性。
  - 已存在 `eval_pairs.csv` 或 `cluster_sample.csv` 时不覆盖（防止丢失标注），除非加 `--force`；评价集每个映射版本只抽一次，再次导出按库里的样本重写。
  - F1 的分母 `P + R` 为 0 时也记“无定义”（与 Precision、Recall 的分母为零同一规则）。
  - 簇抽查：评价池里 ≥3 个商品的簇只有 11 个，取 10 个；2 个商品的簇 182 个，取 10 个。
- **端到端**：`scripts/seed_task4_e2e.py` 在测试容器里为 `etl_test` 准备合成的任务 3 数据和合成 IMDb 快照（脚本经标准输入传入），再经真实 scheduler 跑两个 DAG 的成功、重跑、不存在快照三次，见 runbook 第 2 节。
