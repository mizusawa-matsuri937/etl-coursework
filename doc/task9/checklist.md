# 任务 9 AC 完成情况（字段级数据血缘）

状态：2026-11-03 编写。AC00 已由 AI 运行文档检查并勾选；T9-01 完成后 AC02、AC05 通过并勾选；T9-02 完成后 AC06–AC09 通过并勾选；T9-03 完成后 AC01、AC03、AC04、AC10 通过并勾选；T9-04 完成后 AC11–AC14 通过并勾选，AC15 部分通过（按实体增量重建待定）未勾选；其余未执行。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询（带 `SET default_transaction_read_only = on;`，`psql -Atc` 输出首行会多一个 `SET`）。标注“（计划）”的命令在对应任务实现前不可运行，实现后实际核对再改写期望。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：2026-11-03 任务 8 完成时为 `1186 passed`；T9-01 完成后为 `1218 passed`（新增 32 项，原有测试不减少）；T9-04 完成后为 `1262 passed`。

## AC00 计划文件规范

- [x] 2026-11-03 AI 文档检查通过：五个二级标题顺序正确，R01–R49 均被 AC 与 T9-01 至 T9-07 覆盖，AC00–AC22 与 spec 一一对应，未执行条目保持未勾选；用户同日确认 H01–H04。

