# 任务 2 开发任务（ASIN 采集清单与产品页面爬虫）

状态：第一部分 T2-00 至 T2-03 完成（2026-10-28，已提交到本地分支 `task2-manifest`）。第二部分 T2-04、T2-05 完成（2026-10-28 至 29，由 Claude 实现，已提交到本地分支 `task2-manifest`）；T2-06（接入 Airflow）、T2-07（跳转链与模板证据）于 2026-10-29 完成并提交到同一分支。第二部分于 2026-10-29 经用户按 checklist 人工复核通过。

命令在项目根目录直接复制运行。每项 AC 的详细复核命令见 [checklist](checklist.md)，这里只给阶段级检查。

- [x] T2-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：第一部分文档检查通过；2026-10-29 补写爬虫规则 R16–R31、AC11–AC18 后复查通过。

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

- [x] T2-04 爬虫：限速、多出口、会话、状态判定、快照与写回、续跑。关联 AC11–AC16、AC18。
  - 文件：`src/etl_task2/crawl/crawler.py`、`sql/102_task2_crawl.sql`、`tests/unit/test_task2_crawl.py`、`tests/integration/test_task2_crawl.py`、`compose.yaml`（`crawler` 服务）、`.env.example`。
  - 完成情况：2026-10-29 测试 24 项通过；全部测试 268 项通过。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_crawl.py tests/integration/test_task2_crawl.py
  ```

  期望：`32 passed`（2026-10-29 补 T2-07 后由 24 项增至 32 项）。

- [x] T2-05 多出口与正式爬取。关联 AC17。
  - 文件：`scripts/crawl_mihomo.py`、`src/etl_task2/crawl/check.py`、[runbook](runbook.md) 第 6 节。
  - 完成情况：用户要求停止时（2026-10-29），已处理 29,126 个 ASIN，其中 success 22,321 个，其余 223,933 个保持 `not_collected`；只读核对全部通过，28,913 个快照哈希一致。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task2.crawl.check
  ```

  期望：退出码 0，`status_sum_equals_rows: true`，`snapshot_hash_mismatch: 0`（约 1 分钟）。

- [x] T2-06 爬虫接入 Airflow DAG `amazon_products_task2_crawl`。关联 AC20。
  - 文件：`src/etl_task2/crawl/pipeline.py`、`dags/amazon_products_task2_crawl.py`、`tests/integration/test_task2_crawl_dag.py`、`compose.yaml`（Airflow 服务的出口与句柄上限）。
  - 完成情况：2026-10-29 测试 9 项通过；真实 scheduler 在 `etl_test` 跑成功、重跑、不存在的抽取 3 次，结果符合预期。正式库未经 DAG 发真实请求（用户 2026-10-29 决定不在正式库经 DAG 真实爬取，以命令行全量爬取的现有结果为准）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task2_crawl_dag.py
  ```

  期望：`9 passed`。

- [x] T2-07 跳转链与页面模板证据（迁移 `103`）、正式库模板回填。关联 AC19、AC21。
  - 文件：`sql/103_task2_crawl_evidence.sql`、`src/etl_task2/crawl/{crawler,check,backfill}.py`。
  - 完成情况：2026-10-29 正式库回填 28,913 个请求的模板，没有缺失的快照；核对通过；成功页有两种模板（20,828 / 1,493）。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task2.crawl.backfill
  ```

  期望：退出码 0，`"candidates": 0, "filled": 0`（已回填完，再跑不写任何行）。
