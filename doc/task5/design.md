# 任务 5 设计思路（人物姓名归一与消歧）

状态：2026-10-29 编写计划；2026-10-30 按用户要求加强没有 IMDb 关联作品的跨作品人物匹配（作品年表反查、合作者佐证迭代、稀有度与发行商特征、按影响排序的待复核队列），规则编号随 spec 改为 R01–R47。2026-10-30 用户审批通过，开始实现；实现中的修订记在文末。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明怎么实现、为什么这样选。兼容等级、得分、阈值、年代容差与稀有度分档都是**规划默认值**（规则版本 `t5r1`），只在开发池上调整、评价表导出前冻结；资源数字都是**估计**，实测在 T5-07 补记。规划时的数字来自 2026-10-29、30 正式库只读查询，正式数字以 AC14 为准。

## 整体思路

任务 5 是集成层里的人物部分：输入是任务 3 的页面字段（暂存层）、任务 4 的作品映射 `map0002` 与 IMDb 快照（第三方原始层）；输出是人物实体、别名、外部 ID 和作品–人物关系（集成层与目标层）。全程离线，不联网。

分六步，每一步的结果都入库、都可追溯：

1. **人物提及**（R02–R07）：从参与匹配商品的页面字段里，把导演、演员的每个名字写成一条提及，带原值、来源位置和多种比较键。
2. **外部 ID，途径一：作品演职员**（R10–R13）：商品关联了 IMDb 作品时，在该作品的演职员里找兼容的人；唯一且同角色的自动关联 `nconst`，其余进复核或不关联。
3. **外部 ID，途径二：作品年表反查**（R14–R16）：商品没有 IMDb 关联时，看同名同角色、已经在别处核实的人，他的 IMDb 作品里有没有恰好一部与这个商品同名的作品；有就关联。
4. **作品内人物组**（R18）：同一部作品里同名的提及（多个商品版本、署名行与详情要点重复）合成一组，组是跨作品比较的单位。
5. **跨作品归并**（R17、R19–R25）：同一 `nconst` 的组直接合并；没有外部 ID 的同名组只有在有共同合作者时自动合并，合作者佐证按轮迭代；其余兼容姓名对进复核或不合并。合并时检查一致性，生成稳定 `person_id`、规范姓名、别名与人物映射版本，导出按影响排序的待复核队列。
6. **MoviePerson、报告与评价**（R31–R41）：按角色生成作品–人物关系，导出报告、实例与查询；另有命令导出评价表与簇抽查表、导入标注、计算指标。

**为什么外部 ID 只在关联作品的演职员里找**（R10，H04）：IMDb 205 万人里同名非常多：规划时页面严格键在 IMDb 中对应 2 人及以上的有 2,370 个，对应 0 人的 3,610 个（多半是 IMDb 没收录的 VHS、教学片人员）。按姓名全量搜索等于“姓名相同就是同一人”。而任务 4 已经用标题、导演、演员、年份核实了作品匹配，在这部作品的十几位演职员里找同角色、姓名兼容的人，证据是“共同作品 + 角色 + 姓名”，范围也小到可以判断唯一性。代价是只有约一半商品有 IMDb 关联，其余人物要靠途径二与页面证据。

**为什么加作品年表反查**（R14，H13）：约一半作品没有 IMDb 关联，常见原因是任务 4 的标题键或姓名键太严、停在复核，而不是 IMDb 里没有这部片。这些作品的署名人里，有不少已经在别的作品里核实过（例如 Kevin Costner、Tony Scott）。反查他们的 IMDb 作品年表，如果恰好有一部与该商品同名的作品，证据就是“这个已核实的人确实演过、导过一部同名作品”，不是只凭名字。它只从已核实的人出发，不从 205 万人出发，所以不违背 R10 与用户未选的“IMDb 唯一名直接挂靠”。规划估算：可为 311 个没有 IMDb 关联的商品里的 411 条署名确认外部 ID（见下文“输入依据”）。

**为什么跨作品还要“共同合作者”，并且迭代**（R19、R21，H03、H14）：没有外部 ID 时，同名跨作品是最常见的误合并来源（Robert Young、David Bradley 在规划数据里就各有两个 `nconst`）。作者消歧领域常用合著者重叠作强特征（S2AND 等，见“开源参考”）；对电影，就是两部作品里有同一位已核实的合作者，或有两位同名合作者。一轮合并会产生新的“已核实合作者”（新合成的人物），它们又能佐证别的同名对，所以迭代到没有新增为止。拿不到佐证就保留两个实体、标为待复核，宁可漏合并、不误合并。

## 输入依据与规划时快照

- 任务依据：作业任务 5 与第五、八、九、十部分；交接文档 `doc/handoffs/2026-10-29-task4-to-task5.md` 第 2–8 节。
- 页面署名（2026-10-29 只读查询，`t3p1`，参与匹配的 9,590 个商品）：

  | 区域与标签 | 行数 | 商品数 | 用法 |
  |---|---|---|---|
  | 署名行 Actor | 14,656 | 7,806 | 主演与演员（每行一人，有顺序） |
  | 详情要点 Actors | 8,676 | 8,676 | 演员完整名单（逗号串，约 34,363 个名字，按字母排序） |
  | 署名行 Director | 7,535 | 6,787 | 导演（每行一人） |
  | 详情要点 Director | 6,880 | 6,880 | 导演（逗号串，约 8,548 个名字；只在没有署名行时用） |
  | 详情要点 Writers / Producers，署名行 Writer / Producer | 2,581 / 1,889，1,141 / 530 | — | 只计数（R03） |
  | 概览 Contributor，署名行 Host、Author 等 | 7,376，约 140 | — | 只计数 |

  署名行标签是 “Actor”，不是 “Starring”；但它是页面顶部单独列出的 1–2 位演员（例如 Men in Black 的署名行是 “Tommy Jones”“Will Smith”，详情要点是按字母排的五人），这是 H02 把它定为主演依据的理由。

- 任务 4 的人物断言（只含署名行为主的导演与演员）：不同严格键 15,725 个，其中 507 个有不止一种原始写法（“Bogart, Humphrey” 与 “Humphrey Bogart”、“Ciaran Hinds” 与 “Ciarán Hinds”、“Frank McDonald” 与 “Frank Mcdonald”）；3,111 个严格键出现在至少两部作品中（共 9,659 个键–作品对）。
- 在 IMDb 关联商品（4,544 个）里，把任务 4 断言与该作品的 IMDb 演职员对照：

  | | 严格键命中 | 只有宽松键命中 | 只在另一类别命中 | 未命中 |
  |---|---|---|---|---|
  | 导演 | 4,520 | 42 | 4 | 101 |
  | 演员 | 7,590 | 19 | 30 | 1,244 |

  未命中的例子说明了要处理的写法：“Tommy Jones” 对 “Tommy Lee Jones”（中间名缺失）、“Billy Bob Thorton” 对 “Billy Bob Thornton”（拼写）、“George Hayes” 对 “George 'Gabby' Hayes”（昵称）、“Ian Blake Nelson” 对 “Ian Nelson”、“Andy Wachowski” 对 “Lana/Lilly Wachowski”（改名，IMDb 不给旧名）、“Victoria Adams” 对 “Victoria Beckham”（婚后改姓）。演员未命中的大部分是页面演员不在 IMDb 的主要演职员（约 10 位）里。
- 同一严格键在关联作品里对应两个 `nconst` 的有 6 个：robert young、david bradley、robert taylor、gilbert hernandez、rex ingram、dale reynolds。它们是“同名不合并”实例的直接来源。
- **作品年表反查的规划估算**（2026-10-30 只读，脚本只在 scratchpad，不入库）：
  - 口径：只用任务 4 的 `byline`、`reversed`、`detail` 断言；已核实人物 = 在关联作品里严格键唯一命中的人（(角色, 严格键) 7,913 个、`nconst` 7,781 个，其中 2 个严格键对应两个 `nconst`）；他们在 IMDb 同角色的作品 140,910 部；标题键取任务 4 的 `imdb_title_key`（`t4n1`、只取电影类）。
  - 没有 IMDb 关联的电影类商品 3,409 个、署名 8,031 条：没有同名同角色的已核实人物 6,314 条；有的 1,717 条，其中年表里恰有一部同标题键作品 411 条（311 个商品）、多部或多人满足 6 条、年表里没有同名作品 1,300 条。
  - 至少两条署名指向同一 IMDb 作品的商品 90 个，例如 “Peggy Sue Got Married”（Kathleen Turner、Nicolas Cage 都指向 `tt0091738`）、“Revenge”（Tony Scott、Kevin Costner、Anthony Quinn 都指向 `tt0100485`）。这类商品很可能是任务 4 漏关联的作品，按 R16 只记线索。
  - 估算没有包括详情要点演员、合集成员、X3 兼容与迭代，正式数字以 AC14 为准。

## 开源参考

按 CLAUDE.md 要求，动手前对比了三个开源项目的思路（2026-10-29 检索）：

