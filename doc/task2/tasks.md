# 任务 2 开发任务（第一部分：ASIN 采集清单）

状态：T2-00 至 T2-03 全部完成（第一部分结束）。爬虫部分由用户完成，届时在本文件续接 T2-04 起的任务。

下面源码文件相对 `src/etl_task2/`，测试文件相对 `tests/`；其他路径相对项目根目录。测试入口（计划，工作目录：项目根目录）为 `docker compose --profile test run --rm etl-test pytest -q <测试路径>`，各任务的实际执行结果登记在对应条目下。

- [x] T2-00：编写任务 2 第一部分的规格、设计、任务和验收计划。
  - 文件：`doc/task2/spec.md`、`doc/task2/design.md`、`doc/task2/tasks.md`、`doc/task2/checklist.md`；同步 `README.md`、`CLAUDE.md`。
  - 检查：AC00；文档规范、用户选择、范围排除爬虫、目录链接和状态一致。未写业务代码、未建表、未运行 DAG。
- [x] T2-01：建立清单表，实现链接构造与构建核心。2026-10-28 完成。
  - 文件：`sql/101_task2_schema.sql`、`sql/task2_manifest_source.sql`；`__init__.py`、`urls.py`、`manifest.py`；`unit/test_task2_urls.py`、`integration/test_task2_manifest.py`。
  - 检查：AC02–AC06；在 `etl_test` 装入任务 1 固定样本后构建，验证 6 行与 4/2 分布、约束拒绝非法写入、评论时间、重跑复用与冲突报错。命令：`docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_urls.py tests/integration/test_task2_manifest.py`。
  - 实测（2026-10-28，工作目录：项目根目录）：上述命令 30 项通过（单元 13，集成 17）；`docker compose --profile test run --rm etl-test pytest -q tests` 共 226 项通过（任务 1 的 196 项未改动且全部通过）。另实现了 AC01 的抽取前置检查与 AC07 的提交前故障原子性、遗留 running 收尾，Airflow 层部分留待 T2-02。未在正式库建表或构建。
- [x] T2-02：接通 Airflow、核对、导出与失败处理。2026-10-28 完成。
  - 文件：`pipeline.py`、`dags/amazon_products_task2_manifest.py`、`sql/task2_manifest_checks.sql`、`sql/task2_manifest_export.sql`；`integration/test_task2_dag.py`、`integration/test_task2_failures.py`；`scripts/run_dag_e2e.py` 增加 `--dag-id`；`integration/conftest.py` 的会话夹具同时重建 task2 schema；`doc/task2/runbook.md`。
  - 检查：AC01、AC07、AC08；抽取前置检查、事务中断、暂时错误重试、导出内容；经 `compose.e2e.yaml` 由真实 scheduler 触发成功与失败运行（写 `etl_test`）。命令见 [runbook](runbook.md) 第 1、2 节。
  - 实测（2026-10-28，工作目录：项目根目录）：`docker compose --profile test run --rm etl-test pytest -q tests` 共 244 项通过（任务 2 新增 48 项）。切到 `compose.e2e.yaml` 后由真实 scheduler 触发：任务 1 样本 `t2pre_task1_sample` 成功；清单 DAG `t2_ok` 成功（6 行），`t2_rerun` 成功且构建 `reused`，`t2_no_allow` 在 `check_extraction` 失败（try 1，不重试）、下游 `upstream_failed`、`run_gate` 失败，`t2_transient` 的 `build_manifest` 第 1 次暂时错误、第 2 次成功，`t2_before_commit` 的 `build_manifest` 失败（try 1）且审计为 `failed`，`t2_full_fixture`（测试库里的全量固定样本抽取，不加 `allow_sample`）成功。之后已切回正常配置。证据：`outputs/e2e/task2/`（不入库）、`etl_test` 的 `task2.manifest_run`、`airflow_meta.task_instance`。
- [x] T2-03：正式全量构建并整理交付。2026-10-28 完成。
  - 文件：`doc/task2/runbook.md`（补正式运行记录）、`README.md`、`doc/task2/design.md`（补实测资源）、`doc/handoffs/` 新交接文档；生成 `outputs/task2/<extraction_id>/<build_id>/<run_key>/` 运行证据（不入库）。
  - 检查：AC09、AC10；经 Airflow 对 `etl_movies` 中抽取 `f1e8b845…` 构建，核对 253,059 行、252,603/456 分布、全部 `not_collected`，并与 `distinct_products.csv` 逐行比较；全部测试（含任务 1）通过；扫描受跟踪文件无导出数据与口令。
  - 实测（2026-10-28，工作目录：项目根目录）：Airflow 为正常配置（`ETL_DB_NAME=etl_movies`、`ETL_ENV=prod`），按 [runbook](runbook.md) 第 3 节触发 `t2_full_1`：六个任务成功，构建 24.5 s、核对 12.8 s，整次约 45 s；清单 253,059 行，in_valid 252,603/456，全部 `not_collected`，`url_issue` 0；导出 37,359,597 字节。与 `distinct_products.csv` 逐行比较：ASIN 顺序一致、来源五列 0 处不同。同参数 `t2_full_2_rerun` 构建 `reused`、新增 0 行，导出 SHA-256 与第一次相同。任务 1 的表除 `schema_migration` 新增版本 `101` 一行外无写入。期间发现 `manifest_run.finished_at` 用了事务开始时间、耗时被低估，已改为 `clock_timestamp()`，244 项测试复跑通过；两条正式审计的 `finished_at` 保留原值不改写。证据：`outputs/task2/f1e8b845…/63fca8ef…/`、`outputs/task2-run/`（均不入库）。

规则和验收定义维护于 spec，设计细节维护于 design，实际证据维护于 checklist。
