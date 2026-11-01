# 任务 7 开发任务（处理不同来源的信息冲突）

状态：2026-10-30 在分支 `task7-conflict-resolution`（从 `main` 的 `649ac06` 建立）完成正式库只读探查与计划编写；同日用户确认作品层与版本层字段边界、IMDb 关联范围、演职员可信并集、标量容差与保留冲突状态边界、以及 120 项七层盲测评价方案。T7-00 已执行并通过；T7-01 至 T7-08 尚未实现，其验证命令与期望均标注为 `（计划）`。每完成一个任务在本地提交一次，未经用户要求不推送。

命令均在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板`（macOS 默认 zsh）下直接复制运行，不使用变量缩写。每项 AC 的逐条复核命令见 [checklist](checklist.md)。

- [x] T7-00 计划：编写 `doc/task7/{spec,design,tasks,checklist}.md` 并执行 AC00 规范自检。关联 AC00。
  - 文件：`doc/task7/spec.md`、`doc/task7/design.md`、`doc/task7/tasks.md`、`doc/task7/checklist.md`。
  - 完成情况：2026-10-30 完成正式库只读探查与四份计划文件编写，AC00 自检通过（`spec.md` 严格只有五个二级标题，R01–R39 全部被 AC01–AC20 覆盖，`spec.md` 与 `checklist.md` 的 AC00–AC20 编号逐一对应，未执行条目全部保持未勾选并标注计划）。

  ```bash
  grep -n "^## " doc/task7/spec.md
  diff <(grep -o "^| AC[0-9][0-9]" doc/task7/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task7/checklist.md | tr -d '# ')
  grep -c "^- R[0-9][0-9] \[" doc/task7/spec.md
  ```

  期望：第一条只输出 `目标、范围、规则、AC测试、假设` 五个二级标题；第二条无输出；第三条输出 `39`。

- [ ] T7-01 迁移 601、运行审计与咨询锁、纯函数规则库与商品版本层（`ProductEdition`）冲突消解。关联 AC01、AC02、AC09、AC16。
  - 文件：`sql/601_task7_schema.sql`、`src/etl_task7/{__init__,rules,run,editions}.py`、`tests/fixtures/task7/`、`tests/unit/test_task7_rules.py`、`tests/integration/{_task7_helpers,test_task7_editions}.py`。
  - 目标：实现 schema `task7` 全部表结构与约束（迁移 `601`，含三张分批阶段持久化表 `edition_stage_result`/`movie_scalar_stage_result`/`movie_multivalue_stage_result` 及版本收敛表，作品层 9 个字段名统一为单数 `director`）；实现输入键、阶段键（含 `halt_rule_signature`）、运行审计与抽取锁；实现 `rules.py` 纯函数（标题清洁化、跨字段错配熔断、PAL 24/25 帧率差与片长差分类、MPAA 规范化、30 词类型受控映射、分号多人串拆分、署名顺序众数决胜、出版商词根归一）；实现按 ASIN 分批的商品版本层标量消解（含 `task3.page_field(mpaa_rating)`、标量 `discs`）与 7 项多值版本特征（含 `format_swatch`、`edition.dubbed`、画幅关键词）并集及阶段结果持久化。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）运行任务 7 规则单元测试与版本层消解集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py tests/integration/test_task7_editions.py
  ```

  期望（计划）：全部通过（预计约 `30+ passed`），退出码 `0`。

