# 任务 2 运行手册（第一部分：ASIN 采集清单）

环境准备、口令与 Compose 用法同 [任务 1 runbook](../task1/runbook.md) 第 1 节；本阶段不新增服务或依赖。所有命令的工作目录为项目根目录。清单构建只读任务 1 的数据库结果，不读 `movies.txt.gz`，也不发任何网络请求。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

只跑任务 2：

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task2_urls.py tests/integration/test_task2_manifest.py tests/integration/test_task2_failures.py tests/integration/test_task2_dag.py
```

集成测试连接独立的 `etl_test` 库；会话开始时重建 `public` 与 `task2` 两个 schema。

## 2. 端到端验证（真实 scheduler，写 etl_test）

先切到端到端配置，再用任务 1 的固定样本 DAG 在 `etl_test` 里产生一个样本抽取：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
export COMPOSE_FILE=compose.yaml:compose.e2e.yaml
FX='"source_gzip_path":"/data/fixtures/task1_sample.txt.gz","source_manifest_path":"/data/fixtures/task1_sample.manifest.json"'
python3 scripts/run_dag_e2e.py --run-id t2pre_task1_sample --conf "{\"scope\":\"sample\",\"sample_max_records\":100,\"batch_rows\":3,$FX}"
docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT extraction_id, scope FROM extraction WHERE status = 'complete'"
```

把上一步得到的样本抽取 ID 记为 `S`，然后触发清单 DAG：

```bash
S=<样本抽取 ID>
D=amazon_products_task2_manifest
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_ok --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true}"
# 同输入重跑：构建状态 reused，清单不变
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_rerun --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true}"
# 未允许样本：check_extraction 失败且不重试，下游 upstream_failed，run_gate 失败
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_no_allow --conf "{\"extraction_id\":\"$S\"}"
# 暂时错误：build_manifest 第 1 次连接失败，第 2 次成功
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_transient --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true,\"fault_injection\":\"transient_once:/opt/etl/outputs/e2e/t2_marker\"}"
# 提交前故障：build_manifest 失败且不重试，审计为 failed
python3 scripts/run_dag_e2e.py --dag-id $D --run-id t2_before_commit --conf "{\"extraction_id\":\"$S\",\"allow_sample\":true,\"fault_injection\":\"before_commit\"}"
```

`transient_once` 的标记文件存在时不再注入；重复演示前先删除 `outputs/e2e/t2_marker`。故障注入只在 `ETL_ENV=test` 下有效。演示结束回到正常配置：

```bash
unset COMPOSE_FILE
docker compose up -d
```

## 3. 正式构建

2026-10-28 已按此执行（`t2_full_1` 成功，约 45 s；`t2_full_2_rerun` 复用），结果见 [checklist](checklist.md) AC09。先确认 Airflow 已回到正常配置（写 `etl_movies`），且没有任务 1 的 DAG 正在运行：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_products_task2_manifest --run-id t2_full_1 --conf '{"extraction_id":"f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8"}' --timeout 3600
```

同参数重新触发只复用，不改清单；运行期间防止宿主休眠（如 `caffeinate -s`）。

## 4. 结果位置

- 数据库：`task2.manifest_build`（构建定义）、`task2.manifest_run`（每次执行的审计）、`task2.product_manifest`（清单）。查询必须带 `extraction_id`，例如：

  ```bash
  docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT status, count(*) FROM task2.product_manifest WHERE extraction_id = '<抽取 ID>' GROUP BY status"
  ```

- 文件（`outputs/` 被忽略，不入库）：`outputs/task2/<extraction_id>/<build_id>/<run_key>/` 下的 `product_manifest.csv`、`summary.json`、`report.md`；抽取或构建未知时报告落在 `outputs/task2/_failures/<run_key>/`。每次失败另写 `outputs/task2/_failures/<run_key>/<任务>-try<N>.json`。端到端运行的输出在 `outputs/e2e/task2/`。

## 5. 失败与恢复

- **确定性错误**（抽取不存在/未完成/样本未允许、来源冲突 `ManifestConflict`、数量核对不符、注入的提交前故障）：任务失败且不重试；查明原因后重新触发。
- **暂时错误**（数据库断连、同一构建正被占用 `ManifestBusy`）：自动重试，最多 2 次。
- **中途失败**：构建在单个事务内提交，失败后清单没有部分行；重新触发即可。进程被杀时审计停在 `running`，下一次构建取得锁后把它登记为 `failed`。
- **重跑**：同一抽取、同一模板版本只复用（`reused`），不新增也不修改清单行，爬虫写入的状态不受影响。
- **导出失败**：清单数据已提交且有效；修复输出目录后重新触发，会走复用路径并重新导出。
