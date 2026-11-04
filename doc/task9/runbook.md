# 任务 9 运行手册（字段级数据血缘）

命令在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 运行（macOS 默认 zsh 可直接复制）。任务 9 只读 `public`、`task2`–`task8` 与 `imdb`，结果写入 schema `task9`（迁移 `801`），导出写入 `outputs/task9/`（不入库、不入仓库）。迁移 `801` 由 DAG 的 `check_inputs` 步骤或 `etl_task9.runner` 自动应用，**只在正式运行（T9-08）时才应用到 `etl_movies`**；开发与演练阶段不要在 `etl_movies` 上触发任何写操作。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py tests/integration/test_task9_derivations.py tests/integration/test_task9_relocate.py tests/integration/test_task9_selections.py tests/integration/test_task9_query.py tests/integration/test_task9_impact.py tests/integration/test_task9_runs.py tests/integration/test_task9_report.py tests/integration/test_task9_dag.py tests/integration/test_task9_evaluate.py
```

集成测试会话开始时会清空 `etl_test`；下一节的端到端数据要在测试之后从头准备。

## 2. 端到端验证（真实 scheduler，写 etl_test）

DAG `amazon_movies_task9_lineage` 在真实 scheduler 上跑四种情况：成功、同输入重跑（`reused`）、上游 `cv` 不存在、`verify` 失败后重跑。合成上游来自任务 9 的测试夹具（`_task9_helpers.seed` 与 `add_products`），快照写在 `outputs/e2e/t9_seed/`。**只用 `etl_test`，绝不在 `etl_movies` 上触发。**

1. 准备 `etl_test`（会清空 `etl_test`），输出一行 JSON：

```bash
docker compose --profile test run --rm -T etl-test python - < scripts/seed_task9_e2e.py
```

期望最后一行：`{"extraction_id": "7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8", "conflict_version_id": "cv0001", "review_dedup_version_id": "rd0001", "snapshot_root": "/opt/etl/outputs/e2e/t9_seed/t9root"}`（夹具固定，每次相同）。

2. 切换到端到端配置（Airflow 写 `etl_test` 与 `outputs/e2e/`，允许故障注入，重试间隔 2 秒），等约 40 秒让 DAG 解析完：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
```

Airflow 的 run-id 不能重复：再次验证时先重新执行第 1 步，并把下面命令里的 `--run-id` 换成没用过的值（第 6 步的期望按新 run-id 对照）。2026-11-03 实测用的是下列 run-id，结果与各步期望一致；同日第一轮（无 `b` 后缀）遇到主机暂停，调度器心跳中断 1,044 秒，`verify` 失败的那次被 Airflow 视为执行器异常而再跑了一次（两次都是确定性失败），所以重新准备数据后用 `b` 后缀重做了四种情况，`verify` 只执行一次。

3. 成功与同输入重跑：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task9_lineage --run-id t9_e2e_1b --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","conflict_version_id":"cv0001","snapshot_root":"/opt/etl/outputs/e2e/t9_seed/t9root","batch_size":50}' --timeout 900
```

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task9_lineage --run-id t9_e2e_2b --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","conflict_version_id":"cv0001","snapshot_root":"/opt/etl/outputs/e2e/t9_seed/t9root","batch_size":50}' --timeout 900
```

期望：两次都输出 `DAG run …: success`，10 个任务全部 `success`；第二次的运行是 `reused`（见第 6 步查询）。

