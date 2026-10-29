# 任务 3 运行手册（电影页面筛选）

命令在项目根目录运行。任务 3 不发网络请求，只读 `task2` 的清单、尝试日志与 `outputs/task2/snapshots/` 下的快照；结果写入 `etl_movies` 的 schema `task3` 与 `outputs/task3/`（不入库）。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task3_extract.py tests/unit/test_task3_rules.py tests/integration/test_task3_classify.py tests/integration/test_task3_dag.py tests/integration/test_task3_report.py
```

集成测试会话开始时会清空 `etl_test`；测试库端到端要在测试之后从头准备（第 2 节）。

## 2. 端到端验证（真实 scheduler，写 etl_test）

1. 切换到端到端配置：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
```

2. 依次用任务 1 固定样本产生样本抽取、构建清单、离线假请求爬取（同任务 2 runbook 6.2 节；样本抽取 ID 固定为 `80954174…`，以查询为准）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --run-id t3_pre3_task1_sample --conf '{"scope":"sample","sample_max_records":100,"batch_rows":3,"source_gzip_path":"/data/fixtures/task1_sample.txt.gz","source_manifest_path":"/data/fixtures/task1_sample.manifest.json"}'
```

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_products_task2_manifest --run-id t3_pre3_manifest --conf '{"extraction_id":"80954174b73813ac93b211624595c11c07a6212045b9efa44b8299ac68432bc4","allow_sample":true}'
```

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_products_task2_crawl --run-id t3_pre3_crawl --conf '{"extraction_id":"80954174b73813ac93b211624595c11c07a6212045b9efa44b8299ac68432bc4","fetch_mode":"fixture","concurrency":3}'
```

3. 触发任务 3 DAG：成功、重跑、暂时错误、不存在的抽取（Airflow 的 run id 不能重复，重做时换新的）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_products_task3_classify --run-id t3_e2e_ok3 --conf '{"extraction_id":"80954174b73813ac93b211624595c11c07a6212045b9efa44b8299ac68432bc4"}'
```

期望：`DAG run t3_e2e_ok3: success`；`outputs/e2e/task3/8095…/t3r1/t3_e2e_ok3/summary.json` 的 `input_groups` 为 dp_page 4、access_restricted 1、not_found 1，六类中 pending 6（假页面只有标题）。

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_products_task3_classify --run-id t3_e2e_rerun3 --conf '{"extraction_id":"80954174b73813ac93b211624595c11c07a6212045b9efa44b8299ac68432bc4"}'
```

期望：success，`task3.classify_run` 新增一行 `reused`、`classified` 为 0。

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_products_task3_classify --run-id t3_e2e_transient3 --conf '{"extraction_id":"80954174b73813ac93b211624595c11c07a6212045b9efa44b8299ac68432bc4","fault_injection":"transient_once:/opt/etl/outputs/e2e/t3_marker3"}'
```

期望：success，`classify_products` 的 `try_number` 为 2。标记文件存在时不再注入，重做前先删除 `outputs/e2e/t3_marker3`。

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_products_task3_classify --run-id t3_e2e_bad_extraction3 --conf '{"extraction_id":"0000000000000000000000000000000000000000000000000000000000000000"}'
```

期望：`check_input failed`（只尝试 1 次），下游 `upstream_failed`，`run_gate failed`，`DAG run ...: failed`；报告与 `check_input-try1.json` 在 `outputs/e2e/task3/_failures/t3_e2e_bad_extraction3/`。

4. 回到正常配置：

```bash
docker compose up -d
```

## 3. 正式运行

正常配置（`ETL_ENV=prod`）下经 Airflow 全量运行；全量约 10 分钟，`limit` 只用于试跑（限定运行的核对与门禁会失败）：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_products_task3_classify --run-id t3_full_1 --conf '{}' --timeout 5400
```

同一规则版本重跑（换 run id）只新增一条 `reused` 审计。命令行等价用法（不经 Airflow）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.classify run
```

只读核对（期望退出码 0）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.check
```

## 4. 结果位置

| 内容 | 位置 |
|---|---|
| 解析、字段、分类、证据、质量问题、抽查 | `etl_movies` 的 `task3.page_parse`、`page_field`、`product_classification`、`classification_evidence`、`quality_issue`、`manual_review` |
| 运行审计 | `task3.classify_run` |
| 报告与导出 | `outputs/task3/<抽取>/<规则版本>/<run_key>/`：`summary.json`、`report.md`、`classification.csv`、`field_coverage.csv` |
| 抽查表 | `outputs/task3/<抽取>/<规则版本>/review/review_sample.csv` |
| 失败记录 | `outputs/task3/_failures/<run_key>/` |

## 5. 改规则与比较版本

改 `src/etl_task3/rules.py` 的模式或阈值时，同时把 `src/etl_task3/__init__.py` 的 `RULE_VERSION` 升一版（如 `t3r2`），再运行第 3 节。新版本只重算分类、不重解析页面；旧版本保留。比较两版：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.classify diff --from t3r1 --to t3r2
```

## 6. 人工抽查

导出（每类最多 20 个，排除规则开发样本）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.report review-export
```

在导出的 CSV 里对照 `snapshot_path` 的快照或 `product_url` 填写 `human_category`（`movie`、`non_movie`、`collection`、`tv_series`、`concert_other`、`pending` 之一）和 `note`，不确定填 `pending`。保存后导入（路径为容器内路径，`<姓名或代号>` 换成标注人）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.report review-import /opt/etl/outputs/task3/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/t3r1/review/review_sample.csv --reviewer <姓名或代号>
```

导入后输出各类一致数、一致率与不一致实例；再跑一次第 3 节的 DAG（换 run id），报告里会出现“人工抽查”一节。

## 7. 失败与恢复

| 情况 | 处理 |
|---|---|
| 抽取不存在、没有清单、分组之和不等 | `check_input` 确定性失败，不重试 |
| 数据库暂时断连、锁被占用 | Airflow 最多重试 2 次；已提交批次保留，从剩余 ASIN 续接 |
| 进程被杀 | 未提交批次无部分行；`classify_run` 停在 `running`，下次运行改为 `interrupted` 后续接 |
| 快照缺失或哈希不一致 | 该 ASIN 待确认（`SN1`/`SN2`），记 `task3.quality_issue`，不中断运行 |
| 核对失败 | 门禁失败；按 `outputs/task3/_failures/<run_key>/verify_classification-try1.json` 的原因处理后换 run id 重跑 |