- [ ] T7-02 作品层（`Movie`）标量字段候选收集、跨字段弱匹配错配熔断与冲突消解（`title`、`original_title`、`release_year`、`runtime_minutes`、`mpaa_rating`）。关联 AC02、AC03、AC04、AC05、AC06、AC16。
  - 文件：`src/etl_task7/movies_scalar.py`、`tests/integration/test_task7_movies_scalar.py`。
  - 目标：按 `movie_id` 分批收集作品层 5 个标量字段的 IMDb 候选（覆盖 `verified`、`hint_matched`、`cooccur_matched`）与 Amazon 页面各版本候选（严格区分 `edition` 商品 `entity_id = asin` 与 `contains` 合集成员 `entity_type = 'collection_member' AND entity_id = asin || '#' || member_ordinal`，作品层片长与分级不吸纳合集盒子总片长与合集分级）；实现弱匹配跨字段错配熔断（关联查询 `task6.bound_evidence` 与 `task6.quality_issue(bound_before_release_year)`，并持久化 `cross_source_mismatch_halt` 到 `task7.movie_scalar_stage_result`）；实现主标题与原名分离消解、上映年份分歧复核（含 `title_years_disagree` 转 `conflict_unresolved`）与推断上界独立保留、作品标准片长与版本片长分层分类、以及作品 MPAA 分级消解（具体分级胜过 `NR/Unrated`，双具体分级保留 `conflict_unresolved`）。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）运行作品层标量字段冲突消解集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py
  ```

  期望（计划）：全部通过（预计约 `18+ passed`），退出码 `0`。

- [ ] T7-03 作品层多值字段消解（`genres` 受控词表映射并集、演职员 `director`/`starring`/`cast` 可信并集、`imdb_person_ext` 登记、分号串拆分与署名顺序消解）及跨作品共享 `tconst` 审计。关联 AC07、AC08、AC10。
  - 文件：`src/etl_task7/{movies_multivalue,shared_tconst}.py`、`tests/integration/test_task7_movies_multivalue.py`。
  - 目标：按 `movie_id` 分批从 `task7.movie_scalar_stage_result` 读取 `cross_source_mismatch_halt`；实现作品层 `genres` 30 词受控词表映射、噪声过滤与多源并集（逐词记录 `both`/`imdb_only`/`page_only`）；实现 `director` 与 `cast` 跨源并集、`starring` 署名行边界保护、IMDb 独有人物在 `task7.imdb_person_ext` 的稳定 `person_id` 生成、分号多人导演串拆分匹配、`director_source_disagreement` 标记与 117 项署名顺序冲突众数消解，并持久化到 `task7.movie_multivalue_stage_result`；实现 126 项跨作品共享 `tconst` 的对比与分类（`likely_duplicate_movie` / `false_positive_link`）。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）运行作品层多值字段与共享 tconst 集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py
  ```

  期望（计划）：全部通过（预计约 `15+ passed`），退出码 `0`。

- [ ] T7-04 冲突消解版本物化、跨版本 `diff`、分批中断恢复、统计报告与实例导出、三个只读查询与核对脚本 `check`。关联 AC11、AC12、AC14、AC17。
  - 文件：`src/etl_task7/{runner,report,query,check}.py`、`tests/integration/test_task7_{runs,report}.py`。
  - 目标：实现 `finalize_version` 纯粹从数据库阶段结果表无状态物化生成 `cv0001`…、`task7.movie_resolved`、`task7.edition_resolved`、`task7.movie_field_selection` 与 `task7.edition_field_selection`；实现同参重跑 `reused`、规则/输入变更单阶段增量重算与 `FULL OUTER JOIN` 跨版本差异 `diff`；验证分批故障注入后的跨进程断点恢复与并发咨询锁互斥；实现 `report export`（分字段覆盖率、冲突分类计数、典型实例 `examples.md`）、`query movie|edition|conflicts` 三个只读查询与 `etl_task7.check` 完整性核对。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）运行版本管理、恢复、报告与查询集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py tests/integration/test_task7_report.py
  ```

  期望（计划）：全部通过（预计约 `20+ passed`），退出码 `0`。

- [ ] T7-05 Airflow DAG `amazon_movies_task7_conflict` 编排、真实 scheduler 端到端验证与运行手册 `runbook.md`。关联 AC13。
  - 文件：`src/etl_task7/pipeline.py`、`dags/amazon_movies_task7_conflict.py`、`scripts/seed_task7_e2e.py`、`tests/integration/test_task7_dag.py`、`doc/task7/runbook.md`。
  - 目标：实现 9 节点 Airflow DAG `amazon_movies_task7_conflict`；通过 DAG 结构与顺序执行测试；使用 `compose.e2e.yaml` 在真实 Airflow scheduler 上验证成功运行、二次重跑 `reused` 与上游版本缺失阻断三次端到端运行，验证后切回正常 Compose 配置；编写 `doc/task7/runbook.md`。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）运行 DAG 集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_dag.py
  ```

  期望（计划）：全部通过（预计 `7 passed`），且无来自 `amazon_movies_task7_conflict.py` 的 Airflow 弃用警告。

