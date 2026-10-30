# 任务 6 运行手册（补全上映信息）

命令在项目根目录运行。任务 6 只读 `public`、`task2`、`task3`、`task4`、`task5`、`imdb`，结果写入 `etl_movies` 的 schema `task6`（迁移 `501`），导出写入 `outputs/task6/`（不入库、不入仓库）。输入是任务 4 映射版本 `map0002`（规则 `t4r2`）、任务 5 人物映射版本 `pmap0001`（规则 `t5r1`）与 IMDb 快照 `imdb_2026-10-29`，这些数据须已在正式库中。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_candidates.py tests/integration/test_task6_cooccur.py tests/integration/test_task6_select.py tests/integration/test_task6_product_dates.py tests/integration/test_task6_reviews.py tests/integration/test_task6_bounds.py tests/integration/test_task6_runs.py tests/integration/test_task6_dag.py tests/integration/test_task6_report.py tests/integration/test_task6_evaluate.py
```

集成测试会话开始时会清空 `etl_test`；下一节的端到端数据要在测试之后从头准备。

## 2. 端到端验证（真实 scheduler，写 etl_test）

DAG `amazon_movies_task6_release` 跑三次：成功、同输入重跑、不存在的任务 4 映射版本。合成数据来自测试夹具。

1. 准备 `etl_test`（脚本经标准输入传入，会清空 `etl_test`），输出一行 JSON，其中有 `extraction_id`：

```bash
docker compose --profile test run --rm -T etl-test python - < scripts/seed_task6_e2e.py
```

2. 切换到端到端配置（Airflow 写 `etl_test` 与 `outputs/e2e/`，允许故障注入，重试间隔 2 秒）：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
```

3. 成功（把 `<extraction_id>` 换成第 1 步输出的值）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task6_release --run-id t6_e2e_ok1 --conf '{"extraction_id":"<extraction_id>"}'
```

期望：十个任务 success，`DAG run t6_e2e_ok1: success`，退出码 0；`outputs/e2e/task6/<extraction_id>/rv0001/t6_e2e_ok1/` 产生报告与 CSV 文件。

4. 同输入重跑：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task6_release --run-id t6_e2e_rerun1 --conf '{"extraction_id":"<extraction_id>"}'
```

期望：十个任务 success；`task6.run` 新增一行 `reused`，上映信息版本仍是 `rv0001`。

5. 不存在的任务 4 映射版本：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task6_release --run-id t6_e2e_missing1 --conf '{"extraction_id":"<extraction_id>","t4_mapping_version_id":"map9999"}'
```

期望：`check_inputs failed`（只尝试 1 次），下游任务 `upstream_failed`，`finalize_report success`，`run_gate failed`，`DAG run ...: failed`，退出码 1；报告在 `outputs/e2e/task6/_failures/t6_e2e_missing1/`。

6. 回到正常配置并确认服务正常（必须执行）：

```bash
docker compose up -d
```

```bash
docker compose ps
```

确认 DAG 没有导入错误：

```bash
docker compose exec -T airflow-scheduler airflow dags list-import-errors
```

期望：`No data found`。

## 3. 正式运行（T6-07 / T6-11）

### 3.1 运行前核对任务 1–5 全部主表行数

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT 'review_record', count(*) FROM public.review_record UNION ALL SELECT 'review_staging', count(*) FROM public.review_staging UNION ALL SELECT 'product_manifest', count(*) FROM task2.product_manifest UNION ALL SELECT 'page_field', count(*) FROM task3.page_field UNION ALL SELECT 'movie', count(*) FROM task4.movie UNION ALL SELECT 'product_movie', count(*) FROM task4.product_movie UNION ALL SELECT 'merge_decision', count(*) FROM task4.merge_decision UNION ALL SELECT 'person', count(*) FROM task5.person UNION ALL SELECT 'movie_person', count(*) FROM task5.movie_person ORDER BY 1"
```

### 3.2 全量运行前先冻结开发池（R36，1,732 部作品）

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.evaluate freeze
```

### 3.3 触发 DAG 与复用验证

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task6_release --run-id t6_full_1 --conf '{"imdb_snapshot_id":"imdb_2026-10-29"}' --timeout 7200
```

迁移 `501` 由 `evaluate freeze` 或 `check_inputs` 步骤自动应用。同一参数换 run id（如 `t6_full_2`）重跑为 `reused`：

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task6_release --run-id t6_full_2 --conf '{"imdb_snapshot_id":"imdb_2026-10-29"}' --timeout 7200
```

### 3.4 只读核对、报告导出与查询

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.check
```

期望：退出码 0，`"problems": []`，`"passed": true`。

按真实成功运行的 `run_id` 导出报告、实例与 CSV：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.report export
```

查询指定作品上映信息：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.query movie <movie_id>
```

查询缺失与推断上界作品：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.query gaps --status missing
```

## 4. 评价与标注（T6-06 / T6-10 / T6-11 / T6-12）

评价设计遵守 spec R36–R39 与 AC17、AC19、AC23。开发池按 `md5(movie_id)` 排序取前 20% 作品（1,732 部）在首次全量运行前冻结入 `task6.dev_pool`，只用于观察默认值是否需微调，评价池不含开发池作品。导出 120 部作品分七层（E1–E7），抽样后按 `md5(movie_id)` 全局确定性打乱再顺序分配 `t6_eval_0001`–`t6_eval_0120`；导出表保留任务 4 商品原始标题断言（含括号年份），不含系统年份、状态、层与上界（避免锚定与分层泄露）。AI 只在草稿副本 `eval_items_draft.csv` 里依据盲测公开信息与 `imdb` 公开表独立比对给出预标注建议（不读取系统采用年份或推断上界，也不用 DVD/VHS 发售日、评论时间或推断上界填补 `ai_true_year`），标注必须由人工审核确认后导入。

1. 冻结开发池（按 `md5(movie_id)` 排序的前 20% 作品，在全量运行前执行，幂等）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.evaluate freeze
```

2. 导出评价表与非循环 AI 预标注草稿（120 部作品，七层抽样并全局打乱）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.evaluate export
```

文件导出至 `outputs/task6/<抽取>/<上映信息版本>/eval/`：
- `eval_items.csv`：供人工核验与导入的正式盲测评价表（不含系统判定）。
- `eval_items_draft.csv`：供 AI 给出非循环预标注建议的草稿模板（含 `ai_true_year`、`ai_tconst_label`、`ai_doubt`、`ai_reason`）。
- `LABELING.md`：标注口径说明。

3. 标注导入（T6-12 执行，由付林轩核对后导入）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.evaluate import /opt/etl/outputs/task6/<抽取>/<上映信息版本>/eval/eval_items_labeled.csv --annotator 付林轩
```

导入会校验字段合法性，并检测矛盾项（`tconst_label = same` 但真实年份与 IMDb 记录年份相差大于 1 年），写入 `task6.eval_label`。

4. 计算指标与输出报告：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.evaluate metrics
```

计算完全一致年份准确率、相差 ≤1 年准确率、`tconst` 匹配正确率（按来源与总体）、推断上界成立率（按证据类型与总体）、缺失作品可查到率，不确定单独计数，结果写回 `metrics.json` 与 `metrics.md`。