| 项目 | 借鉴什么 | 不直接用的原因 |
|---|---|---|
| [derek73/python-nameparser](https://github.com/derek73/python-nameparser)（PyPI `nameparser`） | 把姓名拆成称谓、名、中间名、姓、后缀、昵称；支持 “Last, First”；称谓与后缀按词表与位置判断 | 新增依赖要重新锁定 `requirements.lock`；2.0 版 API 与 0.x 不兼容；它的词表和启发式随版本变化，不便随 `t5n1` 冻结。我们只需要其中几条规则，自写并在单元测试里固定 |
| [moj-analytical-services/splink](https://github.com/moj-analytical-services/splink) | 用多条分块规则（如“名首字母 + 姓”）生成候选，任一规则命中即成为候选对；分块过松会爆炸、过严会漏；按词频（稀有度）调整姓名相同的分量 | 它是 Fellegi–Sunter 概率模型，需要估计参数；作业要求判定可解释、有规则版本，而且我们最强的证据（共同作品、外部 ID）不是字段相似度。只借分块思路与“稀有名更有分量”的直觉（R22，默认只记录） |
| [allenai/S2AND](https://github.com/allenai/S2AND) | 作者消歧把合著者重叠当强特征，先分块再聚类，并检查簇的一致性 | 用的是训练过的模型；本任务不用机器学习判定（范围），只借“合作者佐证”和“簇内约束”的思路 |

结论：自写小型确定性规则（H09），把开源项目的做法写成有编号的特征，代价是覆盖的姓名格式没有 `nameparser` 全，未识别的格式按原样进严格键，在复核里暴露。

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/401_task5_schema.sql` | schema `task5` 的下文各表、约束与索引；登记迁移 `401` |
| `src/etl_task5/names.py` | 纯函数：倒序、昵称、括号消歧段、后缀与称谓的拆分，五种比较键，兼容等级 X1–X4 与 N1–N4；`NORM_VERSION = t5n1` 与词表集中在此 |
| `src/etl_task5/mentions.py` | 从 `task3.page_field` 取署名，写人物提及与源记录（R02–R07） |
| `src/etl_task5/external.py` | 途径一：取关联作品的 IMDb 演职员，生成外部 ID 候选与判定（R10–R13）；途径二：已核实人物的作品年表反查与任务 4 升级线索（R14–R16） |
| `src/etl_task5/rules.py` | 纯函数：外部 ID 与跨作品候选的特征、得分、硬冲突与三档判定，稀有度与发行商特征及开关；`RuleSet`、`RULESETS`、`CURRENT_RULE_VERSION` |
| `src/etl_task5/resolve.py` | 作品内人物组、跨作品候选生成与第 1 轮判定落库（分批） |
| `src/etl_task5/cluster.py` | 合作者佐证迭代、人物簇与一致性检查、`person_id` 登记、规范姓名、别名、外部 ID、人物映射版本与事件、人工决策叠加、版本差异 |
| `src/etl_task5/movie_person.py` | MoviePerson 与来源（R31–R34） |
| `src/etl_task5/run.py`、`runner.py` | 迁移应用、输入键、运行审计、抽取锁、批次与实体阶段标记、分步骤编排 |
| `src/etl_task5/review.py` | 待复核队列按影响排序导出；人工决策 CSV 的校验与导入（R29、R30） |
| `src/etl_task5/report.py`、`query.py`、`check.py` | 报告与实例导出；两个查询（R41）；正式库只读核对（AC14） |
| `src/etl_task5/evaluate.py` | 开发池冻结、评价表与簇抽查表导出、标注导入、指标 |
| `src/etl_task5/pipeline.py`、`dags/amazon_movies_task5_person.py` | DAG 步骤函数与编排 |
| `scripts/seed_task5_e2e.py` | 为真实 scheduler 端到端准备合成数据（写 `etl_test`） |
| `tests/fixtures/task5/` | 合成页面字段、任务 4 映射与断言、IMDb 行与标题键，预期值说明 |

复用而不复制：

- `etl_task1`：`config.load_settings`、`db.connect`、`db.advisory_key`、`reporting.write_atomic`、`reporting.file_digest`、`reporting.utc_now`、`pipeline.is_transient`、`pipeline.gate_decision`。
- `etl_task2.pipeline.run_step` 与失败文件写法。
- `etl_task4.normalize`：`fold`、`strict_name`（严格键与任务 4 一致，便于对照与以后升级 `t4r3`）、`title_key`（只用于核对，商品标题键直接读任务 4 断言）、`looks_last_first`、`PLACEHOLDER_NAMES`。
- `etl_task4.evaluate.prf`（指标与“无定义”规则）；`etl_task4.run` 的锁、批次与实体标记写法（任务 5 的表单独建，不写 `task4`）。
- 只读任务 4 的数据：`task4.field_assertion` 中的 `title`（标题键）、`title_year`、`edition.publisher`、`edition.publisher_series` 与合集成员的标题断言；`task4.imdb_title_key`（IMDb 标题键，含别名）。

## 数据模型

作业第八部分的实体与本任务表的对应：

| 作业实体 | 本任务的表 | 说明 |
|---|---|---|
| Person | `task5.person`（按人物映射版本），身份登记 `task5.person_identity`，外部 ID `task5.person_external_id` | 规范姓名、状态、外部 ID |
| PersonAlias | `task5.person_alias` | 每种原始写法一行 |
| MoviePerson | `task5.movie_person`、`task5.movie_person_source` | 角色、署名顺序、来源提及 |
| MergeDecision | `task5.link_decision`（外部 ID，两条途径）、`task5.person_decision`（跨作品），人工决策 `task5.human_decision` | 未采用的候选同样保存 |
| FieldAssertion | `task5.person_mention` | 人名的原值、规范值、来源位置与规则；有任务 4 断言时指回 `assertion_id` |
| SourceRecord | 复用 `task4.source_record`（页面快照） | IMDb 证据记快照、文件、行号与 `nconst`/`tconst`，与任务 4 的做法一致 |

外键指向 `public.extraction`、`task3.page_field`、`task4.field_assertion`、`task4.source_record`、`task4.mapping_version`、`task4.movie`、`imdb.*`，不在任务 1–4 的表上加列或约束（R46）。

**运行与版本**

- `task5.run`：`run_id`，`kind`（`person`/`human_import`/`evaluate`），输入键各项，`status`（`running`/`succeeded`/`failed`/`reused`/`interrupted`），`params`（`batch_size`、`limit`），计数、起止时间、`dag_id`、`airflow_run_id`、`failure_reason`。
- 输入键：（`extraction_id`, `t4_mapping_version_id`, `parser_version`, `norm_version`, `rule_version`, `imdb_snapshot_id`, `human_set_hash`）。已有成功运行时新运行记 `reused`（R42）。
- `task5.run_batch`、`task5.entity_stage`：与任务 4 相同的批次审计与实体阶段标记。阶段键：提及 = 解析版本 + 规范化版本；外部 ID 与跨作品判定 = 映射版本 + 解析版本 + 规范化版本 + 规则版本 + IMDb 快照。所以只改规则版本时不重新抽取提及（AC09）。

**人物提及**

- `task5.person_mention`：唯一键（`extraction_id`, `parser_version`, `norm_version`, `mention_key`）。
  - `mention_key` = `<ASIN>:<区域>:<页面字段序号>:<列表位置>`，例如 `0767802624:byline:1:1`、`0767802624:detail_bullets:8:3`，稳定、可直接定位 `task3.page_field`。
  - 来源：`asin`、`product_kind`、`section`、`page_ordinal`、`list_position`、`label_raw`、`t4_assertion_id`、`source_record_id`（页面快照）。
  - 角色：`role_class`（`director`/`actor`）、`is_byline`、`byline_rank`（署名行内同标签的第几位）。
  - 姓名：`raw_value`（原样）、`display_name`（倒序后的写法）、`nickname`、`disambiguator`（如 `III`）、`suffix`、`honorific`、`strict_key`、`loose_key`、`tokenset_key`、`family_key`、`initial_key`、`flags`（`reversed`、`encoding_damaged`）。
  - `status`：`resolvable`/`placeholder`/`ambiguous`。
- 其他角色署名（编剧、制片等）不建提及，报告时从 `task3.page_field` 直接按标签计数（R03、R40）。

**外部 ID**

- `task5.link_candidate`：唯一键（阶段键, `mention_key`, `route`, `tconst`, `nconst`）。
  - `route`：`credits`（途径一）或 `filmography`（途径二）。
  - 共同字段：`category`（IMDb 的 director/actor/actress）、`level`（X1–X4；途径二固定为 X1）、`cross_role`、IMDb 证据（`title_director.ordinal` 或 `title_principal.ordering`、文件与行号，`name.basics` 行号）。
  - 途径一：`work_movie_id`（商品所在的已关联作品）。
  - 途径二：`anchor_link_decision_id`（N 是在哪条途径一判定里核实的）、`matched_title_key`、`title_key_source`（`primary`/`original`/`aka`）与 `imdb_title_key` 的 `source_line`、命中的是商品标题还是合集成员标题（成员序号）、IMDb `startYear` 与商品标题年份。
- `task5.link_decision`：唯一键（`link_candidate_id`, `rule_version`）；`tier`（`auto`/`review`/`reject`）、`reason`、`confidence`、`thresholds`、`run_id`、`decided_by`。一条提及最多一个 `auto`（两条途径合计）。
- `task5.t4_upgrade_hint`：主键（阶段键, `asin`, `tconst`）；`member_ordinal`、`mention_keys`（数组）、`link_decision_ids`、`mention_count`。只记录，不被任何步骤读来修改作品归属（R16）。

**作品内人物组与跨作品候选**

- `task5.person_group`：`group_key` = `<作品单位>|<组内最小 mention_key>`；`work_unit`（`movie:<movie_id>` 或合集未归属时的 `collection:<ASIN>`）、`movie_id`、`role_classes`、代表性的五种键、`nconst`（组内自动关联的外部 ID，最多一个）与其途径、`year`（作品年份，见下）、`publishers`（作品内商品的发行商与发行系列，规范化后的集合）、`rarity`（见下）。
- `task5.group_member`：`group_key`、`mention_key`、`reason`（`same_strict`、`same_tokenset`、`linked_same_nconst`）。
- `task5.person_candidate`：唯一键（阶段键, `group_a`, `group_b`），`group_a < group_b`；`generator`（C1–C4）。
- `task5.person_decision`：唯一键（`person_candidate_id`, `rule_version`, `round`）；`round` 为迭代轮次（第 1 轮来自分批判定，之后各轮只为判定档变化的候选新增一行）；`tier`、`score`、`confidence`、`veto`（数组）、`reason`、`features`（jsonb：姓名等级、角色、外部 ID、合作者及其所属人物、年代、稀有度、发行商，各带所用提及与 IMDb 证据；`feature_switches` 记录稀有度与发行商是否参与判定，以及打开时的判定档 `tier_if_enabled`）、`thresholds`、`run_id`、`decided_by`。一个候选在某个规则版本下的最终判定是最大轮次那一行。
- `task5.iteration_round`：主键（`pmap`, `round`）；`new_auto_edges`、`new_merged_persons`、`blocked_edges`、`started_at`、`finished_at`。
- `task5.human_decision`：`kind`（`must_link`/`cannot_link`/`detach`）、`left`（`mention_key`）、`right`（`mention_key` 或 `imdb:<nconst>`；`detach` 为空）、`note`、`annotator`、`decided_at`、`import_file_sha256`、`set_hash`。

**人物与映射**

- `task5.person_identity`：`anchor_key` 唯一（`imdb:nm…` 或 `mention:<mention_key>`）；`person_id` 唯一，取 `ps_` 加 `sha256(anchor_key)` 前 16 位十六进制（不含原始 ID）；`created_run`。碰撞由唯一约束发现，即确定性失败。
- `task5.person_mapping_version`：`person_mapping_version_id`（`pmap0001`…），`parent_version_id`、`run_id`、输入键各项、`counts`（R29、R40 的数量）、`created_at`。
- `task5.person`：主键（`pmap`, `person_id`）；`status`（`external_verified`/`page_only`/`review_pending`）、`has_external_id`、`external_route`（`credits`/`filmography`/`co_credit`，人物最早获得外部 ID 的途径）、`anchor_key`、`canonical_name`、`canonical_source`（`imdb:<nconst>` 或 `mention:<mention_key>`）、`mention_count`、`work_count`、`pending_candidates`。
- `task5.person_external_id`：主键（`pmap`, `person_id`, `system`）；`system = 'imdb'`、`external_id`（`nconst`）、`snapshot_id`、`name_line_no`、`birth_year`、`death_year`、`primary_profession`、`basis_decision_ids`。
- `task5.mention_person`：主键（`pmap`, `mention_key`）；`person_id`、`group_key`、`attribution`（`movie`/`unattributed`）、`basis`（`credits`/`filmography`/`within_work`/`co_credit`/`human`/`singleton`）、`round`（经合作者佐证合入时的轮次）。只有 `resolvable` 的提及有行。
- `task5.person_alias`：主键（`pmap`, `person_id`, `alias`）；`alias_types`（数组）、`source`（`page`/`imdb`）、`occurrences`。提及到别名经 `mention_person` 与 `raw_value` 对应。
- `task5.mapping_event`：`pmap`、`mention_key`、`event`（`create`/`merge`/`split`/`move`/`retire`）、`from_person_id`、`to_person_id`、`reason`、决策 ID。
- `task5.cluster_check`：`pmap`、`round`、`group_a`、`group_b`、`conflict`（`two_external_ids`/`era_conflict`/`different_disambiguator`）、`action`（`edge_blocked` 等）、`resolved_by`。
- `task5.movie_person`：主键（`pmap`, `movie_id`, `person_id`, `role`）；`credit_order`、`order_conflict`、`source_labels`（数组，如 `byline:Actor`、`detail_bullets:Actors`）、`mention_count`。
- `task5.movie_person_source`：主键（`pmap`, `movie_id`, `person_id`, `role`, `mention_key`）；`asin`、`byline_rank`、`list_position`。
- `task5.quality_issue`：运行、实体、类型（`ambiguous_names`、`block_overflow`、`edge_blocked`、`human_vs_veto`、`order_conflict`、`two_nconst_in_group` 等）与细节。

**评价**：`task5.dev_pool`（冻结的姓氏键）、`task5.eval_set`、`task5.eval_pair`（层、双方 `mention_key` 与组、导出时的人物映射版本、系统预测与合并途径）、`task5.eval_label`、`task5.cluster_sample`、`task5.cluster_label`。

## 人物提及（R02–R05）

1. 取参与匹配的商品：`task4.product_edition` 中 `match_scope = matched`，并按 R01 的抽取与任务 3 规则版本限定。
2. 对每个商品读 `task3.page_field`（`parser_version = t3p1`）：
   - 导演：署名行 `field_key = director` 每行一人；没有署名行导演时，才读详情要点 Director 并按逗号拆分。
   - 演员：署名行 `field_key = actors` 每行一人；详情要点 Actors 按逗号拆分，**有无署名行都读**（R04，这是和任务 4 的区别）。
3. 逗号串的边界：沿用任务 4 的 `looks_last_first`。偶数个单词项（“Arlt, Lewis, Brown, Blair”）整串的提及标 `ambiguous`，记一条质量问题，不建人物（R05）。不尝试用署名行配对恢复：规划时这类串只有几百个，配对规则一旦错就会造出不存在的人。
4. 占位署名用任务 4 的 `PLACEHOLDER_NAMES` 判定，提及标 `placeholder`。
5. 与任务 4 断言对应：署名行的提及按（ASIN、区域、序号）找到 `task4.field_assertion` 中 `director`/`actor` 断言，记 `assertion_id`；详情要点的提及在任务 4 只有“没有署名行”时才有断言，对不上的留空。
6. 合集商品同样抽取；它们的作品归属在外部 ID 之后才决定（见“MoviePerson”）。

规模估计：导演约 7.6 千条、署名行演员约 1.5 万条、详情要点演员约 3.4 万条，合计约 5.7 万条提及。

## 姓名规范化（`NORM_VERSION = t5n1`，R06–R09）

依次执行，每一步拆出的部分都存进提及的对应列，不丢弃：

1. **倒序**：恰好一个逗号、两侧非空、右侧不是后缀（Jr、Sr、II、III、IV）时，“姓, 名” → “名 姓”，记 `reversed`；“Robert Downey, Jr.” 不倒序，`Jr.` 作为后缀。
2. **昵称**：成对的引号段（`'Gabby'`、`"Gabby"`、`“Gabby”`）拆出为 `nickname`。
3. **括号消歧段**：末尾的 `(I)`、`(II)`、`(III)`、`(IV)` 等拆出为 `disambiguator`；其他括号段也拆出，存入 `disambiguator` 并加 `flags.paren_other`。
4. **称谓与后缀**：开头的 Dr、Sir、Dame、Mr、Mrs、Ms、Prof 拆为 `honorific`；末尾的 Jr、Sr、II、III、IV 拆为 `suffix`。
5. **显示名**：剩下的部分加回后缀，作为 `display_name`。
6. **键**（都在显示名上计算）：
   - 严格键 = `etl_task4.normalize.strict_name(显示名)`：去重音、大小写折叠、删撇号与缩写点、其他标点换空格；后缀保留在严格键里（Lon Chaney 与 Lon Chaney Jr. 不同键）。
   - 宽松键：严格键去掉中间的单字母词（“alan j pakula” → “alan pakula”）。
   - 词集键：严格键的词排序后连接（“kim yu mi” 与 “yu mi kim” 相同），用于姓名顺序不同的东亚姓名。
   - 姓氏键：严格键去掉后缀后的最后一个词。
   - 首字母键：名的首字母 + 姓氏键（“j smith”）。
7. **编码损坏**：原值含 U+FFFD 时标 `encoding_damaged`；X4 比较时把它当作通配一个字符。

IMDb 一侧对 `primary_name` 走同一流程（R09）。

**兼容等级**（R11；两个姓名之间，用于外部 ID 与跨作品候选）：

| 等级 | 条件 | 例子 |
|---|---|---|
| X1 / N1 | 严格键相同 | “Bogart, Humphrey” 与 “Humphrey Bogart” |
| X2 / N2 | 词集键相同（严格键不同） | “Kim Yu-mi” 与 “Yu-mi Kim” |
| X3 / N2 | 首词与末词相同，只差中间的缩写、中间名、昵称或后缀 | “Alan Pakula” 与 “Alan J. Pakula”；“Tommy Jones” 与 “Tommy Lee Jones”；“George Hayes” 与 “George 'Gabby' Hayes” |
| — / N3 | 首字母键相同，至少一边名只有首字母 | “J. Smith” 与 “John Smith” |
| X4 / N4 | 替换字符通配后严格键相同；或编辑距离 ≤2、首字母相同、姓氏键编辑距离 ≤1 | “Fran�ois Girard” 与 “François Girard”；“Billy Bob Thorton” 与 “Billy Bob Thornton” |

外部 ID 途径一用 X 等级（同作品内比较），跨作品用 N 等级；X3 在同作品内可以自动关联，N2 在跨作品时最多复核，见下文。

## 外部 ID 途径一：作品演职员（R10–R13）

**作品与演职员**：

- 电影类商品：取 `map0002` 中它的 `edition` 关系作品；作品状态为 `imdb_linked` 时取 `tconst`。
- 合集商品：取它的全部 `contains` 成员作品中 `imdb_linked` 的那些，逐个比较。
- 导演提及对 `imdb.title_director`；演员提及对 `imdb.title_principal` 中 category 为 actor、actress 的行；另一类别只用于判断 `cross_role`。
- 只取这些作品的演职员，不查全量人物（R10）。规模：关联作品 4,473 部，导演 4,754 行、主要演员 42,523 行。

**判定**（规划默认值）：

1. 对提及计算与每位演职员的兼容等级，只保存达到 X1–X4 的候选。
2. 同一作品、同一角色类别里：
   - 恰好一位达到 X1–X3，且没有其他人达到 X1–X4：`auto`，原因 `unique_in_credits`。置信度 X1 0.95、X2 0.90、X3 0.85。
   - 两位及以上达到 X1–X4：都为 `review`，原因 `multiple_in_credits`（例如同一作品有两位 Robert Young）。
   - 只达到 X4：`review`，原因 `near_name`。
3. 只在另一角色类别兼容（页面写演员，IMDb 只把他列为导演）：`review`，原因 `cross_role`。
4. 合集：只有一个成员作品给出 `auto`、其他成员没有兼容者时才 `auto`，同时决定该提及归属的成员作品；多个成员都兼容为 `review`。
5. 没有兼容者：不写候选，提及记为“关联作品中未找到”，报告计数。

**为什么同作品内 X3 可以自动关联，而 R08 说去中间名只能生成候选**：X3 只是让这位演职员成为候选；合并依据是“已核实的同一作品 + 同一角色 + 该作品里只有这一位兼容者”。同一部电影的演职员表里，同时出现 “Alan Pakula” 与另一位 “Alan X. Pakula” 的情况，会被“没有其他兼容者”这一条拦下进复核。跨作品没有这种封闭的名单，所以 N2 不能自动合并。

## 外部 ID 途径二：作品年表反查（R14–R16）

**适用的提及**：途径一之后仍没有 `auto` 外部 ID、`resolvable` 的提及，且满足以下之一：

- 电影类商品，所在作品为 `page_only`（没有 IMDb 关联）；
- 合集商品，提及没有经途径一归属到成员作品。

已关联作品里“演职员表中没找到”的提及不走途径二：那部作品的 IMDb 演职员已经看过，再用别的作品年表挂靠，等于把另一部片的人当成这部片的人。

**已核实人物集合**：途径一 `auto` 判定得到的（角色类别, 严格键）→ `nconst` 集合。只用途径一的结果（R15），不用途径二或合作者佐证新得到的外部 ID，避免“用挂靠出来的人再去挂靠”的链式放大；这也让途径二只需算一次，不参与迭代。

**判定**（规划默认值）：

1. 对提及取同角色类别、同严格键的已核实 `nconst` 集合 {N}；为空则不走途径二。
2. 对每个 N，取其同角色的 IMDb 作品（导演取 `title_director`，演员取 `title_principal` 中 actor/actress），再取这些作品在 `task4.imdb_title_key`（同一快照、`t4n1`、`title_class = movie`）里的全部标题键（主标题、原名、别名）。
3. 与提及所在商品的标题键（任务 4 的 `title` 断言）比较；合集提及与每个成员的标题键（任务 4 的 `collection_member` 标题断言）比较。
4. 命中的（N, 作品）对：
   - 恰好一对：`auto`，原因 `filmography_title_match`，置信度 0.85（标题键只命中别名时 0.80）。
   - 多对（同一 N 年表里有多部同名作品，或多个同名的已核实 N 都命中）：都为 `review`，原因 `filmography_ambiguous`。
   - 没有：不写候选。IMDb 只有主要演职员（约 10 位），没命中不算反证（R15）。
5. **年份与硬冲突**：命中作品的 `startYear` 与商品的标题年份（如有）相差超过 1 年时改为 `review`，原因 `filmography_year_conflict`；N 的出生、去世年份与命中作品年份不相容（同下文 Y 特征）时 `reject`。括号消歧段不同（页面写 “(II)”、而 N 的规范化 IMDb 姓名带不同消歧段）时 `reject`。
6. **合集归属**：命中某个成员的标题键时，该提及归属到该成员对应的作品（任务 4 `contains` 关系里的成员作品）；该成员在任务 4 没有关联作品时，提及仍为 `unattributed`，只记外部 ID。
7. **任务 4 升级线索**（R16）：同一商品（或同一合集成员）有至少两条提及经 `auto` 指向同一 `tconst` 时，写一条 `t4_upgrade_hint`。它只进报告与交接文档，不改变任何作品归属；人物的 `movie_id` 仍是 `map0002` 里的 `page_only` 作品。

**为什么不放宽到 X3、不用别名之外的标题相似**：途径二的证据链比途径一长（姓名 → 别处核实 → 年表 → 标题），每一环都取最严的条件：严格键相同、标题键完全相同、恰好一对。规划估算里多部或多人满足的只有 6 条，说明这些条件下歧义很少。

## 作品内人物组（R18）

- **作品单位**：电影类商品的提及取其 `edition` 作品的 `movie:<movie_id>`；合集提及在外部 ID 决定了成员作品时取该成员作品，否则取 `collection:<ASIN>`。
- **分组**：同一作品单位内，严格键相同的提及（不分角色）合成一组；同一角色类别内词集键相同的也合成一组。组内有提及已 `auto` 关联 `nconst`（任一途径）时，组的 `nconst` 即为它。
- **组内两个不同 `nconst`**（例如两位同名演员都在关联作品里）：按 `nconst` 拆成两组，没有关联的提及单独成组，并记质量问题；不让一组带两个外部 ID。
- **作品年份**：作品为 `imdb_linked` 时取 IMDb `startYear`；否则取作品内商品的任务 4 标题年份（`title_year` 断言）的最小值；都没有为 NULL。经途径二命中的作品年份不回填到 `page_only` 作品（那是任务 4 与任务 6 的事），只在该提及的决策证据里使用。
- **发行商**：作品内商品的 `edition.publisher` 与 `edition.publisher_series` 断言，严格键规范化后去重，存入 `publishers`（R22 的发行商特征用）。
- **稀有度**：`rarity.page_works` = 该严格键在全部 `resolvable` 提及中出现的作品单位数；`rarity.imdb_people` = IMDb 全部人名中同严格键的人数（一次性对 `imdb.name` 计算，只读）。

同一作品的多个商品版本、署名行与详情要点重复出现的同一人，在这一步合一。同一作品内同名的导演与演员（导演兼演员）也合一，在 MoviePerson 中有两种角色（AC05、AC08）。

## 跨作品候选与判定（R17、R19–R25）

**直接合并**：`nconst` 相同的组属于同一人物，不需要候选对；依据是各组的外部 ID 判定，写在 `link_decision` 里。

**候选生成**（只在不同作品单位的组之间，且不是同一 `nconst`）：

| 生成器 | 分块键 | 主要检验 |
|---|---|---|
| C1 | 严格键相同 | 同名同人、同名不同人 |
| C2 | 词集键或宽松键相同（严格键不同） | 姓名顺序、中间缩写 |
| C3 | 首字母键相同，且至少一边名只有首字母 | 名字缩写 |
| C4 | 姓氏键相同的块内，N4 兼容 | 拼写、编码损坏 |

块上限：一个分块键下超过 50 个组时，按作品年份、`group_key` 排序后仍枚举，但只能判 `review`，记 `block_overflow`；C4 的姓氏块超过 200 个组时不枚举 C4，记溢出（“smith”“johnson” 这样的大块）。

**特征**：

| 特征 | 取值与默认分值 |
|---|---|
| N 姓名 | N1 +3；N2 +2；N3 +1；N4 +1 |
| R 角色 | 角色类别有交集 +1，否则 0 |
| E 外部 ID | 两边都有且不同：硬冲突 `different_external_id`；一边有：记录，不计分 |
| W 合作者 | 两部作品里除这两组以外的其他组：属于**当前同一人物**的合作者（同一 `nconst`，或在之前的轮次已合成同一人物）每位 +2；同严格键（至少两个词）但尚未合成同一人物的合作者每位 +1；上限 +4 |
| Y 年代 | 一边有 IMDb 出生年份 b、去世年份 d，另一边作品年份 y：y < b + 5 或 y > d + 5 为硬冲突 `era_conflict`；否则记录，不计分 |
| P 消歧段 | 两边都有括号消歧段且不同：硬冲突 `different_disambiguator` |
| F 稀有度（R22，只记录） | `rare` = `page_works ≤ 3` 且 `imdb_people ≤ 1`；记录两项原值与 `rare` |
| B 发行商（R22，只记录） | 两边作品的 `publishers` 有交集；记录交集 |

满分 3 + 1 + 4 = 8；置信度 = 得分 / 8，两位小数；有硬冲突时为 0。年代容差取 5 年，是为了容纳童星和身后上映的作品；它只拦下明显不可能的组合，不作为合并证据。

**判定**（`t5r1`，F、B 开关关闭）：

1. 有硬冲突：`reject`。
2. N1、角色类别有交集、W ≥ 2：`auto`，原因 `co_credit`（R19）。即至少一位已核实（或已合成）的共同合作者，或两位同名合作者。
3. N1 但 W < 2：`review`，原因 `same_name_no_corroboration`（R17、R24）。
4. N2：`review`（W 再高也不自动合并）。
5. N3、N4：W ≥ 1 时 `review`，否则 `reject`，原因 `weak_name_only`（名字缩写或近似名不能只凭姓名进复核队列，否则队列会被大块淹没）。
6. 块溢出：最高 `review`。

**F、B 开关**（R22，H15）：

- 两个开关是 `RuleSet` 的参数 `use_rarity`、`use_publisher`，`t5r1` 都为 `False`。关闭时特征照算、照存，并在 `features.feature_switches.tier_if_enabled` 里记“打开时会得到的判定档”，报告统计会改变判定的候选数，供开发池观察（R40）。
- `use_publisher = True`：B 有交集时 W 加 1（仍受上限 4 约束）。单独的发行商相同不能让 W 从 0 到 2，所以不能单独促成自动合并。
- `use_rarity = True`：只在 N1、角色有交集、W = 1 且其中那位合作者是同名合作者、两边都 `rare` 时，把第 3 条的 `review` 升为 `auto`，原因 `rare_name_co_credit`。没有合作者时稀有度不起作用（用户未选“IMDb 唯一名直接挂靠”）。
- 打开任一开关就是新规则版本（如 `t5r2`），在开发池上观察后决定，记入“规则调整记录”。

**样例推演**（开关关闭）：

| 情况 | 得分 | 判定 |
|---|---|---|
| 同名导演、两部作品有一位共同的已核实演员 | 3 + 1 + 2 = 6 | `auto` |
| 同名演员、两部作品各有一位同名合作者（没有外部 ID） | 3 + 1 + 1 = 5 | `review`（W = 1；打开稀有度且两边稀有时为 `auto`） |
| 同名演员、两部作品有两位同名合作者 | 3 + 1 + 2 = 6 | `auto` |
| 同名、一边 `nm0001870`、另一边 `nm0950000` | — | `reject`（硬冲突） |
| “Alan Pakula” 与 “Alan J. Pakula”，没有外部 ID，有两位共同合作者 | 2 + 1 + 2 = 5 | `review`（N2） |
| “J. Smith” 与 “John Smith”，没有合作者 | 1 + 1 = 2 | `reject` |
| 同名演员、只有发行商相同 | 3 + 1 = 4 | `review`（打开发行商开关后 W = 1，仍为 `review`） |

## 合作者佐证迭代、聚类与一致性（R21、R23）

**迭代**：

1. **第 0 步**：外部 ID 簇。同一 `nconst` 的组（任一途径）合成一簇。
2. **第 1 轮**：跨作品候选的分批判定（W 按第 0 步的簇计算）。取本轮 `auto` 边按（得分降序、`person_candidate_id` 升序）依次尝试合并两簇；合并前检查两簇所有组对之间的硬冲突（两个不同 `nconst`、年代冲突、不同消歧段），有冲突时不执行，写 `cluster_check`（`edge_blocked`）。
3. **第 k 轮（k ≥ 2）**：只重算“两边作品里有组在上一轮换了簇”的候选的 W 与判定；判定档变化时新增一行 `round = k` 的决策。新的 `auto` 边按同样顺序尝试合并。
4. **停止**：某一轮没有新执行的合并即停止；轮数上限 10，超过时停止并记质量问题（规划时预计 2–4 轮）。
5. 每轮写 `iteration_round`：新执行的自动边、新合成的人物数、被拦下的边数（R21、R40）。

**确定性与 `person_id` 稳定**：每轮内的顺序只由得分与 `person_candidate_id` 决定，而候选 ID 按（阶段键, `group_a`, `group_b`）的字典序分配，与输入行顺序无关；合并只改变簇，不改变锚点规则（见下文），所以同一输入重跑得到同一组簇与 `person_id`（AC06）。W 只会随合并增大，判定档只会从 `review` 升为 `auto`，不会把已执行的合并撤回，迭代单调、必然终止。

**复核与拒绝边**：永远不进入并查集（R23）。

**人工决策**（迭代结束后执行）：

- `must_link(a, b)`：合并 a、b 所在人物；`b` 为 `imdb:<nconst>` 时把 a 的组关联到该 `nconst`。两边是不同 `nconst` 时导入即拒绝（IMDb 的两个编号就是两个人，本任务不处理 IMDb 自身的重复）；与年代冲突或消歧段矛盾时仍执行，写质量问题 `human_vs_veto`、`cluster_check.resolved_by = human`。
- `cannot_link(a, b)`：a、b 在同一人物时把 b 所在的组移出成为新人物（b 的组若是因外部 ID 合入，改为把 b 的组的外部 ID 判定视为不采用）。
- `detach(a)`：把 a 所在组移出。
- 人工决策之后不再迭代：人工改变的簇不再自动牵动其他候选，避免一条人工决策连锁改动大量人物；需要时再导入更多人工决策。

## `person_id`、规范姓名、别名与版本（R24–R29）

1. **状态**：有外部 ID 的为 `external_verified`；没有外部 ID、有待复核候选的为 `review_pending`；其余为 `page_only`。有外部 ID 而仍有待复核候选的，状态仍为 `external_verified`，`pending_candidates` 记数量。经合作者佐证并入外部 ID 人物的页面组，人物仍是 `external_verified`，提及的 `basis` 为 `co_credit`。
2. **锚点与 `person_id`**：有外部 ID 的锚点为 `imdb:<nconst>`；否则为 `mention:<簇内最小 mention_key>`。`person_identity` 按锚点登记，已登记的直接复用，所以重跑得到同一组 `person_id`。页面人物簇拆开时，最小提及所在的部分保持原 `person_id`；整个页面簇并入外部 ID 人物时，旧 `person_id` 在新版本中不再出现，记 `retire`，`to_person_id` 指向新人物。
3. **规范姓名**（R27）：有外部 ID 取 IMDb `primary_name`；否则取簇内 `display_name` 出现次数最多者，并列取字典序最小；`canonical_source` 记来源。
4. **别名**（R28）：簇内每种不同的 `raw_value` 一行，加上 IMDb `primary_name`（来源 `imdb`）。`alias_types` 由别名与规范姓名的比较得出，可多选：`identical`、`reversed_order`、`case_variant`、`accent_variant`、`punctuation`、`middle_initial`、`middle_name`、`nickname`、`suffix`、`token_order`、`encoding_damaged`、`imdb_primary`、`other`。
5. **人物映射版本与事件**：每次非 `reused` 的运行或人工决策导入后的重算生成新版本（`pmap0001`、`pmap0002`…），记录父版本；事件由新旧版本逐提及对比生成，语义与任务 4 的 `mapping_event` 相同。差异命令列出 `person_id` 变化的提及与新旧决策（R44）。

## 待复核队列（R30）

- `python -m etl_task5.review queue [--pmap <版本>]` 导出 `outputs/task5/<抽取>/<人物映射版本>/review/review_queue.csv`。
- 每行是一个当前为 `review` 的候选：跨作品候选（`person_decision` 的最终判定），以及外部 ID 的复核候选（`link_decision`）。
- **影响** = 采纳这一条后，结果人物跨越的作品单位数减去两边人物中较大一方原有的作品单位数，即“合并后能新连起的作品数”；外部 ID 候选的影响 = 该 `nconst` 现有人物的作品数 + 1 减去提及所在人物的作品数。按影响降序，并列按候选 ID 升序。
- 列：`queue_rank`、`impact`、候选类型与 ID、双方提及的原始署名、角色、作品显示标题与年份、商品 ASIN 与页面地址、`nconst` 与 IMDb 人物页地址、共同合作者、系统原因；空列 `decision`（`must_link`/`cannot_link`/留空）、`note`。
- 导入时只取 `decision` 非空的行，转换成 R29 的人工决策（外部 ID 候选的 `must_link` 右侧为 `imdb:<nconst>`），走同一个 `review import` 校验。队列文件已存在时不覆盖，除非加 `--force`（保护已填写的决定）。

## MoviePerson（R31–R34）

对每条 `resolvable` 且归属到作品（`attribution = movie`）的提及：

| 提及 | 生成的角色 |
|---|---|
| 导演（署名行或详情要点） | `director` |
| 署名行演员 | `starring` 与 `cast` |
| 详情要点演员 | `cast` |

- 同一（作品、人物、角色）合成一行，来源提及全部写进 `movie_person_source`（同一作品的多个商品版本、署名行与详情要点）。
- **署名顺序**（R33）：`director` 与 `starring` 取来源提及的 `byline_rank`（署名行内同标签的第几位）；`cast` 有署名行来源时取其 `byline_rank`，只有详情要点来源时为 NULL（字母顺序不是署名顺序，`list_position` 只作定位）。多个来源不一致时取最小值并设 `order_conflict`，记质量问题。
- **合集**：外部 ID（途径一，或途径二命中成员标题）把提及归到某个成员作品时，按该成员作品建关系，`source_labels` 带 `collection:<ASIN>`；其余合集提及为 `unattributed`，人物照常建立，但没有 MoviePerson（R34）。
- **IMDb 的 `ordering`**：只写进外部 ID 决策的证据，不参与 `starring` 判定（H02），也不为页面没有的人建行（H04）。
- 只读 `map0002` 的作品；187 部“只有合集成员”的作品可能经合集得到关系。途径二的任务 4 升级线索不改变作品，所以经途径二核实的人物，其 MoviePerson 仍挂在 `page_only` 作品上。

## 查询（R41）

- `python -m etl_task5.query person <person_id>`：规范姓名、状态、外部 ID（含 IMDb 源记录与获得途径）、全部别名与次数、每条提及的原始署名与页面定位、归并依据（外部 ID 判定及途径、作品年表命中的作品与标题键、作品内分组原因、跨作品决策与轮次、人工决策）、参与作品与角色。
- `python -m etl_task5.query director "<姓名>"`：按严格键找到所有导演提及所在的人物，同名的不同人物分别列出；每个人物给出作品（`movie_id`、显示标题、`tconst`）、商品 ASIN 与来源提及。

两者只读，参数为人物映射版本（默认最新）。作业第九部分的“输入 `person_id` 查询原始署名、别名归并过程和参与作品”由第一个查询满足；完整的字段级血缘仍在任务 9。

## 评价设计（R35–R39）

**开发池与评价池**：

- 首次全量（`t5r1`）后，对全部 `resolvable` 提及的姓氏键按 `md5(姓氏键)` 排序，前 20% 冻结为开发池，记入 `dev_pool`。按姓氏键划分，使 “J. Smith” 与 “John Smith”、同名的多个人落在同一侧。
- 调整默认值、决定 F、B 开关时只看开发池里的判定（AI 观察，不产生标注、不算评价）；调整记在本文新增的“规则调整记录”。
- 评价表导出前冻结规则版本；之后再改规则，报告必须注明“评价集已被看过，不再独立”。
- 姓名顺序不同（C2 词集键）导致两边姓氏键不同的对：任一边在开发池即不进评价池。

**120 对评价样本的分层**（每对是两个不同作品单位的组，各取组内最小提及作代表；每层按 `md5(pair_id)` 取前 N，不足时按 S1、S4 的顺序补足，实际层数写入评价集）：

| 层 | 内容 | 对数 | 主要检验 |
|---|---|---|---|
| S1 | 系统判为同一人物、原始写法不同（别名合并，经外部 ID） | 25 | 别名合并的误合并 |
| S2 | 系统判为同一人物、经合作者佐证（`co_credit`，含迭代各轮） | 10 | 页面途径的误合并 |
| S3 | 严格键相同、分属不同 `nconst`（同名不同人） | 15 | 同名的正确拒绝 |
| S4 | 严格键相同、系统未合并且没有硬冲突（多为复核） | 20 | 漏合并与复核档的真实比例 |
| S5 | N2、N3 兼容（中间缩写、名字缩写、姓名顺序）的跨作品对 | 15 | 缩写与顺序 |
| S6 | N4 近似名的跨作品对 | 10 | 拼写、编码损坏 |
| S7 | 系统判为同一人物、严格键相同（经途径一的同一 `nconst`） | 15 | 容易的正例，稳定 Recall 的分母 |
| S8 | 系统判为同一人物、其中一边经作品年表反查得到 `nconst` | 10 | 途径二的误合并（避免像任务 4 的 S2 那样为 0） |

- S8 的一对 = 经途径二关联的提及所在组，与同一 `nconst` 在途径一里核实的组。
- **导出**：`outputs/task5/<抽取>/<人物映射版本>/eval/eval_pairs.csv`。每行含 `pair_id`、层，以及双方的原始署名、角色、商品 ASIN 与标题、作品显示标题与年份、该作品里的前 5 位合作者、关联的 `nconst` 与 IMDb 人物页地址、`https://www.amazon.com/dp/<ASIN>`；S8 另给出年表命中的 IMDb 作品与地址；空列 `label`（`same`/`different`/`unsure`）与 `note`。不导出系统判定与途径。
- **标注口径**：是否同一个真实的人；证据以页面、IMDb 人物页（作品年表、出生年份）为准；无法判断标 `unsure` 并写 `note`。
- **AI 预标注**（H06）：另存 `eval_pairs_draft.csv`，带 `ai_label`、`ai_doubt`、`ai_reason`，依据只用表中字段与公开的 IMDb 人物页；工具、用途与人工验证方式记入报告。用户逐条审核后导入，标注人为用户。
- **分歧处理**：只有用户一人标注；导入时列出明显矛盾（两边同一 `nconst` 却标 `different`，两边不同 `nconst` 却标 `same`），供用户复看，不改动标注。

**指标**：预测同一 = 导出时的人物映射版本中双方属于同一 `person_id`，且合并只来自系统判定。TP、FP、FN 只在 `same`/`different` 上计算，Precision、Recall、F1 与“无定义”规则同任务 4（复用 `etl_task4.evaluate.prf`），并按层与按合并途径（途径一、途径二、合作者佐证）报告（R37）。人工复核覆盖率 = 有人工决策的复核候选数 / 复核候选总数；待复核率 = `review_pending` 人物数 / 人物总数，另报有待复核候选的人物（含 `external_verified`）占比。

**簇抽查**（R38）：从评价池随机取 20 个跨至少两个作品单位的人物：作品数 ≥3 的 10 个、2 个的 10 个，不足互补；导出每个人物的全部提及与证据（含合并途径与轮次），用户标“全部同一人 / 混入不同的人（列出提及）”。不计入 Precision。迭代合并产生的人物优先保证至少 5 个进入抽查（不足时有多少取多少），因为传递错误最可能出在这里。

**实例**（R39）：别名合并优先取 S1、S8 中已标 `same` 的；同名或近似名不合并优先取 S3、S4、S6 中已标 `different` 的；不足 5 组时用确定性排序补足并注明“未标注”。每组导出原始署名、作品、外部 ID 或冲突证据、页面与 IMDb 源记录定位。

## 统计与报告（R40）

`summary.json` 与 `report.md` 包含：

- 提及：总数，`resolvable`、`placeholder`、`ambiguous` 分布；按角色与区域计数；其他角色署名按标签计数。
- 合并前后：`resolvable` 提及 → 不同原始写法 → 不同严格键 → 作品内人物组 → 人物。
- 外部 ID：按途径（作品演职员、作品年表）分别计判定档分布、原因与得到外部 ID 的提及数；有外部 ID 的人物数与提及数（第三方来源单列，不并入页面识别数）；关联作品中未找到的提及数；任务 4 升级线索数与前 20 个例子。
- 跨作品：各生成器候选数、判定档分布、硬冲突类型计数、块溢出、一致性检查拦下的边数；迭代轮数与每轮新增合并数；F、B 特征打开时会改变判定的候选数（分开计）。
- 人物状态分布、待复核率、人工复核覆盖率、待复核队列条数与影响最大的前 20 条。
- MoviePerson：按角色计数、按来源区域计数、有 `order_conflict` 的行数、多角色人物数（同一作品两种以上角色，不把 `starring` + `cast` 算作多角色）、`unattributed` 提及数。
- 别名类型分布，未识别的括号段与称谓（前 50 个），用于补词表。

导出文件：`summary.json`、`report.md`、`persons.csv`、`aliases.csv`、`movie_person.csv`、`decisions_sample.csv`（每档按 `md5` 取前 200）、`t4_upgrade_hints.csv`、`examples.md`，写入 `outputs/task5/<抽取>/<人物映射版本>/<run_key>/` 并记录哈希；不含评论正文与整页 HTML。

## 运行、恢复与编排（R42–R45）

**分批**：

- 提及与外部 ID 途径一按商品分批，默认 500 个 ASIN 一批；每批的提及、候选、判定、实体阶段标记与 `run_batch` 在同一事务提交。
- 外部 ID 途径二在途径一全部完成后执行，按商品分批，实体为同一 ASIN（阶段名 `filmography`）；已核实人物集合在阶段开始时一次算出并写入运行的 `params`（集合的哈希），保证分批重跑用同一集合。
- 跨作品候选与第 1 轮判定按分块键分批（默认 2,000 个键一批），实体为 `block:<生成器>:<分块键>`。
- 作品内分组、迭代第 2 轮起的判定、聚类、人物、别名、外部 ID 表、事件与 MoviePerson 是全局步骤，在一个事务里生成整个人物映射版本（规模约 6 万条提及，允许一次提交）。
- 中断后同一输入键重跑跳过已标记的实体；全局步骤没有提交就从头重做（AC10）。

**锁**：按抽取加 advisory lock（与任务 4 不同的键），拿不到锁抛 `RunBusy`，确定性失败。

**DAG `amazon_movies_task5_person`**：`check_inputs → mentions → link_external → resolve → movie_person → verify → finalize_report → gate`。

- 参数：抽取、任务 4 映射版本、IMDb 快照、`norm_version`、`rule_version`、`batch_size`、`limit`。人工决策集不是参数，按抽取累积，哈希进输入键（同任务 4）。
- `check_inputs`：映射版本存在且属于该抽取、IMDb 快照为 `loaded`、该快照与 `t4n1` 的 `imdb_title_key` 存在、迁移 `302`、`401` 已应用。
- `link_external` 依次做途径一与途径二；`resolve` 包含跨作品第 1 轮判定（分批）与迭代、聚类（单事务）；`movie_person` 与聚类共用同一人物映射版本，靠 `task5.run.airflow_run_id` 找回同一条运行（任务 4 的做法）。
- `verify` 只读核对 AC14 的约束；`finalize_report` 与 `gate` 的触发规则为 `all_done`，门禁只在上游全部成功时通过。
- 暂时错误（连接中断、死锁）最多重试 2 次；确定性错误（输入缺失、约束违反、锁被占用）不重试。
- `limit` 只限制提及阶段的商品数，之后的步骤因仍有未处理商品而确定性失败，不生成人物映射版本（试跑不冒充全量，同任务 3、4）。

**命令**（不进 DAG，由人触发，写库后再触发一次 DAG 重算）：`python -m etl_task5.review queue|import <csv>`；`python -m etl_task5.evaluate freeze|export|import|metrics|clusters-export|clusters-import`；`python -m etl_task5.report diff <旧版本> <新版本>`。

**故障注入**：`before_commit:<阶段>:<批号>` 与 `transient_once:<标记文件>`，仅 `ETL_ENV=test`。

## 资源预算（估计）

| 项目 | 估计 | 依据 |
|---|---|---|
| 人物提及 | 约 5.7 万条，约 50 MB | 上文规模估计 |
| 外部 ID 候选（途径一） | 约 2–3 万条 | 关联商品约 4,500 个、每条提及只存兼容者 |
| 作品年表（途径二） | 已核实人物约 8 千个、年表作品约 14 万部、其标题键数十万个，只读、内存中比较 | 2026-10-30 规划估算 |
| 跨作品候选 | 约 2–10 万对 | 3,111 个跨作品严格键；大块按上限处理 |
| 迭代 | 2–4 轮，每轮只重算受影响的候选 | 单调、上限 10 轮 |
| 全量运行 | 10 分钟以内 | IMDb 只读关联作品的约 4.7 万行演职员与已核实人物的年表；全局步骤在内存中完成；规划估算脚本读取年表与标题键约 1 分钟 |
| schema `task5` | 1 GB 以内 | 以上行数与 jsonb 证据 |

实测在 T5-07 补记，估计与实测分开写。

## 异常与恢复

| 情况 | 处理 |
|---|---|
| 映射版本不存在或不属于该抽取、IMDb 快照未装载或缺少标题键 | `check_inputs` 确定性失败，下游 `upstream_failed` |
| 逗号串边界不确定 | 提及 `ambiguous`，质量问题，不建人物 |
| 一组提及关联到两个 `nconst` | 按 `nconst` 拆组，质量问题 |
| 作品年表多部同名或多人满足 | 复核，不自动关联 |
| 自动边造成冲突 | 不执行该边，`cluster_check`，按复核计 |
| 迭代超过 10 轮 | 停止迭代、保留已执行的合并，质量问题 |
| 分块过大 | 只判复核或不枚举，记溢出 |
| 人工决策引用不存在的提及或 `nconst`，或 `must_link` 两个不同 `nconst` | 导入整体失败，不写部分行 |
| `person_id` 哈希碰撞 | 唯一约束失败，确定性失败 |
| 中断 | 已提交批次保留，重跑跳过；全局步骤整体重做 |

## 已知局限与风险

- **外部 ID 覆盖**：途径一只有 IMDb 关联作品的人能得到 `nconst`，且 IMDb principals 只有约 10 位主要演职员；途径二只从已核实的人出发，从未出现在关联作品里的人得不到外部 ID。详情要点里的配角多数只能靠作品内分组与合作者佐证。
- **作品年表反查的误挂风险**：如果页面人物与已核实人物只是同名，而这位已核实人物恰好也演过一部同名作品（例如同名翻拍里各有一位同名演员），会误挂。条件“严格键相同、标题键完全相同、恰好一对”与年份检查把风险压低，评价层 S8 与簇抽查专门检验。
- **任务 4 升级线索不回写**：途径二说明一些 `page_only` 作品其实能关联到 IMDb，但本任务不改任务 4 的映射（H07）；这些作品的 MoviePerson 挂在 `page_only` 作品上，年份等信息也不回填。
- **改名与婚后改姓**：“Andy Wachowski” 对 “Lana/Lilly Wachowski”、“Victoria Adams” 对 “Victoria Beckham”：IMDb 公开文件不提供曾用名，本任务不关联，留作漏合并；人工决策可以纠正。
- **合作者佐证偏向多产的人**：冷门人物很少有共同合作者，跨作品同名会大量留在复核里；迭代与途径二能补一部分，稀有度开关默认关闭。这是有意的取舍（R17），待复核率如实报告（H10）。
- **迭代的传递风险**：迭代让一次合并成为下一次合并的证据，误合并会放大；一致性检查只能拦下与外部 ID、年代、消歧段矛盾的边。簇抽查优先抽迭代产生的人物。
- **东亚姓名与音译**：词集键只处理词序；不同的音译拼法（如 Woo-ping / Wu Ping）只能靠 N4 近似或外部 ID，跨作品最多复核。
- **合集的人物归属**：没有关联到成员作品的合集提及不建 MoviePerson（`unattributed`）。
- **任务 4 的映射质量**：人物的作品归属沿用 `map0002`；任务 4 的误合并会让同名不同人落入同一作品而被 R18 合成一组。任务 4 评价样本的误合并为 0，风险较低，簇抽查会暴露。
- **评价样本有偏**：分层刻意多取难例，指标不代表全量准确率。

## 与后续任务的接口

- 任务 4 升级（`t4r3`，H07）：可以把导演、演员的姓名键比较换成 `person_id` 比较；`t4_upgrade_hint` 列出的商品（规划估算 90 个）可作为新增 IMDb 关联的候选；需单独规划并说明评价集已被看过。
- 任务 6：MoviePerson 不涉及上映信息；外部 ID 的出生、去世年份可作为年份合理性的旁证。
- 任务 7：页面与 IMDb 演员表的并集、主演来源冲突在 MoviePerson 与外部 ID 证据之上处理。
- 任务 9：`person_mention`（指回页面字段与任务 4 断言）、外部 ID 判定（含作品年表证据）、跨作品决策与轮次、人工决策构成人物字段的处理链；`query person` 是其人物部分的雏形。

## 实现中的修订（T5-01，2026-10-30）

以下是 T5-01 至 T5-03 实现时与上文不一致或上文没写清的地方，以本节为准；对应测试见 checklist。

**姓名与提及（T5-01）**

- **括号段**：上文“其他括号段也存入 `disambiguator` 并加 `flags.paren_other`”改为：`disambiguator` 只收罗马数字括号段（`(I)`–`(X)`），其他括号段存入单独的列 `paren_text`（多个以 `; ` 连接），不进严格键。`flags` 只有 `reversed`、`encoding_damaged`。表 `person_mention` 因此多一列 `paren_text`。
- **主键**：`person_mention` 的主键直接用唯一键（抽取、解析版本、规范化版本、`mention_key`），不另设 `mention_id`；后续表用 `mention_key` 加这三项引用。
- **兼容等级**：编辑距离的 L4 分支增加一条保护——词数相同且对应位置上都是不同的单字母（`John A. Smith` 与 `John B. Smith`）时判为不兼容，否则它们会被当成拼写错误。`J. Smith` 对 `John Smith` 仍是 LI（X4/N3）。
- **署名行排名**：`byline_rank` 按署名行内相同 `label_raw` 数第几行（夹具与正式数据里同角色的标签一致，两种口径结果相同）。
- **详情要点与任务 4 断言**：署名行提及按（ASIN、角色、区域、页面字段序号）对应任务 4 断言；详情要点提及在任务 4 只有“没有署名行”时才有断言，按原值依次对应，对不上留 NULL（逗号串里并回后缀的名字如 `Robert Downey, Jr.` 因任务 4 把它拆成两项而对不上）。
- **阶段名**：`entity_stage.stage` 取 `mentions`、`credits`、`filmography`、`decide_pair`；提及阶段键只含解析与规范化版本，其余阶段含映射版本、解析版本、规范化版本、规则版本与 IMDb 快照。
- **`check_inputs`**：额外返回参与匹配的商品数与标题键行数，用于测试观察；映射版本属于别的抽取时报“属于抽取”。
- **试跑旧映射版本**：测试用手工登记的映射版本（任务 3 规则 `t3r1`）验证“读指定映射版本”，因为任务 4 同一抽取在两个任务 3 规则版本下重跑判定会撞上判定的唯一键（任务 4 的既有行为，不改）。

**外部 ID（T5-02）**

- `t4_upgrade_hint` 的主键多一个 `member_ordinal`（电影类为 0），因为合集的不同成员可以各自指向同一个 `tconst`。
- 途径二是否适用：商品是电影类且作品为 `page_only`，或商品是合集（有成员标题断言）且该提及途径一没有 `auto`；已关联作品里“演职员表没找到”的提及不进入。合集是否为合集按任务 4 的成员标题断言（实体 `ASIN#序号`）判断，因为未能关联任何成员的合集在 `product_movie` 里没有行。
- 合集在途径一里：多个成员作品都有候选时，即使其中一个本是 `unique_in_credits`，也一律改为 `review`，原因 `multiple_members`。
- 途径二的已核实集合只来自途径一的 `auto`，其哈希（`verified_hash`）写入运行的 `params`；`link_candidate.anchor_link_decision_id` 指向该 `nconst` 的一条途径一 `auto` 判定（取最小 ID）。IMDb 一侧的出生、去世年份来自 `imdb.name`，作品年份来自 `imdb.title.start_year`。

**归并与人物（T5-03）**

- 新增阶段键：作品内组与候选、判定都用 `stage_key('decide_pair')` 同形的串（映射 | 解析 | 规范化 | 规则 | 快照）；`person_group` 等表的 `stage_key` 列用外部 ID 阶段的阶段键，`person_candidate` 用同一个，所以换规则版本得到新的组与候选。
- 全局步骤的幂等：`build_groups` 在同一阶段键下已有组时不重算；`build_people` 每次成功运行生成一个新的 `pmap`（编号按全库已有版本数 + 1，`pmap0001` 起，全局唯一）。
- 候选只在第一个产生它的块里：块的顺序是 C1、C2（词集键，再宽松键）、C3、C4；块 ID 形如 `block:C1:<严格键>`；C4 的姓氏块超过 200 个组时不枚举，只记 `block_overflow`（`skipped: true`）。
- **第 1 轮被一致性检查拦下的边**：直接把该候选的第 1 轮决策行改为 `review`、原因 `edge_blocked`（同一事务内），第 2 轮起则新增一行；`cluster_check` 同时记录。
- **迭代统计**：`new_auto_edges` 只数真正连接了两个不同簇的 `auto` 边（两组已在同一簇的候选不重新判定也不计数）；`new_merged_persons` 为本轮发生合并的簇数。所以 AC06 夹具的轮数是 3，每轮为 (2, 1)、(1, 1)、(0, 0)。最后一轮没有新合并时停止并也写一行。
- **角色不相交的同名**：规则没有覆盖“严格键相同但角色类别没有交集”的情形，记为 `review`、原因 `role_differs`（不自动合并）。
- **发行商开关**：按上文“W 加 1”实现，所以“发行商相同 + 一位同名合作者”在打开开关后 W = 2、会升为 `auto`；“只有发行商相同、没有合作者”仍是 `review`（得分 +1）。spec 的 AC06 对前一种情形措辞含糊，这里按 design 的公式执行并记录。
- **人工决策**：`must_link` 右侧是 `imdb:<nconst>` 时把左边的组的外部 ID 设为该 `nconst`（途径记为 `human`）并并入同一 `nconst` 的簇；`cannot_link`、`detach` 移出组时同时清除该组的外部 ID（否则同一锚点会出现在两个人物上）；与否决条件矛盾的 `must_link` 仍执行，写 `cluster_check`（`kept_by_human`）和质量问题 `human_vs_veto`。导入时的整体校验放在 `review.import_decisions`，错误一律整体拒绝。
- **`person_alias.alias_types`**：同时是 IMDb 主名与页面写法时，页面别名的类型追加 `imdb_primary`，不另设一行；无法归类的记 `other`。
- **事件与差异**：第一个人物映射版本没有父版本，不生成逐提及事件；之后按提及对比生成 `create`、`merge`、`split`、`move`、`retire`。
- **稀有度的 IMDb 人数**：对 `imdb.name` 做一次服务端游标扫描，只统计页面出现过的严格键。
- **串联函数**：`runner.execute_resolve` 把提及、途径一、途径二、分组、分批判定、聚类串成一次运行（带锁、复用判断和运行审计），是 T5-05 之前测试的端到端入口；分步骤的 `run_stage`、DAG、报告与查询见 T5-05，MoviePerson 见 T5-04。

## 实现中的修订（T5-04 至 T5-06，2026-10-30）

**MoviePerson（T5-04）**

- 是否已生成 MoviePerson 记在 `person_mapping_version.counts` 的 `movie_person` 键里（含各项计数）；`build_movie_person` 看到该键就直接返回 `reused`，所以重复调用与 `reused` 运行都不重写。生成在一个事务里，`before_commit("movie_person", 1)` 是测试的注入点。
- 聚类（`build_people`）新增 `before_commit("cluster", 1)` 注入点；同一输入键已有人物映射版本时（上一次运行在生成版本之后、MoviePerson 之前失败）直接沿用，不再重算，否则会撞上 `(extraction_id, input_key)` 的唯一约束。
- `source_labels` 为 `<区域>:<原始标签>`（如 `byline:Actor`、`detail_bullets:Actors`），合集来源另加 `collection:<ASIN>`；多角色人物数不把 `starring` 与 `cast` 算作两种角色。

**运行、DAG、报告与核对（T5-05）**

- **默认的任务 4 映射版本**：`map0002` 属于该抽取时取它，否则取该抽取最新的任务 4 映射版本（测试库里只有 `map0001`）；参数显式给出不存在的版本时 `check_inputs` 确定性失败。IMDb 快照默认取映射版本所用的快照。
- **run_stage**：步骤为 `mentions`、`link_external`（途径一再途径二）、`resolve`（作品内组、分批判定、聚类与人物）、`movie_person`（收尾，运行记 `succeeded`）。`resolve` 成功后运行记下 `person_mapping_version_id` 但仍是 `running`。外部 ID 未做完时 `resolve` 确定性失败。`limit` 使后续步骤在 `link_external` 处失败。
- **故障注入名**：`before_commit:<阶段>:<批号>`，阶段为 `mentions`、`credits`、`filmography`、`decide_pair`、`cluster`、`movie_person`（后两者批号固定为 1）。
- **核对的口径**：AC14 的“任务 4 表未被写入”在本任务代码内无法在运行前后对比，改为核对任务 4 映射版本记录的数量与重数一致，并输出各 task4 表的行数（`t4_table_rows`），运行手册 3.1 在运行前后各取一次对比；R19 核对按判定特征里的合作者数重算 W（含发行商开关），不依赖判定行里的 `w` 字段（后续轮次的行没有它）。
- **报告**：`persons.csv` 不含锚点（锚点里有 `nconst`）；`decisions_sample.csv` 把外部 ID 判定与跨作品判定放在一起（列 `kind`），各按档取 md5 前 200；人工复核覆盖率 = 有人工决策的复核候选数 / 复核候选总数（跨作品候选按双方代表提及匹配，外部 ID 候选按 `imdb:<nconst>` 匹配）；块溢出计数取本运行的质量问题。
- **命令行**：`review` 增加 `queue`、`import`；导入只写人工决策，重算靠再触发一次 DAG。
