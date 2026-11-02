# 任务 7 运行手册（处理不同来源的信息冲突）

命令在项目根目录运行（macOS 默认 zsh 可直接复制）。任务 7 只读 `public`、`task2`–`task6`、`imdb`，结果写入 `etl_movies` 的 schema `task7`（迁移 `601`），导出写入 `outputs/task7/`（不入库、不入仓库）。输入是任务 4 映射版本 `map0002`（规则 `t4r2`）、任务 5 人物映射版本 `pmap0001`（规则 `t5r1`）、任务 6 上映版本 `rv0001`（规则 `t6r1`）与 IMDb 快照 `imdb_2026-10-29`，这些数据须已在正式库中。迁移 `601` 由 DAG 的 `check_inputs` 步骤（或 `etl_task7.run.apply_task7_schema`）自动应用，**只在正式运行时才应用到 `etl_movies`**；规划与开发阶段不要在 `etl_movies` 上触发任何写操作。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py tests/unit/test_task7_multivalue.py tests/integration/test_task7_editions.py tests/integration/test_task7_link_assess.py tests/integration/test_task7_movies_scalar.py tests/integration/test_task7_movies_multivalue.py tests/integration/test_task7_runs.py tests/integration/test_task7_report.py tests/integration/test_task7_dag.py tests/integration/test_task7_evaluate.py
```

集成测试会话开始时会清空 `etl_test`；下一节的端到端数据要在测试之后从头准备。

## 2. 端到端验证（真实 scheduler，写 etl_test）

DAG `amazon_movies_task7_conflict` 在真实 scheduler 上跑四种情况：成功、同输入重跑（`reused`）、上游版本不存在、`verify` 失败后重跑。合成数据来自测试夹具（5 部作品，不运行上游流水线）。**只用 `etl_test`，绝不在 `etl_movies` 上触发。**

1. 准备 `etl_test`（脚本经标准输入传入，会清空 `etl_test`），输出一行 JSON，其中有 `extraction_id`（夹具固定为下面的值）：

```bash
docker compose --profile test run --rm -T etl-test python - < scripts/seed_task7_e2e.py
```

2. 切换到端到端配置（Airflow 写 `etl_test` 与 `outputs/e2e/`，允许故障注入，重试间隔 2 秒）：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
```

3. 成功：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task7_conflict --run-id t7_e2e_ok1 --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","batch_size":2}'
```

期望：九个任务 success，`DAG run t7_e2e_ok1: success`，退出码 0；`outputs/e2e/task7/7b787cf4…/cv0001/t7_e2e_ok1/` 产生报告、实例与 CSV；`etl_test` 的 `task7.run` 一行 `succeeded`、`task7.conflict_version` 一行 `verified`。

4. 同输入重跑（换 run id）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task7_conflict --run-id t7_e2e_rerun1 --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","batch_size":2}'
```

```bash
docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT status, conflict_version_id, airflow_run_id FROM task7.run ORDER BY started_at"
```

期望：九个任务 success；`task7.run` 新增一行 `reused`，版本仍是 `cv0001`。

5. 上游版本不存在（不存在的任务 6 上映版本）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task7_conflict --run-id t7_e2e_missing1 --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","t6_release_version_id":"rv9999"}'
```

期望：`check_inputs failed`（只尝试 1 次：`outputs/e2e/task7/_failures/t7_e2e_missing1/` 只有 `check_inputs-try1.json`），其余七个执行任务 `upstream_failed`，`finalize_report success`，`gate failed`，`DAG run ...: failed`，退出码 1。

6. `verify` 失败后重跑。先重新准备数据（第 1 步），使输入没有已核对的运行，再注入核对失败：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task7_conflict --run-id t7_e2e_vf1 --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","batch_size":2,"fault_injection":"verify_fail"}'
```

```bash
docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT status, conflict_version_id FROM task7.run; SELECT conflict_version_id, status FROM task7.conflict_version"
```

期望：`verify failed`（只尝试 1 次）、`gate failed`、`finalize_report success`，退出码 1；`task7.run` 为 `failed`，`task7.conflict_version` 为 `unverified`。去掉注入后换 run id 重跑：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task7_conflict --run-id t7_e2e_vf2 --conf '{"extraction_id":"7b787cf4805546ffebc4fc35aa2610af96845cd96e5232d8412360f0e6c314c8","batch_size":2}'
```

```bash
docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT status, conflict_version_id, airflow_run_id FROM task7.run ORDER BY started_at; SELECT conflict_version_id, status FROM task7.conflict_version"
```

期望：九个任务 success；`task7.run` 为 `failed` 与 `succeeded`（不是 `reused`），版本仍是 `cv0001` 且变为 `verified`（在原版本上重写，阶段结果不重算）。

7. 回到正常配置并确认服务正常（必须执行）：

```bash
docker compose up -d
```

```bash
docker compose ps
```

```bash
docker compose exec -T airflow-scheduler env | grep -E "^ETL_(ENV|DB_NAME|OUTPUT_DIR|RETRY_DELAY_SECONDS)="
```

期望：三个 Airflow 服务运行（api-server 为 healthy），`ETL_ENV=prod`、`ETL_DB_NAME=etl_movies`、`ETL_OUTPUT_DIR=/opt/etl/outputs`、`ETL_RETRY_DELAY_SECONDS=60`。确认 DAG 没有导入错误：

```bash
docker compose exec -T airflow-scheduler airflow dags list-import-errors
```

期望：`No data found`。

## 3. 正式运行（T7-08）

### 3.1 运行前核对任务 1–6 全部主表行数

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT 'review_record', count(*) FROM public.review_record UNION ALL SELECT 'product_manifest', count(*) FROM task2.product_manifest UNION ALL SELECT 'page_field', count(*) FROM task3.page_field UNION ALL SELECT 'movie', count(*) FROM task4.movie UNION ALL SELECT 'product_movie', count(*) FROM task4.product_movie UNION ALL SELECT 'person', count(*) FROM task5.person UNION ALL SELECT 'movie_person', count(*) FROM task5.movie_person UNION ALL SELECT 'release_selection', count(*) FROM task6.release_selection ORDER BY 1"
```

