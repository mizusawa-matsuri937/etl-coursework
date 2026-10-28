# 交接：任务 2 第一部分（采集清单）→ 第二部分（爬虫）

日期：2026-10-28。写给接手任务 2 爬虫部分的人（用户本人，或协助审阅的 agent）。第一部分 T2-00 至 T2-03 已完成，提交在本地分支 `task2-manifest`（提交 `30ce205`，未推送、未合并到 `main`）。规则、设计与证据以 `doc/task2/` 为准，这里只讲接手所需的事实。

## 1. 先做什么

1. 读 `CLAUDE.md`、`README.md`，再读本文。
2. 读 `doc/task2/spec.md`（R01–R15、AC00–AC10）与 `doc/task2/design.md` 的“爬虫接口约定”。
3. 爬虫的规则在 `doc/task2/spec.md` 续接：规则从 R16、AC 从 AC11、任务从 T2-04 起；不改 R01–R15 的含义。作业对爬虫的要求见作业说明“任务2 产品页面采集”与第十部分。

## 2. 第一部分完成了什么

从任务 1 全量抽取生成产品采集清单：每个 observed ASIN 一行，候选链接 `https://www.amazon.com/dp/<ASIN>`（模板版本 `dp-v1`），初始状态全部 `not_collected`。由 Airflow DAG `amazon_products_task2_manifest` 生成、核对、导出，单事务构建，重跑只复用、不覆盖爬虫写入的列。未发出任何网络请求。

## 3. 结果在哪里

**数据库**（容器 `postgres`，库 `etl_movies`，schema `task2`）

- 抽取 ID：`f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`；build ID：`63fca8ef9ff264befb248a27f580de3beb972adc916a8009b9924f60328768a9`。
- `task2.product_manifest`：253,059 行，主键 `(extraction_id, asin)`，含索引 132 MB。
- `task2.manifest_build`、`task2.manifest_run`：构建定义与审计（`t2_full_1` succeeded、`t2_full_2_rerun` reused）。

**文件**（`outputs/` 被忽略，GitHub 上没有）

- `outputs/task2/f1e8b845…/63fca8ef…/t2_full_1/`：`product_manifest.csv`（37,359,597 字节，SHA-256 `3318127cb09b48342d3f5616b9c8067a15bcd034b44557aa9ca5cffa1e5df504`）、`summary.json`、`report.md`；`t2_full_2_rerun/` 同内容。
- `outputs/task2-run/`：正式运行日志、与任务 1 `distinct_products.csv` 的逐行比较结果、任务 1 表写入计数前后对比。
- `outputs/e2e/task2/`：测试库端到端运行的输出。

## 4. 关键数字

| 指标 | 数值 |
|---|---|
| 清单行数 | 253,059（＝任务 1 observed 产品数） |
| in_valid_records 真 / 假 | 252,603 / 456 |
| 状态 | not_collected 253,059，其余 0 |
| url_issue | 0（ASIN 211,293 个 `B` 开头、41,766 个数字型） |
| 评论时间范围（有效记录） | 1997-08-20 至 2012-10-25；456 个仅隔离 ASIN 为 NULL |
| 正式 DAG 耗时 | 约 45 s（构建 24.5 s、核对 12.8 s） |

## 5. 给爬虫的接口约定（别推翻）

- 读：`WHERE extraction_id = '<f1e8…>' AND status = 'not_collected' AND original_url IS NOT NULL`，按 `asin` 分批；索引 `(extraction_id, status, asin)`。
- 写：只改爬虫列 `status、final_url、http_status、attempt_count、fetched_at、snapshot_path、content_hash、status_reason、updated_at`；不改 `original_url` 等构建列，最终跳转地址写 `final_url`。
- 状态只有 6 个值：`success、not_found、access_restricted、timeout、parse_failed、not_collected`。新增值属于规则变化，先改 spec 和约束。
- 置 `success` 时五项证据（final_url、http_status、fetched_at、snapshot_path、content_hash）缺一项数据库都会拒绝。HTTP 200 的验证页、错误页算不算成功，由爬虫规则判定（作业要求：不得标成功）。
- 逐次请求明细、重定向链需要另建 `task2` 下的表，用 `(extraction_id, asin)` 外键关联，不往清单行里塞历史。
- 456 个仅隔离 ASIN 按用户决定照常采集。
- 重新跑清单 DAG 是安全的：已有行只比对、不更新；构建列与来源不一致会 `ManifestConflict` 并整体回滚。

## 6. 环境与用法

- 栈与任务 1 相同，目前是正常配置（`ETL_ENV=prod`，写 `etl_movies`）。没有新增服务或依赖。
- 命令见 `doc/task2/runbook.md`：测试（第 1 节）、测试库端到端（第 2 节，`compose.e2e.yaml`，结束后 `unset COMPOSE_FILE; docker compose up -d`）、正式构建（第 3 节）。
- `scripts/run_dag_e2e.py` 新增 `--dag-id`，默认仍是任务 1 的 DAG。
- 测试：`docker compose --profile test run --rm etl-test pytest -q tests`，共 244 项（任务 1 196＋任务 2 48）。

## 7. 踩过的坑

- `manifest_run.finished_at` 起初用 `now()`（事务开始时间），审计耗时接近 0；已改 `clock_timestamp()`。正式库 `t2_full_1`、`t2_full_2_rerun` 两条审计保留原值，耗时看 summary.json 的 `elapsed_seconds`。
- 集成测试会话开始时会删除并重建 `etl_test` 的 `public` 与 `task2`：`task2` 的表外键引用 `public.extraction`，只重建 `public` 会留下孤立的 `task2`。
- 测试库端到端依赖一个已完成的样本抽取：先跑任务 1 的固定样本 DAG，再用 `allow_sample=true` 构建清单。
- `task2` 在 `public.schema_migration` 登记版本 `101`，这是任务 1 表里唯一的新增行。

## 8. 遗留事项

- 本阶段改动已提交到本地分支 `task2-manifest`，未推送、未开 PR；包含：新增 `src/etl_task2/`、`dags/amazon_products_task2_manifest.py`、`sql/101_task2_schema.sql`、`sql/task2_manifest_*.sql`、`tests/*/test_task2_*.py`、`doc/task2/` 四份计划与 runbook、本交接；修改 `README.md`、`CLAUDE.md`、`scripts/run_dag_e2e.py`、`tests/integration/conftest.py`。推送与合并须由用户要求。
- 2026-10-28 用户已按 `doc/task2/checklist.md` 的命令人工复核，测试全部通过。
- 业务库仍有任务 1 的样本抽取 `39019c6f…`，任务 2 未用它；是否删除仍由用户决定。
- 爬虫部分（robots.txt、限速、缓存、有上限重试、快照、成功判定、重定向与模板变化证据）尚未规划。

## 9. 文件索引

| 需要什么 | 看哪里 |
|---|---|
| 任务 2 规则与验收 | `doc/task2/spec.md`、`doc/task2/checklist.md` |
| 表结构、数据流、爬虫接口约定 | `doc/task2/design.md`、`sql/101_task2_schema.sql` |
| 运行与恢复 | `doc/task2/runbook.md` |
| 链接规则 | `src/etl_task2/urls.py` |
| 构建、复用与冲突 | `src/etl_task2/manifest.py` |
| DAG 步骤与报告 | `src/etl_task2/pipeline.py`、`dags/amazon_products_task2_manifest.py` |
| 任务 1 交接（上游数据） | `doc/handoffs/2026-10-26-task1-to-task2.md` |
