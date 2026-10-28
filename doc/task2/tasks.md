# 任务 2 开发任务（第一部分：ASIN 采集清单）

状态：T2-00 至 T2-03 全部完成（2026-10-28），已提交到本地分支 `task2-manifest`。爬虫部分由用户完成，届时从 T2-04 续接。

命令在项目根目录直接复制运行。每项 AC 的详细复核命令见 [checklist](checklist.md)，这里只给阶段级检查。

- [x] T2-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：文档检查通过。

  ```bash
  grep -n "^## " doc/task2/spec.md
  ```

  期望：只有 `目标、范围、规则、AC测试、假设` 五行。

- [x] T2-01 清单表、链接构造、构建核心。关联 AC02–AC06。
  - 文件：`sql/101_task2_schema.sql`、`sql/task2_manifest_source.sql`、`src/etl_task2/{urls,manifest}.py`。
  - 完成情况：测试库固定样本构建 6 行，复用与冲突检测正常。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_urls.py tests/integration/test_task2_manifest.py
  ```

  期望：`31 passed`。

- [x] T2-02 Airflow DAG、核对、导出、失败处理。关联 AC01、AC07、AC08。
  - 文件：`src/etl_task2/pipeline.py`、`dags/amazon_products_task2_manifest.py`、`sql/task2_manifest_{checks,export}.sql`、[runbook](runbook.md)。
  - 完成情况：真实 scheduler 在 `etl_test` 跑 6 次（成功、复用、样本未允许、暂时错误重试、提交前故障、全量样本），结果均符合预期。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_failures.py tests/integration/test_task2_dag.py
  ```

  期望：`17 passed`。

- [x] T2-03 正式全量构建与交付。关联 AC09、AC10。
  - 文件：`scripts/check_task2_manifest.py`、交接文档 `doc/handoffs/2026-10-28-task2-part1-to-part2.md`。
  - 完成情况：正式库 253,059 行，全部 `not_collected`，与任务 1 产品汇总逐行一致；整次约 45 s；重跑为 `reused`。

  ```bash
  python3 scripts/check_task2_manifest.py outputs/task2/f1e8b845*/63fca8ef*/t2_full_1/product_manifest.csv outputs/task1/f1e8b845*/full_run_2_rerun/distinct_products.csv
  ```

  期望：退出码 0，`manifest_rows: 253059`，`asin_sequence_equal: true`，`column_mismatches: 0`。
