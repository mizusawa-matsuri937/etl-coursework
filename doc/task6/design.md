# 任务 6 设计思路（补全上映信息）

状态：2026-10-30 编写计划，待用户审批；实现未开始，实现中的修订将记在文末。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明怎么实现、为什么这样选。择优顺序、置信度、B3 的天数与上界置信度分档都是**规划默认值**（规则版本 `t6r1`），只在开发池上调整、评价表导出前冻结；资源数字都是**估计**，实测在 T6-06 补记。规划时的数字来自 2026-10-30 正式库只读查询（下文标“实测”），正式数字以 AC16 为准。

## 整体思路

任务 6 是集成层里的“缺失信息补全”：输入是任务 4 的作品映射 `map0002`（作品与 `tconst`）、任务 3/4 的页面字段与断言（标题年份、商品发售日期）、任务 5 的升级线索、任务 1 的有效评论与 IMDb 快照；输出是作品的上映信息（候选、选择记录、推断上界）与商品层的日期（集成层与目标层）。全程离线，不联网。

先定语义，再取值：

| 时间 | 语义 | 粒度 | 本任务的来源 | 去向 |
|---|---|---|---|---|
| 首次公开发行年份 | 作品第一次公开发行（院线、电视首播或直接发行录像，不分地区） | 作品 | IMDb `startYear`（经任务 4 关联，或经任务 5 线索）；Amazon 商品标题年份 | `release_candidate` → `release_selection` |
| 地区院线日期、完整首映日期 | 某地区院线上映日 / 首映日 | 作品 × 地区 | 无获准且带日期的来源（H01） | 缺失，原因 `no_permitted_dated_source` |
| 商品发售日期 | 这个 DVD、Blu-ray、VHS 商品的发售日；或 Amazon 上架日 | 商品（ASIN） | 页面 `Release date`、`Publication date`、`Date First Available` | `product_release_date` |
| 最早评论时间 | 该作品在 Amazon 的第一条评论 | 作品 / 商品 | 任务 1 有效评论 | `movie_review_time`、`product_review_time` |
| 推断上界 | “首次公开发行不晚于该时间” | 作品 | 最早评论时间 + 条件 B1–B5 | `inferred_release_bound`（`status = inferred`） |

不同行的时间不放进同一张候选表，也不互相择优（R04）：选择记录只读“首次公开发行年份”的候选；商品发售日期和评论时间各在自己的表里，推断上界只引用评论时间。任务 7 的跨来源冲突（例如 IMDb 年份与页面标题年份差很多）在此基础上处理，本任务只记录并按固定顺序采用。

分六步，每一步的结果都入库、都可追溯：

1. **年份候选**（R06–R10）：对每部作品收集同语义候选：IMDb 关联作品的 `startYear`、任务 5 线索指向的 IMDb 作品的 `startYear`、商品标题年份。每条候选带来源定位与匹配证据。
2. **商品发售日期**（R15–R16）：对参与匹配的每个商品，把任务 4 的 `edition.product_release_date` 断言按页面标签分成发售与上架两类，标出可疑值。
3. **评论时间**（R17）：按商品算最早与第二早有效评论，再按作品（只经 `edition`）汇总。
4. **择优与状态**（R11–R14）：按固定顺序采用候选，写选择记录、状态、缺失原因与分歧质量问题，生成上映信息版本。
5. **推断上界**（R18–R22）：对有评论的作品逐条评估 B1–B5，全部通过的另存推断上界；B4 矛盾写质量问题。
6. **报告、查询与评价**（R29–R35）：导出覆盖率、实例与查询结果；另有命令导出评价表、导入标注、计算指标。

**为什么只做年份**（R03，H01）：作业明确 IMDb 公开基础表只提供上映年份；带完整日期与地区的来源要么没有开放接口（豆瓣），要么需要联网抓取或注册 API key（Wikidata、TMDb），用户选择本轮不联网。所以 `release_date` 全部为 NULL，只有 `release_year` 与 `precision = year`，地区院线日期整体缺失并写原因；不把商品发售日期或评论时间拿来“补日期”。

**为什么任务 5 线索可以当候选**（R07，H03）：线索的含义是“这个没有 IMDb 关联的商品上，至少两位署名人物经作品年表反查都指向同一部 IMDb 作品”，证据是“多位已核实的人 + 同标题键作品”，比单靠标题强，但它不是任务 4 的作品判定，所以单列来源 `imdb_t5_hint`、状态 `hint_matched`、置信度低于直接关联，评价单独成层。`tconst` 已被另一部作品占用的 40 条说明任务 4 漏合并（这两部作品应是同一部），若直接采用会让两部“不同作品”共享一个 IMDb 年份而掩盖问题，所以只记录、留给任务 4 升级。

**为什么推断上界要设条件**（R18，H02）：最早评论时间只在“评论确实是这部作品的、时间可信”时才意味着作品此前已公开。实测有三类反例（见下文“输入依据”）：翻拍或映射错位（The Punisher 2004 的最早评论在 2000 年，像是 1989 版的评论）、提前评论（Kill Bill: Vol. 2 的最早评论在 2003-12，比 2004 年上映早）、孤立的早期评论。条件 B1–B5 分别针对映射未确认、合集、孤立点、与已有年份矛盾、时间超范围；预售没有标记，只能讨论、不能宣称排除（H07）。

## 输入依据与规划时快照

