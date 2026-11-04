# 任务 9 设计思路（字段级数据血缘）

本文说明任务 9 如何在任务 1–8 已有的表与版本之上建立字段级血缘。规则与验收以 [spec](spec.md) 为准，本文引用其规则编号（R）与验收编号（AC），不另立验收口径。数字分三类标注：**试算**（2026-11-03 正式库只读查询）、**预算**（估计值，不是实测）、**实测**（实现后填写）。

## 整体思路与分层

各任务已经各自保存了很多血缘片段：任务 4 的 `source_record` 与带 `parent_assertion_id` 的 `field_assertion`，任务 5 的提及键与归并依据，任务 6 的年份候选与上界证据，任务 7 的 `field_candidate`、`movie_field_selection` 与成员表，任务 8 的 `review_mapping`。问题在于：定位格式各不相同（jsonb 键名、文本键、数组），没有统一的父子边，不能从一个源记录反查受影响的字段，也有几处只到“ASIN + 区域”而没有序号。任务 9 不重算这些结果（R03），而是把它们**翻译**成统一的四层结构，并补齐断链：

```
原始层  task9.source_record      页面快照 / IMDb 文件行 / 评论文件记录 / 人工修订
          │  extract（R13）
暂存层  task9.assertion(level=extract)   page_field、任务 4 无父断言、人物提及、IMDb 行、上界引用的评论
          │  normalize / split / vocab_map / align / link / merge / infer（R14、R17）
集成层  task9.assertion(level=candidate|derived) + task9.derivation（父子边）
          │  select / union / infer / aggregate
目标层  task9.field_selection（每个最终字段或成员一行） + task9.field_impact（源记录 → 字段闭包）
        视图 task9.v_review_lineage、v_product_lineage（评论层不复制，R10）
```

与 W3C PROV-DM 的对应：源记录与断言是 `Entity`；上游运行（任务 N 的 `run_id`）加规则是 `Activity`；派生边是带 activity 的限定 `wasDerivedFrom`（qualified derivation），源记录到抽取断言是 `hadPrimarySource`；多值字段的成员是 `hadMember`；人工修订记录是 `wasAttributedTo` 某个标注人（`Agent`）。派生边同时按 OpenLineage 列级血缘标 `DIRECT`/`INDIRECT` 与子类型（R18），方便和通用工具对照。

## 正式库只读探查基线（规划试算）

2026-11-03 对 `etl_movies` 的只读查询（每条带 `SET default_transaction_read_only = on;`），输入为 R01 的版本。“可追溯”指采用的候选或成员能经已有表连接落到带定位、哈希与获取时间的源记录。脚本是规划用的简化查询，未重算快照哈希，也未执行重放（H08）。

### 上游版本与原始证据

| 项目 | 试算 |
|---|---|
| 迁移 | `001,101,102,103,201,301,302,401,501,601,701` |
| 上游版本 | `map0002`（t4r2）、`pmap0001`（t5r1）、`rv0001`（t6r1）、`cv0003`（t7r3，`cv0001`、`cv0002` 仍在）、`rd0001`（`verified`） |
| 评论文件 | `public.source_file` 1 行，SHA-256 `4b6ee09e…cc715`，`download_completed_at` 2026-10-21 02:25:40Z |
| 页面快照 | 清单中有快照路径的 28,913 个 ASIN（success 22,321、access_restricted 4,152、not_found 2,378、parse_failed 62），磁盘 `outputs/task2/snapshots/` 正好 28,913 个文件、4.7 GB；`content_hash` 是**解压后正文**的 SHA-256（抽样 `0001489305` 核对一致，gzip 文件本身的哈希不同） |
| 任务 4 源记录 | 页面 9,590 行（`acquired_at` 齐全）；IMDb 28,323 行（`acquired_at` 全为空，需取 `imdb.snapshot.downloaded_at` 2026-10-29 07:43:16Z） |
| IMDb 文件 | 5 个文件均有 SHA-256 与行数 |
| 人工修订 | `task4.human_decision`、`task5.human_decision` 均 0 行 |
| 库体量 | `etl_movies` 36 GB；`task7` 1,408 MB、`task8` 6,200 MB；磁盘可用 110 GB |

### 各类最终字段的现状可追溯性

| 类别 | 非空最终值 | 能落到原始证据 | 断链说明 |
|---|---|---|---|
| 作品标量 5 字段（`resolved`） | 34,630（title 8,659、original_title 5,400、release_year 5,450、runtime 8,124、mpaa 6,997） | 34,630（100%）；未采用候选 11,307 个也全部可落地 | 派生候选（`edition_rating`、`page_edition_runtime`）经 `edition_candidate_ids` 再落到页面 |
| 版本标量 6 字段（`resolved`） | 49,980 | 49,980（100%）；未采用候选 18,529 个全部可落地 | — |
| 作品演职员成员（`adopted`） | 80,612（另 `conflict_pending` 452） | 100%：页面侧经 `task5.movie_person_source` → 提及 → 快照，IMDb 侧经有效 `tconst` 与 `nconst` 落到 `title_director`/`title_principal` 行 | 能连上，但要跨任务 5、7 与 `imdb` 三处拼接，没有现成的边 |
| 作品类型成员 | 18,558（both 4,127、imdb_only 8,238、page_only 6,193） | IMDb 侧 12,365 可精确到行；页面侧 10,320 只有 `asins` 与 `sections`，没有序号或断言 ID | **断链**：按任务 4 `genre` 断言的原值前缀粗配只配上 6,485/13,505 个（ASIN, 词面） |
| 版本多值特征 7 项 | 46,027 个值（media_format 13,050、language 11,096、aspect_ratio 8,129、video_system 7,556、subtitles 5,925、region 166、cut_type 105） | `attributes_detail` 只有“值 → 区域列表” | **断链**：值经任务 7 再规范化（如 `dvd` → `DVD`），按规范值只配上 aspect_ratio 4,012 个，其余 0 |
| 作品别名 | 90,433（aka 79,716、primary 5,400、page 4,466、original 851） | 100%（断言 ID 或 IMDb 行号） | — |
| 有效 `tconst` | 5,407 | 100%（任务 6 选中候选带行号） | — |
| 推断上界 | 6,978（media_release 3,963、review 3,015） | 证据 ID 100% | **断链**：评论证据只记最早评论 ID；B3 依赖的第二早评论只有时间；“全部依赖评论”（作品 `edition` 商品的全部有效评论）未列出，试算 217,435 条，单部最多 2,385 条 |
| 人物规范名 | 29,635（IMDb 13,233、提及 16,402） | 100% | — |
| 人物别名 | 30,863（page 30,140、imdb 723） | 只有来源与次数，没有逐条提及键 | 可经 `mention_person` 与提及显示名连接重建，不需重放 |
| 任务 7 登记的 IMDb 独有人物 | 22,157 | 经 `nconst` 落到 `imdb.name` | — |
| 商品到作品 | 8,960（edition auto 4,544、contains auto 230、singleton 4,186） | auto 有决策 ID 与特征；singleton 没有决策 | singleton 的依据是规则本身，需显式写成边 |
| 商品类别 | 9,590（参与匹配商品） | 任务 3 `rule_id` 与解析版本 | — |
| 逻辑评论 | 1,908,386 | 代表记录 100%、helpfulness 来源记录 1,908,378（另 8 条为 `NULL`） | 评论层用视图，不计入下面合计 |

**试算基线（不含评论层）**：范围内非空最终值 411,673 个；严格口径（每个成员的全部来源都能精确定位）下可追溯 352,311 个，**覆盖率 85.6%**；缺口 59,362 个 = 类型页面成员 10,320 + 版本多值特征 46,027 + 评论上界 3,015。若只要求“至少一个来源精确定位”，类型的 `both` 成员（4,127）可算作可追溯，覆盖率约 86.6%。

除了上述缺口，还有三处**结构性**断链：一是没有统一反查（“这个快照影响哪些字段”要逐任务写 SQL）；二是 IMDb 源记录的获取时间要绕到快照表；三是快照文件在 `outputs/` 下，不在库里，清理时若删掉会使证据失效（H05）。

### 演示用真实 ID（试算时存在）

| 用途 | ID | 说明 |
|---|---|---|
| 作品字段 | `mv_24a89e3536e837e6`（Legend）`release_year` | 采用 IMDb `tt0089469` 的 1985（`imdb.title` 第 87,476 行）；两条页面标题年份 1986 为 `rejected`（`minor_tolerance`）；另有评论上界 1999-08-23 |
| 商品 | `B000063UR2` | Legend 的 `edition` 商品，快照 `outputs/task2/snapshots/R2/B000063UR2.html.gz`，任务 4 源记录 2879 |
| 原始评论 | `8f17b11666f52dc1fcca4fb62c233df9d5dc755992823c0470d135bb6341b55b` | Legend 最早评论（B000063UR2，1999-08-23） |
| 人物 | `ps_fb262c7cedc84d5e`（Olivia de Havilland） | `external_verified`，6 次提及、4 部作品、2 个页面别名 |
| 缺失年份 | `mv_cbefb17430301a33` | `release_year` 为空，只有评论上界 2005-04-13 |

## 开源参考

- **W3C PROV-DM**（https://www.w3.org/TR/prov-dm/）：Entity / Activity / Agent，`wasGeneratedBy`、`used`、`wasDerivedFrom` 及其子类型 `Revision`、`Quotation`、`PrimarySource`，限定派生（带 activity、generation、usage），`Collection`/`hadMember`。采纳：派生边带上游活动（运行与规则）；源记录到抽取断言用 `PrimarySource` 语义；多值字段按成员表达。未采纳：完整的 PROV-O RDF 序列化（课程只要求关系库可查询），但 `report export` 可另出 PROV-JSON 片段作为演示。
- **OpenLineage 列级血缘 facet**（https://openlineage.io/docs/spec/facets/dataset-facets/column_lineage_facet）：输出列 → `inputFields`（namespace、dataset、field）→ `transformations`（`DIRECT`：`IDENTITY`、`TRANSFORMATION`、`AGGREGATION`；`INDIRECT`：`JOIN`、`GROUP_BY`、`FILTER`、`SORT`、`WINDOW`、`CONDITIONAL`），另有数据集级依赖避免笛卡尔积。采纳：边上的类型/子类型标注（R18）；评分统计这类“整组依赖”只在查询里说明筛选条件，不逐行展开（R05，对应其 dataset 级依赖的思路）。差异：OpenLineage 是列到列（模式级），本作业要求**值级**（某部电影某字段的某个值），所以我们的节点是断言行而不是列。

## 组件与职责

`src/etl_task9/`（计划）：

