# 任务 6 开发任务（补全上映信息）

状态：2026-10-30 编写计划；同日按用户要求加入人物共现反查、评论年份提及、被占用 `tconst` 的线索与商品发售日期上界。同日完成 T6-01 至 T6-07 初版实现后经独立审查（见 [review](review.md)），针对审查发现的 12 项问题新增修复任务 T6-08 至 T6-11（原导入标注任务顺延为 T6-12）与回归验收 AC21–AC23；待用户审批后执行修复。分支 `task6-release-info`（从 `main` 的 `1625ad9` 建立）。每完成一个任务本地提交一次；不推送。

命令在项目根目录直接复制运行。T6-01 至 T6-06 的命令已实际运行核对；T6-08 至 T6-12 的命令标注为**（计划）**：对应修复尚未实现，期望值在实现后按实际运行结果改写。每项 AC 的复核命令见 [checklist](checklist.md)。

- [ ] T6-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：2026-10-30 AI 文档检查通过（五个板块、R01–R39、AC00–AC23 一一对应，每条规则都有 AC，审查修复任务 T6-08 至 T6-12 已纳入）；待用户审批。

  ```bash
  grep -n "^## " doc/task6/spec.md
  ```

  期望：只有 `目标、范围、规则、AC测试、假设` 五行。

- [x] T6-01 迁移 501、运行审计与锁、IMDb 关联、线索（含被占用 `tconst`）与商品标题年份三类候选。关联 AC01、AC03、AC04、AC07。
  - 文件：`sql/501_task6_schema.sql`、`src/etl_task6/{run,rules,candidates}.py`、`tests/unit/test_task6_rules.py`、`tests/integration/{_task6_helpers,test_task6_candidates}.py`、`tests/fixtures/task6/`。
  - 完成情况：2026-10-30 完成。迁移 501（含 `release_selection`、`release_version` 全部表结构）、输入键与阶段键、运行审计、咨询锁、三类候选与线索处理记录；夹具世界（任务 4 匹配用 t4r2，与正式库 map0002 同规则）与预期值说明。单元 14 项 + 集成 13 项通过；全量 718 passed（任务 1–5 的 691 项不变）。实现中的修订见 design 文末（`release_candidate` 补 `confidence`/`rule_version` 列、`number_guard` 允许 NULL、线索阶段键动态取人物映射版本的规则版本、评论时间与共现结果的幂等重写）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_candidates.py
  ```

  期望：`27 passed`。

- [x] T6-02 人物共现反查（评分、标题与编号条件、唯一性、`shared_tconst`）与评论年份提及（抽取、计票、消歧）。关联 AC05、AC06。
  - 文件：`src/etl_task6/{cooccur,review_years}.py`、`tests/integration/test_task6_cooccur.py`、`tests/fixtures/task6/`（IMDb 演职员与人物、含正文的评论）。
  - 完成情况：2026-10-30 完成。评分记录（含未通过的条件）、标题与编号条件、唯一性、评论年份消歧与 `shared_tconst`；提及模式 t6m1 与计票。集成 11 项通过；全量 729 passed。修复 `resolve` 未预取共现命中 tconst 的作品行（年份取不到、消歧失效），记入 design 修订第 7 条。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_cooccur.py
  ```

  期望：`11 passed`。

