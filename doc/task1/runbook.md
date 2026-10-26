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

## 5.1 正式全量与资源记录

先 `unset COMPOSE_FILE; docker compose up -d` 回到正常配置（写业务库 `etl_movies`），确认磁盘剩余远大于 `ETL_MIN_FREE_BYTES`（默认 10 GiB；全量数据库约 23 GB，另需 WAL 与临时空间）。运行期间防止宿主休眠（如 `caffeinate -s`），休眠曾导致任务失联并被重试（见任务 1 报告）。

```bash
python3 scripts/run_dag_e2e.py --run-id full_run_1 --conf '{"scope":"full"}' --timeout 7200
```

全量约需 45–60 分钟装载（速率随表增大由约 1 万降到约 1,500 条/秒）加约 2 分钟校验；脚本超时只影响等待，DAG 仍在运行，可在 Airflow 界面查看。差异未解释时 `run_gate` 失败、状态 `review_required`；有依据后用同参数加 `baseline_explanation` 重新触发，已完成的抽取只做完整性核对并复用。

每个批次提交后装载器向 `<运行目录>/resources.jsonl` 追加一行样本（RSS、累计记录/字节、速率、数据库大小、磁盘剩余），结束时写 `resources.json` 汇总。宿主侧补充采样（容器 CPU/内存、PostgreSQL 数据与 WAL 目录）：

```bash
scripts/sample_host_stats.sh outputs/full-run/host-stats.csv 15   # 后台运行，结束后终止该进程
```

100,000 条真实记录试跑是一个集成测试（需挂载真实数据，否则跳过），结果写 `outputs/resource-trial/trial-100k-summary.json`：

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_resource_limits.py
```

磁盘保护：剩余空间低于 `ETL_MIN_FREE_BYTES` 时在已提交批次之后停止（`LowDiskSpace`，确定性错误不重试），释放空间后用同参数重新触发即可续跑。

## 6. 失败与恢复

- **确定性错误**（哈希/大小不符、缺文件、gzip 损坏、数量守恒破坏、证据不完整、检查点不匹配）：任务失败且不重试；修正输入或配置后重新触发。
- **暂时错误**（数据库断连/重启等）：自动重试，最多 2 次，间隔 `ETL_RETRY_DELAY_SECONDS`（默认 60 秒）。
- **进程被杀或任务失联**：旧运行记录停在 `running`；新运行取得抽取锁后把它登记为 `failed`。旧进程若仍存活会持有抽取锁，新尝试得到 `ExtractionBusy`（暂时错误）并按重试等待。
- **中断恢复**：重新触发同参数的运行。已提交的批次保留；装载器重新顺序解压扫描到检查点位置，核对检查点记录的哈希后继续。普通 gzip 不能随机跳转，扫描耗时计入恢复时间。
- **输入或配置变化**：得到另一个 `extraction_id`，不会沿用旧检查点，也不会混入旧数据。
- **已完成的抽取再次运行**：核对完整性后复用，只新增一条 `reused` 执行审计。
- **数据库核对**：`etl_task1.db.run_integrity` 执行 `sql/task1_integrity.sql`；`etl_task1.validation.collect_counts` 执行 `sql/task1_counts.sql`。所有查询必须限定 `extraction_id`。