| 模块 | 职责 |
|---|---|
| `rules.py` | 规则版本 `t9r1`、操作类型闭集、OpenLineage 映射、ID 生成（`sr_`、`as_`、`fs_`）、定位串规范化等纯函数 |
| `sources.py` | 读取上游版本与阶段键（R01），统一的上游只读取数 |
| `run.py` | 运行审计、输入键、阶段键、咨询锁、`entity_stage` 与 `run_batch` |
| `source_records.py` | `build_sources`：登记四类源记录、获取时间来源、快照哈希核验（R06–R09） |
| `assertions.py` | `build_assertions`：抽取级、候选级、派生级断言（R11–R16） |
| `derivations.py` | `build_derivations`：父子边、实体合并边、推断依赖（R17–R20、R22） |
| `relocate.py` | 只读重放补定位：类型词面、版本特征、第二早评论（R21），调用 `etl_task7`、`etl_task6` 的同一函数 |
| `selections.py` | `build_selections`：字段选择、状态、`trace_status`、与目标层一致性（R23–R27） |
| `impact.py` | `build_impact`：源记录 → 字段闭包；`verify-sources`、规则影响、上游重跑映射（R32–R35） |
| `versions.py` | 血缘版本、阶段键解析、版本差异（R34、R36） |
| `runner.py` / `pipeline.py` | 完整运行与 DAG 步骤（R37–R40） |
| `check.py` | 只读核对（`verify` 与正式库 `check` 共用） |
| `query.py` | `field`、`product`、`review`、`source`、`person`、`impact` 六个只读子命令（R28–R31、R33） |
| `report.py` | 导出、覆盖率、实例（R44、R45） |
| `evaluate.py` | 抽查表、AI 草稿、导入、指标（R46–R49） |

DAG：`dags/amazon_movies_task9_lineage.py`；迁移：`sql/801_task9_schema.sql`。

## 数据模型（schema `task9`，迁移 `801`）

所有业务表都带 `lineage_version_id`（或阶段键），同一库可保存多个血缘版本。源记录与断言的 ID 由上游定位确定（R08、R12），跨版本稳定，便于 `diff`。

| 表 | 粒度与关键列 |
|---|---|
| `run`、`run_batch`、`entity_stage`、`stage_key`、`quality_issue` | 同任务 7、8 的写法；`stage_key` 存完整串与父阶段键 |
| `lineage_version` | `lv0001`…；输入键；R01 各版本；五个阶段键；`rule_version`；`status`（`unverified`/`verified`）；`counts` |
| `source_record` | `(lineage_stage_key, source_record_id)`；`source_kind`（`page_snapshot`/`imdb_row`/`review_record`/`manual_revision`）；`locator`（jsonb）、`locator_key`；`content_hash` 与 `hash_scope`（`page_body_sha256`/`imdb_file_sha256`/`review_record_sha256`/`import_file_sha256`）；`acquired_at` 与 `acquired_at_basis`；`upstream_table`、`upstream_key`；`evidence_status`（`ok`/`snapshot_missing`/`snapshot_hash_mismatch`/`not_checked`） |
| `assertion` | `assertion_id`；`level`（`extract`/`candidate`/`derived`）；`entity_type`（`movie`/`edition`/`collection_member`/`person`/`mention`/`product_movie`/`review`/`imdb_title`/`imdb_name`；后两者为 T9-01 增补，IMDb 行按 `tconst`、`nconst` 归属）；`entity_id`；`field_name`；`member_key`；`raw_value`；`normalized_value`；`source_record_id`（抽取级必填）；`field_locator`（区域、序号、列、行号）；`extract_rule_version`；`upstream_status` 与原因；`upstream_table`、`upstream_key`、`upstream_run_id`；`confidence` |
| `derivation` | `(child_kind, child_id, parent_kind, parent_id, operation)`，`child_kind` 为 `assertion` 或 `selection`（T9-01 增补，字段选择也是子节点）；`parent_kind`（`assertion`/`source_record`）；`operation`（R17 闭集）；`ol_type`、`ol_subtype`；`rule_id`、`rule_version`；`upstream_task`、`upstream_run_id`、`occurred_at`；`confidence`；`role`（如 `direct_dependency`）；`relocated` |
| `field_selection` | `selection_id = 'fs_' || substr(sha256(entity_type|entity_id|field_name|member_key), 1, 16)`；最终值（文本与类型化列）；`status`（R24）；`missing_reason`；`selected_assertion_ids`、`considered_assertion_ids`；`decision_rule`、`reason`、`confidence`；`decision_version`；`trace_status`（R25）；`target_ref`（目标表与主键） |
| `field_impact` | `(source_record_id, selection_id)` 与最短路径长度；由派生图在 `build_impact` 末尾一次写入（R38） |
| `relocation` | 重放结果：被补定位的断言、补出的区域/序号/断言 ID 或源评论 ID、与上游是否一致 |
| `dev_pool` 不需要；`eval_set`、`eval_item`、`eval_label` | 同任务 8 写法，`eval_item` 存系统路径与证据摘录的哈希 |

视图（不复制数据，R10；写在 `sql/801_task9_views.sql`，由 `apply_task9_schema` 在 801 之后每次重建，文件须可重复执行，T9-03 加入）：

- `task9.v_review_lineage`：`public.review_record`（定位、哈希）+ `review_staging`（原值）+ `task8.review_mapping`（`rd0001` 的去重阶段键）+ `logical_review`、`family_member` + `logical_review_work`（归属阶段键）+ `stat_group_member`。
- `task9.v_product_lineage`：`task2.product_manifest`（状态、证据）+ `task3.product_classification`（`t3r2`）+ `task4.product_edition`、`product_movie`、`product_match_status`（`map0002`）+ `task7.edition_resolved`（`cv0003`）。

### 预算

| 对象 | 预算行数 | 依据 |
|---|---|---|
| 源记录 | 约 45 万 | 页面 9,590；IMDb 被引用的行约 22 万（title、aka 79,716、director、principal、name）；上界引用的评论 217,435 |
| 断言 | 约 80 万至 100 万 | 任务 4 断言约 17 万、提及 56,809、任务 7 候选（`cv0003`）约 10 万、任务 6 候选约 2.5 万、IMDb 行约 22 万、评论 21.7 万 |
| 派生边 | 约 150 万 | 每个候选平均 1–3 条父边，上界 21.7 万条 `aggregate` |
| 字段选择 | 约 45 万 | 非空 411,673 加 `NULL` 字段的状态行 |
| 影响闭包 | 约 200 万至 300 万 | 每个字段平均数个源记录；上界 21.7 万 |
| 体量 | 约 1.5–2.5 GB（含索引） | 低于任务 7（1.4 GB）与任务 8 之间；实现后实测替换 |

## 各阶段算法

### 1. `build_sources`（R06–R09）

1. 页面：从任务 4 `source_record`（`source_type = 'page'`）与 `task2.product_manifest` 取 ASIN、最终地址、快照路径、正文哈希、`fetched_at`，再从 `task2.fetch_attempt` 取保留快照的那次尝试序号。`locator_key = 'page|' || 抽取 || '|' || asin || '|' || content_hash`。
2. IMDb：被下游引用的 `(快照, 文件, 行号)` 才登记；文件 SHA-256 来自 `imdb.source_file`，获取时间来自 `imdb.snapshot.downloaded_at`（H06）。
3. 评论：只登记推断上界引用的评论（R10）；定位取 `public.review_record` 的记录序号、行号、偏移与 `raw_sha256`，文件哈希与 `download_completed_at` 取 `public.source_file`。
4. 人工修订：任务 4、5 的 `human_decision`（当前 0 行，H07）。
5. 快照核验：对被引用的页面快照逐个解压重算正文 SHA-256（约 9,590 个文件；按批并行读文件，不进事务）；不一致或缺失写质量问题并把 `evidence_status` 标出（R09）。

### 2. `build_assertions`（R11–R16）

按实体批次（作品、版本、人物各 1,000 个）把上游行翻译为断言，`upstream_table` 与 `upstream_key` 原样保存：

| 上游 | 级别 | 断言字段 | 定位 |
|---|---|---|---|
| `task3.page_field` | extract | 字段键或区域 | 区域 + 序号 + 快照哈希 |
| `task4.field_assertion`（无父） | extract | `field_name` 与 `ordinal` | `source_locator` + `source_record_id` |
| `task4.field_assertion`（有父） | derived | 同上 | 父断言 |
| `task5.person_mention` | extract | `role_class`、显示名 | `mention_key` = ASIN:区域:序号:位置 |
| IMDb `title`/`title_aka`/`title_director`/`title_principal`/`name` | extract | 列名 | 快照、文件、行号 |
| `task6.release_candidate` | candidate | `release_year` | `source_ref`（行号或断言 ID） |
| `task7.field_candidate`（`cv0003` 的阶段键） | candidate | 字段名 | `source_ref`；`edition_*` 派生候选指向版本候选 |
| `task7.credit_alignment` | derived | 署名段 | 页面人物与 IMDb 人物 |
| `task7.movie_genre_term` | derived | 词面处置 | ASIN + 区域（重放后补序号） |
| `task4.merge_decision`、`task5.link_decision`、`task5.person_decision` | candidate | 匹配候选 | 特征中的断言与 IMDb 行 |
| `public.review_staging`（上界引用） | extract | `review_time` | 源评论 ID |

未采用候选的上游状态与原因原样保存（R15）。多值字段成员键（R16）：演职员 `person_id|role`，类型为受控词，版本特征为 `特征名|值`。

### 3. `build_derivations`（R17–R20、R22）与重放（R21）

- 抽取级断言 → 源记录：`extract`（`DIRECT/IDENTITY`）。
- 任务 4 的规范化与拆分：`normalize`、`split`（`DIRECT/TRANSFORMATION`），`rule_id` 取 `field_assertion.rule_id`（如 `title.key`、`name.strict`）。
- 任务 7 候选 → 任务 4 断言或 IMDb 行：`select` 前的 `normalize`；类型词面 → 受控词：`vocab_map`；署名对齐：`align`（`INDIRECT/JOIN`），`rule_id` 为对齐等级（`L1`…`LS`）。
- 商品 → 作品：`link`（auto 决策，`rule_id` 取决策的 `tier`/`reason`，置信度）或 `singleton`（规则 `t4r2:singleton`）。
- 提及 → 人物：`merge`，`rule_id` 取 `mention_person.basis`，`decision_ref` 与轮次写在边上。
- 字段选择 ← 采用断言：`select`（`INDIRECT/CONDITIONAL`）；多值并集 `union`；推断上界 `infer`，其评论依赖 `aggregate`（`DIRECT/AGGREGATION`），最早与第二早评论 `role = 'direct_dependency'`。
- 上游运行 ID 与时间：任务 4–7 候选有 `run_id`，取对应 `run.started_at`；没有逐行 `run_id` 的表（如任务 3 `page_field`）用版本的生成运行。

