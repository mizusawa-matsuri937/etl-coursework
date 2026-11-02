# 任务 7 开发任务（处理不同来源的信息冲突）

状态：2026-10-30 编写计划；2026-11-02 经复核修订（用户同意按复核结论修订），新增 `link_assess` 阶段，规则与验收重新编号为 R01–R66、AC00–AC22，任务调整为 T7-00 至 T7-09。T7-00 已执行并通过；T7-01 至 T7-04 于 2026-11-02 实现并本地提交，命令与期望已按实际运行核对；T7-05 至 T7-09 尚未实现，其验证命令与期望仍标注为 `（计划）`。每完成一个任务在本地提交一次，未经用户要求不推送。

命令均在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板`（macOS 默认 zsh）下直接复制运行，不使用变量缩写。每项 AC 的逐条复核命令见 [checklist](checklist.md)。

- [x] T7-00 计划：编写并修订 `doc/task7/{spec,design,tasks,checklist}.md`，执行 AC00 规范自检。关联 AC00。
  - 文件：`doc/task7/spec.md`、`doc/task7/design.md`、`doc/task7/tasks.md`、`doc/task7/checklist.md`。
  - 完成情况：2026-10-30 初版；2026-11-02 修订并在正式库只读试算新规则，AC00 自检通过。

  ```bash
  grep -n "^## " doc/task7/spec.md
  diff <(grep -o "^| AC[0-9][0-9]" doc/task7/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task7/checklist.md | tr -d '# ')
  grep -c "^- R[0-9][0-9] \[" doc/task7/spec.md
  ```

  期望：第一条只输出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条输出 `66`。

- [x] T7-01 迁移 601、运行审计与咨询锁、规则纯函数、商品版本层消解。关联 AC01、AC02、AC09、AC16。
  - 文件：`sql/601_task7_schema.sql`、`src/etl_task7/{__init__,rules,run,selection,sources,editions}.py`、`tests/fixtures/task7/`、`tests/unit/test_task7_rules.py`、`tests/integration/{_task7_helpers,test_task7_editions}.py`；`tests/integration/conftest.py` 增加一行 `DROP SCHEMA IF EXISTS task7`。
  - 完成情况：2026-11-02 完成，规则单元测试 141 项与版本层集成测试 11 项通过（共 152）；迁移 601 含 27 张表与冲突类型闭集 CHECK；版本层取 `task4.product_edition` 的 matched 商品，6 个标量字段与 7 项多值特征落到 `edition_stage_result`。AC16、AC09 通过；AC01、AC02 的作品层与 `movie_resolved`/`edition_resolved` 物化部分待后续阶段。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py tests/integration/test_task7_editions.py
  ```

  期望：最后一行 `152 passed`，退出码 `0`。

- [x] T7-02 关联评估：有效 `tconst`、四信号、错配熔断、共享 `tconst` 分类。关联 AC10、AC21。
  - 文件：`src/etl_task7/link_assess.py`、`tests/integration/test_task7_link_assess.py`。
  - 完成情况：2026-11-02 完成，12 项集成测试通过：L1–L10 的四信号与熔断（L1–L3 熔断，L4–L9 不熔断，强关联 L6 不熔断）、导演比较复用 `etl_task5.names` 的 `parse`/`level`、T 信号读任务 4 规范键（`normalized_value`），共享 `tconst` 4 条问题 → 3 对（`false_positive_link`、`likely_duplicate_movie`、`undetermined`）且任务 4 摘要不变。AC10 通过；AC21 的 `movie_multivalue` 读取同一判定待 T7-04。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_link_assess.py
  ```

  期望：最后一行 `12 passed`，退出码 `0`。

- [x] T7-03 作品层标量字段：标题、原名、别名、年份决策表、片长差异表、分级。关联 AC02–AC06、AC16。
  - 文件：`src/etl_task7/movies_scalar.py`、`tests/integration/test_task7_movies_scalar.py`。
  - 完成情况：2026-11-02 完成，33 项集成测试通过（AC01 作品层 3、AC02 2、AC03 5、AC04 11、AC05 4、AC06 5，另有 3 项分批故障恢复冒烟）：读 `link_assessment` 决定是否采用 IMDb；`edition` 与 `contains` 候选严格区分；写 `movie_scalar_stage_result`、`runtime_diff`、`movie_title_alias`；推断上界只进参考列。AC03–AC06 通过；AC01、AC02 的 `movie_resolved` 物化与类型不下推待 T7-04/T7-05。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py
  ```

  期望：最后一行 `33 passed`，退出码 `0`。