4. 上游 `cv` 不存在：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task9_lineage --run-id t9_e2e_missing_b --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","conflict_version_id":"cv9999","snapshot_root":"/opt/etl/outputs/e2e/t9_seed/t9root","batch_size":50}' --timeout 600
```

期望：`check_inputs` 为 `failed`（不重试），中间步骤 `upstream_failed`，`finalize_report` 为 `success`，`gate` 为 `failed`，`DAG run t9_e2e_missing_b: failed`。

5. `verify` 失败后重跑（用夹具的另一个 cv `cv0002` 与 `rd0002`，得到新的输入键）：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task9_lineage --run-id t9_e2e_vf_b --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","conflict_version_id":"cv0002","review_dedup_version_id":"rd0002","snapshot_root":"/opt/etl/outputs/e2e/t9_seed/t9root","batch_size":50,"fault_injection":"verify_fail"}' --timeout 900
```

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task9_lineage --run-id t9_e2e_vf2_b --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","conflict_version_id":"cv0002","review_dedup_version_id":"rd0002","snapshot_root":"/opt/etl/outputs/e2e/t9_seed/t9root","batch_size":50}' --timeout 900
```

期望：第一次 `verify` 为 `failed`（不重试）、`gate` 为 `failed`；第二次全部 `success`，在原版本 `lv0002` 上重写。

6. 运行与版本状态（只读 `etl_test`）：

```bash
docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT airflow_run_id, status, lineage_version_id FROM task9.run WHERE kind = 'lineage' ORDER BY started_at; SELECT lineage_version_id, status FROM task9.lineage_version ORDER BY 1"
```

期望：`t9_e2e_1b|succeeded|lv0001`、`t9_e2e_2b|reused|lv0001`、`t9_e2e_vf_b|failed|lv0002`、`t9_e2e_vf2_b|succeeded|lv0002`，以及 `lv0001|verified`、`lv0002|verified`（上游缺失的那次没有运行行）。

7. 回到正常配置并确认服务正常（必须执行）：

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

## 3. 业务库副本演练（T9-07，R43）

**副本已于 2026-11-04 删除**；下面是当时的实际步骤，再次演练须先按 design“业务库副本演练”重建副本（停 Airflow → `CREATE DATABASE etl_t9_rehearsal TEMPLATE etl_movies OWNER etl_app` → `CHECKPOINT` → 恢复 Airflow）。结果见 design“业务库副本全链路演练实测”。

1. 清出空的 `task9`（只在副本上；先确认没有其他会话）：

```bash
docker compose exec -T postgres psql -U postgres -Atc "SELECT count(*) FROM pg_stat_activity WHERE datname = 'etl_t9_rehearsal'"
docker compose exec -T postgres psql -U postgres -d etl_t9_rehearsal -v ON_ERROR_STOP=1 -c "BEGIN; DROP SCHEMA task9 CASCADE; DELETE FROM public.schema_migration WHERE version = '801'; COMMIT;"
docker compose exec -T postgres psql -U postgres -c "CHECKPOINT"
```

2. 完整运行（从迁移 801 开始，不经 Airflow，2026-11-04 实测 440 秒）、同输入重跑（`reused`）、核对与导出（导出写到 `outputs/rehearsal_t9/`，不与正式库的 `outputs/task9/` 混在一起）：

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.runner run
```

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.check > /dev/null; echo "exit=$?"
```

```bash
docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal -e ETL_OUTPUT_DIR=/opt/etl/outputs/rehearsal_t9 crawler python -m etl_task9.report export --run-key rehearsal_full
```

3. 演示查询与影响分析见 design“演示查询”（加 `-e ETL_DB_NAME=etl_t9_rehearsal`）；评论文件的来源核验在 Airflow 容器里：

```bash
docker compose exec -T -e ETL_DB_NAME=etl_t9_rehearsal airflow-scheduler python -m etl_task9.impact verify-sources --kinds review_record > /dev/null; echo "exit=$?"
```

4. 删库并回收空间（确认副本无会话后）：

```bash
docker compose exec -T postgres psql -U postgres -c "DROP DATABASE etl_t9_rehearsal"
docker compose exec -T postgres psql -U postgres -c "CHECKPOINT"
docker compose exec -T postgres df -h /var/lib/postgresql
```

2026-11-04 实测：删库前空闲 68 GB，删库并 `CHECKPOINT` 后 108 GB。

## 4. 正式运行（T9-08）

经 Airflow 触发（默认参数即 R01 的版本），再同参重跑一次（期望 `reused`），然后只读核对：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task9_lineage --run-id t9_full_1 --conf '{}' --timeout 7200
```

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task9_lineage --run-id t9_full_2 --conf '{}' --timeout 3600
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.check > /dev/null; echo "exit=$?"
```

来源核验（评论文件只挂载在 Airflow 容器里，在那里核验评论文件）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.impact verify-sources --kinds page_snapshot,imdb_row > /dev/null; echo "exit=$?"
```

```bash
docker compose exec -T airflow-scheduler python -m etl_task9.impact verify-sources --kinds review_record > /dev/null; echo "exit=$?"
```

2026-11-04 实测：`t9_full_1` 10 个任务 success（449 秒），`t9_full_2` 为 `reused`（31 秒）；`check` 与两条 `verify-sources` 均 `exit=0`。DAG 的 `finalize_report` 已把导出写到 `outputs/task9/<抽取>/lv0001/<run-id>/`；需要另导一份时：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.report export --version lv0001 --run-key manual
```

Airflow 的 run-id 不能重复使用；再次触发时换新的 run-id（同输入会记 `reused`）。

## 5. 血缘链抽查（T9-08，R46–R49）

2026-11-04 已在正式库执行下面的 `export`（因修正评论摘录又以 `--force` 重导一次，见 design“T9-08 正式库运行实测”）与 `draft`：60 项，草稿 `correct` 47、`unsure` 13。文件在 `outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/eval/`（不入 Git）。同日付林轩审核后按草稿原样采纳，另存 `eval_labels.csv` 并执行下面的 `import` 与 `metrics`：导入 60 项，血缘链正确率 1.000（47/47，`unsure` 13 不进分母）。已导入标注后 `export` 会拒绝重导；`--force` 会清除已导入的标注，审核开始后不要再用。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.evaluate export --version lv0001
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.evaluate draft --version lv0001
```

草稿 `eval_items_draft.csv` 由付林轩逐项审核：填 `label`（`correct`/`incorrect`/`unsure`），`incorrect` 填 `error_type`，改判或 `ai_doubt = true` 的项填 `human_evidence`（标 `unsure` 的除外），另存为 `eval_labels.csv`（规则见同目录 `LABELING.md`）。导入与指标：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.evaluate import --version lv0001 --file /opt/etl/outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/eval/eval_labels.csv --annotator 付林轩
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.evaluate metrics --version lv0001
```

IMDb 原始文件需在容器内可读（默认 `IMDB_DATA_DIR`）；不可读时审核表给装载表行并在草稿中存疑。