- 任务依据：作业任务 6 与第五、八、十部分；交接文档 `doc/handoffs/2026-10-30-task5-to-task6.md` 第 2、3 节。
- Git：任务 5 经 PR #5 合并到 `main`（`1625ad9`）；本任务分支 `task6-release-info` 从该提交建立。
- **实测**（2026-10-30，正式库 `etl_movies` 只读查询；与交接文档一致处不重复列出）：

| 项目 | 实测 | 与交接文档的差异 |
|---|---|---|
| 迁移版本 | `001,101,102,103,201,301,302,401` | 一致 |
| 映射版本 | `map0001`（`t4r1`）、`map0002`（`t4r2`） | 一致 |
| `map0002` 作品 | 8,659 部：`imdb_linked` 4,473（有 `tconst`，`tconst` 互不相同）、`page_only` 4,186；经 `edition` 的 8,472 部，只经 `contains` 出现的 187 部 | 交接文档写“8,730 个商品 → 8,472 部作品”，未计只经 `contains` 出现的 187 部；本任务以 8,659 为作品分母，另报 8,472 |
| IMDb 关联作品的 `startYear` | 4,473 部全部非空；`titleType`：`movie` 3,863、`tvMovie` 335、`video` 275 | — |
| 商品–作品关系 | `edition` 8,730（`auto_decision` 4,544、`singleton` 4,186），`contains` 230 行（223 部成员作品） | 一致 |
| 任务 4 匹配状态（ASIN） | `singleton` 7,333、`review_pending` 899、`merged` 498、`collection_unsplit` 488、`collection_split` 372 | — |
| 商品标题年份（`title_year` 断言） | 190 条（商品 189、合集成员 1）；有标题年份的作品：IMDb 关联 91 部、页面独有 62 部；与 IMDb 年份比较：相同 87、相差 1 年 7、相差超过 1 年 0；没有作品内标题年份不一致 | — |
| 商品发售日期（`edition.product_release_date`） | 9,558 条、7,940 个 ASIN，规范值全部为 `YYYY-MM-DD`；按页面标签：`Release date` 7,664、`Date First Available` 1,724、`Publication Date` 111、`Publication date` 59 | 交接文档只写了总数；`Date First Available` 是上架日期，语义不同（R15） |
| 商品发售日期可疑值 | `Release date` 中 1 月 1 日 342 条（4.5%），晚于 2012 年的 429 条；最早 1931-01-01，最晚 2027-01-01 | — |
| 有效评论（任务 1 `review_staging`） | 时间范围 1997-08-20 至 2012-10-25，全部为 UTC 零点（精度为日）；电影类商品 298,769 条（8,718 个 ASIN、8,461 部作品），合集商品 860 个、16,241 条 | 交接文档的 301,326 条是 `review_record` 行数，含 2,557 条隔离记录；本任务只用有效评论 |
| 最早评论与 IMDb 年份 | 有评论的 IMDb 关联作品 4,281 部：最早评论年份早于 IMDb 年份 24 部、同年 746、晚于 3,511；排除孤立点后早于 IMDb 年份的 17 部 | — |
| 最早评论与商品发售日期 | 同时有评论与 `Release date` 的 7,041 个 ASIN 中，最早评论早于该 ASIN 发售日的 3,265 个，早一年以上的 1,967 个 | 说明 Amazon 合并展示各版本评论，不能用“评论早于本商品发售日”判定预售（H07） |
| 第二早评论间隔（按作品） | 只有一条评论 1,502、第二条在 365 天内 5,157、超过 365 天 1,802 | — |
| 任务 5 升级线索 | 335 行（332 个 ASIN、328 个 `tconst`）：电影类商品 293 行（`tconst` 未被占用 253、已被另一作品占用 40），合集成员 42 行（未占用 26、已占用 16）；没有一部作品得到多个 `tconst`；线索年份与标题年份同时存在的 2 部，均一致 | 交接文档只写了 335 |
| 年份来源估算（按 R07–R11） | IMDb 关联 4,473、线索 253、只有标题年份 60、无候选 3,873（44.7%） | — |
| 上界评估估算（按 R18） | 有评论作品 8,461：通过 4,577（有年份 2,967、无年份 1,610），B1 不通过 899，B3 不通过 2,967，B4 不通过 18 | — |

- 最早评论早于 IMDb 年份的例子（实测）：Nautilus 2000 / 1999-12-01、Once Upon a Time in Mexico 2003 / 1999-03-16、The Punisher 2004 / 2000-06-17、The Omen 2006 / 2000-04-19、Sleepless Nights 2022（`video`）/ 2003-09-29、Kill Bill: Vol. 2 2004 / 2003-12-18、Harry Potter and the Half-Blood Prince 2009 / 2008-05-29。前几部多为翻拍或映射到同名的另一部作品，后两部是上映前的提前评论。

## 开源参考

按 CLAUDE.md 要求，动手前对比了几个开源项目与公开数据规范的做法（2026-10-30 检索）：