**重放补定位**（`relocate.py`）只读执行：

1. 类型页面词面：对 `movie_genre` 中 `page_only`/`both` 成员涉及的 ASIN，读 `task3.page_field`（`t3p1`）的 `genre`、`breadcrumb` 区域，调用任务 7 处理词面的同一函数（去 ` see more`、还原下划线、映射表），得到每个受控词对应的（区域, 序号）；与 `movie_genre` 的词集合一致才写定位。
2. 版本多值特征：对每个 ASIN 调用任务 7 汇总特征的同一函数（输入任务 4 断言与任务 3 字段），记录每个值来自哪些（区域, 序号, 断言 ID）；与 `edition_resolved` 的数组一致才写定位。
3. 第二早评论：按 `etl_task6.reviews` 的同一排序（商品内按 `review_time_utc`、`source_review_id`，取每个商品前两条；作品内再按时间、ASIN 合并）取前两条，核对最早评论 ID 与两条时间都等于 `task6.movie_review_time`。任务 6 只保存了第二早的时间，ID 由这一步补出。
4. 不一致写 `replay_mismatch`（附上游值与重放值），该字段 `trace_status = 'partial'`，不回写上游（R03）。

### 4. `build_selections`（R23–R27）

| 目标字段 | 目标表（版本） | 选择来源 | 状态来源 |
|---|---|---|---|
| 作品 5 个标量 | `task7.movie_resolved`（`cv0003`） | `movie_field_selection` 的 `selected/considered_candidate_ids` | 同表 `status` |
| 作品 `genres` | `task7.movie_genre` | 每个受控词一行，父为 IMDb 行与页面词面 | `movie_field_selection` |
| `director`、`starring`、`cast` | `task7.movie_person_resolved` | 每个 `person_id|role` 一行，父为页面提及（经 `movie_person_source`）与 IMDb 行（`title_director`/`title_principal`） | 成员 `adopted`/`conflict_pending` |
| 有效 `tconst` | `task7.movie_resolved` | 任务 6 选中候选 | `release_selection.status` |
| 推断上界 | `task6.inferred_release_bound`（`rv0001`） | 采用证据 | `inferred` |
| 作品别名 | `task7.movie_title_alias` | 断言 ID 或 IMDb 行 | `resolved` |
| 版本 6 个标量 | `task7.edition_resolved` | `edition_field_selection` | 同表 |
| 版本 7 项特征 | `task7.edition_resolved` 数组 | 重放补出的断言 | `resolved` |
| 商品类别 | `task3.product_classification`（`t3r2`） | 分类规则 `rule_id` 与解析结果 | `resolved` 或 `pending` |
| 商品所属作品 | `task4.product_movie`（`map0002`） | 决策或 `singleton` | `product_match_status` |
| 人物规范名、外部 ID、别名 | `task5.person`、`person_external_id`、`person_alias`（`pmap0001`），`task7.imdb_person_ext` | `canonical_source`、`basis_decision_ids`、提及 | `resolved` |

`trace_status` 判定：沿派生边走到底，每个采用成员的每个来源都终止于 `evidence_status = 'ok'` 的源记录为 `traced`；路径中含重放边为 `relocated`；终止于人工修订为 `manual`；有来源只到 ASIN + 区域为 `partial`；源记录证据有问题为 `evidence_unverified`；没有任何路径为 `untraced`。最终值与目标表逐行比对（R26），不一致即核对失败。

### 5. `build_impact`（R32–R35）

从源记录沿派生边正向传递到字段选择，写 `field_impact(source_record_id, selection_id, depth)`。按实体批次计算后，在本阶段最后一个事务中替换为完整闭包（R38），保证没有半写入。规则影响不另建闭包：`derivation` 上有 `rule_id` 与规则版本的索引，`impact --rule` 先找边，再沿子边走到选择。

## 演示查询

`src/etl_task9/query.py`（T9-04 实现）。命令在项目根目录运行，输出 JSON；默认取与默认输入相同的 verified 血缘版本，还没有版本时（演练副本）由输入版本推出阶段键，也可用 `--lineage-version lv0001` 指定。正式库没有 `task9` 之前不能运行；下面的输出要点是 2026-11-03 在业务库副本 `etl_t9_rehearsal` 上的实测（命令加 `-e ETL_DB_NAME=etl_t9_rehearsal`），T9-08 正式运行后在正式库复核。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.query field --movie-id mv_24a89e3536e837e6 --field release_year
```

实测要点：最终值 `1985`、`resolved`、`trace_status = traced`、决策版本 `cv0003`，规则 `year.table:YR2`，理由“IMDb 与页面标题年份相差 1 年，采用 IMDb”；采用候选的证据为 IMDb `title.basics.tsv.gz` 第 87,476 行（文件 SHA-256 `ecc495f7…`，获取时间 2026-10-29 07:43:16Z，来源列 `imdb.snapshot.downloaded_at`），路径 `select:year.table:YR2 → normalize → normalize → extract`，路径长度 4；两条未采用候选 `1986`（`rejected`，原因 `minor_tolerance`）分别落到 B000063UR2 与 B004QSQMKE 的页面快照（正文哈希、`fetched_at` 2026-10-28），路径 `normalize:amazon_title_year → split:title.year → extract`；另列推断上界 `1999-08-23`（`inferred`、`relocated`）。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.query product --asin B000063UR2
docker compose --profile crawl run --rm -T crawler python -m etl_task9.query review --source-review-id 8f17b11666f52dc1fcca4fb62c233df9d5dc755992823c0470d135bb6341b55b
```

实测要点：商品 `collected`/`success`，类别 `movie`（`MV1`、`structured`），版本属性（标题 `Legend (1986)`、片长 204、`NR/Unrated`、2 碟、`DVD`、`NTSC` 等），所属作品 `mv_24a89e3536e837e6`（`edition`、`auto_decision`、决策 46262），匹配状态 `merged`，页面快照源记录与 20 个版本与归属字段选择；评论为第 6,450,929 条记录（行 58,058,313–58,058,320，偏移 7,609,597,242，记录哈希），`rd0001` 下 `mapped`、`D3_family_copy`，逻辑评论 `lr_a206995a2a4ec3f45864`（10 条复制）、家族 `rf_f4c0c1854b2f4130`，归属 Legend 与统计组，并标出它是 Legend 推断上界的 `aggregate` 直接依赖。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.query source --asin B000063UR2
docker compose --profile crawl run --rm -T crawler python -m etl_task9.query source --tconst tt0089469
```

实测要点：快照 B000063UR2（1 条源记录）影响 9 个实体的 41 个选择（版本 14 个字段成员、作品的 mpaa、类型、导演与演职员成员、人物别名与外部 ID、商品所属作品），重跑步骤为任务 3 → 4 → 5 → 6 → 7 → 8 → 9；`tt0089469` 的 35 条 IMDb 行（title、crew、principals、akas）影响 12 个实体的 56 个选择（作品 title、original_title、release_year、runtime、有效 tconst、23 个别名、演职员与类型成员、9 个人物外部 ID、2 个商品所属作品），重跑步骤从 IMDb 装载开始。每行带最短路径长度。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.query person --person-id ps_fb262c7cedc84d5e
```

实测要点：规范名 Olivia de Havilland，来源 IMDb `name.basics.tsv.gz` 第 15 行（`nm0000014`）；6 条页面署名（B00004CK47、B000PC72QS、B002KE4ZI2、B004KZP7Z4 的 byline 与 detail_bullets，原值含大小写变体 `Olivia De Havilland`），每条的归并依据 `credits` 与任务 5 外部 ID 判定编号；外部 ID `imdb:nm0000014` 及其人名行；两个页面别名（`identical,imdb_primary` 5 条提及、`case_variant` 1 条）；参与 4 部作品 6 个角色成员（cast 4、starring 2），全部 `traced`。任务 5 `mapping_event` 中没有该人物的事件（0 行，如实返回空）。