```bash
grep -n "^## " doc/task9/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task9/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task9/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task9/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条为 `49`。规则覆盖的检查命令见 [tasks](tasks.md) T9-00，期望 `49 [] []`。

## AC01 输入版本、分层与上游只读

- [x] 2026-11-03 通过（T9-01 至 T9-03）：只读指定抽取与版本，上游各表行数与摘要不变，新对象只在 `task9`；版本不一致或 cv 未核实时确定性失败。T9-03 补上：IMDb 行、提及、评论、合集成员等范围外实体有断言但没有选择，选择只来自 cv、`rv0001`、`map0002`、`pmap0001`、`t3r2`；评分统计没有选择、断言或逐条评论边（R05）。副本上不在 cv 的商品没有版本选择。

```bash
# 只读指定版本、上游不变、只写 task9、范围外实体不生成选择、评分统计无逐条边
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py tests/integration/test_task9_selections.py -k "ac01"
```

期望：`13 passed, 49 deselected`，退出码 `0`。

## AC02 源记录定位与获取时间

- [x] 2026-11-03 通过（T9-01）：四类源记录各有完整定位，获取时间与来源列正确（页面 `fetched_at`、IMDb `downloaded_at`、评论 `download_completed_at`、人工修订 `decided_at`）；`fetched_at` 为空的页面获取时间为 `NULL` 并写 `missing_acquired_at`；同一定位两次构建 `source_record_id` 相同且等于 Python 公式；同阶段键重跑与注入故障后续跑结果一致。业务库副本实测见 design“T9-01 演练实测”。

```bash
# 四类源记录、获取时间来源列、缺获取时间的质量问题、ID 稳定
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py -k "ac02"
```

期望：`7 passed, 25 deselected`，退出码 `0`。


## AC03 快照哈希核验

- [x] 2026-11-03 通过（T9-01、T9-03）：正常、内容被改、文件缺失三个快照依次为 `ok`、`snapshot_hash_mismatch`、`snapshot_missing`，构建不中断；T9-03 补上：经后两个快照得到的非空版本字段 `trace_status = 'evidence_unverified'` 并写 `untraced_value`，正常快照的字段仍为 `traced`。副本 9,590 个快照全部 `ok`，没有 `evidence_unverified`。

```bash
# 三种快照（源记录层）与相关字段的 evidence_unverified
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py tests/integration/test_task9_selections.py -k "ac03"
```

期望：`6 passed, 56 deselected`，退出码 `0`。

## AC04 评论层视图

- [x] 2026-11-03 通过（T9-03）：任务 8 小世界（经任务 1 装载与任务 8 运行生成）里有效、隔离、不完整评论分别返回 `mapped`、`quarantined`、`incomplete`；D3 复制返回逻辑评论、层次、家族与作品；只在合集上的评论只有商品没有作品；`task9` 里评论与商品只有两个视图。副本上 Legend 最早评论返回 `D3_family_copy` 与 Legend。

```bash
# 评论视图三种状态、D3、合集评论；商品视图
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py tests/integration/test_task9_selections.py -k "ac04"
```

期望：`2 passed, 60 deselected`，退出码 `0`。

```bash
# 可选：副本上的商品与评论视图（只读；副本保留到 T9-07）
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT collection_status, category, movie_id, relation_type, match_status, edition_title FROM task9.v_product_lineage WHERE asin = 'B000063UR2' AND conflict_version_id = 'cv0003'"
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT record_no, mapping_status, match_level, logical_review_id, family_id, movie_id FROM task9.v_review_lineage WHERE source_review_id = '8f17b11666f52dc1fcca4fb62c233df9d5dc755992823c0470d135bb6341b55b' AND review_dedup_version_id = 'rd0001'"
```

期望：第一条在 `SET` 之后输出 `success|movie|mv_24a89e3536e837e6|edition|merged|Legend (1986)`；第二条输出 `6450929|mapped|D3_family_copy|lr_a206995a2a4ec3f45864|rf_f4c0c1854b2f4130|mv_24a89e3536e837e6`。

## AC05 字段断言与未采用候选

- [x] 2026-11-03 通过（T9-01）：页面字段、任务 4 无父（extract）与有父（derived）断言、人物提及、IMDb 五类行、推断上界依赖评论都登记为断言，各列与上游逐行一致；`assertion_id` 两次构建相同且等于 Python 公式；Legend 标题候选的 `adopted`、`rejected`、`superseded_by_specific` 与任务 6 的 `selected`、`not_selected` 都登记并保留上游原因。业务库副本上 Legend `release_year` 的 3 条任务 6 候选与 3 条任务 7 候选全部登记（见 design“T9-01 演练实测”）。

```bash
# 各上游来源的断言、ID 稳定、三种候选状态保留
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py -k "ac05"
```

期望：`9 passed, 23 deselected`，退出码 `0`。


## AC06 多值成员与实体合并边

- [x] 2026-11-03 通过（T9-02）：对齐的导演成员同时连到对齐断言（页面提及）与 IMDb title.crew 行；同一人的 starring 与 cast 两行；多人串原样保留；B0Lg、B0LgC#1 连到合并决策，B0LgB 连到 singleton 规则；提及 → 人物的 merge 边带依据、轮次与决策引用；starring 沿用 cast 对齐的成员也连到本角色提及。业务库副本上 81,064 个演职员成员全部有父边（design“T9-02 演练实测”）。

```bash
# 对齐成员、同人多角色、多人串、商品到作品、人物归并、类型成员
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py -k "ac06"
```

期望：`7 passed, 26 deselected`，退出码 `0`。

## AC07 派生边闭集、无环与终止

- [x] 2026-11-03 通过（T9-02）：操作类型都在闭集内且 OpenLineage 标注一致，每条边有 `rule_id`、规则版本、上游运行与时间；无环、每条断言都走到源记录；注入的环、悬空父断言、悬空父源记录与缺父边的断言都被检出，闭集外的操作被表约束拒绝；故障后续跑与一次跑完相同。业务库副本 2,106,585 条边，悬空 0、无父节点 0、环 0。

```bash
# 操作类型闭集、边属性、无环、路径终止于源记录、注入环与悬空边、续跑
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py -k "ac07"
```

期望：`5 passed, 28 deselected`，退出码 `0`。

```bash
# 可选：业务库副本上按操作类型的边数（只读；副本保留到 T9-07）
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT operation, count(*) FROM task9.derivation WHERE stage_key = 'd8f913d99aa00f0e' GROUP BY 1 ORDER BY 1"
```

期望：在 `SET` 之后输出 12 行：`aggregate|217435`、`align|17213`、`extract|1062036`、`infer|3963`、`link|276300`、`merge|53250`、`normalize|125979`、`relocate|121283`、`select|74913`、`split|9010`、`union|132045`、`vocab_map|13158`。

## AC08 只读重放补定位

- [x] 2026-11-03 通过（T9-02）：类型词面补出 genre 区的任务 4 断言 ID 与面包屑的区域、序号；版本特征补出来源断言；第二早评论补出源评论 ID；都标 `relocated`。上游值被改得不一致时写 `replay_mismatch`、不写重放边，上游各表不变。业务库副本：类型词面 34,531、版本特征 46,027、第二早评论 3,015 全部一致（对照试算缺口 10,320、46,027、3,015）。T9-03 补上：不一致的类型成员、版本特征与推断上界的字段选择 `trace_status = 'partial'` 并计入缺口。

```bash
# 类型词面、版本特征、第二早评论补定位与 replay_mismatch
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py tests/integration/test_task9_selections.py -k "ac08"
```

期望：`6 passed, 56 deselected`，退出码 `0`。

```bash
# 可选：业务库副本上的重放一致性与质量问题（只读）
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT target_kind, consistent, count(*) FROM task9.relocation WHERE stage_key = 'd8f913d99aa00f0e' GROUP BY 1, 2 ORDER BY 1, 2"
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT issue_code, detail->>'nconst', count(*) FROM task9.quality_issue WHERE stage_key = 'd8f913d99aa00f0e' GROUP BY 1, 2"
```

期望：第一条在 `SET` 之后输出 `edition_feature|t|46027`、`genre_term|t|34531`、`second_review|t|3015`（没有 `f` 行，即 `replay_mismatch` 为 0）；第二条输出 `dangling_edge|nm17757708|1`（`imdb.name` 缺该人名行，路径改连演职员行，见 design）。

## AC09 推断上界的全部依赖

- [x] 2026-11-03 通过（T9-02）：Legend 三个 edition 商品 7 条评论全部为 aggregate 父节点，最早与第二早标 `direct_dependency`（第二早为重放补出），合集评论不在内；只有发售日期证据的 Mv1 以所用 media_release 断言为父节点（infer，带 M1–M4）；上界断言记录 B1–B5、两种证据与算法 `etl_task6.bounds.build_bounds`、规则 `t6r1`。业务库副本 aggregate 边 217,435 条（等于试算），直接依赖 6,030。

```bash
# 评论上界 7 条 aggregate 父节点、直接依赖标记、合集排除、发售日期上界
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py -k "ac09"
```

期望：`2 passed, 31 deselected`，退出码 `0`。

```bash
# 可选：业务库副本上评论上界的依赖边（只读）
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT count(*), count(*) FILTER (WHERE role = 'direct_dependency'), count(*) FILTER (WHERE relocated) FROM task9.derivation WHERE stage_key = 'd8f913d99aa00f0e' AND operation = 'aggregate'"
```

期望：在 `SET` 之后输出 `217435|6030|3015`。

## AC10 字段选择、状态与一致性

- [x] 2026-11-03 通过（T9-03）：作品与版本标量的状态与任务 7 一致（resolved、conflict_unresolved、missing），空值都有缺失原因；多值字段按成员一行；推断上界为 `inferred`；注入的改值与删成员都被 `check_values` 检出；人工修订只来自 human_decision，补一条到人工修订的边后选择判为 `manual`，评价标注不算修订。副本 531,371 个选择，非空 469,671 个全部可追溯（覆盖率 100%，试算 85.6%），与目标层不一致 0。

```bash
# 四种状态、NULL 缺失原因、成员、推断上界、不一致检出、人工修订路径、覆盖率
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py tests/integration/test_task9_selections.py -k "ac10"
```

期望：`5 passed, 57 deselected`，退出码 `0`。

```bash
# 可选：副本上的状态与 trace_status 分布、非空值覆盖率（只读）
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT status, trace_status, count(*) FROM task9.field_selection WHERE stage_key = 'l7292c0631b27f9a' GROUP BY 1, 2 ORDER BY 1, 2"
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT count(*), count(*) FILTER (WHERE trace_status IN ('traced', 'relocated', 'manual')) FROM task9.field_selection WHERE stage_key = 'l7292c0631b27f9a' AND value_text IS NOT NULL"
```

期望：第一条在 `SET` 之后输出 7 行：`conflict_unresolved|traced|478`、`inferred|relocated|3015`、`inferred|traced|3963`、`missing|traced|51`、`missing|untraced|61623`、`resolved|relocated|56347`、`resolved|traced|405894`；第二条输出 `469671|469671`。

## AC11 查询 field

- [x] 2026-11-03 通过（T9-04）：Legend `release_year` 返回最终值、规则与理由（与任务 7 一致），采用的 IMDb 候选落到 `title.basics.tsv.gz` 的行号、文件哈希与下载时间，两条 rejected 的页面年份落到各自快照的正文哈希与 `fetched_at`，路径带操作、规则与上游运行，另列推断上界；Y2 `director` 按成员列出，对齐成员同时有页面快照与 title.crew 行证据。副本实测见 design“演示查询”。

```bash
# movie_id + 字段名 → 最终值、采用与未采用候选、原始证据、理由
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac11"
```

期望：`2 passed, 8 deselected`，退出码 `0`。

```bash
# 可选：副本上 Legend release_year 的未采用候选（只读查询命令，副本保留到 T9-07）
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.query field --movie-id mv_24a89e3536e837e6 --field release_year | grep -c '"upstream_reason": "minor_tolerance"'
```

期望：输出 `4`（两条 rejected 候选，各在候选摘要与其路径根节点里出现一次）。

## AC12 查询 product 与 review

- [x] 2026-11-03 通过（T9-04）：电影单品、合集与未采集商品返回采集状态、类别、版本属性、作品与关系类型、匹配状态，未采集商品返回 `not_collected` 不报错，不在清单的 ASIN 返回 `not_in_manifest`；任务 8 小世界的有效、隔离、不完整、D3 与合集评论的 `query review` 与 AC04 视图一致；Legend 最早评论标为推断上界的直接依赖。

```bash
# ASIN 与源评论 ID → 版本、作品、匹配状态、逻辑评论与归属
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac12"
```

期望：`3 passed, 7 deselected`，退出码 `0`。

## AC13 查询 source

- [x] 2026-11-03 通过（T9-04）：页面快照、IMDb `tconst`、人物 `nconst` 与评论四种输入，`query source` 列出的受影响选择与最短路径长度与测试里沿派生边手工追踪的结果完全相同；快照给出任务 3 起的重跑步骤。副本：B000063UR2 影响 41 个选择、`tt0089469` 影响 56 个（design“演示查询”）。

```bash
# 快照、tconst、nconst、评论 → 受影响实体与字段及路径长度
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac13"
```

期望：`4 passed, 6 deselected`，退出码 `0`。

## AC14 查询 person

- [x] 2026-11-03 通过（T9-04）：Es One 的规范名与来源、3 条署名的页面定位与原值、归并依据（credits、co_credit）与轮次、外部 ID 与 IMDb 人名行证据、别名、参与作品与角色，均与任务 5、7 的表一致。副本上 Olivia de Havilland 返回 6 条署名、2 个页面别名、4 部作品 6 个角色成员。

```bash
# person_id → 原始署名、别名归并过程、参与作品
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac14"
```

期望：`1 passed, 9 deselected`，退出码 `0`。

## AC15 来源与规则变化定位、版本差异

- [ ] 部分通过，未勾选（T9-04，2026-11-03）：`verify-sources` 列出被改快照、受影响字段与任务 3 至 9 的重跑步骤，文件不可读记 `unavailable` 不算不一致；`impact --rule` 与规则版本只列出手工追踪可达的字段；第二个上游 cv（只改片长容差）生成的 `lv0002` 与 `lv0001` 的 `diff` 只在 `runtime_minutes` 上列出变化，与任务 7 两个版本逐行一致。**“第二个血缘版本只重建受影响实体”未实现**：当前新的上游版本改变全部阶段键、全量重建；三种方案与代价见 design“T9-04 实现要点”，待用户决定。

```bash
# verify-sources、impact --rule、report diff（已实现部分）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_impact.py -k "ac15"
```

期望：`4 passed`，退出码 `0`。

## AC16 版本、幂等、恢复、并发与 DAG

- [ ] 未执行（计划）。

```bash
# 重跑 reused、注入故障后恢复、并发锁、DAG 拓扑与 verify 失败重跑（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_runs.py tests/integration/test_task9_dag.py -k "ac16"
```

期望（计划）：相关用例全部通过，退出码 `0`；真实 scheduler 端到端步骤见 runbook（计划，可选）。

## AC17 报告与实例

- [ ] 未执行（计划）。

```bash
# summary.json 与数据库一致、覆盖率分列、examples.md 覆盖四个查询与四类作业示例（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_report.py -k "ac17"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC18 抽查评价工具