- [x] T6-03 择优、状态与缺失原因、分歧质量问题、上映信息版本与版本差异，日期语义分离的约束。关联 AC02、AC08。
  - 文件：`src/etl_task6/select.py`、`sql/501_task6_schema.sql`（`release_selection`、`release_version`）、`tests/integration/test_task6_select.py`。
  - 完成情况：2026-10-30 完成。择优固定顺序与状态、缺失原因优先级、相差 1 年与 `year_disagreement`/`tconst_disagreement`、`release_selection` 的日期语义约束（`release_date` 恒 NULL、`precision` 恒 `year`、`regional_release_missing_reason`）；版本差异的查询与断言在 T6-05 的 runs 测试（AC13）。集成 10 项通过；全量 739 passed。顺带修复 `product_dates` 页面标签键缺 section 导致 14 条断言误判 unknown_date_label（表空），记入 design 修订第 8 条。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_select.py
  ```

  期望：`10 passed`。

- [x] T6-04 商品发售日期与标记、商品层与作品层最早评论、评论与发售日期两种上界证据、推断上界、预售合集跨版本计数。关联 AC09、AC10、AC11、AC12。
  - 文件：`src/etl_task6/{product_dates,reviews,bounds}.py`、`tests/integration/test_task6_{product_dates,reviews,bounds}.py`。
  - 完成情况：2026-10-30 完成。商品发售日期分类与可疑标记、商品层与作品层最早评论计算、两种上界证据评估、推断上界与上界早于上映年份质量问题；预售、合集与跨版本数据面准备。集成 21 项通过；全量 760 passed。修复作品层第二早评论跨商品候选排序、上界评估覆盖仅有上架日期的作品，以及夹具隔离评论识别，记入 design 修订第 9–11 条。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_product_dates.py tests/integration/test_task6_reviews.py tests/integration/test_task6_bounds.py
  ```

  期望：`21 passed`。

- [x] T6-05 分步骤运行与分批恢复、重跑复用、报告与实例、两个查询、只读核对、DAG、真实 scheduler 端到端、运行手册。关联 AC13、AC14、AC15、AC16。
  - 文件：`src/etl_task6/{runner,pipeline,report,query,check}.py`、`dags/amazon_movies_task6_release.py`、`scripts/seed_task6_e2e.py`、`tests/integration/test_task6_{runs,dag,report}.py`、`doc/task6/runbook.md`。
  - 完成情况：2026-10-30 完成。分批中断与重跑恢复（AC14）、并发锁、重跑复用与规则/映射版本切换（AC13）、DAG 依赖与门禁（AC15）、报告导出与实例查询（AC16）全部通过；端到端使用 `compose.e2e.yaml` 在真实 scheduler 上验证成功（`t6_e2e_ok1` 成功、`t6_e2e_rerun1` 复用、`t6_e2e_missing1` 阻断），并在完成后切回正常配置。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py tests/integration/test_task6_dag.py tests/integration/test_task6_report.py
  ```

  期望：`17 passed, 8 warnings`。真实 scheduler 端到端均符合预期。

- [x] T6-06 评价工具：开发池冻结、七层评价表与 AI 草稿模板导出、标注导入、指标。关联 AC17。
  - 文件：`src/etl_task6/evaluate.py`、`tests/integration/test_task6_evaluate.py`、`doc/task6/runbook.md`（评价一节）。
  - 完成情况：2026-10-30 完成。开发池冻结（R36）、七层评价表与 AI 草稿导出（R37）、人工标注校验导入（R39）、完全一致/相差1年年份准确率、tconst 正确率、上界成立率与缺失可查到率计算（R38）全部通过。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py
  ```

  期望：`4 passed`。全量 781 passed。