影响分析与来源核验：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.query impact --rule rating:MR2
docker compose --profile crawl run --rm -T crawler python -m etl_task9.impact verify-sources
```

实测：`rating:MR2` 有 26 条 select 边，只影响 26 个作品的 `mpaa_rating`；`verify-sources` 9,590 个快照与 5 个 IMDb 文件全部一致，退出码 0。`crawler` 容器没有挂载评论文件 `/data/source/movies.txt.gz`，该项记为 `unavailable`（不算不一致）；在挂载了它的 Airflow 容器里核验（`docker compose exec -T airflow-scheduler python -m etl_task9.impact verify-sources --kinds review_record`）一致。

## 规则或来源改变后定位受影响字段（R32–R35）

1. **来源改变**：`verify-sources` 对 `lineage_version` 引用的源记录重算哈希（页面快照正文、IMDb 文件、评论文件），不一致的源记录经 `field_impact` 得到受影响字段，再按下表给出要重跑的上游步骤与 DAG：

| 变化的源 | 受影响的上游 | 重跑顺序（DAG） |
|---|---|---|
| 页面快照 | 任务 3 解析与分类 → 任务 4 断言与匹配 → 任务 5 提及 → 任务 6 候选 → 任务 7 消解 → 任务 8 归属 | `amazon_products_task3_classify` → `amazon_movies_task4_match` → `amazon_movies_task5_person` → `amazon_movies_task6_release` → `amazon_movies_task7_conflict` → `amazon_reviews_task8_dedup` → `amazon_movies_task9_lineage` |
| IMDb 文件 | IMDb 装载 → 任务 4–7 → 任务 8（rd 基于 cv） | `imdb_reference_task4_load` → 任务 4 至 8 → 任务 9 |
| 评论文件 | 任务 1 → 任务 2 清单 → 任务 6 评论时间 → 任务 7（读 rv）→ 任务 8 | `amazon_reviews_task1` → `amazon_products_task2_manifest` → `amazon_movies_task6_release` → `amazon_movies_task7_conflict` → `amazon_reviews_task8_dedup` → `amazon_movies_task9_lineage` |

2. **规则改变**：`query impact --rule <rule_id>` 或 `--rule-version t7r3` 列出依赖这些边的字段；上游以新规则版本生成新版本（如 `cv0004`）后，任务 9 以新输入键生成 `lv0002`；`report diff lv0001 lv0002` 按实体、字段、成员列出最终值、状态、决策、采用断言与来源集合的变化。新的上游版本会改变全部五个阶段键，任务 9 **全量重建**（用户 2026-11-04 选定方案 C，spec H09、R35；副本上约 7.5 分钟）；增量是否正确改由差异核对检验：`diff` 报出的变化字段集合须等于事前 `impact` 定位的受影响字段，变化的选择须落在 `impact` 列出的选择之内，其余选择的值、状态、决策、采用断言签名与来源集合不变（AC15）。
3. 夹具演示（AC15）：改一个快照文件、生成只改一条规则的第二个 `cv` 版本，分别验证上面两条路径；规则变化一项先在 `lv0001` 上 `impact --rule RT1 --rule-version t7r3`（片长容差 `runtime_tol_variance` 只用于 `RT1` 的差异分类），再全量生成 `lv0002` 并与 `diff` 对照。

## 版本、幂等、分批与编排（R36–R40）

- 输入键：R01 各版本、`cv0003` 与 `rd0001` 的阶段键、IMDb 快照、规则版本 `t9r1`、批大小以外的参数；同输入键已有 `verified` 版本时记 `reused`。
- 阶段键：`sources ⊂ assertions ⊂ derivations ⊂ selections ⊂ impact`，下游键包含上游键；只换 `rd` 版本时只重建评论相关断言与选择。
- 分批：作品、版本、人物按 ID 排序每 1,000 个一批；结果与 `entity_stage`、`run_batch` 同事务；影响闭包最后一个事务整体替换；每步持抽取级咨询锁（与任务 7、8 同法）。
- DAG `amazon_movies_task9_lineage`：`check_inputs → build_sources → build_assertions → build_derivations → build_selections → build_impact → finalize_version → verify → finalize_report → gate`；确定性错误（输入缺失、版本未核实、核对失败）不重试，数据库连接等暂时错误最多重试 2 次；`finalize_report`、`gate` 为 `all_done`，`gate` 在任何上游失败时失败。
- `verify`：R19 无环与终止、R25 覆盖率、R26 值一致、计数自洽、上游行数未变。

## 业务库副本演练（R43）

每个实现阶段完成后、T9-08 正式运行前，在副本上全链路运行并核对：

1. 停止 Airflow 三个服务：`docker compose stop airflow-scheduler airflow-dag-processor airflow-apiserver`。
2. 建库：`docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE etl_t9_rehearsal TEMPLATE etl_movies OWNER etl_app"`，随后 `docker compose exec -T postgres psql -U postgres -c "CHECKPOINT"`（任务 8 演练时 `TEMPLATE` 按 `WAL_LOG` 复制写了约 27 GB WAL，不 `CHECKPOINT` 会一直占盘）；之后恢复 Airflow。库约 36 GB，磁盘可用 110 GB。
3. 以 `docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.runner …` 运行并 `check`，对照本文试算逐项解释差异；抽查四个演示查询。
4. 删库：`DROP DATABASE etl_t9_rehearsal` 后再执行 `CHECKPOINT`。演练发现的缺陷先补夹具与测试，再改实现。

## T9-01 实现要点与对计划的修正

- **阶段键**：`sources` 含抽取、t3、解析、map、t4 规范化、pmap、t5 规范化、rv、cv 与 cv 的四个阶段键、IMDb 快照与规则签名；`assertions`、`derivations` 只加规则签名；rd 与其归属阶段键只进入 `selections`（原始层与暂存层不读任务 8），所以只换 rd 时前三个阶段原样复用。短键前缀 `s`、`a`、`d`、`l`、`i`。
- **分批**：`entity_stage` 按“单元:实体”逐实体标记（如 `page:<ASIN>`、`imdb_title:<tconst>`、`review:<movie_id>`），与该批数据同事务提交；重跑只处理未标记的实体，与批大小无关。各单元全部完成后单独一个事务写 `__complete__`。
- **“被下游引用”的 IMDb 行**（`etl_task9.sources`）：作品行取任务 7 有效 tconst、任务 4 映射作品 tconst、任务 4 已登记的 IMDb 源记录、任务 6 考虑过的 IMDb 候选与任务 7 两个阶段键下 IMDb 候选的 tconst 并集（28,714）；导演与主要演职员行只取有效 tconst 与任务 4 映射作品 tconst（5,284 个）；别名行只取任务 7 作品别名引用的行号（77,229）；人名行取上述演职员的 nconst、任务 5 外部 ID 与任务 7 IMDb 独有人物（35,425）。若把别名与演职员扩到全部 28,714 个 tconst，会多出约 59 万行（别名 377,196、演职员 234,388、人名 146,618），远超预算，且不在任何最终字段的路径上。
- **评论源记录**：推断上界以评论为证据的作品经 `map` 的 edition 关系、`product_edition.product_kind = 'movie'` 的商品上的全部 `review_staging` 行（任务 6 R20 的口径）；正式库合计正好等于这些作品 `task6.movie_review_time.review_count` 之和 217,435。
- **断言粒度**：任务 3 字段每行一条（字段名为 `field_key`，没有时为区域）；任务 4 断言每行一条，有父的记 `derived`（派生边在 T9-02）；IMDb 作品行按列拆（`primary_title`、`original_title`、`title_type`、`start_year`、`runtime_minutes`、类型按成员），人名行按列拆（`primary_name`、`birth_year`、`death_year`）；任务 6 候选取 rv 选择记录考虑过的（含选中的），状态记 `selected`/`not_selected`；任务 7 候选取 cv 的 scalar 与 edition 阶段键下全部行。
- **视图位置**：801 只建表；视图放在 `sql/801_task9_views.sql`，避免 T9-03 改视图时迁移 801 已应用、不能重放的问题。
- **快照根目录**：清单里的快照路径相对项目根（容器内 `/opt/etl`，与任务 2 `resolve_snapshot` 相同），可用 `--snapshot-root` 或 `ETL_SNAPSHOT_ROOT` 覆盖；每批先在事务外用线程池流式解压重算正文 SHA-256，再与源记录同事务写入。
- **夹具**：没有从任务 1 起逐任务重跑全部流水线（任务 4–6 的真实运行难以造出 AC 所需形态），而是复用任务 7 已对照正式库的上游写法，再用各任务自己的代码补齐（任务 7 真实运行两次、任务 1 装载器、任务 4 的 `_title_block` 与 `import_decisions`），逐表说明见 `tests/fixtures/task9/README.md`。任务 8 只写了 `dedup_version` 元数据，评论层视图（AC04）需在 T9-03 改用任务 8 的真实运行。

## T9-01 演练实测（业务库副本 `etl_t9_rehearsal`，2026-11-03）

2026-11-03 停 Airflow、确认 `etl_movies` 无连接后 `CREATE DATABASE etl_t9_rehearsal TEMPLATE etl_movies OWNER etl_app`（用时 3 分 33 秒）并 `CHECKPOINT`，随即恢复 Airflow；副本保留到 T9-07。以 `docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.runner stages --through assertions` 运行，退出码 0，墙钟 194 秒（`sources` 82 秒，其中 9,590 个快照核验约 10 秒；`assertions` 110 秒）。阶段短键 `s11f2e418823b9fe`、`a65adeae3bb06227`。

| 项目 | 预算（规划） | 实测 | 说明 |
|---|---|---|---|
| 页面快照源记录 | 9,590 | 9,590（全部 `ok`） | 正文 SHA-256 全部与清单一致，`snapshot_missing`、`snapshot_hash_mismatch` 均为 0 |
| IMDb 行源记录 | 约 22 万 | 202,298 | 作品 28,714、别名 77,229、title.crew 5,276、主要演职员 55,655、人名 35,424 |
| 评论源记录 | 217,435 | 217,435 | 与 3,015 部评论上界作品的 `movie_review_time.review_count` 之和一致 |
| 人工修订 | 0 | 0 | H07 |
| 源记录合计 | 约 45 万 | 429,323 | 质量问题 0（获取时间全部取到） |
| 断言合计 | 约 80–100 万 | 1,191,277 | 超出约 19%：预算未计任务 3 页面字段 246,856 条，IMDb 作品按列拆为 185,445 条 |
| 其中 extract | — | 1,062,036 | 页面字段 246,856、任务 4 无父 147,835、提及 56,809、IMDb 作品 185,445、别名 77,229、导演 5,876、主要演职员 55,655、人名 68,896、评论 217,435 |
| 其中 derived | — | 9,010 | 任务 4 有父断言（与正式库 8,015 + 995 一致） |
| 其中 candidate | 约 12.5 万 | 120,231 | 任务 6 候选 5,748（预算估 2.5 万偏高：rv 选择记录实际只考虑这么多）、任务 7 候选 114,483 |
| `task9` 体量 | 全部阶段 1.5–2.5 GB | 1,692 MB（只到 T9-01） | `assertion` 1,061 MB、`source_record` 584 MB；后续派生边与闭包会使总体量超过预算上限，T9-02 起需关注 |

Legend 抽查（`mv_24a89e3536e837e6`）：`release_year` 登记任务 6 候选 3 条（`tt0089469` 1985 `selected`，B000063UR2 与 B004QSQMKE 的标题年份 1986 `not_selected`）与任务 7 候选 3 条（1985 `adopted`，两条 1986 `rejected`、原因 `minor_tolerance`），IMDb 候选定位到 `title.basics.tsv.gz` 第 87,476 行；该行源记录 `sr_38c893f868656a38`，获取时间 2026-10-29 07:43:16Z（`imdb.snapshot.downloaded_at`）；商品 B000063UR2 的页面源记录 `sr_c90a9a2904d071ce` 为 `ok`，尝试序号 1，任务 4 源记录 2879；最早评论 `8f17b116…` 的源记录 `sr_94c5693fb5c5f4e6` 为第 6,450,929 条记录（行 58,058,313，偏移 7,609,597,242）；Legend 依赖评论断言 546 条，等于其 `movie_review_time.review_count`。

发现的上游事实（留给 T9-02）：人名集合里 `nm17757708` 出现在 `imdb.title_principal` 与 `task7.imdb_person_ext`，但不在 `imdb.name`（名字文件里没有这一行），所以人名源记录比实体少 1 条；从它出发的路径只能到演职员行，不能到人名行。

## T9-02 实现要点与对计划的修正