- [ ] 未执行（计划）。

```bash
# 五层配额、审核表、AI 草稿、导入校验、指标（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_evaluate.py -k "ac18"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC19 业务库副本全链路演练

- [ ] 未执行（计划）。步骤见 design“业务库副本演练”与 runbook（计划）；演练结论写入 design“业务库副本全链路演练实测”。

## AC20 正式库全量运行与核对

- [ ] 未执行（计划）。

```bash
# 正式库只读核对（计划）
docker compose --profile crawl run --rm -T crawler python -m etl_task9.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`。

```bash
# 血缘版本与 trace_status 分布（只读，计划）
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT lineage_version_id, status FROM task9.lineage_version; SELECT trace_status, count(*) FROM task9.field_selection WHERE lineage_version_id = 'lv0001' AND final_value IS NOT NULL GROUP BY 1 ORDER BY 1;"
```

期望（计划）：`lv0001|verified`；非空最终值的 `trace_status` 只有 `traced`、`relocated`（与 `manual`，正式库应为 0）。四个演示查询命令见 design“演示查询”。

## AC21 正式库抽查评价

- [ ] 未执行（计划）。审核表由 AI 预填、付林轩审核后导入；指标写入 report（计划）。

## AC22 迁移、测试与仓库内容

- [ ] 未执行（计划）。

```bash
# 迁移清单（只读，计划）
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration;"
```

期望（计划）：`SET` 之后为 `001,101,102,103,201,301,302,401,501,601,701,801`。

```bash
git ls-files | grep -E "^outputs/|\.env$|eval_items|\.tsv\.gz$|\.html\.gz$" | wc -l
```

期望（计划）：`0`。