- [ ] T7-06 盲测评价工具：开发池冻结、120 项七层盲测表导出（全局 MD5 打乱）、非循环 AI 预标注草稿生成、人工审核导入与评价指标计算。关联 AC15。
  - 文件：`src/etl_task7/evaluate.py`、`tests/integration/test_task7_evaluate.py`、`doc/task7/runbook.md`（补充评价章节）。
  - 目标：实现 `evaluate freeze|export|import|metrics`；确保开发池作品及关联任一开发池作品的商品（含 `E7`）严格排除、七层配额（`15/15/20/20/15/15/20`）与不足补足逻辑正确、盲测表按 MD5 打乱编号且不泄露系统胜出值与分层、AI 预标注草稿仅读公开原始候选而不读系统选择表；测试决策动作准确率、消解值准确率（含 `E4`/`E5` 规范化 `|` 分隔集合等价判分）与 `conflict_unresolved` 精确率/召回率计算。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）运行评价工具集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_evaluate.py
  ```

  期望（计划）：全部通过（预计 `8+ passed`），退出码 `0`。

- [ ] T7-07 正式库运行：先冻结开发池 1,732 部，再经 Airflow 全量运行生成 `cv0001` 与同参重跑验证 `reused`，执行只读核对 `check`，导出正式报告、冲突实例与 120 项盲测评价表及非循环 AI 草稿。关联 AC18、AC20。
  - 文件：`doc/task7/{design,tasks,checklist}.md`；`outputs/task7/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/cv0001/`（不入 Git 仓库）。
  - 目标：在正式库 `etl_movies` 先冻结 `task7.dev_pool`（1,732 部），经 Airflow 触发 `t7_full_1` 全量运行生成 `cv0001`（在开发池核查效果并记录到 `design.md`），再触发 `t7_full_2` 验证 `reused`；运行 `python -m etl_task7.check` 确认 `"problems": []` 且任务 1–6 所有表行数不变；导出正式报告与 120 项盲测表 `eval_items.csv` 及非循环 AI 草稿 `eval_items_draft.csv`。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）正式库只读核对
  docker compose --profile crawl run --rm -T crawler python -m etl_task7.check
  ```

  期望（计划）：退出码 `0`，JSON 中 `"problems": []` 且 `"passed": true`。

  ```bash
  # （计划）核对正式库运行记录与作品层字段消解状态分布
  docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT field_name, status, count(*) FROM task7.movie_field_selection WHERE conflict_version_id = 'cv0001' GROUP BY field_name, status ORDER BY field_name, status;"
  ```

  期望（计划）：每个作品层字段在 `resolved`、`conflict_unresolved`、`missing` 下的作品数之和均严格等于 `8659`。

- [ ] T7-08 导入付林轩审核后的 120 项正式评价标注并计算指标；编写任务 7 报告 `doc/task7/report.md` 与任务 7 → 任务 8 交接文档 `doc/handoffs/2026-10-30-task7-to-task8.md`。关联 AC19、AC20。
  - 文件：`outputs/task7/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/cv0001/eval/`（不入库）、`doc/task7/report.md`、`doc/handoffs/2026-10-30-task7-to-task8.md`、`README.md`、`CLAUDE.md`。
  - 目标：由付林轩审核 `eval_items_draft.csv` 后生成 `eval_items_labeled.csv` 并导入 `task7.eval_label`；运行 `python -m etl_task7.evaluate metrics` 生成 `metrics.json` 与 `metrics.md`；完成 `doc/task7/report.md` 与下一任务交接文档，更新 `checklist.md`、`README.md` 与 `CLAUDE.md`。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）计算并输出正式库 120 项评价指标
  docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate metrics
  ```

  期望（计划）：输出包含 `labeled_total: 120`、`annotators: ["付林轩"]`、`action_accuracy`、`value_accuracy` 与 `conflict_retention`（按层、按字段与总体）的 JSON 并保存 `metrics.md`。