- **集成层断言放在 assertions 阶段**（算法标记 `a1 → a2`）：合并、外部 ID 判定、提及 → 人物、人物规范名与外部 ID、任务 7 IMDb 独有人物、署名对齐、演职员成员、类型词面与类型成员、版本多值特征、推断上界都翻译成 `candidate`/`derived` 断言（`etl_task9.assertions.integration_units`，字段名与成员键见该模块说明）。这样全部断言在同一个阶段键下，T9-03/T9-04 只需按 `(stage_key, assertion_id)` 查；多值字段的每个最终成员都有一条 derived 断言（类型 `task7.movie_genre`、演职员 `task7.movie_person_resolved`、版本特征 `task7.edition_resolved`），字段选择只需连到它。`a2` 与 `d1 → d2`（见下）都发生在 `t9r1` 生成任何血缘版本之前，只改阶段键、不换规则版本；`lv0001` 生成之后再改算法必须换规则版本。
- **派生边分族写入**（`etl_task9.derivations.FAMILIES`）：抽取边（断言 → 源记录）也物化为边，不用视图拼接，原因是 R18 要求每条边带上游运行与写入时间，而各上游表的运行列不一致（任务 4 断言、IMDb 行、评论没有逐行运行），在写边时统一补齐（任务 4 取映射版本的运行、IMDb 取快照、评论取任务 1 装载运行）；代价是约 106 万行。边按子断言 ID 的前 3 位十六进制分 4,096 个桶作为 `entity_stage` 的实体，与数据同事务提交；最后一个单元 `graph_check` 补 IMDb 人名缺行的路径并检查 R19，再写阶段完成标记。
- **操作类型的取法**：抽取 `extract`；任务 4 拆分 `split`；候选取自上游值 `normalize`；作品候选取自版本选择 `select`；商品 → 作品、外部 ID 判定与证据 `link`；提及 → 人物 `merge`（`rule_id` 为依据 `credits`/`filmography`/`within_work`/`co_credit`/`singleton`，`detail` 带轮次、组、`link_decision_ids`、`person_decision_ids`）；规范名与外部 ID 的来源 `select`；署名对齐 `align`；演职员与类型成员的来源 `union`，页面词面 → 受控词 `vocab_map`；发售日期上界 `infer`；评论上界 `aggregate`；重放补出的边 `relocate`（`relocated = true`，`upstream_task = task7`，`rule_version` 为被重放的上游规则版本，便于 R33 按 `t7r3` 找到）。
- **合并决策只登记被采用的**：`task4.merge_decision` 只翻译 `product_movie` 引用的 4,774 条（未采用的合并候选是实体候选，不是字段候选，其 IMDb 行大多不在“被引用的 IMDb 行”里，见 T9-01 修正 4）；外部 ID 判定登记 pmap 的 link 阶段键下全部 31,563 条（含 review）。
- **starring 沿用 cast 的对齐**（演练发现，`d1 → d2`）：任务 7 每个页面人物只在 cast 角色下对齐一次，starring 成员直接沿用；第一次演练有 502 个 starring 成员没有父边。修正为：成员连到其 `page_person_ids` 在本角色下的全部提及（`union`），页面人物经对齐成为成员本人时再连到任一角色下的对齐断言（`align`）。夹具补了同形案例（`test_ac06_starring_member_reuses_cast_alignment`）。
- **重放**（`etl_task9.relocate`）：类型词面调用 `etl_task7.sources.product_links`、`genre_terms` 与 `etl_task7.rules.classify_genre_term`；版本特征调用 `etl_task7.sources.assertions`、`page_fields` 与 `etl_task7.editions.feature_values`（整商品结果须等于 `attributes_detail`，再对每条断言、每个字段单独调用得到来源）；第二早评论没有可调用的纯函数（任务 6 的排序写在 `movie_review_times` 里且会写表），按同一排序在 `relocate.earliest_two` 里重写，并用 `etl_task6.rules` 的 `b3_days`、`_shift` 复核 B3，一致性由与上游值逐项比对保证。不一致只写 `relocation.consistent = false` 与 `replay_mismatch`，不写重放边。
- **IMDb 人名缺行**：`nm17757708` 的 `task7.imdb_person_ext` 断言连不到 `imdb.name`，改连同一 nconst 的演职员行断言（`rule_id = imdb_name_missing`）并写 1 条 `dangling_edge` 质量问题（`detail.reason` 说明缺人名行）；T9-03 判定它的 `trace_status` 时应把这条问题列出。
- **夹具补形**：任务 7 夹具省略了正式库都有的列（任务 6 候选 `source_ref`、合并决策、外部 ID 判定、来源提及、评论时间、上界证据），`_task9_helpers` 在任务 7 运行前按正式库形状补齐，评论时间调用任务 6 自己的写入函数，逐项说明见 `tests/fixtures/task9/README.md`。
- **留给 T9-03/T9-04 的接口**：`derivations.check_graph(conn, ctx, edge_keys=[d, l], ak, sk)` 同时检查选择边（`child_kind = 'selection'`，选择边写在 selections 阶段键下）；`derivations.insert_edges` 写小批量边；`relocation` 表按 `target_kind`、`target_id`（`movie|区域|词面`、`asin|特征|值`、`movie_id`）给出一致性，页面侧成员 `trace_status` 取 `relocated` 还是 `partial` 以此为准；人物别名（`task5.person_alias`）尚无断言，T9-03 可由 `mention_person` 断言按显示名连接。

## T9-02 演练实测（业务库副本 `etl_t9_rehearsal`，2026-11-03）

`docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.runner stages --through derivations`，退出码 0。第一次（`a2`、`d1`）墙钟 4 分 26 秒（`sources` 跳过；`assertions` 81 秒；`derivations` 183 秒），检出 502 个 starring 成员没有父边（见上）；修正后第二次（`d2`，assertions 跳过）墙钟 2 分 54 秒，`derivations` 172 秒。阶段短键：`s11f2e418823b9fe`、`a0d345df84d5280a`、`d8f913d99aa00f0e`。副本里另保留 T9-01 的 `a65adeae3bb06227` 与第一次的 `db275e5c0b4e3fe1`（未删除）。

| 项目 | 预算或试算 | 实测（`d8f913d99aa00f0e`） | 说明 |
|---|---|---|---|
| 断言（`a2`） | 约 80–100 万（预算） | 1,548,754（extract 1,062,036、candidate 156,568、derived 330,150） | 比 T9-01 多 357,477 条集成层断言：合并决策 4,774、商品 → 作品 8,960、外部 ID 判定 31,563、提及 → 人物 53,250、人物规范名 29,635、外部 ID 13,233、IMDb 独有人物 22,157、署名对齐 6,747、演职员成员 81,064、类型词面 34,531、类型成员 18,558、版本特征 46,027、推断上界 6,978 |
| 派生边合计 | 约 150 万（预算） | 2,106,585 | 超出约 40%：预算未计抽取边（1,062,036） |
| 按操作 | — | extract 1,062,036；link 276,300；aggregate 217,435；union 132,045；normalize 125,979；relocate 121,283；select 74,913；merge 53,250；align 17,213；vocab_map 13,158；split 9,010；infer 3,963 | 12 种操作全部出现；全部边 `upstream_run_id`、`occurred_at` 非空 |
| 无环与终止（R19） | — | 悬空 0、无父节点 0、环上节点 0 | 第一次演练无父节点 502（已修正） |
| 类型页面成员 | 断链 10,320（试算） | 词面 34,531 条全部一致（`replay_mismatch` 0）；页面侧成员 10,320（both 4,127、page_only 6,193）全部经一致的词面连到页面断言 | 试算按原值前缀粗配只配上 6,485/13,505 个（ASIN, 词面） |
| 版本多值特征 | 断链 46,027（试算） | 46,027 全部一致（`replay_mismatch` 0），每个值都有重放边 | 9,575 个商品整商品 `feature_values` 与 `attributes_detail` 完全相等 |
| 第二早评论 | 断链 3,015（试算） | 3,015 全部一致（`replay_mismatch` 0），补出源评论 ID | 最早评论 ID、两条时间与 B3 都与任务 6 一致 |
| 推断上界依赖评论 | 217,435（试算） | aggregate 边 217,435，`direct_dependency` 6,030（每部 2 条），其中第二早 3,015 条 `relocated` | Legend：546 条，2 条直接依赖，第二早评论与最早同日（1999-08-23） |
| 发售日期上界 | 3,963 | infer 边 3,963 | — |
| 演职员成员 | 80,612 + 452 | 81,064 条成员断言全部有父边：页面侧（both、page_only）50,085 条都有本角色提及，IMDb 侧（both、imdb_only）55,864 条都有演职员行，经对齐的 4,644 条另有对齐边 | — |
| 质量问题（d 键） | — | 1（`nm17757708` 缺 `imdb.name` 行，改连演职员行） | T9-01 已记录的上游事实 |
| `task9` 体量 | 全部阶段 1.5–2.5 GB（预算） | 副本合计 5,354 MB（含 T9-01 的 `a1` 与第一次的 `d1` 两组旧键）；按行数比例估算当前一组键（s、a2、d2）约 3.2 GB：`source_record` 584 MB、`assertion` 约 1.4 GB、`derivation` 约 1.0 GB、`relocation` 约 56 MB、`entity_stage` 约 90 MB | 已超预算上限且尚未计字段选择与影响闭包。原因：抽取边物化约 0.5 GB（见上）、集成层断言 36 万条、各表主键与索引都是长文本键；未为凑预算删减范围（H02、H04）。T9-08 正式库只有一组键；磁盘可用约 100 GB |

## T9-03 实现要点与对计划的修正