- [x] T6-07 正式库初版运行：经 Airflow 全量运行，只读核对，导出报告与评价表初稿；独立审查发现的问题由 T6-08 至 T6-11 修复后重跑。关联 AC18、AC20。
  - 文件：`doc/task6/design.md`（资源实测、审查发现与修复设计）、`doc/task6/review.md`；`outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/`（不入库）。
  - 完成情况：2026-10-30 完成初版运行（正式库 `8ec587275f974c1489b7f903a1976a98` 耗时 85.32s，`713a09bff89f416db09652bb3a78c859` 复用）。经独立审查（`doc/task6/review.md`）发现共现反查、报告统计、评价分层与 AI 草稿循环等问题，需在 T6-08 至 T6-10 修复代码后于 T6-11 清空 `task6` 重跑。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task6.check > /dev/null; echo "exit=$?"
  ```

  期望：`exit=0`（输出 JSON 的 `"problems": []`）。

  ```bash
  docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = 'rv0001' GROUP BY status ORDER BY status"
  ```

  期望：初版五行 `cooccur_matched|690`、`hint_matched|293`、`missing|3155`、`page_reported|48`、`verified|4473`，合计 8,659（T6-11 重跑后更新为修复后实测值）。

- [x] T6-08 修复人物共现反查与运行阶段键（审查 ISSUE-04、ISSUE-05、ISSUE-08）。关联 AC05、AC13、AC21。
  - 文件：`src/etl_task6/{rules,run,cooccur}.py`、`tests/unit/test_task6_rules.py`、`tests/integration/test_task6_{cooccur,runs}.py`。
  - 目标：（1）`cooccur._title_keys_of` 改为按 `primary → original → aka` 排序，`score_movies` 改为寻找同时满足 `cooccur_title_ok` 与 `cooccur_number_guard` 的标题键，不因排在前面的无编号 `aka` 提前 `break` 误杀续集；（2）`rules.cooccur_title_ok` 增加单实词别名保护，禁止单实词标题键跨作品子集匹配含其他额外核心实词的长商品标题；（3）`run.Inputs.stage_key("cooccur")` 纳入 `RuleSet` 共现评分参数（导演分、演员分、最低分），使共现规则变更触发阶段重算而非误判 `reused`；（4）补充对应单元与集成测试。
  - 完成情况：2026-10-30 完成。`_title_keys_of` 固定 `primary → original → aka` 优先序，`score_movies` 遍历寻找同时满足标题与编号条件的标题键；`cooccur_title_ok` 对单实词标题键仅允许与单实词相等匹配、保留 ≥2 个实词且字符数 ≥6 按子集匹配；`stage_key("cooccur")` 纳入 `RuleSet` 共现评分参数。新增 3 项 AC21 单元与集成测试通过；全量 784 passed（任务 1–5 的 691 项不减少）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_cooccur.py tests/integration/test_task6_runs.py -k "ac21 or cooccur or stage_key"
  ```

  期望：`16 passed, 18 deselected`。

- [ ] T6-09 修复报告统计、版本差异、只读查询、核对脚本与 DAG 弃用警告（审查 ISSUE-06、ISSUE-09、ISSUE-10、ISSUE-12）。关联 AC13、AC15、AC16、AC22。
  - 文件：`src/etl_task6/{report,query,check}.py`、`dags/amazon_movies_task6_release.py`、`tests/integration/test_task6_{report,runs,dag}.py`。
  - 目标：（1）`report.py` 修正 `source_ref->>'title_type'`、独立 `COUNT(*)` 统计 `candidate_shared_count`（不被 `LIMIT 20` 截断）、校验真实 `run_id` 并在按版本回退时限定 `status = 'succeeded'`、各阶段表查询补齐当前版本的 `stage_key`/`parser_version` 过滤、`diff_versions` 改为 `FULL OUTER JOIN` 覆盖跨版本增删作品与上界；（2）`query.py` 的 `query_movie` 补充商品层 `product_review_time` 与 `cooccur_score` 标题匹配证据字段，并按 `stage_key`/`parser_version` 过滤；（3）`check.py` 移除 `release_year::text = bound_time::text` 永假条件，改为核查候选引用一致性与 `cooccur_score.passed = true`；（4）`dags/amazon_movies_task6_release.py` 优先从 `airflow.sdk.exceptions` 导入 `AirflowFailException` 消除弃用警告；（5）补充对应集成测试。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_report.py tests/integration/test_task6_runs.py tests/integration/test_task6_dag.py
  ```

  期望（计划）：全部通过，且 `test_task6_dag.py` 中来自 `amazon_movies_task6_release.py` 的弃用警告消除。

- [ ] T6-10 修复评价分层键名、盲测导出与非循环 AI 预标注草稿（审查 ISSUE-01、ISSUE-02、ISSUE-03、ISSUE-11）。关联 AC17、AC23。
  - 文件：`src/etl_task6/evaluate.py`、`tests/integration/test_task6_evaluate.py`、`doc/task6/runbook.md`。
  - 目标：（1）`_classify_movie_stratum` 改为读取 `source_ref.get("title_type")`、`disagreement.get("year_diff_1")`、`disagreement.get("year_disagreement")`、`disagreement.get("tconst_disagreement")`，确保 `tvMovie`/`video`/分歧作品归入 `E2`；（2）`_fetch_product_details` 改为从 `task4.field_assertion` 读取商品原始标题（保留括号年份供 `E5` 核查），`export_eval_set` 在抽样后按 `md5(movie_id)` 全局打乱再分配 `it_0001`–`it_0120` 消除行号对分层与状态的泄露；（3）重写 `_generate_ai_draft`，严禁读取 `system_year`/`status`/`bound_year`，仅依据商品原始标题、演职员与 `imdb` 公开表独立比对，严禁用商品发售日、评论时间或推断上界填充 `ai_true_year`；（4）补充 `E1/E2` 分层、配额补足、盲测导出、AI 草稿非循环性及 `by_stratum`/`by_source`/`bounds_by_evidence` 全量指标测试。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py
  ```

  期望（计划）：新增 AC23 与原有评价测试全部通过。

