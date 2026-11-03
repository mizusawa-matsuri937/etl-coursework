# 任务 8 运行手册（评论清洗与重复识别）

命令在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 运行（macOS 默认 zsh 可直接复制）。任务 8 只读 `public`、`task2`–`task7`，结果写入 schema `task8`（迁移 `701`），导出写入 `outputs/task8/`（不入库、不入仓库）。输入是抽取 `f1e8b845…`、任务 3 规则 `t3r2`、任务 7 冲突消解版本 `cv0003`。迁移 `701` 由 DAG 的 `check_inputs` 步骤、`etl_task8.runner` 或 `evaluate freeze` 自动应用，**只在正式运行（T8-08）时才应用到 `etl_movies`**；开发与演练阶段不要在 `etl_movies` 上触发任何写操作。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task8_rules.py tests/integration/test_task8_clean.py tests/integration/test_task8_family.py tests/integration/test_task8_dedup.py tests/integration/test_task8_attribution.py tests/integration/test_task8_runs.py tests/integration/test_task8_report.py tests/integration/test_task8_dag.py tests/integration/test_task8_evaluate.py
```

集成测试会话开始时会清空 `etl_test`；下一节的端到端数据要在测试之后从头准备。

## 2. 端到端验证（真实 scheduler，写 etl_test）

DAG `amazon_reviews_task8_dedup` 在真实 scheduler 上跑四种情况：成功、同输入重跑（`reused`）、上游 `cv` 不存在、`verify` 失败后重跑。合成数据来自测试夹具（`_task8_helpers.small_world`，经任务 1 装载器装入，任务 2、3、7 上游按正式库同形写入）。**只用 `etl_test`，绝不在 `etl_movies` 上触发。**

1. 准备 `etl_test`（会清空 `etl_test`），输出一行 JSON，`extraction_id` 固定为下面的值：

```bash
docker compose --profile test run --rm -T etl-test python - < scripts/seed_task8_e2e.py
```

期望：`{"extraction_id": "6214025170700962bf5d7e8cf164f9c66c38b3090f8eb20f327edb5740155e61", "records": 47, "products": 14}`（夹具固定，每次相同）。

2. 切换到端到端配置（Airflow 写 `etl_test` 与 `outputs/e2e/`，允许故障注入，重试间隔 2 秒）：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
```

3. 成功、同输入重跑、上游缺失、`verify` 失败后重跑的命令与实测结果见 `doc/task8/tasks.md` T8-05（命令中的抽取 ID 为第 1 步输出）。

4. 回到正常配置并确认服务正常（必须执行）：

```bash
docker compose up -d
```

```bash
docker compose exec -T airflow-scheduler env | grep -E "^ETL_(ENV|DB_NAME|OUTPUT_DIR|RETRY_DELAY_SECONDS)="
```

期望：`ETL_ENV=prod`、`ETL_DB_NAME=etl_movies`、`ETL_OUTPUT_DIR=/opt/etl/outputs`、`ETL_RETRY_DELAY_SECONDS=60`。

```bash
docker compose exec -T airflow-scheduler airflow dags list-import-errors
```

期望：`No data found`。

## 3. 业务库副本演练（T8-07，R41）

1. 停止会连接 `etl_movies` 的 Airflow 服务，建副本（约 2 分钟），立即恢复：

```bash
docker compose stop airflow-scheduler airflow-dag-processor airflow-apiserver
```

```bash
docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE etl_t8_rehearsal TEMPLATE etl_movies OWNER etl_app"
```

```bash
docker compose start airflow-scheduler airflow-dag-processor airflow-apiserver
```

建库默认按 `WAL_LOG` 复制，会先写约 27 GB WAL（2026-11-03 实测建库后 VM 磁盘只剩 814 MB）；立即执行检查点回收 WAL，再确认剩余约 28 GB：

```bash
docker compose exec -T postgres psql -U postgres -c "CHECKPOINT"
```