- `selections.py` 按 10 个家族写选择（f_movie_scalar、f_movie_multi、f_tconst、f_bound、f_movie_alias、f_edition_scalar、f_edition_feature、f_category、f_product_movie、f_person），每个家族一条语句：从目标层表取最终值与状态，按 `selection_id` 前 3 位十六进制分 4,096 桶（同派生边），同一语句里写字段选择与 `select` 边（`child_kind = 'selection'`，selections 阶段键）。只给已存在的采用断言写边；商品类别没有对应断言，直接连页面快照源记录。
- 多值字段按成员一行；没有成员的字段（任务 7 `missing`）和空特征数组各一行 `missing`，带缺失原因；没有上界的作品一行 `missing`。任务 7 的 `conflict_unresolved` 成员（`conflict_pending`）记 `conflict_unresolved`。
- `trace_status`（R25）在阶段最后一个单元一次判定：内存中沿 derivations 与 selections 两个键的边做后序 DFS，每个断言的标记是全部父边标记之并；优先级 untraced（断链）> partial（断链处是 `replay_mismatch` 的断言）> evidence_unverified（到达快照缺失或哈希不符的源记录）> manual（到达人工修订）> relocated（路径含重放边）> traced。没有采用断言的空值选择按考虑过的断言判定，都没有时为 untraced；只有非空值计入缺口并写 `untraced_value`。
- R26 由 `check_values` 实现：重新从目标层表取全部家族的（实体、字段、成员、值、状态），和 `field_selection` 全外连接比对，值或状态不同、缺选择、多选择都写 `value_mismatch`。`check_graph` 对选择只查非空值的无父节点。
- 视图写在 `sql/801_task9_views.sql`（tasks 原写 801_task9_schema.sql，按 design 改为独立文件，由 `apply_task9_schema` 每次重建）：`v_review_lineage` 每条原始评论在每个去重版本、每个归属作品一行；`v_product_lineage` 每个商品在每个映射版本、冲突版本、作品成员一行，都带版本列，查询须限定版本。
- 修正：任务 7 作品别名表的阶段键是 scalar 阶段键（不是 multi）；人物页面别名是署名原文（如 `Yen, Donnie`），按提及的原文或规范名匹配（第一次演练 1,569 个别名没有父节点，算法 l1 → l2）；任务 7 IMDb 独有人物 `nm17757708` 没有人名，记 `missing` 而不是空值 `resolved`（l2 → l3）。三次都在 t9r1 发布前，只改阶段键。
- 人工修订：夹具的任务 4 人工决策导入晚于映射，`map0002` 没有采用它，没有选择是 manual；manual 路径用在任务 9 自己的派生图里补一条“商品 → 人工修订源记录”边验证（AC10）。正式库人工修订 0 条，manual 计数 0。
- 夹具没有 `task3.product_classification` 与 `task4.product_match_status` 的行（任务 7 夹具只建任务 7 需要的表），所以夹具里类别都是 `missing`（`not_classified`）、商品视图的类别与匹配状态为空；这两项在副本上核对（见下）。

## T9-03 演练实测（业务库副本 `etl_t9_rehearsal`，2026-11-03）

`docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.runner stages --through selections`，退出码 0，前三阶段跳过，selections 82 秒（墙钟 1 分 23 秒）。短键 `l7292c0631b27f9a`（l3）；第一、二次的 `l795de69afe13c83`（l1）、`ledf2a5e574a5a8e`（l2）保留未删。

| 项目 | 预算或试算 | 实测（`l7292c0631b27f9a`） | 说明 |
|---|---|---|---|
| 字段选择 | 约 45 万（预算） | 531,371（resolved 462,241、missing 61,674、inferred 6,978、conflict_unresolved 478） | 非空 469,671；空值 61,700 |
| 非空值与试算 | 411,673（试算口径） | 469,671 | 多出的是试算未计入的人物外部 ID（35,390）、IMDb 独有人物规范名（22,157）、商品类别（9,590）、商品所属作品成员与演职员成员拆分口径等；试算各类数字逐一对上：作品标量 34,630、版本标量 49,980、类型成员 18,558、版本特征 46,027、别名 90,433、有效 tconst 5,407、上界 6,978 |
| 覆盖率（R25） | 85.6%（严格口径试算），期望 100% | 100%（469,671 / 469,671） | traced 410,386 中非空 410,309；relocated 59,362 = 类型页面成员 10,320 + 版本特征 46,027 + 评论上界 3,015，正好是试算缺口 |
| trace_status | — | traced 410,386、relocated 59,362、untraced 61,623（全部是空值） | 无 partial、evidence_unverified、manual |
| 与目标层一致（R26） | — | 不一致 0 | — |
| select 边 | — | 493,144 | 悬空 0、非空选择无父节点 0、环 0 |
| 视图 | — | B000063UR2：success、movie、Legend、edition、merged；Legend 最早评论 `rd0001` 下 mapped、D3_family_copy、归属 Legend；没有作品的行 mapped 5,612,062、quarantined 62,131、incomplete 12 | 评论不复制 |
| `task9` 体量 | 1.5–2.5 GB（预算，全部阶段） | 副本合计约 7.3 GB（含 T9-01、T9-02、T9-03 的旧键）；当前一组键估约 3.7 GB（T9-02 估的 3.2 GB 加字段选择约 500 MB、select 边） | 超预算，原因同 T9-02；影响闭包尚未计入 |

## T9-04 实现要点与对计划的修正

- **影响闭包**（`etl_task9.impact.build_impact`，算法 `i1`）：从每条字段选择沿 derivations 与 selections 两个键的边向上递归到源记录（`UNION` 去重、深度上限 40 只防异常），按源记录与选择分组取最短边数写 `field_impact`。整个闭包与完成标记在**一个事务**里写入（单元 `closure` 只有一个实体 `all`，R38“影响闭包在该阶段最后一个事务中一次写入”）；4,096 个桶按每 256 个一条语句执行，控制递归中间结果；该事务里 `SET LOCAL work_mem = '256MB'`、关闭并行（任务 8 的经验）。中断后没有完成标记，重跑整体重算。
- **规则影响**（`rule_impact`）：按 `rule_id` 或 `rule_version`（两者同时给出取交）找边，再沿子边（`derivation_parent_idx`）向下递归到选择；不另建闭包（同计划）。
- **来源核验**（`verify_sources`）：页面快照逐个流式解压重算正文哈希（线程池）；IMDb 与评论按文件重算 SHA-256（IMDb 目录沿用任务 4 的 `snapshot_dir`，即 `IMDB_DATA_DIR` 或 `/data/imdb`）。文件不可读记 `unavailable`，**不算不一致**：`crawler` 容器没有挂载评论文件，只有 Airflow 容器有。不一致的源记录经 `field_impact` 得到受影响字段，按源种类映射到重跑步骤（`impact.RERUN`，即上表），命令在有不一致时退出码 1。
- **查询**（`etl_task9.query`）：`Tracer` 从选择或断言沿父边逐层展开（带缓存），每个节点给出断言或源记录摘要，每条边给出操作、规则、上游任务、运行、时间、角色与是否重放；父节点超过 `max_parents`（默认 5）时先列直接依赖再列其他，并给出总数与按操作计数（推断上界的全部依赖评论）。`evidence` 是展开到的源记录清单与路径长度。人物的署名经 `task5.mention_person` 按公式算出断言 ID（不扫 `assertion` 表），参与作品取 `task7.movie_person_resolved`（cv 的 multivalue 阶段键）。`source --tconst/--nconst` 按源记录定位的 `tconst`/`nconst`（crew 行为 `nconsts` 数组）取 IMDb 行。
- **阶段键解析**（`etl_task9.versions.resolve`）：给出 `--lineage-version` 时取该版本；否则取与输入键相同的 verified 版本；都没有时由输入版本推出阶段键（演练副本在 T9-05 之前就是这种情况）；所需阶段未完成即确定性失败。
- **版本差异**（`versions.diff_keys`/`report diff`）：按 `selection_id`（由实体、字段、成员导出，跨版本稳定）全外连接两个版本的选择；比较最终值、状态、`trace_status`、决策（规则、理由、置信度，计划外增补：规则变化常常只改判定依据不改值）、采用断言与来源集合。采用断言按“上游表:值:上游状态”的签名比较，因为任务 7 候选的 `candidate_id` 在每个阶段键下重新编号、断言 ID 随之变化，直接比 ID 会把所有选择都报成变化。
- **血缘版本**（`versions.finalize_version`/`mark_verified`）：T9-04 先实现写版本（AC15 的 diff 需要两个版本），由 T9-05 的完整运行调用；`lv` 号按已有版本数递增，同一输入键已有未核实版本时在原版本上重写。
- **夹具补形**：`_task9_helpers.add_products` 补任务 3 类别、任务 4 匹配状态与一个未采集商品 `B0NC`（逐项说明见 `tests/fixtures/task9/README.md`）。任务 3 的 `_classify_no_page` 不能直接调用：夹具的 `final_url` 不是 amazon.com，任务 3 的分组 SQL 会把全部夹具商品归为 `other_site`；所以按它的写法与正式库的规则编号（`NP1`、`MV1`、`CL2`）写同形行。任务 4 的匹配状态写在 `cluster` 的大函数里，同样按其口径（`merged`、`singleton`、`collection_split`）写同形行。
- **AC15 按实体增量重建：用户 2026-11-04 选定方案 C（全量重建），AC15 改为“全量重建 + diff 与事前 impact 一致”**（spec H09、R35）。决定前的分析保留如下。现状：各阶段按 ID 哈希桶分批，阶段键包含上游版本；新的 cv 改变 sources 键（“被引用的 IMDb 行”依赖 cv），因而五个阶段键全部改变、全量重建（副本约 7–8 分钟、约 3.9 GB）。按实体复用需要满足两个前提，而现有实现都不满足：一是断言与选择的上游键不含版本号（任务 7 的成员、类型、特征断言的上游键含 `cv`，任务 7 候选的 `candidate_id` 每个阶段键重新编号），二是每个单元按实体而不是按 ID 桶分批（现有约 30 个单元都按桶）。可选方案与代价：
  - **方案 A 按实体输入指纹复制**：每个单元按实体（作品、商品、人物、tconst）计算上游输入指纹（上游行去掉版本列后的摘要），记 `entity_fingerprint`；新阶段键下指纹未变的实体从父版本阶段键复制行，只重译指纹变化的实体。须把断言 ID 改为不含版本的上游键（算法 `a3`，连带 derivations 与 selections 的 ID 公式），约 30 个单元改为按实体分批，重新演练前四个阶段。工作量约 1–2 天；体量不减（复制的行照样占盘，每个版本约 3.9 GB）。
  - **方案 B 按上游阶段键拆子阶段并引用旧行**：把单元按所读的上游阶段键分组（任务 7 edition/link/scalar/multi、t4、t5、t6、IMDb），每组一个子阶段键，版本表记录每组的键，查询与核对按组取键；上游子阶段键未变的组直接引用旧行。粒度与任务 7 的阶段键增量相同（是“受影响的组”而不是逐个实体），cv 只改 scalar 规则时只重建作品标量相关的组。须改表的键语义、全部查询与核对的键解析；约 1 天；不重复占盘。
  - **方案 C 保持全量重建**：修改 AC15 该子项为“第二个血缘版本可经 `diff` 只列出受影响字段的变化”（已实现并测试）。需要用户同意修改 AC。
  - **选择方案 C 的理由与代价**：代价是每生成一个新血缘版本都要全量重建，实测约 7.5 分钟（T9-01 至 T9-04 各阶段之和）、每个版本约 3.9 GB（T9-04 副本估算，旧版本按 R36 保留，需要时由用户清理）；本作业的上游版本只在任务重跑时才变，一学期内最多几次。放弃方案 A：要改断言 ID 公式（`a3`）并把约 30 个单元改为按实体分批，前四阶段重新演练，工作量 1–2 天，体量也不减；放弃方案 B：要改阶段键语义与全部查询、核对的键解析，约 1 天，且增量粒度只到“组”。两者的收益都只是省几分钟，且引入复用错误的风险（旧行与新上游不一致时难以察觉）。全量重建下“只影响该影响的字段”仍然可验证：AC15 用事前 `impact` 定位与事后 `diff` 对照（测试 `test_ac15_full_rebuild_diff_matches_prior_rule_impact`）。