| 项目 | 借鉴什么 | 不直接用的原因 |
|---|---|---|
| [scrapinghub/dateparser](https://github.com/scrapinghub/dateparser)（[设置文档](https://dateparser.readthedocs.io/en/latest/settings.html)） | 解析结果单独返回 `period`（`day`/`month`/`year`），缺日时用 `PREFER_DAY_OF_MONTH` 补成当月首日、末日或当前日 | 正是作业禁止的“补成 1 月 1 日”的做法：它把精度和补全混在一个日期对象里。我们只借“精度与值分开返回”的思路，年份不变成日期；页面日期已由任务 4 规范为 `YYYY-MM-DD`，不需要再解析 |
| [python-edtf](https://pypi.org/project/edtf/)（PyPI `edtf`，实现美国国会图书馆 EDTF；GitHub 有多个分支，如 [ccnmtl/python-edtf](https://github.com/ccnmtl/python-edtf)） | 用精度/掩码表示不完整日期（`1985`、`1985-06`、`198X`），并按精度导出区间的上下界 | 新增依赖要重新锁定 `requirements.lock`；我们只需要 `year`/`month`/`day` 三档和“不晚于”上界，用两列（值 + 精度）表达，便于 SQL 查询与约束 |
| [Wikidata 的 publication date（P577）](https://www.wikidata.org/wiki/Property:P577) 与时间值模型 | 时间值带精度（9 年、10 月、11 日），上映日期用“地区”等限定符区分，一部作品可有多条日期 | 本轮不联网（H01）；借“精度随值保存、地区与类型作为限定”的模型，`release_selection` 留 `region`、`release_type` 列，当前只有 `first_release` 与 NULL 地区 |
| [TMDb release_dates](https://developer.themoviedb.org/docs/region-support)（[类型说明](https://tmdb.org/bible/movie/59f3b16d9251414f20000009)，开源客户端 [tmdb-api](https://docs.rs/tmdb-api/latest/src/tmdb_api/common/release_date.rs.html) 的枚举） | 上映类型分首映、限映、院线、数字、实体（VHS/DVD/Blu-ray）、电视六类，按国家给日期；“主上映日”按限映/院线 → 首映的顺序取 | 需要 API key 与逐片请求（H01）；借“实体发行（DVD 等）是一种独立的发行类型，不与院线日期混排”的分类，作为 R04、R15 的依据，以及“按类型固定优先序”的择优方式（R11） |
| [IMDb Non-Commercial Datasets](https://developer.imdb.com/non-commercial-datasets) 数据字典 | `startYear` 为作品的发行年份（剧集为开播年份），`\N` 为缺失 | 只限个人、非商业使用；文件与导出不入仓库（R28） |

结论：不新增依赖，自写“值 + 精度 + 语义 + 来源”的小模型；择优用固定顺序而不是加权打分，理由是各来源语义相同、强弱分明，固定顺序可解释、可在报告里逐条说明。代价是不能表达“多个弱来源一致胜过一个强来源”，这类情况在本数据里很少（标题年份与 IMDb 年份相差超过 1 年的实测为 0），并以 `year_disagreement` 暴露。

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/501_task6_schema.sql` | schema `task6` 的下文各表、约束与索引；登记迁移 `501` |
| `src/etl_task6/rules.py` | 纯函数：候选置信度、择优顺序、状态与缺失原因、分歧判定、B1–B5 与上界置信度、商品日期标记；`RuleSet`、`RULESETS`、`CURRENT_RULE_VERSION = t6r1` |
| `src/etl_task6/candidates.py` | 三类年份候选与线索处理记录（R06–R10），按作品分批 |
| `src/etl_task6/product_dates.py` | 商品发售日期与标记（R15–R16），按商品分批 |
| `src/etl_task6/reviews.py` | 商品层最早、第二早评论（按商品分批）与作品层汇总（R17） |
| `src/etl_task6/select.py` | 择优、选择记录、质量问题、上映信息版本、版本差异（R11–R14、R23） |
| `src/etl_task6/bounds.py` | 上界评估与推断上界、`review_before_release_year`（R18–R21） |
| `src/etl_task6/run.py`、`runner.py` | 迁移应用、输入键、运行审计、抽取锁、批次与实体阶段标记、分步骤编排 |
| `src/etl_task6/report.py`、`query.py`、`check.py` | 报告、实例与 R22 的计数；两个查询（R30）；正式库只读核对（AC16） |
| `src/etl_task6/evaluate.py` | 开发池冻结、评价表导出、标注导入、指标（R32–R35） |
| `src/etl_task6/pipeline.py`、`dags/amazon_movies_task6_release.py` | DAG 步骤函数与编排 |
| `scripts/seed_task6_e2e.py` | 为真实 scheduler 端到端准备合成数据（写 `etl_test`） |
| `tests/fixtures/task6/` | 合成页面字段、任务 4 映射与断言、任务 5 线索、评论与 IMDb 行，预期值说明 |

复用而不复制：

- `etl_task1`：`config.load_settings`、`db.connect`、`db.advisory_key`、`reporting.write_atomic`、`reporting.file_digest`、`reporting.utc_now`、`pipeline.is_transient`、`pipeline.gate_decision`。
- `etl_task2.pipeline.run_step` 与失败文件写法。
- `etl_task4.evaluate.prf` 的“无定义”约定（本任务的指标是比例，不是 P/R，自写 `ratio()`，分母为 0 同样返回“无定义”）；`etl_task4.run`、`etl_task5.run` 的锁、批次与实体标记写法（任务 6 的表单独建，不写 `task4`、`task5`）。
- 只读：`task4.movie`、`task4.product_movie`、`task4.product_match_status`、`task4.product_edition`、`task4.field_assertion`（`title_year`、`edition.product_release_date`）、`task4.source_record`、`task3.page_field`（取原始标签）、`task5.person_mapping_version`、`task5.t4_upgrade_hint`、`task5.link_decision`、`imdb.title`、`imdb.snapshot`、`imdb.source_file`、`public.review_staging`。

## 数据模型

作业第八部分的实体与本任务表的对应：

| 作业实体 | 本任务的表 | 说明 |
|---|---|---|
| Movie 的上映信息 | `task6.release_selection`（按上映信息版本） | `release_year`、`release_date`、`precision`、`region`、`release_type`、`status` |
| FieldAssertion | `task6.release_candidate` | 每条候选的原值、规范值、精度、语义、来源与证据 |
| FieldSelection | `task6.release_selection` 的采用候选、参与候选与理由 | 每部作品一条 |
| ProductEdition 的商品发售信息 | `task6.product_release_date` | 按 ASIN，与作品年份分开 |
| 推断 | `task6.bound_assessment`、`task6.inferred_release_bound` | 条件评估与推断上界分开 |
| SourceRecord | 复用 `task4.source_record`（页面快照）与 `imdb.source_file` + 行号；评论用任务 1 的 `source_review_id` | 不另建 |
| ETLRun 与 QualityIssue | `task6.run`、`task6.run_batch`、`task6.quality_issue` | — |

外键指向 `public.extraction`、`task4.mapping_version`、`task4.field_assertion`、`task4.source_record`、`task5.person_mapping_version`、`imdb.snapshot`，不在任务 1–5 的表上加列或约束（R27）。

**运行与版本**

- `task6.run`：`run_id`，`kind`（`release`/`evaluate`），输入键各项，`status`（`running`/`succeeded`/`failed`/`reused`/`interrupted`），`params`（`batch_size`、`limit`），计数、起止时间、`dag_id`、`airflow_run_id`、`failure_reason`。
- 输入键：（`extraction_id`, `t4_mapping_version_id`, `parser_version`, `t5_person_mapping_version_id`, `imdb_snapshot_id`, `rule_version`）。已有成功运行时新运行记 `reused`（R24）。
- `task6.run_batch`、`task6.entity_stage`：与任务 4、5 相同的批次审计与实体阶段标记。阶段键：候选 = 映射版本 + 解析版本 + 人物映射版本 + IMDb 快照（候选值与规则版本无关，置信度在择优时按规则版本取）；商品发售日期 = 抽取 + 解析版本；评论时间 = 抽取 + 映射版本。所以只改规则版本时不重新抽取（AC11）。
- `task6.release_version`：`release_version_id`（`rv0001`…），`parent_version_id`、`run_id`、输入键各项、`counts`（R29 的数量）、`created_at`。

**候选与线索**

- `task6.release_candidate`：唯一键（阶段键, `movie_id`, `source`, `source_key`）。
  - `source`：`imdb_t4_link`、`imdb_t5_hint`、`amazon_title_year`；`source_key` 为 `tconst` 或 `<ASIN>:<断言 ID>`。
  - 值：`raw_value`（原文，如 `1999`）、`value_year`、`precision`（本任务恒为 `year`，列上有 CHECK）、`semantic = first_release`、`region`（NULL）。
  - 来源：`source_ref` jsonb（IMDb：快照、文件名、行号、`tconst`、`titleType`；页面：ASIN、区域、页面字段序号、断言 ID、`source_record_id`、最终地址）、`source_acquired_at`（IMDb 快照下载时间或页面采集时间）、`extracted_at`（本次抽取时间）。
  - 匹配证据：`match_evidence` jsonb（`imdb_t4_link`：任务 4 的 `product_movie.basis`、`decision_id`、关联的 ASIN；`imdb_t5_hint`：线索的 `mention_keys`、`link_decision_ids`、`mention_count`、人物映射版本；`amazon_title_year`：商品经 `edition` 关系归到该作品）。
- `task6.hint_use`：主键（阶段键, `asin`, `member_ordinal`, `tconst`）；`movie_id`、`action`（`candidate`/`tconst_owned`/`collection_member`/`multiple_tconst`/`movie_already_linked`）、`owner_movie_id`（`tconst_owned` 时占用它的作品）。

**选择与推断**

- `task6.release_selection`：主键（`release_version_id`, `movie_id`）。
  - `status`：`verified`/`hint_matched`/`page_reported`/`missing`；`missing_reason`。
  - `release_year`、`release_date`（恒为 NULL）、`precision`、`region`（NULL）、`release_type = first_release`、`regional_release_missing_reason = no_permitted_dated_source`。
  - `selected_candidate_id`、`considered_candidate_ids`（数组）、`selection_rule`（如 `t6r1:order:imdb_t4_link>imdb_t5_hint>amazon_title_year`）、`reason`、`confidence`、`disagreement` jsonb（与未采用候选的年差）。
  - CHECK：`status = missing` 当且仅当 `release_year` 与 `selected_candidate_id` 为 NULL；`precision = year` 时 `release_date` 为 NULL。
- `task6.bound_assessment`：主键（`release_version_id`, `movie_id`）；`checks` jsonb（B1–B5 各自的结果与所用值，如 `review_pending` 的 ASIN、第二早评论间隔天数、比较的年份）、`passed`、`failed`（数组）。
- `task6.inferred_release_bound`：主键（`release_version_id`, `movie_id`）；`bound_time`、`bound_precision = day`、`bound_kind = not_later_than`、`basis_source_review_id`、`basis_asin`、`conditions`（与评估相同的条件快照）、`confidence`、`status = inferred`、`rule_version`。没有外键指向 `release_selection` 的值列，也不被择优读取（R20）。

**商品层**

- `task6.product_release_date`：主键（`extraction_id`, `parser_version`, `assertion_id`）；`asin`、`kind`（`media_release`/`listing_first_available`）、`label_raw`、`raw_value`、`value_date`、`precision`（`day`）、`flags`（`jan_first_suspect`、`after_review_period`）、`section`、`page_ordinal`、`source_record_id`。
- `task6.product_review_time`：主键（`extraction_id`, `asin`）；`product_kind`（`movie`/`collection`）、`earliest_time`、`earliest_source_review_id`、`second_time`、`review_count`。
- `task6.movie_review_time`：主键（`extraction_id`, `t4_mapping_version_id`, `movie_id`）；`earliest_review_time`、`earliest_precision = day`、`earliest_source_review_id`、`earliest_asin`、`second_review_time`、`review_count`、`reviewed_asin_count`、`edition_asin_count`。由 `product_review_time` 汇总：作品的第二早评论一定在各商品的前两条之中，所以商品层只存两条即可。
- `task6.quality_issue`：运行、实体、类型（`year_disagreement`、`review_before_release_year`、`hint_tconst_owned`、`jan_first_product_date` 等）与细节。

**评价**：`task6.dev_pool`（冻结的 `movie_id`）、`task6.eval_set`、`task6.eval_item`（层、作品、导出时的上映信息版本、系统年份、状态与上界，用于计算，不导出）、`task6.eval_label`（`true_year`、`year_label`（`known`/`unsure`）、`tconst_label`（`same`/`different`/`unsure`/`na`）、`note`、`annotator`、`import_file_sha256`）。

## 年份候选（R06–R10）

- **`imdb_t4_link`**：`task4.movie.tconst` 非空时读 `imdb.title`（同一快照）；`start_year` 为 NULL 不产生候选。证据取该作品 `edition` 关系中 `basis = auto_decision` 的商品与决策 ID；只经 `contains` 出现的成员作品取 `contains` 关系的决策。置信度 0.95（H05）。
- **`imdb_t5_hint`**：读 `pmap0001` 对应阶段键（`map0002|t3p1|t5n1|t5r1|imdb_2026-10-29`，由 `task5.person_mapping_version` 的列拼出）的线索，逐条分类：
  1. `member_ordinal > 0`：`collection_member`；
  2. 商品所在作品已有 `tconst`：`movie_already_linked`（实测 0 条，保留防御）；
  3. 线索 `tconst` 是映射版本中某部作品的 `tconst`：`tconst_owned`，记占用者，写质量问题 `hint_tconst_owned`（任务 4 漏合并的线索）；
  4. 同一作品经不同商品得到多个未占用的 `tconst`：全部记 `multiple_tconst`；
  5. 其余：`candidate`，产生候选，置信度 0.80。
- **`amazon_title_year`**：读 `field_name = 'title_year'` 的断言，`entity_type = product_edition` 的经 `edition` 关系归到作品，`collection_member` 的经 `contains` 成员序号归到成员作品；置信度 0.60。
- 来源获取时间：IMDb 取 `imdb.snapshot.downloaded_at`，页面取 `task4.product_edition.fetched_at`；抽取时间为本次运行时间（R10）。

## 择优与状态（R11–R14）

1. 收集作品的全部候选（同一阶段键）。
2. 有 `imdb_t4_link` → 采用，`verified`；否则有 `imdb_t5_hint` → 采用，`hint_matched`；否则商品标题年份全部相同 → 采用（取断言 ID 最小的一条为代表），`page_reported`；标题年份不一致 → `missing`（`title_years_disagree`）；作品有 `multiple_tconst` 线索且没有其他可用候选 → `missing`（`hint_ambiguous`）；什么都没有 → `missing`（`no_candidate`）。
3. 采用值与每条未采用候选比较：相差 1 年计数，相差超过 1 年写 `disagreement` 与质量问题 `year_disagreement`（R13）。
4. `confidence` 取采用候选的置信度；`reason` 写成可读的一句，例如“采用 IMDb tt0120737 startYear 2001（经任务 4 自动关联）；另有商品标题年份 2001 一致”。

选择记录只读 `release_candidate`；SQL 层面没有任何路径从 `product_release_date`、`movie_review_time` 或推断上界取值（R14），核对脚本检查所有非 NULL 的 `release_year` 都等于其 `selected_candidate_id` 的 `value_year`。

## 商品发售日期（R15–R16）

- 从 `task4.field_assertion`（`edition.product_release_date`）出发，按断言的 `source_locator`（区域、序号）回查 `task3.page_field.label_raw`，标签不区分大小写：`release date`、`publication date` → `media_release`；`date first available` → `listing_first_available`；其他标签（实测没有）→ 质量问题 `unknown_date_label`，不入表。
- 规范值沿用任务 4（`YYYY-MM-DD`，精度 `day`）；只有年份或年月的规范值（实测没有）按实际精度保存，不补日。
- 标记：月日为 01-01 → `jan_first_suspect`；日期晚于有效评论的最大时间（实测 2012-10-25）→ `after_review_period`（多为后来的重新发行，页面是 2026 年采集的当前版本）。
- 商品发售日期只在报告、查询、R22 的计数与评价导出的“参考信息”里出现。

## 评论时间与推断上界（R17–R22）

**商品层**：对参与匹配的商品（电影类与合集），从 `public.review_staging`（`classification = valid`，用 `(extraction_id, product_id)` 索引）取每个 ASIN 按（`review_time_utc`, `source_review_id`）排序的前两条与条数。按商品分批，默认 1,000 个 ASIN 一批（实测全量聚合约 7 秒）。

**作品层**：只汇总 `edition` 关系的电影类商品；合集商品的评论不进入成员作品（与任务 4、5 一致，R17）。

**上界条件（`t6r1` 默认值）**：

| 条件 | 判定 | 针对的问题 |
|---|---|---|
| B1 映射已确认 | 作品的 `edition` 商品在 `task4.product_match_status` 中都不是 `review_pending` | 作品归属未定，评论可能属于别的作品 |
| B2 非合集 | 作品至少有一个 `edition` 商品；评论只来自 `edition` 商品 | 合集评论说的可能是另一部成员作品 |
| B3 不孤立 | 第二早评论存在，且距最早评论不超过 365 天 | 孤立的早期评论（错标商品、年份错误）；也排除只有一条评论的作品 |
| B4 不矛盾 | 作品有 `verified` 或 `hint_matched` 年份 Y 时，最早评论年份 ≥ Y；没有这两类年份时视为通过 | 翻拍或映射错位（最早评论早于作品年份）；`page_reported` 年份不参与比较，因为标题年份可能是版本年份 |
| B5 时间可信 | 最早评论时间在该抽取有效评论的时间范围内，且不早于 1997-01-01 | 解析异常的时间 |

- **置信度**（工程默认）：通过时 0.6；最早评论后 365 天内的评论不足 3 条时 0.4。上界是“不晚于”，对没有年份的作品只说明“2003 年 5 月前已公开”，不代替年份。
- **B4 矛盾**：写 `review_before_release_year`，细节含作品、`tconst`、年份、最早评论的源评论 ID 与 ASIN；不改任务 4，作为任务 4 升级或任务 7 的线索（R21）。
- **为什么 B4 不用 `page_reported`**：标题年份是商品自述，有时是重新发行年份；用它否决上界会误伤。
- **为什么不用商品发售日期判断预售**（R22，H07）：实测 46% 的 ASIN 最早评论早于本商品发售日，原因是 Amazon 把同一作品各版本的评论合并展示（SNAP 数据也继承了这一点），这些评论多是对旧版本或院线上映的评论，不是预售。数据里没有任何预售标记，因此报告只给出：合集商品评论数（不归入作品）、多版本作品（实测 240 部）中最早评论所在商品与最早 `media_release` 所在商品是否相同、最早评论早于作品全部 `media_release` 日期的作品数；并说明这些评论在 B1–B5 下仍可能是提前评论（如 Kill Bill: Vol. 2），推断上界因此标 `inferred`、置信度不高于 0.6。

## 版本、幂等、分批与编排（R23–R26）

**分批**：

- 年份候选按作品分批（默认 1,000 部一批，按 `movie_id` 排序），实体为 `movie_id`，阶段名 `candidates`；线索处理与它同批（按线索所在商品的作品）。
- 商品发售日期按商品分批（默认 1,000 个 ASIN），阶段名 `product_dates`；商品层评论按商品分批，阶段名 `product_reviews`。
- 作品层评论汇总、择优、上界评估与推断上界、质量问题、版本计数是全局步骤，在一个事务里生成整个上映信息版本（规模约 8,659 部作品，允许一次提交）。
- 每批的目标行、实体阶段标记与 `run_batch` 在同一事务提交；中断后同一输入键重跑跳过已标记的实体；全局步骤没有提交就从头重做（AC12）。

**锁**：按抽取加 advisory lock（与任务 4、5 不同的键），拿不到锁抛 `RunBusy`，确定性失败。

**版本与差异**：同一输入键已有成功运行 → `reused`。规则版本变化 → 候选、商品日期、评论时间的阶段键不变而复用，只重做全局步骤，生成新版本（父版本为上一版本）。`python -m etl_task6.report diff <旧> <新>` 列出状态、年份、上界或上界条件变化的作品与原因（例如“B3 天数 365 → 180”“输入映射版本变化”）。

**DAG `amazon_movies_task6_release`**：`check_inputs → candidates → product_dates → review_times → select → bounds → verify → finalize_report → gate`。

- 参数：抽取、任务 4 映射版本、任务 5 人物映射版本、IMDb 快照、`rule_version`、`batch_size`、`limit`。
- `check_inputs`：映射版本存在且属于该抽取；人物映射版本基于同一映射版本；IMDb 快照为 `loaded`；迁移 `302`、`401`、`501` 已应用（缺 `501` 时应用）。
- `select` 生成上映信息版本，`bounds` 在同一版本上写上界，靠 `task6.run.airflow_run_id` 找回同一条运行（任务 4、5 的做法）。
- `verify` 只读核对 AC16 的约束；`finalize_report` 与 `gate` 的触发规则为 `all_done`，门禁只在上游全部成功时通过。
- 暂时错误（连接中断、死锁）最多重试 2 次；确定性错误（输入缺失、约束违反、锁被占用）不重试。
- `limit` 只限制候选阶段的作品数，之后的步骤因仍有未处理作品而确定性失败，不生成上映信息版本（试跑不冒充全量）。

**命令**（不进 DAG，由人触发）：`python -m etl_task6.evaluate freeze|export|import|metrics`；`python -m etl_task6.report summary|examples|export|diff`；`python -m etl_task6.query movie <movie_id>`、`query gaps [--status missing|inferred]`；`python -m etl_task6.check`。

**故障注入**：`before_commit:<阶段>:<批号>` 与 `transient_once:<标记文件>`，仅 `ETL_ENV=test`。

## 统计、报告与查询（R29–R31）

`summary.json` 与 `report.md` 包含：

- 作品数（8,659 = 经 `edition` 8,472 + 只经 `contains` 187）；`release_year` 按状态的作品数与占比，`missing` 按原因；同样口径按参与匹配的商品（作品有年份的商品数 / 9,590）与全部产品（/ 253,059）各报一次覆盖率。
- 候选：按来源计数；`year_disagreement` 数与年差分布；相差 1 年的数；`titleType` 分布。
- 线索处理：按 `action` 计数，`tconst_owned` 前 20 例（含占用者）。
- 地区院线日期与完整首映日期：覆盖 0，缺失原因 `no_permitted_dated_source`。
- 商品发售日期：按 `kind` 与标记计数，有发售日期的商品数。
- 评论时间：有 `earliest_review_time` 的作品数与占比；上界评估：评估数、各条件未通过数（一个作品可以多项未通过，另报“首个未通过条件”的互斥分布）；推断上界数与置信度分布，其中无年份作品的数；`review_before_release_year` 数与前 20 例。
- R22 一节：合集商品与其评论数、多版本作品的最早评论商品比较、最早评论早于全部 `media_release` 的作品数，以及“数据没有预售标记”的说明。
- 实例（`examples.md`，R31）：按 `md5(movie_id)` 在每类中取前 3（评价已标注后优先取已标注的并注明结论），每例给作品标题、候选与来源定位（IMDb 文件与行号、页面字段与快照）、采用理由、上界条件。

导出文件：`summary.json`、`report.md`、`release_selection.csv`、`release_candidates.csv`、`product_release_dates.csv`、`bounds.csv`、`hint_use.csv`、`examples.md`，写入 `outputs/task6/<抽取>/<上映信息版本>/<run_key>/` 并记录哈希；不含评论正文与整页 HTML（评论只给源评论 ID 与时间）。

**查询**（R30）：`query movie <movie_id>` 输出 JSON：作品与商品、全部候选（含未采用）、选择记录、上界评估与推断上界、各商品发售日期、作品与商品的最早评论；`query gaps --status missing|inferred` 列出缺失或有推断上界的作品与原因（作业第十部分的查询之三）。

## 评价设计（R32–R35）

**开发池与评价池**：首次全量（`t6r1`）后，按 `md5(movie_id)` 排序，前 20% 的作品冻结为开发池，记入 `dev_pool`；调整 H05、B3 天数或置信度分档时只看开发池（AI 观察，不产生标注、不算评价），调整记在本文新增的“规则调整记录”。评价表导出前冻结规则版本。

**120 部作品的分层**（评价池内每层按 `md5(movie_id)` 取前 20，不足时按 E1、E6 的顺序补足，实际层数写入评价集）：

| 层 | 内容 | 部数 | 主要检验 |
|---|---|---|---|
| E1 | `verified`，`titleType = movie`，无分歧、无 B4 矛盾 | 20 | IMDb 年份与关联的基本正确率 |
| E2 | `verified` 中的难例：`titleType` 为 `tvMovie`/`video`，或有 `year_disagreement`、相差 1 年、B4 矛盾 | 20 | 发行语义与映射错位 |
| E3 | `hint_matched` | 20 | 线索关联的作品是否正确 |
| E4 | `page_reported` | 20 | 商品标题年份是否是作品年份（实测估算约 60 部，评价池约 48 部） |
| E5 | 有推断上界、没有年份的作品 | 20 | 上界是否成立、能否查到真实年份 |
| E6 | `missing` 且没有推断上界 | 20 | 缺失是否真的查不到（漏补率） |

- 上界成立率在所有层中有推断上界、且标注了真实年份的作品上计算（E1–E3 中通过 B1–B5 的作品也计入），不只是 E5。
- **导出**：`outputs/task6/<抽取>/<上映信息版本>/eval/eval_items.csv`。每行含 `item_id`、作品标题、各商品 ASIN、商品标题与 `https://www.amazon.com/dp/<ASIN>`、导演与主演（来自任务 4 断言，帮助确认作品）、已关联的 `tconst` 与 `https://www.imdb.com/title/<tconst>/`（线索也给出），空列 `true_year`、`year_label`（`known`/`unsure`）、`tconst_label`（`same`/`different`/`unsure`/`na`）、`note`。不导出系统年份、状态、层与上界（R33）。
- **标注口径**：`true_year` 是作品首次公开发行（院线、电视首播或直接发行录像）的年份，以作品本身为准，不是 DVD 发行年；`tconst_label` 判断 IMDb 作品与 Amazon 商品是否同一作品；证据不足标 `unsure` 并写 `note`。
- **AI 预标注**（H04）：另存 `eval_items_draft.csv`，带 `ai_true_year`、`ai_tconst_label`、`ai_doubt`、`ai_reason`；依据只用表中字段与公开页面，AI 给出的年份只是建议，不作为事实来源，付林轩核查后才导入，标注人为付林轩；工具、用途与人工验证方式记入报告（R35）。
- **分歧处理**：只有一位标注人；导入时列出明显矛盾（`tconst_label = same` 但 `true_year` 与该 `tconst` 的 IMDb 年份相差超过 1 年），供标注人复看，不改动标注。

**指标**（R34）：

- 年份准确率（完全一致）= 系统年份等于 `true_year` 的作品数 / 系统有年份且 `year_label = known` 的作品数；另报“相差 ≤1 年”的同口径比例；按层与按状态。
- `tconst` 正确率 = `same` / (`same` + `different`)，按来源（任务 4 关联、线索）分开。
- 缺失可查到率 = `missing` 作品中 `year_label = known` 的数 / `missing` 作品中已标注（不含 `unsure`）的数；它是漏补的上界估计。
- 推断上界成立率 = `true_year` ≤ 上界年份的作品数 / 有推断上界且 `year_label = known` 的作品数。
- `unsure` 单独计数、不进分母；分母为 0 记“无定义”。报告注明样本分层刻意多取难例，指标只代表评价样本。

## 资源预算（估计）

| 项目 | 估计 | 依据 |
|---|---|---|
| 年份候选 | 约 4,900 条（IMDb 4,473 + 线索 253 + 标题年份 190） | 上文实测 |
| 线索处理记录 | 335 行 | 上文实测 |
| 商品发售日期 | 约 9,600 行 | 9,558 条断言 |
| 商品层评论 | 约 9,300 行（有评论的电影类与合集商品） | 8,718 + 部分合集 |
| 选择、评估与推断 | 选择 8,659、评估约 8,500、推断约 4,600 | 上文估算 |
| 全量运行 | 5 分钟以内 | 评论聚合实测约 7 秒，其余为小表；Airflow 调度开销为主 |
| schema `task6` | 100 MB 以内 | 以上行数与 jsonb 证据 |
| 重跑（`reused`） | 1 分钟以内 | 任务 5 同类重跑约 12 秒 |

## 异常与恢复

| 情况 | 处理 |
|---|---|
| 映射版本不存在或不属于该抽取、人物映射版本不基于该映射版本、IMDb 快照未装载 | `check_inputs` 确定性失败，下游 `upstream_failed` |
| 关联的 `tconst` 在快照里找不到 | 不产生候选，质量问题 `tconst_not_in_snapshot`，作品按其他候选择优 |
| `startYear` 为 NULL | 不产生候选，作品可能为 `missing`（`no_candidate`） |
| 线索 `tconst` 被占用或多 `tconst` | 只记 `hint_use`，不产生候选 |
| 商品日期标签未知或规范值无法解析 | 不入表，质量问题 |
| 一部作品出现两条同来源同键的候选 | 唯一约束失败，确定性失败（说明上游数据异常） |
| 评论时间超出范围 | B5 不通过，不生成上界 |
| 中断 | 已提交批次保留，重跑跳过；全局步骤整体重做 |
| 修了影响候选的代码 | 已有阶段标记不会重算：升规则版本不够，要清空 `task6` 后从头重跑（任务 5 T5-07 的教训），清空前后核对任务 1–5 行数 |

## 已知局限与风险

- **只有年份**：没有一部作品有完整日期或地区院线日期；作业允许的“指定地区院线上映日期”整体缺失（H01）。
- **IMDb 年份的语义**：`startYear` 是 IMDb 认定的发行年份，可能是电影节首映年，也可能是 `video`、`tvMovie` 的首次发行或播出年；本任务不细分，`titleType` 保存为证据，E2 层检验。
- **年份正确依赖任务 4 的关联**：任务 4 关联错了，年份也会错（例如实测 B4 矛盾的翻拍对）；B4 与评价的 `tconst_label` 只能暴露一部分。
- **缺失多**：估算 44.7% 的作品没有任何年份候选，主要是页面独有作品（VHS、教学片、冷门片）；推断上界只说明“不晚于”，不能当年份；任务 4 漏合并的 40 条线索与 899 个待复核商品留待任务 4 升级。
- **线索的误挂**：任务 5 作品年表反查对同名翻拍可能误挂，线索年份因此单独成层、置信度 0.80。
- **预售无法识别**：见 H07；推断上界可能被提前评论拉早，置信度因此不高于 0.6。
- **商品标题年份**：可能是重新发行或修复版的年份；只在没有 IMDb 来源时采用，实测与 IMDb 一致性很高（94 条中相差超过 1 年的 0 条），但样本小。
- **评价有偏、标注来自 AI 预标注**：分层多取难例；AI 草稿经付林轩核查后导入，AI 与系统可能用相近的证据（IMDb 页面），指标可能偏乐观，照实报告。

## 与后续任务的接口

- 任务 7（来源冲突）：`release_candidate` 已保存同语义的全部候选与年差，`year_disagreement` 是冲突入口；跨语义的比较（例如商品发售日期早于首次发行年份、推断上界早于年份）在任务 7 处理。
- 任务 8（评论去重）：`earliest_review_time` 用有效评论、未去重；同一评论被复制到多个版本时时间相同，最早时间不受影响，但评论数会偏多。
- 任务 9（字段级血缘）：`release_selection` → `release_candidate` → IMDb 文件行号或页面字段与快照；推断上界 → 源评论 ID；`query movie` 是上映字段血缘的雏形。
- 任务 4 升级：`hint_use` 中 `tconst_owned` 的 40 条与 B4 矛盾的作品可作为新映射版本的输入。
