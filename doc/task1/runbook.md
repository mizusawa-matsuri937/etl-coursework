# 任务 1 运行手册

状态：2026-10-26 随 T1-04 建立，覆盖样本与端到端验证；全量运行章节在 T1-05 实测后补充。命令的工作目录均为项目根目录，环境为 macOS arm64 + Colima（见 README）。

## 1. 准备

```bash
cp .env.example .env          # 填写口令、Airflow 密钥与 SOURCE_HOST_DIR（口令生成方法见文件内注释）
docker compose build
docker compose up -d          # postgres、airflow-init、api-server、scheduler、dag-processor
docker compose ps             # postgres 与 api-server 应为 healthy
```

首次启动自动创建三个隔离的库：`etl_movies`（业务）、`airflow_meta`（Airflow 元数据）、`etl_test`（测试与端到端验证）。业务库的表结构由第一次 `extract_load` 自动应用（`etl_task1.db.apply_schema`，已应用则跳过）。

## 2. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests            # 单元 + 集成，连接 etl_test
docker compose --profile test run --rm etl-test pytest -q tests/unit       # 仅单元，不需要数据库
```

## 3. 经 Airflow 运行

Airflow 界面：`http://127.0.0.1:8080`（仅本机、免登录）。DAG `amazon_reviews_task1` 手动触发，参数：

| 参数 | 含义 |
|---|---|
| `scope` | `sample` 或 `full`；`full` 读到 EOF 并验证 gzip，不允许记录上限 |
| `sample_max_records` | `scope=sample` 必填 |
| `batch_rows` | 可选，覆盖每批行数（改变它会产生另一个抽取 ID） |
| `source_gzip_path` / `source_manifest_path` | 可选，默认读 `.env` 配置的位置 |
| `baseline_explanation` | 全量与官方基准有差异时填写有依据的说明；留空则状态为 `review_required` |
| `fault_injection` | 仅 `ETL_ENV=test` 有效（见第 5 节） |

命令行触发并等待（通过真实 scheduler）：

```bash
python3 scripts/run_dag_e2e.py --run-id my_run_1 --conf '{"scope":"sample","sample_max_records":20000}'
```

任务依赖：`verify_source → extract_load → validate_counts → export_results → finalize_report → run_gate`。`finalize_report` 与 `run_gate` 无论上游成败都运行；只有 `run_gate` 通过才代表整体成功。

## 4. 结果位置

每次运行输出到 `outputs/task1/<extraction_id>/<run_id>/`（容器内 `/opt/etl/outputs`）：

| 文件 | 内容 |
|---|---|
| `summary.json` | 机器可读：最终状态、各任务状态、数量、不变量、基准对照、质量分布、失败记录、资源 |
| `report.md` | 同内容的可读报告，含 AI 辅助说明 |
| `manifest.json` | 本次使用的输入 manifest 副本 |
| `quality_issues_by_code.csv` | 问题码分布 |
| `distinct_products.csv` | 仅 `scope=full`：不同产品汇总，供任务 2 接续（不是页面采集清单） |

失败记录写在 `outputs/task1/_failures/<run_id>/<任务>-try<N>.json`，数据库不可用或 `extraction_id` 未知时也能留下原因；此时报告也落在 `_failures/<run_id>/`。超限记录的旁路文件在 `outputs/evidence/<extraction_id>/`。

## 5. 端到端验证与故障演示

不要让样本污染业务库：端到端验证使用覆盖文件，让 Airflow 写入 `etl_test` 与 `outputs/e2e/`，并允许故障注入、缩短重试间隔：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
export COMPOSE_FILE=compose.yaml:compose.e2e.yaml
FX='"source_gzip_path":"/data/fixtures/task1_sample.txt.gz","source_manifest_path":"/data/fixtures/task1_sample.manifest.json"'
python3 scripts/run_dag_e2e.py --run-id demo_ok --conf "{\"scope\":\"sample\",\"sample_max_records\":100,\"batch_rows\":3,$FX}"
# 错误哈希：verify_source 失败且不重试，下游 upstream_failed，run_gate 失败
python3 scripts/run_dag_e2e.py --run-id demo_hash --conf '{"scope":"sample","sample_max_records":100,"source_gzip_path":"/data/fixtures/task1_sample.txt.gz","source_manifest_path":"/data/fixtures/task1_sample.bad_hash.manifest.json"}'
# gzip 尾部缺失：extract_load 失败，抽取标为 failed
python3 scripts/run_dag_e2e.py --run-id demo_trunc --conf '{"scope":"sample","sample_max_records":100,"source_gzip_path":"/data/fixtures/task1_truncated.txt.gz","source_manifest_path":"/data/fixtures/task1_truncated.manifest.json"}'
# 暂时错误：第一次连接失败，按 retries=2 重试后成功
python3 scripts/run_dag_e2e.py --run-id demo_transient --conf "{\"scope\":\"sample\",\"sample_max_records\":100,$FX,\"fault_injection\":\"transient_once:/opt/etl/outputs/e2e/marker\"}"
```

`fault_injection` 取值：`before_commit:N`、`after_commit:N`（第 N 批提交前/后抛错）、`transient_once:<标记文件>`（首次抛连接错误，之后成功）。恢复验证用同一参数重跑即可：已提交批次保留，从检查点后继续。回到正常配置：`unset COMPOSE_FILE; docker compose up -d`。

## 6. 失败与恢复

- **确定性错误**（哈希/大小不符、缺文件、gzip 损坏、数量守恒破坏、证据不完整、检查点不匹配）：任务失败且不重试；修正输入或配置后重新触发。
- **暂时错误**（数据库断连/重启等）：自动重试，最多 2 次，间隔 `ETL_RETRY_DELAY_SECONDS`（默认 60 秒）。
- **中断恢复**：重新触发同参数的运行。已提交的批次保留；装载器重新顺序解压扫描到检查点位置，核对检查点记录的哈希后继续。普通 gzip 不能随机跳转，扫描耗时计入恢复时间。
- **输入或配置变化**：得到另一个 `extraction_id`，不会沿用旧检查点，也不会混入旧数据。
- **已完成的抽取再次运行**：核对完整性后复用，只新增一条 `reused` 执行审计。
- **数据库核对**：`etl_task1.db.run_integrity` 执行 `sql/task1_integrity.sql`；`etl_task1.validation.collect_counts` 执行 `sql/task1_counts.sql`。所有查询必须限定 `extraction_id`。