## T9-04 演练实测（业务库副本 `etl_t9_rehearsal`，2026-11-03）

`docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.runner stages --through impact`，退出码 0，前四阶段跳过，impact 31 秒。短键 `if9ab0bf333bd203`。

| 项目 | 预算 | 实测 | 说明 |
|---|---|---|---|
| 影响闭包行 | 约 200 万至 300 万（预算） | 887,035 | 预算按“每个字段数个源记录”加上界 21.7 万估计；实际作品标量的采用路径多只到 1 条 IMDb 行，评论上界只经采用的推断断言连到依赖评论（每部作品 1 个选择） |
| 涉及源记录 | — | 399,451 / 429,323 | 未出现的 29,872 条不在任何选择的采用路径上（如只作为未采用候选证据的 IMDb 行、别名之外的作品行） |
| 涉及选择 | — | 469,672 | 等于非空选择 469,671 加 1 个有父边的空值选择 |
| 路径长度分布 | — | 1：9,590（商品类别直连快照）；2：135,770；3：512,451；4：192,736；5：36,488 | 最长 5 |
| `field_impact` 体量 | — | 213 MB | 副本 `task9` 合计 7.5 GB（含旧键）；当前一组键估约 3.9 GB |
| 查询耗时 | — | 每条约 1 秒（含容器启动） | field 输出约 80 KB，person 约 160 KB（含全部路径树） |
| verify-sources | — | 快照 9,590 全部 ok，5 个 IMDb 文件 ok，约 13 秒；评论文件在 Airflow 容器核验 ok | `crawler` 容器无评论文件挂载，记 `unavailable` |

## T9-05 实现要点与对计划的修正

- **完整运行**（`etl_task9.runner.execute_lineage`，CLI `runner run`）：输入检查 → 迁移 → 抽取锁 → 复用判断（同输入键已有 `succeeded` 且版本 `verified` 的运行 → 只记一条 `reused` 审计）→ 五个阶段（已完成的阶段键跳过）→ `versions.finalize_version`（单事务写 `lineage_version`，`unverified`，计数含上游行数快照 `upstream_counts`）→ `check.verify` 通过才 `mark_verified`（版本 `verified`、运行 `succeeded`），否则运行 `failed`、版本保持 `unverified`，同输入重跑在原版本号上重写（R36–R40）。
- **DAG 步骤**（`runner.run_stage`）：七个执行步骤共用一条 `kind = 'lineage'` 的运行，靠 `airflow_run_id` 找回；每步各持一次抽取咨询锁；重试或后续步骤把失败的运行改回 `running` 接着用；后续步骤核对运行的 `inputs` 与本步输入一致。第一个步骤命中可复用版本时整条 DAG run 记 `reused`，其余步骤直接返回。`pipeline.py` 与 DAG 文件写法同任务 8（确定性错误抛 `AirflowFailException` 不重试，暂时错误最多重试 2 次，`finalize_report`、`gate` 为 `all_done`）；故障注入解析直接复用任务 8 的 `_fault_hook`（只读 `fault_injection`），只在 `ETL_ENV=test` 生效。
- **verify / check**（`etl_task9.check`）：在只读事务里核对五个阶段键的登记、父子关系与完成标记；派生图（`derivations.check_graph`，悬空、缺父、环）；`selections.check_values`（R26）；非空值缺口都有 `untraced_value` 质量问题（R25 的缺口如实列出、不当作通过条件；副本实测缺口为 0）；可追溯的非空值都有影响闭包行；版本计数与重算一致、上游行数与物化时相同；`check` 命令另要求版本 `verified`、运行 `succeeded`。
- **导出**（`report export`，DAG 的 `finalize_report` 在核对通过时调用）：`outputs/task9/<抽取>/<血缘版本>/<run_key>/` 下的 `summary.json`（覆盖率按实体、按字段与 `trace_status`，源记录、断言（按级别与上游表）、派生边、重放补定位、闭包计数，质量问题，评论层视图按映射状态的核对数）、`report.md`、`quality_issues.csv`、`examples.md` 与 `checksums.json`。`examples.md` 由查询函数只读生成：五个演示查询各 3 例（field 取“采用 IMDb 且另有被拒候选的年份”、“页面与 IMDb 都有证据的导演”、标题；product 取单品、合集、未采集商品；review 取推断上界的直接依赖、隔离记录、非代表的复制；source 取快照、tconst、评论；person 取别名最多的 3 人）与四类作业示例各 1 例；评论只给定位与记录哈希，不含正文；页面不含 HTML。
- **端到端种子**（`scripts/seed_task9_e2e.py`）：用任务 9 集成测试的夹具（`_task9_helpers.seed` 与 `add_products`），快照写到 `outputs/e2e/t9_seed/`（测试容器与 Airflow 容器都挂载了 `outputs/`），DAG 参数 `snapshot_root` 指向它。
- **对计划的修正**：DAG 增加参数 `snapshot_root`（e2e 的快照不在项目根）；`check` 的缺口判定是“缺口都有质量问题”而不是“缺口为 0”，以免夹具里故意制造的证据问题（AC03、AC08）让核对失败，正式库的“全部可追溯”由 AC20 的查询另行检验。

## T9-05 实测（2026-11-03）

- **真实 scheduler**（`compose.e2e.yaml`，写 `etl_test`，步骤见 runbook 第 2 节）：成功（10 个任务 success，`lv0001`）、同输入重跑（`reused`）、上游 `cv9999` 缺失（`check_inputs` failed 不重试、`gate` failed）、`verify_fail`（`verify` failed 一次、版本 `unverified`）后重跑（在原版本 `lv0002` 上重写为 `verified`）四种情况全部符合预期。第一轮遇到主机暂停（调度器心跳中断 1,044 秒），`verify` 失败的任务被执行器判为异常又跑了一次（两次都是确定性失败），所以重新准备数据后用 `b` 后缀 run-id 重做，`verify` 只执行一次。做完已 `docker compose up -d` 切回正常配置，三个 Airflow 服务正常、`ETL_DB_NAME=etl_movies`、无 DAG 导入错误。DAG 没有在正式库触发。
- **业务库副本**（`etl_t9_rehearsal`，前五个阶段已完成）：`runner run` 生成 `lv0001`（`verified`，墙钟 23 秒，主要是 verify），同输入再跑为 `reused`（1 秒）；`check` 退出码 0（18 秒）；`report export` 12 秒，`examples.md` 19 例（五个查询各 3 例、四类作业示例各 1 例），覆盖率按实体 edition 105,597、movie 237,070、person 118,044、product_movie 8,960 全部 1.0，质量问题 1（`nm17757708` 缺人名行），评论层视图 mapped 7,849,553、quarantined 62,131、incomplete 12（合计 7,911,696）。前五个阶段全量从头运行约 7.5 分钟（T9-01 至 T9-04 各阶段实测之和），留给 T9-07 实测。

## T9-06 实现要点与对计划的修正

- **四个命令**（`etl_task9.evaluate`，目录 `outputs/task9/<抽取>/<血缘版本>/eval/`）：`export --version lvNNNN` 要求版本 `verified`，抽样后写 `eval_set`（`ev_<lv>`，含各层目标、实际与补足数）、`eval_item`（选择键或评论键、路径与证据摘录的 SHA-256），导出 `eval_items.csv` 与 `LABELING.md`；已导入标注时拒绝重导（`--force` 清除标注后重导）。`draft` 只读 `eval_items.csv` 与原始证据文件生成 `eval_items_draft.csv`，不连数据库。`import --file --annotator` 整批校验后写 `eval_label`。`metrics` 输出 JSON 并写 `metrics.md`。
- **抽样**（R46）：L1–L4 直接取 `field_selection` 的非空值按 `md5(selection_id)` 排序；L5 的商品归属（`product_movie` 选择）与评论归属（`rd` 版本的 `review_mapping` → `logical_review_work`，键为 `review:<source_review_id>`）各自按 md5 排序后交替排列，保证两类都被抽到（只按 md5 合并时夹具与正式库的前 10 个可能全是商品）。补足按 `L2 → L1 → L3` 取这些层的后续候选。
- **审核表**（R47）：路径由 `query.Tracer`（每层最多 3 个父节点，其余写“共 N 个父节点，按操作…”）展开为“操作[rule_id|规则版本|上游任务] ← 上游表:键”；证据最多 6 条：页面给快照路径、正文哈希与该页面源记录的直接子断言原文（≤ 300 字符）；IMDb 给文件、行号，按行号从原始 gz 文件顺序读出原行（文件不在容器时标 `file_unavailable` 并附装载表行）；评论给记录号、起始行、记录哈希与正文前 80 个字符。审核表不含 `trace_status` 等系统状态列。
- **AI 草稿**（R49）：逐条解压快照核对正文哈希并检索定位原文，IMDb 逐行比对最终值；`ai_read` 写每个文件的读取结果。快照不可读、哈希不一致、定位原文未找到或 IMDb 原文件不可读时 `ai_label = unsure`、`ai_doubt = true`；最终值出现在原始证据中时 `correct`；其余（值经规范化或受控词映射）`unsure` 并存疑，留给人工按路径里的规则核对。草稿不会给出 `incorrect`，错判只能由人工给出。
- **导入校验**（R49）：`label` 闭集；`incorrect` ⇔ 有 `error_type`（闭集）；与 `ai_label` 不同或 `ai_doubt = true` 的项须填 `human_evidence`（标 `unsure` 的除外）；`note` 不得原样粘贴 `ai_reason`；任何一条不合格整批拒绝，不写入。
- **指标**（R48）：正确率 = `correct` /（`correct` + `incorrect`），总体与分层，`unsure` 单列，分母为 0 记“无定义”；另给错误类型计数与标注人；`metrics.md` 声明只代表 60 条抽查样本、标注为 AI 预填与人工逐项审核。
- **对计划的修正**：L5 由“合并后按 md5 排序”改为两类交替（理由见上）；快照哈希不一致也列为存疑原因。夹具补了任务 8 评论映射（见 `tests/fixtures/task9/README.md`）。正式库的审核表与草稿留给 T9-08 生成，T9-06 没有在正式库或副本上导出。

## 业务库副本全链路演练实测（T9-07，2026-11-04）