- [x] T7-04 作品层多值字段：类型词面处置与并集、同片署名对齐、多人串、IMDb 人物登记、署名顺序、覆盖范围。关联 AC07、AC08、AC22，补完 AC02、AC21。
  - 文件：`src/etl_task7/movies_multivalue.py`（另在 `sources.py` 增加 `imdb_principals`、`imdb_names`、`imdb_person_ids`、`page_credits`、`genre_terms`，在 `selection.py` 增加 `multi_selection`）、`tests/integration/test_task7_movies_multivalue.py`、`tests/unit/test_task7_multivalue.py`；`tests/fixtures/task7/world.py` 与 `tests/integration/_task7_helpers.py` 增加类型断言、面包屑与任务 5 页面人物（与真实输出同形）。
  - 目标：写 `movie_genre`、`movie_genre_term`、`credit_alignment`、`movie_person_resolved`、`imdb_person_ext`、`movie_multivalue_stage_result`；`starring` 只取页面；计算 `cast_coverage`；不修改 `task5`。
  - 完成情况：2026-11-02 完成，集成 28 项与单元 8 项通过（AC07 7、AC08 10、AC22 8、AC02 合集类型 2、AC21 读取同一判定 1）；类型读任务 4 的 `normalized_value`，改读 `raw_value` 会使 AC07 的 5 项失败（已人工验证）。临时库 `etl_t7_rehearsal`（业务库完整副本）全链路：署名对齐 3,358 条（试算 3,335），IMDb 新增 `cast` 30,182、`director` 869，`imdb_person_ext` 22,157，页面类型词面未映射 276/5,311 行（与试算一致），面包屑未映射 331/30,146 行（试算 223，差异原因见 design 与交接）；每部未熔断关联作品的 IMDb 演员 49,838 个、导演 6,026 个与 `movie_person_resolved` 逐项相等。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py tests/unit/test_task7_multivalue.py
  ```

  期望：最后一行 `36 passed`，退出码 `0`。

- [ ] T7-05 版本物化、差异、恢复、报告、查询与核对。关联 AC11、AC12、AC14、AC17。
  - 文件：`src/etl_task7/{versions,runner,report,query,check}.py`、`tests/integration/test_task7_{runs,report}.py`。
  - 目标：`finalize_version` 只从阶段表物化；验证阶段依赖链的增量重算、`report diff`、分批故障恢复与并发锁；实现 `report export`、`query movie|edition|conflicts`、`etl_task7.check`。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）版本、恢复、报告与查询集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py tests/integration/test_task7_report.py
  ```

  期望（计划）：全部通过，退出码 `0`。

- [ ] T7-06 Airflow DAG、核对后才成功的运行状态、真实 scheduler 验证与 runbook。关联 AC13。
  - 文件：`src/etl_task7/pipeline.py`、`dags/amazon_movies_task7_conflict.py`、`scripts/seed_task7_e2e.py`、`tests/integration/test_task7_dag.py`、`doc/task7/runbook.md`。
  - 目标：实现 9 节点 DAG；`verify` 通过才记 `succeeded`；用 `compose.e2e.yaml` 跑通成功、重跑 `reused`、上游缺失、`verify` 失败四种情况，验证后切回正常配置。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）DAG 集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_dag.py
  ```

  期望（计划）：全部通过，且无来自 `amazon_movies_task7_conflict.py` 的弃用警告。

- [ ] T7-07 盲测评价工具。关联 AC15。
  - 文件：`src/etl_task7/evaluate.py`、`tests/integration/test_task7_evaluate.py`、`doc/task7/runbook.md`（评价章节）。
  - 目标：实现 `evaluate freeze|export|import|metrics`；七层配额与 `conflict_unresolved` 保底；盲测证据由上游原始表生成；缺 `human_evidence` 的存疑项拒绝导入；`ai_reason` 不入标注。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）评价工具集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_evaluate.py
  ```

  期望（计划）：全部通过，退出码 `0`。

- [ ] T7-08 正式库运行。关联 AC18、AC20。
  - 文件：`doc/task7/{design,tasks,checklist}.md`；`outputs/task7/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/cv0001/`（不入 Git）。
  - 目标：先冻结开发池 1,732 部，在开发池上核查熔断、对齐与类型映射并冻结 `t7r1`；经 Airflow 触发 `t7_full_1` 生成 `cv0001`，`t7_full_2` 验证 `reused`；用实测替换 design 与 H08 的试算数字；导出报告、盲测表与 AI 草稿。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）正式库只读核对
  docker compose --profile crawl run --rm -T crawler python -m etl_task7.check > /dev/null; echo "exit=$?"
  ```

  期望（计划）：`exit=0`。

- [ ] T7-09 导入 120 项标注、计算指标、报告与交接。关联 AC19、AC20。
  - 文件：`outputs/task7/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/cv0001/eval/`（不入 Git）、`doc/task7/report.md`、`doc/handoffs/<完成日期>-task7-to-task8.md`、`README.md`、`CLAUDE.md`。
  - 目标：付林轩审核 `eval_items_draft.csv` 后导入；生成 `metrics.json`、`metrics.md`；完成报告与交接文档，更新 checklist、README、CLAUDE.md；报告写明 H09 的处理结果。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）正式库 120 项评价指标
  docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate metrics
  ```

  期望（计划）：输出含 `labeled_total: 120`、`annotators: ["付林轩"]`、`action_accuracy`、`value_accuracy`、`conflict_retention`。
