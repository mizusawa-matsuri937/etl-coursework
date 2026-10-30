# 任务 6 开发任务（补全上映信息）

状态：2026-10-30 编写计划，待用户审批；实现未开始。分支 `task6-release-info`（从 `main` 的 `1625ad9` 建立）。审批通过后由 Sonnet（high effort）子对话分批实现，每完成一个任务本地提交一次，主对话核对结果；不推送。

命令在项目根目录直接复制运行。除 T6-00 外，下列命令都是**（计划）**：对应代码尚未实现，期望值在实现后按实际运行结果改写。每项 AC 的复核命令见 [checklist](checklist.md)。

- [ ] T6-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：2026-10-30 AI 文档检查通过（五个板块、R01–R35、AC00–AC18 一一对应，每条规则都有 AC）；待用户审批。

  ```bash
  grep -n "^## " doc/task6/spec.md
  ```

  期望：只有 `目标、范围、规则、AC测试、假设` 五行。

- [ ] T6-01 迁移 501、运行审计与锁、三类年份候选与线索处理。关联 AC01、AC03、AC04、AC05。
  - 文件：`sql/501_task6_schema.sql`、`src/etl_task6/{run,rules,candidates}.py`、`tests/unit/test_task6_rules.py`、`tests/integration/{_task6_helpers,test_task6_candidates}.py`、`tests/fixtures/task6/`。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_candidates.py
  ```

  期望（计划）：全部通过，数量在实现后填写。

- [ ] T6-02 择优、状态与缺失原因、分歧质量问题、上映信息版本与版本差异，日期语义分离的约束。关联 AC02、AC06。
  - 文件：`src/etl_task6/select.py`、`sql/501_task6_schema.sql`（`release_selection`、`release_version`）、`tests/integration/test_task6_select.py`。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_select.py
  ```

  期望（计划）：全部通过。

- [ ] T6-03 商品发售日期与标记、商品层与作品层最早评论、上界评估与推断上界、预售合集跨版本计数。关联 AC07、AC08、AC09、AC10。
  - 文件：`src/etl_task6/{product_dates,reviews,bounds}.py`、`tests/integration/test_task6_{product_dates,reviews,bounds}.py`。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_product_dates.py tests/integration/test_task6_reviews.py tests/integration/test_task6_bounds.py
  ```

  期望（计划）：全部通过。

- [ ] T6-04 分步骤运行与分批恢复、重跑复用、报告与实例、两个查询、只读核对、DAG、真实 scheduler 端到端、运行手册。关联 AC11、AC12、AC13、AC14。
  - 文件：`src/etl_task6/{runner,pipeline,report,query,check}.py`、`dags/amazon_movies_task6_release.py`、`scripts/seed_task6_e2e.py`、`tests/integration/test_task6_{runs,dag,report}.py`、`doc/task6/runbook.md`。
  - 完成情况：未执行。端到端用 `compose.e2e.yaml`，做完必须 `docker compose up -d` 回到正常配置；不在正式库触发 DAG。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py tests/integration/test_task6_dag.py tests/integration/test_task6_report.py
  ```

  期望（计划）：全部通过；端到端步骤与结果写入 runbook。

- [ ] T6-05 评价工具：开发池冻结、评价表与 AI 草稿模板导出、标注导入、指标。关联 AC15。
  - 文件：`src/etl_task6/evaluate.py`、`tests/integration/test_task6_evaluate.py`、`doc/task6/runbook.md`（评价一节）。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py
  ```

  期望（计划）：全部通过。

- [ ] T6-06 正式库：经 Airflow 全量运行，冻结开发池并在开发池上确认默认值，冻结规则版本，只读核对，导出报告、实例与查询结果，导出评价表并生成 AI 预标注草稿；资源实测记入 design。关联 AC16、AC18。
  - 文件：`doc/task6/design.md`（资源实测、规则调整记录）、README；`outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/`（不入库）。
  - 完成情况：未执行。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task6.check > /dev/null; echo "exit=$?"
  ```

  期望（计划）：`exit=0`（输出 JSON 的 `"problems": []`）。

  ```bash
  docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = 'rv0001' GROUP BY status ORDER BY status"
  ```

  期望（计划）：四行 `hint_matched`、`missing`、`page_reported`、`verified`，合计 8,659（规划估算约 253 / 3,873 / 60 / 4,473）。

- [ ] T6-07 导入标注（120 部作品）并报告指标；写任务 6 → 任务 7 交接文档。关联 AC17。
  - 文件：`outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/eval/`（`eval_items_labeled.csv`、`metrics.json`、`metrics.md`；不入库）、`doc/handoffs/2026-10-30-task6-to-task7.md`（日期以实际为准）。
  - 完成情况：未执行；需要付林轩核查 AI 草稿后再导入。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task6.evaluate metrics
  ```

  期望（计划）：输出年份准确率、`tconst` 正确率、缺失可查到率、上界成立率与不确定数，标注人为付林轩。
