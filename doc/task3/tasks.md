# 任务 3 开发任务（电影页面筛选）

状态：2026-10-29 编写计划；同日用户同意假设按建议执行并授权连续完成，T3-00 至 T3-05 由 Claude 实现并通过 AI 验证；同日用户按 checklist 复核全部通过，并人工抽检了抽查表（口头结论“已分类的基本没有问题”，未逐条标注导入）。分支 `task3-movie-filter`。

命令在项目根目录直接复制运行。每项 AC 的详细复核命令见 [checklist](checklist.md)，运行与恢复见 [runbook](runbook.md)。

- [x] T3-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：2026-10-29 AI 文档检查通过；实现中按页面实例修订 R11、R14、R16 与 AC04，已同步 design。

  ```bash
  grep -n "^## " doc/task3/spec.md
  ```

  期望：只有 `目标、范围、规则、AC测试、假设` 五行。

- [x] T3-01 表结构、快照读取与字段抽取。关联 AC02、AC03。
  - 文件：`sql/201_task3_schema.sql`、`src/etl_task3/extract.py`、`tests/unit/test_task3_extract.py`、`tests/fixtures/task3/`。
  - 完成情况：2026-10-29 通过；抽取在 4 个真实快照上人工比对过字段。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task3_extract.py
  ```

  期望：`6 passed`。

- [x] T3-02 输入分组、分类规则与证据。关联 AC01、AC04、AC05、AC06。
  - 文件：`sql/task3_input_groups.sql`、`src/etl_task3/{rules,classify}.py`、`tests/unit/test_task3_rules.py`、`tests/integration/{_task3_helpers,test_task3_classify}.py`。
  - 完成情况：2026-10-29 通过。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task3_rules.py tests/integration/test_task3_classify.py
  ```

  期望：`34 passed`（t3r2 起；t3r1 时 31）。

- [x] T3-03 分批与幂等、规则版本差异、报告、Airflow DAG。关联 AC07、AC08、AC09、AC10。
  - 文件：`src/etl_task3/{report,pipeline,check}.py`、`sql/task3_classification_export.sql`、`dags/amazon_products_task3_classify.py`、`tests/integration/test_task3_{dag,report}.py`、[runbook](runbook.md)。
  - 完成情况：2026-10-29 通过；真实 scheduler 在 `etl_test` 跑成功、重跑、暂时错误、不存在的抽取 4 次，结果符合预期（第一次成功运行暴露“无信号页面没有证据”，修正后重跑通过）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_dag.py tests/integration/test_task3_report.py
  ```

  期望：`11 passed`。

- [x] T3-04 正式库全量运行与只读核对。关联 AC11、AC13。
  - 文件：`src/etl_task3/check.py`，README、交接文档。
  - 完成情况：2026-10-29 经 Airflow `t3_full_1` 全量运行约 5.5 分钟，分类 253,059 行（电影 8,808、待确认 239,274），快照哈希全部一致；重跑 `t3_full_2_rerun` 为 reused，导出哈希相同；上游表无写入。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task3.check
  ```

  期望：退出码 0，`"problems": []`，`classification_rows` 与 `manifest_rows` 均为 `253059`。

- [x] T3-05 人工抽查：导出抽查表。关联 AC12。
  - 文件：`src/etl_task3/report.py`（`review-export`、`review-import`）。
  - 完成情况：2026-10-29 已导出抽查表；用户同日人工抽检，口头结论“已分类的基本没有问题”，未逐条标注导入，AC12 保持未勾选。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task3.report review-export
  ```

  期望（t3r2）：退出码 0，`"rows": 115`，`by_stratum` 为 `non_movie` 15、其余五类各 20（t3r1 时 120）。

- [x] T3-06 规则修订 t3r2：修复载体列表误判合集、占位署名算作演员、非视频首级影片判为非电影。关联 AC00、AC04、AC11、AC12。
  - 文件：`src/etl_task3/{__init__,rules}.py`、`tests/fixtures/task3/pages.py`、spec R11、R14、R15、AC04，design“规则版本 t3r2”。
  - 完成情况：2026-10-29 通过（AI 验证，用户尚未复核）；正式库 `t3_full_r2_1` 35 秒只重算分类，212 个产品变化，核对通过；336 项测试通过。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task3.classify diff --from t3r1 --to t3r2
  ```

  期望：退出码 0，开头为 `"changed": 212`。