### 3.2 全量运行前先冻结开发池（R61，1,732 部作品）

`evaluate freeze` 先幂等应用迁移 `601`（建 schema `task7`，写 `public.schema_migration` 的 `601`），再按 `md5(movie_id)` 排序取前 20% 冻结到 `task7.dev_pool`；已冻结时直接返回已有结果。须在首次全量运行前执行：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate freeze
```

期望：`"status": "frozen"`、`"dev_movies": 1732`、`"total_movies": 8659`、`"frozen_before_first_run": true`；重复执行为 `already_frozen`。

### 3.3 触发 DAG 与复用验证

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task7_conflict --run-id t7_full_1 --conf '{"imdb_snapshot_id":"imdb_2026-10-29"}' --timeout 7200
```

同一参数换 run id 重跑为 `reused`：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task7_conflict --run-id t7_full_2 --conf '{"imdb_snapshot_id":"imdb_2026-10-29"}' --timeout 7200
```

### 3.4 只读核对、报告导出与查询

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.check
```

期望：退出码 0，`"problems": []`，`"passed": true`。

按真实成功运行导出报告、实例与 CSV（输出到 `outputs/task7/<抽取>/<版本>/<run_key>/`）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.report export
```

两个版本的差异：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.report diff cv0001 cv0002
```

三个只读查询：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.query movie <movie_id>
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.query edition <asin>
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.query conflicts --type cross_source_mismatch_halt --limit 20
```

## 4. 评价与标注（T7-07 / T7-09）

`python -m etl_task7.evaluate freeze|export|import|metrics` 的设计见 design“盲测评价设计”。要点：

- 开发池 1,732 部（`md5(movie_id)` 前 20%）严格排除；版本层抽样时任一关联作品在开发池的 ASIN 也排除。
- 七层 120 项：`E1` 标题 15、`E2` 年份 15、`E3` 片长 20、`E4` 演职员 20（导演 8、署名对齐 8、署名顺序 4）、`E5` 类型 15、`E6` 分级 15、`E7` 版本层 20（发行商 10、发售日 10）；系统给出 `resolved` 或 `conflict_unresolved` 的项才入池（`missing` 没有候选，无从评价）。`E1`、`E2`、`E3`、`E6` 先取 `min(可用数, 4)` 项 `conflict_unresolved`，署名对齐至少一半取 `L4`，类型中含未映射词面的作品优先，发售日带 `date_flags` 的优先；某层不足 120 项配额时按 `E3 → E4 → E1 → E7` 补足。各层实际分布写入 `task7.eval_set.strata`。
- 盲测表 `eval_items.csv` 只有实体、目标字段、概况与原始候选证据（由上游原始表重新生成，按 `(source, source_key)` 排序，不读 `task7` 候选与选择表），编号 `t7_eval_0001`–`t7_eval_0120` 按 `md5(entity_id:target_field)` 全局打乱，不含胜出值、状态、冲突类型与层号。系统结果只存在 `task7.eval_item`，用于算指标。
- `eval_items_draft.csv` 是 AI 预标注草稿：由规则式脚本只依据盲测行生成 `ai_action`、`ai_winning_value`、`ai_doubt`、`ai_reason`，不读 `task7` 选择结果。它只是核对线索，不是事实；`ai_reason` 不进入标注表。

1. 冻结开发池（见 3.2）。
2. 正式运行（见 3.3、3.4）完成后导出盲测表与草稿（输出到 `outputs/task7/<抽取>/<冲突消解版本>/eval/`：`eval_items.csv`、`eval_items_draft.csv`、`LABELING.md`）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate export
```

已有导入的标注时拒绝重导，确需重导加 `--force`（会清除标注）。

3. 标注人（付林轩）以 `eval_items_draft.csv` 为底稿，把结论填在 `human_action`（`resolve`/`keep_conflict`/`unsure`）、`human_winning_value`（`resolve` 时必填，格式见 `LABELING.md`）、`human_evidence`、`note`，另存为 `eval_items_labeled.csv`。`ai_doubt` 为 `true` 的项，或结论与 AI 建议不同的项，必须填 `human_evidence`（可核实的外部依据），否则导入拒绝；标为 `unsure` 的项除外。`note` 不得原样粘贴 `ai_reason`。
4. 导入（校验通过才全部写入 `task7.eval_label`；默认要求 120 项齐全，可用 `--allow-partial` 部分导入）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate import /opt/etl/outputs/task7/<抽取>/cv0001/eval/eval_items_labeled.csv --annotator 付林轩
```

5. 计算指标并写 `metrics.json`、`metrics.md`（总体、按层、按字段）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate metrics
```

指标定义：`unsure` 单独计数，不进任何分母；分母为零记“无定义”。动作准确率 = 系统动作（`resolved` 对 `resolve`，`conflict_unresolved` 对 `keep_conflict`）与人工一致的项 / 非 `unsure` 项；值准确率 = 系统 `resolved` 且人工 `resolve` 的项里值一致的比例（动作不一致的项由动作准确率体现）；`conflict_unresolved` 精确率 = 系统保留冲突的项里人工也保留的比例；召回率 = 人工保留冲突的项里系统也保留的比例。指标只代表分层样本，不代表全量准确率。