```bash
docker compose exec -T postgres df -h /var/lib/postgresql
```

2. 在副本上先冻结开发池、再完整运行（不经 Airflow）、核对、导出到单独目录（评价表用 `ETL_OUTPUT_DIR` 写到 `outputs/rehearsal_t8/`，不与正式库的 `outputs/task8/` 混在一起），最后同输入重跑（期望 `reused`）：

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler python -m etl_task8.evaluate freeze
```

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler python -m etl_task8.runner run
```

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler python -m etl_task8.check --extraction-id f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8
```

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler python -m etl_task8.report export --extraction-id f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8 --output-dir /opt/etl/outputs/rehearsal_t8 --run-key rehearsal1
```

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal -e ETL_OUTPUT_DIR=/opt/etl/outputs/rehearsal_t8 crawler python -m etl_task8.evaluate export
```

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler python -m etl_task8.runner run
```

3. 删除演练库、回收 WAL 并确认：

```bash
docker compose exec -T postgres psql -U postgres -c "DROP DATABASE etl_t8_rehearsal"
```

```bash
docker compose exec -T postgres psql -U postgres -c "CHECKPOINT"
```

```bash
docker compose exec -T postgres psql -U postgres -Atc "SELECT datname FROM pg_database ORDER BY 1"
```

期望：`airflow_meta`、`etl_movies`、`etl_test`、`postgres`、`template0`、`template1`。

## 4. 正式运行（T8-08）

1. 运行前核对任务 1–7 主表行数（只读）：

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT 'review_record', count(*) FROM public.review_record UNION ALL SELECT 'product_classification', count(*) FROM task3.product_classification UNION ALL SELECT 'edition_resolved', count(*) FROM task7.edition_resolved UNION ALL SELECT 'shared_tconst_result', count(*) FROM task7.shared_tconst_result"
```

2. 首次全量运行前冻结开发池（R45；会应用迁移 `701`）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.evaluate freeze
```

期望：`"status": "frozen"`、`"total_reviewers": 886308`、`"frozen_before_first_run": true`；重复执行为 `already_frozen`。

3. 经 Airflow 全量运行与复用验证（默认参数：最近全量抽取、`t3r2`、`cv0003`、`t8r1`、批 500,000、32 个分区）：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_reviews_task8_dedup --run-id t8_full_1 --conf '{}' --timeout 7200
```

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_reviews_task8_dedup --run-id t8_full_2 --conf '{}' --timeout 7200
```

期望：`t8_full_1` 九个任务 success，生成 `rd0001`（`verified`）；`t8_full_2` 为 `reused`。

4. 只读核对、报告导出、版本差异与查询：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.check
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.report export
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.query stats --by group --limit 5
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.query review 1
```

## 5. 评价与标注（R45–R49）

1. 正式运行后导出盲测表与 AI 草稿（输出到 `outputs/task8/<抽取>/<去重版本>/eval/`：`eval_items.csv`、`eval_items_draft.csv`、`LABELING.md`）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.evaluate export
```

2. 标注人（付林轩）以 `eval_items_draft.csv` 为底稿填 `human_label`（`same`/`different`/`unsure`）、`human_evidence`、`note`，另存为 `eval_items_labeled.csv`。`ai_doubt` 为 `true` 的项，或结论与 `ai_label` 不同的项，必须填 `human_evidence`，否则导入拒绝（`unsure` 除外）；`note` 不得原样粘贴 `ai_reason`。AI 草稿由 `generate_ai_draft` 只读盲测行生成，也可在审核前由 Claude 子代理只读盲测表另行预填，报告如实写“AI 预填、付林轩审核”。

3. 导入与指标：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.evaluate import /opt/etl/outputs/task8/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rd0001/eval/eval_items_labeled.csv --annotator 付林轩
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task8.evaluate metrics
```

指标定义见 spec R48：`unsure` 单独计数，不进分母；分母为 0 记“无定义”；只代表分层样本。