- [ ] T6-11 正式库清空 `task6` 重跑：先冻结开发池，经 Airflow 全量运行与复用验证，只读核对，重导报告与非循环评价集草稿，修正文档失实数据（审查 ISSUE-07）。关联 AC18、AC20。
  - 文件：`doc/task6/{design,report,tasks,checklist}.md`；`outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/`（不入库）。
  - 目标：（1）清空正式库 `task6` 各表数据，核对清空前后任务 1–5 表行数不变；（2）在全量运行前先执行 `evaluate freeze` 冻结 `task6.dev_pool`（1,732 部作品）；（3）经 Airflow 全量运行生成 `rv0001`（在开发池核查效果并记录到 `design.md`“规则调整记录”），再同参重跑一次验证 `reused`；（4）运行 `etl_task6.check` 确认 `problems: []`，用真实 `run_id` 导出报告与实例，导出 120 部盲测评价表 `eval_items.csv` 与非循环 AI 草稿 `eval_items_draft.csv`（严禁将 DVD/VHS 发售日或推断上界填入 `missing` 作品的 `ai_true_year`）；（5）根据正式库实测结果与 `pytest` 实测输出修正 `doc/task6/{design,report,tasks,checklist}.md` 中的运行 ID、分阶段耗时、测试数、AC 表与缺失原因说明。
  - 完成情况：未执行。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task6.check
  ```

  期望（计划）：退出码 0，JSON 中 `"problems": []` 且 `"passed": true`。

  ```bash
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT status, run_id, release_version_id FROM task6.run ORDER BY started_at;"
  ```

  期望（计划）：两行，`succeeded|<真实_run_id_1>|rv0001` 与 `reused|<真实_run_id_2>|rv0001`，且 `dev_pool.frozen_at` 早于首次运行完成时间。

- [ ] T6-12 导入标注（120 部作品）并报告指标；写任务 6 → 任务 7 交接文档。关联 AC19。
  - 文件：`outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/eval/`（`eval_items_labeled.csv`、`metrics.json`、`metrics.md`；不入库）、`doc/task6/report.md`、`doc/handoffs/<日期>-task6-to-task7.md`（日期以实际为准）。
  - 完成情况：未执行；需在 T6-11 重新生成非循环 AI 草稿并经付林轩核查后再导入。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task6.evaluate metrics
  ```

  期望（计划）：输出年份准确率（按层、来源与总体）、`tconst` 正确率（按来源）、缺失可查到率、上界成立率（按证据类型）与不确定数，标注人为付林轩。
