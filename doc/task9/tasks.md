# 任务 9 开发任务（字段级数据血缘）

状态：2026-11-03 编写计划；T9-00 已执行并通过（AC00）；T9-01 至 T9-03 已完成；T9-04 至 T9-06 已完成（2026-11-04 用户选定 AC15 方案 C，AC15 改为全量重建加差异核对后通过）；T9-07、T9-08 未开始。每完成一个任务在本地提交一次，未经用户要求不推送。

命令均在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板`（macOS 默认 zsh）下直接复制运行，不使用变量缩写。标注“（计划）”的命令在对应任务实现前不可运行，实现后实际核对再改写期望。每项 AC 的逐条复核命令见 [checklist](checklist.md)。每个实现阶段完成后先在业务库副本 `etl_t9_rehearsal` 演练（design“业务库副本演练”），再进入下一阶段。

- [x] T9-00 计划：正式库只读探查，编写 `doc/task9/{spec,design,tasks,checklist}.md`，执行 AC00 规范自检。规则：全部；关联 AC00。
  - 文件：`doc/task9/spec.md`、`doc/task9/design.md`、`doc/task9/tasks.md`、`doc/task9/checklist.md`；`README.md`、`CLAUDE.md`（任务 8 合并状态与任务 9 状态）。
  - 完成情况：2026-11-03 完成。正式库只读探查（试算覆盖率 85.6%，见 design），用户确认 H01–H04；AC00 自检通过。

  ```bash
  grep -n "^## " doc/task9/spec.md
  diff <(grep -o "^| AC[0-9][0-9]" doc/task9/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task9/checklist.md | tr -d '# ')
  grep -c "^- R[0-9][0-9] \[" doc/task9/spec.md
  python3 -c "import re; s=open('doc/task9/spec.md').read(); t=open('doc/task9/tasks.md').read(); ex=lambda x: {i for a,b in re.findall(r'R(\d\d)(?:–R(\d\d))?', x) for i in range(int(a), int(b or a)+1)}; rules={int(n) for n in re.findall(r'^- R(\d\d) \[', s, re.M)}; ac=ex(''.join(re.findall(r'^\| AC\d\d \| ([^|]*)\|', s, re.M))); tk=ex(''.join(re.findall(r'规则：([^；]*)；', t))); print(len(rules), sorted(rules-ac), sorted(rules-tk))"
  ```

  期望：第一条只输出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条输出 `49`；第四条输出 `49 [] []`（49 条规则，没有未被 AC 或 T9-01 至 T9-07 覆盖的规则；T9-08 的正式运行另检验全部规则，不计入这项检查）。

- [x] T9-01 迁移 801、运行审计与咨询锁、ID 与定位纯函数、原始层与暂存层（`build_sources`、抽取级与候选级断言）。规则：R01–R03、R06–R13、R15、R41、R42；关联 AC01、AC02、AC03、AC05。
  - 文件：`sql/801_task9_schema.sql`、`src/etl_task9/{__init__,rules,sources,run,source_records,assertions,runner}.py`、`tests/fixtures/task9/README.md`、`tests/unit/test_task9_rules.py`、`tests/integration/{_task9_helpers,test_task9_sources,test_task9_assertions}.py`；`tests/integration/conftest.py` 增加 `DROP SCHEMA IF EXISTS task9`。
  - 目标：四类源记录与获取时间来源、快照哈希核验；各上游表翻译为断言并保留上游状态；夹具经各任务自己的写入函数生成；演练库上源记录数、断言数与 design 预算对照，抽查 Legend 的断言。
  - 完成情况：2026-11-03 完成。32 项新测试通过，全量 `1218 passed`（原 1,186 项不减）；业务库副本 `etl_t9_rehearsal` 上 `stages --through assertions` 用时 194 秒，源记录 429,323（快照 9,590 全部 `ok`）、断言 1,191,277，质量问题 0；Legend 断言抽查一致。详见 design“T9-01 演练实测”。AC01、AC03 只完成本阶段部分（见 checklist）。

  ```bash
  # 规则单元测试与原始层、暂存层集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py
  ```

  期望：`32 passed`，退出码 `0`。

  ```bash
  # 可选：业务库副本上的源记录与断言计数（只读；副本保留到 T9-07）
  docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT source_kind, evidence_status, count(*) FROM task9.source_record WHERE stage_key = 's11f2e418823b9fe' GROUP BY 1, 2 ORDER BY 1, 2"
  docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT level, count(*) FROM task9.assertion WHERE stage_key = 'a65adeae3bb06227' GROUP BY 1 ORDER BY 1"
  ```

  期望：第一条在 `SET` 之后输出 `imdb_row|not_checked|202298`、`page_snapshot|ok|9590`、`review_record|not_checked|217435`；第二条输出 `candidate|120231`、`derived|9010`、`extract|1062036`。

- [x] T9-02 集成层：派生边、实体合并边、推断依赖与只读重放补定位。规则：R14、R16–R22；关联 AC06–AC09。
  - 文件：`src/etl_task9/{derivations,relocate}.py`、`src/etl_task9/{assertions,rules,runner}.py`（集成层断言、算法标记 `a2`/`d2`、`--through derivations`）、`tests/integration/{test_task9_derivations,test_task9_relocate}.py`、`tests/integration/_task9_helpers.py`（夹具补成正式库形状）、`tests/fixtures/task9/README.md`。
  - 目标：操作类型闭集与 OpenLineage 标注；商品到作品、提及到人物的合并依据成边；评论上界的全部依赖评论；类型词面、版本特征与第二早评论的重放补定位及不一致检出；演练库上重放一致率与试算缺口（10,320、46,027、3,015）对照。
  - 完成情况：2026-11-03 完成。20 项新测试通过；全量 `1237 passed, 1 failed`（失败的是任务 1 的 `test_resource_limits`：Docker 磁盘剩余 9.7 GB 低于 10 GB 保留阈值，由保留的演练副本占用，与本次改动无关）。副本 `stages --through derivations`：断言 1,548,754、派生边 2,106,585，无环、无悬空、无缺父节点；重放 34,531 + 46,027 + 3,015 全部一致；`task9` 当前一组键约 3.2 GB（超预算，见 design“T9-02 演练实测”）。

  ```bash
  # 派生边与重放补定位集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py
  ```

  期望：`20 passed`，退出码 `0`。

  ```bash
  # 可选：业务库副本上的断言与重放计数（只读；副本保留到 T9-07）
  docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT level, count(*) FROM task9.assertion WHERE stage_key = 'a0d345df84d5280a' GROUP BY 1 ORDER BY 1"
  docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT target_kind, consistent, count(*) FROM task9.relocation WHERE stage_key = 'd8f913d99aa00f0e' GROUP BY 1, 2 ORDER BY 1, 2"
  ```

  期望：第一条在 `SET` 之后输出 `candidate|156568`、`derived|330150`、`extract|1062036`；第二条输出 `edition_feature|t|46027`、`genre_term|t|34531`、`second_review|t|3015`。

- [x] T9-03 目标层：字段选择、`trace_status`、与目标层一致性、人工修订路径、评论与商品视图。规则：R04、R05、R10、R23–R27；关联 AC04、AC10。
  - 文件：`src/etl_task9/selections.py`、`sql/801_task9_views.sql`（视图，由 `apply_task9_schema` 每次重建）、`src/etl_task9/{derivations,rules,runner}.py`（非空选择的无父检查、算法 `l3`、`--through selections`）、`tests/integration/test_task9_selections.py`。
  - 目标：范围内每个最终字段与成员一行选择；`NULL` 字段记状态与原因；最终值与目标表逐行一致；评论层视图不复制；演练库上覆盖率与 design 试算（85.6% → 期望 100%）对照。
  - 完成情况：2026-11-03 完成。10 项新测试通过，全量 `1248 passed`。副本 `stages --through selections`：选择 531,371 个，非空 469,671 个全部可追溯（覆盖率 100%），relocated 59,362 正好是试算缺口，与目标层不一致 0。

  ```bash
  # 字段选择与视图集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_selections.py
  ```

  期望：`10 passed`，退出码 `0`。

  ```bash
  # 可选：副本上的非空值覆盖率（只读；副本保留到 T9-07）
  docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT count(*), count(*) FILTER (WHERE trace_status IN ('traced', 'relocated', 'manual')) FROM task9.field_selection WHERE stage_key = 'l7292c0631b27f9a' AND value_text IS NOT NULL"
  ```

  期望：在 `SET` 之后输出 `469671|469671`。

- [x] T9-04 影响闭包、四个演示查询、影响分析、来源核验与版本差异。规则：R28–R35；关联 AC11–AC15。
  - 文件：`src/etl_task9/{impact,query,versions,report}.py`、`src/etl_task9/{runner,rules}.py`（`--through impact`）、`tests/integration/{test_task9_query,test_task9_impact}.py`、`tests/integration/_task9_helpers.py`（`add_products`）、`tests/fixtures/task9/README.md`。
  - 目标：`field`、`product`、`review`、`source`、`person`、`impact` 子命令；`verify-sources`；`report diff`；上游重跑映射；演练库上对 design 中真实 ID 执行四个查询并人工看一遍输出。
  - 完成情况：2026-11-03 完成实现，14 项新测试通过，全量 `1262 passed`；AC15 的按实体增量重建经用户 2026-11-04 选定方案 C 改为“全量重建 + `diff` 与事前 `impact` 一致”，补测试 `test_ac15_full_rebuild_diff_matches_prior_rule_impact` 后 AC15 通过（本文件共 15 项）。副本影响闭包 887,035 行（31 秒），四个演示查询输出要点见 design“演示查询”，`verify-sources` 快照与 IMDb 文件全部一致。

  ```bash
  # 查询与影响分析集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py tests/integration/test_task9_impact.py
  ```

  期望：`15 passed`，退出码 `0`。

  ```bash
  # 可选：副本上的影响闭包行数与 Legend release_year 查询（只读；副本保留到 T9-07）
  docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT count(*), max(depth) FROM task9.field_impact WHERE stage_key = 'if9ab0bf333bd203'"
  docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.query field --movie-id mv_24a89e3536e837e6 --field release_year | grep -m 3 -E '"(value_text|status|decision_rule)"'
  ```

  期望：第一条在 `SET` 之后输出 `887035|5`；第二条输出 `"value_text": "1985",`、`"status": "resolved",`、`"decision_rule": "year.table:YR2",` 三行。

- [x] T9-05 版本与完整运行、分批恢复与并发锁、`verify`、报告与实例、Airflow DAG、runbook。规则：R36–R40、R44、R45；关联 AC16、AC17。
  - 文件：`src/etl_task9/{runner,pipeline,check,report,versions}.py`、`dags/amazon_movies_task9_lineage.py`、`scripts/seed_task9_e2e.py`、`tests/integration/{test_task9_runs,test_task9_dag,test_task9_report}.py`、`doc/task9/runbook.md`、`tests/fixtures/task9/README.md`。
  - 目标：10 节点 DAG，`verify` 通过才 `succeeded`；`compose.e2e.yaml` 下经真实 scheduler 跑通成功、重跑 `reused`、上游缺失、`verify` 失败四种情况后切回正常配置；`report export` 覆盖率与四类作业示例。
  - 完成情况：2026-11-03 完成。16 项新测试通过，全量 `1278 passed`；真实 scheduler 四种情况符合预期，已切回正常配置（runbook 第 2 节）；副本 `runner run` 生成 `lv0001` 并 `verified`，重跑 `reused`，`check` 退出码 0，导出 19 例。

  ```bash
  # 运行、DAG 与报告集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_runs.py tests/integration/test_task9_dag.py tests/integration/test_task9_report.py
  ```

  期望：`16 passed`，退出码 `0`（另有 Airflow 的弃用警告）。

  ```bash
  # 可选：副本上的血缘版本与核对（只读；副本保留到 T9-07）
  docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -Atc "SET default_transaction_read_only = on; SELECT lineage_version_id, status FROM task9.lineage_version; SELECT status, count(*) FROM task9.run WHERE kind = 'lineage' GROUP BY 1 ORDER BY 1"
  docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.check > /dev/null; echo "exit=$?"
  ```

  期望：第一条在 `SET` 之后输出 `lv0001|verified`、`reused|1`、`succeeded|1`；第二条输出 `exit=0`。

- [x] T9-06 抽查评价工具：审核表、AI 草稿、导入校验、指标。规则：R46–R49；关联 AC18。
  - 文件：`src/etl_task9/evaluate.py`、`tests/integration/{_task9_helpers,test_task9_evaluate}.py`、`tests/fixtures/task9/README.md`、`doc/task9/runbook.md`（第 5 节）。
  - 目标：五层配额与补足；审核表含路径与原始证据摘录；草稿只读审核表与原始证据；缺依据的改判被拒。
  - 完成情况：2026-11-03 完成。7 项新测试通过，全量 `1285 passed`（原 1,278 项不减）；正式库审核表留给 T9-08。L5 改为商品与评论两类交替抽样（见 design“T9-06 实现要点”）。

  ```bash
  # 抽查评价集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_evaluate.py
  ```

  期望：`7 passed`，退出码 `0`。

- [ ] T9-07 业务库副本全链路演练。规则：R43；关联 AC19。
  - 文件：`doc/task9/design.md`（“业务库副本全链路演练实测”）、`doc/task9/runbook.md`、`doc/task9/tasks.md`、`doc/task9/checklist.md`。
  - 目标：停 Airflow 建副本并 `CHECKPOINT`，全链路运行 `lv0001` 与 `check`，四个演示查询与影响分析，删库并 `CHECKPOINT`；差异逐项解释。
  - 完成情况：未执行（计划）。步骤见 design“业务库副本演练”与 runbook。

- [ ] T9-08 正式库运行与报告：经 Airflow 生成 `lv0001`、重跑 `reused`、核对、四个演示查询、导出报告与审核表；审核标注导入后计算指标，编写 `report.md` 与交接文档。规则：正式库运行检验全部规则（见 AC20–AC22 的 Rule 列）；关联 AC20–AC22。
  - 文件：`doc/task9/{report,tasks,checklist,design}.md`、`doc/handoffs/<日期>-task9-to-<下一步>.md`、`README.md`、`CLAUDE.md`。
  - 完成情况：未执行（计划）。

  ```bash
  # 正式库只读核对（工作目录：项目根目录）（计划）
  docker compose --profile crawl run --rm -T crawler python -m etl_task9.check > /dev/null; echo "exit=$?"
  ```

  期望（计划）：`exit=0`。