副本 `etl_t9_rehearsal` 是 T9-01 用 `CREATE DATABASE … TEMPLATE etl_movies` 建的（当时停 Airflow、建库后 `CHECKPOINT`），T9-02 至 T9-05 在上面分阶段运行，保留了多组旧阶段键（`task9` 约 10 GB）。为了从头验证全链路，本次没有重建副本，而是在副本里清出空的 `task9`：确认副本无其他连接后，一个事务内 `DROP SCHEMA task9 CASCADE` 并删除 `public.schema_migration` 的 `801` 登记行，随后 `CHECKPOINT`；`runner run` 再从迁移 801 开始完整运行。这一步只动副本，正式库 `etl_movies` 的迁移仍为 `001`–`701`、没有 `task9`（运行前后只读核对）。不建库就不需要停 Airflow。

| 项目 | 试算或预算（规划） | 分阶段演练（T9-01 至 T9-05） | 全链路实测（`lv0001`） | 差异解释 |
|---|---|---|---|---|
| 运行 | — | 五个阶段分五次运行 | `runner run` 退出码 0，`succeeded`，`lv0001` `verified`；同输入再跑 `reused`（1 秒） | — |
| 墙钟 | 20–40 分钟（预算） | 各阶段之和约 7.5 分钟 | 440 秒（sources 59、assertions 86、derivations 164、selections 84、impact 32 秒，其余为迁移、版本与 verify） | 预算按逐个解压快照估计；实际快照核验只用约 10 秒（线程池），主要是库内连接。各阶段与分阶段运行相差在 ±25 秒内（同机记录，不是性能实测） |
| 阶段键 | — | `s11f2e418823b9fe`、`a0d345df84d5280a`、`d8f913d99aa00f0e`、`l7292c0631b27f9a`、`if9ab0bf333bd203` | 五个键完全相同 | 键只由输入与算法签名决定，全链路与分阶段得到同一结果 |
| 源记录 | 约 45 万 | 429,323 | 429,323（快照 9,590 全部 `ok`、IMDb 202,298、评论 217,435、人工修订 0） | 预算对 IMDb 被引用行的估计偏高（实测 202,298，见 T9-01 修正） |
| 断言 | 80–100 万 | 1,548,754 | 1,548,754（extract 1,062,036、candidate 156,568、derived 330,150） | 预算未计任务 3 页面字段、IMDb 按列拆分与集成层断言（T9-01、T9-02） |
| 派生边 | 约 150 万 | 2,106,585 + select 边 493,144 | 同左，12 种操作计数逐项相同；悬空、缺父、环均为 0 | 预算未计抽取边与选择边 |
| 重放补定位 | 缺口 10,320 + 46,027 + 3,015（试算） | 类型词面 34,531、版本特征 46,027、第二早评论 3,015，全部一致 | 同左，`replay_mismatch` 0 | 类型按词面计（34,531 条词面覆盖 10,320 个页面侧成员） |
| 字段选择 | 约 45 万 | 531,371 | 531,371（非空 469,671） | 试算 411,673 未计人物外部 ID、IMDb 独有人物、商品类别等（T9-03） |
| 覆盖率（严格口径） | 85.6%（试算） | 100% | **100%**（traced 410,386、relocated 59,362、untraced 61,623 全部是空值；非空值缺口 0） | relocated 59,362 正好等于试算缺口 |
| 与目标层一致 | — | 不一致 0 | 不一致 0 | — |
| 影响闭包 | 200–300 万（预算） | 887,035 | 887,035（涉及源记录 399,451、选择 469,672，最长 5） | 预算高估了每个字段的源记录数（T9-04） |
| 质量问题 | — | 1 | 1（`nm17757708` 缺 `imdb.name` 行，改连演职员行） | 上游事实（T9-01） |
| `task9` 体量 | 1.5–2.5 GB（预算） | 估约 3.9 GB（按行数比例从含旧键的表推算） | **4,218 MB**（`assertion` 1,411、`derivation` 1,246、`field_selection` 614、`source_record` 584、`field_impact` 213、`entity_stage` 96、`relocation` 54 MB） | 这次只有一组键，是实测值；比按行数比例的估计多约 0.3 GB（索引与 TOAST 不随行数线性分摊）。超预算原因同 T9-02 |
| 导出 | — | `outputs/rehearsal_t9/…/lv0001/rehearsal/` | `outputs/rehearsal_t9/…/lv0001/rehearsal_full/`：`summary.json` 与上次相比只有 `run_id`、`created_at`、`verified_at` 不同，`examples.md` 逐字节相同 | 旧导出保留未删 |

演示查询（命令同“演示查询”一节，加 `-e ETL_DB_NAME=etl_t9_rehearsal`）结果与 T9-04 实测一致：Legend `release_year` 为 `1985`、`resolved`、`year.table:YR2`，两条 `minor_tolerance` 候选；B000063UR2 影响 9 个实体的 41 个选择、`tt0089469` 影响 12 个实体的 56 个选择；Olivia de Havilland 6 条署名、2 个别名、1 个外部 ID、4 部作品 6 个角色、映射事件 0；`impact --rule rating:MR2` 26 个 `mpaa_rating`。`verify-sources`：`crawler` 容器里快照 9,590 与 5 个 IMDb 文件全部 `ok`、评论文件 `unavailable`（退出码 0）；Airflow 容器里（`docker compose exec -T -e ETL_DB_NAME=etl_t9_rehearsal airflow-scheduler python -m etl_task9.impact verify-sources --kinds review_record`）评论文件 217,435 条引用 `ok`。`check` 退出码 0（14 秒）、`problems` 为空。

**删库与空间回收**：确认副本没有会话后 `DROP DATABASE etl_t9_rehearsal`，再 `CHECKPOINT`。postgres 卷空闲由演练开始时约 64 GB（清 `task9` 后 72 GB，跑完 68 GB）回收到 **108 GB**（已用 42 GB），`pg_wal` 1.1 GB；剩下的库为 `etl_movies` 36 GB、`etl_test`、`airflow_meta`。演练没有发现新缺陷，未改实现。

## T9-08 正式库运行实测（`etl_movies`，2026-11-04）

- **运行**：运行前只读记录上游行数；Airflow `t9_full_1`（`--conf '{}'`，即 R01 默认版本）在 `check_inputs` 应用迁移 801，10 个任务 success，触发到结束 449 秒；`t9_full_2` 31 秒，`task9.run` 记 `reused`。阶段键与 T9-07 副本完全相同（`s11f2e418823b9fe`、`a0d345df84d5280a`、`d8f913d99aa00f0e`、`l7292c0631b27f9a`、`if9ab0bf333bd203`），各项计数、覆盖率（100%）与导出的 `examples.md`（sha256 `12d28395…`）逐项相同；运行后上游行数不变，迁移为 `001`–`801`。
- **体量**：`task9` 4,222 MB（副本 4,218 MB），`etl_movies` 36 → 40 GB，卷空闲 104 GB。
- **核对**：`check` 退出码 0（12 秒）；`verify-sources` 快照与 IMDb（`crawler`，12 秒）、评论文件（Airflow 容器）全部一致；六个查询的 JSON 与副本相同。
- **审核表的修正**：首次 `evaluate export` 后发现推断上界这类选择项的依赖评论证据缺正文前 80 个字符（R47），L5 评论归属项不受影响。补测试 `test_ac18_bound_review_evidence_has_text_head`，`evaluate.item_rows` 改为对没有摘录的评论证据从 `public.review_staging` 只读补齐；标注尚未导入，`evaluate export --version lv0001 --force` 重新导出（样本按 md5 确定，60 项不变）并重新 `draft`。草稿 `correct` 47、`unsure` 13，分层见 report 第 7 节。
- **待办**：付林轩审核草稿、导入、计算指标（AC21），见交接文档 `doc/handoffs/2026-11-04-task9-review-pending.md`。

## 夹具设计（R42）

任务 7 的教训是夹具与真实输出不同形会掩盖缺陷。任务 9 的夹具在 `etl_test` 中按顺序建立任务 1–8 的真实表结构（应用迁移 `001`–`701`），用各任务自己的装载或写入函数生成上游行（任务 1 装载器读合成 gzip；任务 3–8 用各自 `tests/fixtures` 的写法），保证 jsonb 键名、数组与阶段键与正式库一致；只有 IMDb 与快照文件是小型合成文件。`tests/integration/conftest.py` 增加 `DROP SCHEMA IF EXISTS task9`。每个 AC 独立初始化。

## 抽查评价设计（R46–R49）

- 抽样：在 `lv0001` 的 `field_selection` 中按 `md5(selection_id)` 排序分层取前 N：`L1` 作品标量 15、`L2` 作品多值成员 15、`L3` 版本字段 10、`L4` 人物 10、`L5` 评论与商品归属 10（商品归属取 `product_movie` 选择，评论归属取 `rd` 版本的评论映射与归属，两类交替排列，见“T9-06 实现要点”）。
- 审核表（CSV，不入 Git）：实体、字段、最终值、处理路径（每步操作、`rule_id`、上游表与键），原始证据摘录（页面按定位取区域附近文字 ≤ 300 字符、IMDb 原行、评论定位与正文前 80 个字符），标注列 `label`、`error_type`、`human_evidence`。
- AI 草稿：AI 只读审核表与原始证据文件，逐项写“读到的原始值”与判断，不读 `task9` 的状态列；人工（付林轩）逐项审核后导入，改判或存疑项须填依据（R49）。
- 指标：血缘链正确率 = `correct` /（`correct` + `incorrect`），总体与分层；`unsure` 单列。只代表抽查样本。

## 资源预算与估算

| 项目 | 预算 | 说明 |
|---|---|---|
| 运行时间 | 约 20–40 分钟（预算） | 快照哈希核验读 9,590 个 gzip（约 1.6 GB 压缩）；其余是库内连接 |
| `task9` 体量 | 约 1.5–2.5 GB（预算） | 见上文预算表 |
| 演练副本 | 约 36 GB + WAL | 磁盘可用 110 GB；建删后 `CHECKPOINT` |

## 异常与恢复处理

- 上游版本缺失或未 `verified`：`check_inputs` 确定性失败，不重试。
- 快照缺失或哈希不一致：不中断构建，写质量问题并降级 `trace_status`（R09）；正式库若出现，报告逐条列出。
- 重放不一致：不中断，写 `replay_mismatch`，字段保持 `partial`（R21）。
- 中断：已提交批次由 `entity_stage` 跳过；影响闭包整体替换；`verify` 失败时版本保持 `unverified`，同输入重跑在原版本上重写（R40）。
- 大连接的查询计划：沿用任务 8 经验，必要时只在该事务里 `SET LOCAL enable_nestloop = off`、调大 `work_mem`，并关闭并行哈希以免占满 `/dev/shm`。
