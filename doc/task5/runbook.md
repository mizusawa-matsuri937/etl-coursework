# 任务 5 运行手册（人物姓名归一与消歧）

命令在项目根目录运行。任务 5 只读 `public`、`task2`、`task3`、`task4`、`imdb`，结果写入 `etl_movies` 的 schema `task5`（迁移 `401`），导出写入 `outputs/task5/`（不入库、不入仓库）。输入是任务 4 映射版本 `map0002`（规则 `t4r2`）与 IMDb 快照 `imdb_2026-10-29`，二者须已在正式库中。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task5_names.py tests/unit/test_task5_rules.py tests/integration/test_task5_mentions.py tests/integration/test_task5_external.py tests/integration/test_task5_resolve.py tests/integration/test_task5_movie_person.py tests/integration/test_task5_runs.py tests/integration/test_task5_dag.py tests/integration/test_task5_report.py tests/integration/test_task5_evaluate.py
```

集成测试会话开始时会清空 `etl_test`（含 `task5`、`task4`、`imdb`、`task3`、`task2` schema）；下一节的端到端数据要在测试之后从头准备。

## 2. 端到端验证（真实 scheduler，写 etl_test）

DAG `amazon_movies_task5_person` 跑三次：成功、同输入重跑、不存在的任务 4 映射版本。合成数据来自测试夹具（手写，非真实商品、人物或 IMDb 文件）。

1. 准备 `etl_test`：合成任务 3 数据、合成 IMDb 快照（直接装载进库）、真正运行任务 4 匹配得到 `map0001`（脚本经标准输入传入，会清空 `etl_test`），输出一行 JSON，其中有 `extraction_id`：

```bash
docker compose --profile test run --rm -T etl-test python - < scripts/seed_task5_e2e.py
```

2. 切换到端到端配置（Airflow 写 `etl_test` 与 `outputs/e2e/`，允许故障注入，重试间隔 2 秒）：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up -d
```

3. 成功（把 `<extraction_id>` 换成第 1 步输出的值；以下 `COMPOSE_FILE` 让脚本用 e2e 配置；默认参数下任务 4 映射版本取该抽取最新的 `map0001`，因为 `map0002` 不存在）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task5_person --run-id t5_e2e_ok1 --conf '{"extraction_id":"<extraction_id>"}'
```

期望：八个任务 success，`DAG run t5_e2e_ok1: success`，退出码 0；`outputs/e2e/task5/<extraction_id>/pmap0001/t5_e2e_ok1/` 有 `summary.json`、`report.md`、`persons.csv`、`aliases.csv`、`movie_person.csv`、`decisions_sample.csv`、`t4_upgrade_hints.csv`、`examples.md`。2026-10-30 实测合成数据：123 条 resolvable 提及 → 83 个人物（external_verified 33、page_only 34、review_pending 16），迭代 2 轮，MoviePerson 158 条（director 37、starring 57、cast 64），升级线索 1 个，别名合并实例 4 组、不合并实例 5 组（合成数据里别名合并不足 5 组；正式库的要求见 AC16）。

4. 同输入重跑：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task5_person --run-id t5_e2e_rerun1 --conf '{"extraction_id":"<extraction_id>"}'
```

期望：八个任务 success；`task5.run` 新增一行 `reused`，人物映射版本仍是 `pmap0001`。

5. 不存在的任务 4 映射版本：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task5_person --run-id t5_e2e_missing1 --conf '{"extraction_id":"<extraction_id>","t4_mapping_version_id":"map9999"}'
```

期望：`check_inputs failed`（只尝试 1 次，`check_inputs-try1.json`），`mentions`、`link_external`、`resolve`、`movie_person`、`verify` 为 `upstream_failed`，`finalize_report success`（写失败汇总），`run_gate failed`，`DAG run ...: failed`，退出码 1；报告在 `outputs/e2e/task5/_failures/t5_e2e_missing1/`。

6. 暂时错误重试（可选；重做前先删除标记文件 `outputs/e2e/t5_marker1`）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task5_person --run-id t5_e2e_transient1 --conf '{"extraction_id":"<extraction_id>","fault_injection":"transient_once:/opt/etl/outputs/e2e/t5_marker1"}'
```

期望：success，`mentions` 第 2 次尝试成功，`outputs/e2e/task5/_failures/t5_e2e_transient1/mentions-try1.json` 记着第 1 次失败。

7. 回到正常配置（`.env` 里的 `IMDB_HOST_DIR` 等保持不变）并确认服务正常：

```bash
docker compose up -d
```

```bash
docker compose ps
```

期望：postgres、airflow-apiserver（healthy）、scheduler、dag-processor 都是 Up。新 DAG 文件放进 `dags/` 后正常运行的 Airflow 会解析它，导入错误用下面的命令确认（期望 `No data found`）：

```bash
docker compose exec -T airflow-scheduler airflow dags list-import-errors
```

2026-10-30 实测：三次（加可选的重试）运行结果与上述期望一致（`t5_e2e_ok1` success、`t5_e2e_rerun1` success 且 `reused`、`t5_e2e_missing1` failed、`t5_e2e_transient1` success），随后已回到正常配置。

## 3. 正式运行（T5-07，由用户另行安排）

### 3.1 运行前后各取一次任务 4 表行数（核对“未被写入”）

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT 'product_movie', count(*) FROM task4.product_movie UNION ALL SELECT 'movie', count(*) FROM task4.movie UNION ALL SELECT 'merge_decision', count(*) FROM task4.merge_decision UNION ALL SELECT 'mapping_version', count(*) FROM task4.mapping_version"
```

### 3.2 全量运行

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task5_person --run-id t5_full_1 --conf '{"imdb_snapshot_id":"imdb_2026-10-29"}' --timeout 7200
```

抽取默认取最近一次全量清单（`f1e8b845…`），任务 4 映射版本默认 `map0002`，规范化 `t5n1`，规则默认 `t5r1`（其他版本用 `rule_version` 参数）。同参数重跑（换 run id）为 `reused`。`limit` 只限制提及阶段的商品数，之后的步骤会失败、不生成人物映射版本，仅用于试跑。

### 3.3 只读核对、实例与查询

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.check
```

期望：退出码 0，`"problems": []`。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.report examples
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.query person <person_id>
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.query director "<导演姓名>"
```

统计与导出：`report summary`、`report export --run-key <名>`；两个人物映射版本之间的差异：`report diff <旧版本> <新版本>`。

## 4. 结果位置

| 内容 | 位置 |
|---|---|
| 人物提及、外部 ID 候选与判定、升级线索 | `task5.person_mention`、`link_candidate`、`link_decision`、`t4_upgrade_hint` |
| 作品内组、跨作品候选与判定、迭代 | `task5.person_group`、`group_member`、`person_candidate`、`person_decision`、`iteration_round`、`cluster_check` |
| 人物与映射 | `task5.person_identity`、`person_mapping_version`、`person`、`person_external_id`、`mention_person`、`person_alias`、`mapping_event` |
| MoviePerson | `task5.movie_person`、`movie_person_source` |
| 运行审计、人工决策 | `task5.run`、`run_batch`、`entity_stage`、`quality_issue`、`human_decision` |
| 评价 | `task5.dev_pool`、`eval_set`、`eval_pair`、`eval_label`、`cluster_sample`、`cluster_label` |
| 报告与导出 | `outputs/task5/<抽取>/<人物映射版本>/<run_key>/`：`summary.json`、`report.md`、`persons.csv`、`aliases.csv`、`movie_person.csv`、`decisions_sample.csv`、`t4_upgrade_hints.csv`、`examples.md` |
| 待复核队列 | `outputs/task5/<抽取>/<人物映射版本>/review/review_queue.csv` |
| 评价表与簇抽查表 | `outputs/task5/<抽取>/<人物映射版本>/eval/` |
| 失败记录 | `outputs/task5/_failures/<run_key>/` |

## 5. 改规则与比较版本

- 改 `src/etl_task5/rules.py` 的阈值、分值或特征开关时，用 `RuleSet.with_version` 派生新版本并登记到 `RULESETS`（不改已登记版本），再用 DAG 参数 `rule_version` 运行。只改规则版本时不重新抽取提及，只重算外部 ID 判定及以后的阶段，生成新的人物映射版本。
- 改 `src/etl_task5/names.py` 要升 `NORM_VERSION`，从提及开始重算。
- 比较两个人物映射版本（列出 `person_id` 变化的提及与原因）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.report diff pmap0001 pmap0002
```

## 6. 人工纠正与待复核队列

导出按“采纳后能连起的作品数”降序的待复核队列（已存在时不覆盖，加 `--force` 才覆盖）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.review queue
```

在文件的 `decision` 列填 `must_link` 或 `cannot_link`（可在 `note` 写原因），然后导入（路径为容器内路径）。导入整体一个事务，两个不同 IMDb 人物的 `must_link`、不存在的提及都会让整个文件被拒绝；导入只写人工决策，之后再触发一次 DAG（换新的 run id）得到新的人物映射版本：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.review import /opt/etl/outputs/task5/<抽取>/<人物映射版本>/review/review_queue.csv --annotator <姓名>
```

## 7. 失败与恢复

| 情况 | 处理 |
|---|---|
| 抽取不存在、任务 4 映射版本不存在或不属于该抽取、IMDb 快照未装载或缺少标题键、规则版本未登记、迁移 302 未应用 | `check_inputs` 确定性失败，不重试，下游 `upstream_failed` |
| 同一抽取已有另一个任务 5 运行持有锁 | 步骤确定性失败（`RunBusy`），不自动重试；等对方结束后换 run id 重跑 |
| 数据库暂时断连 | Airflow 最多重试 2 次；已提交的批次保留，重试只处理没有标记的实体；归并与 MoviePerson 两个全局步骤整体重做 |
| 归并已提交、MoviePerson 失败 | 重试沿用已生成的人物映射版本，只补 MoviePerson |
| 进程被杀 | 未提交批次无部分行；运行停在 `running`，下次拿到锁时改为 `interrupted` 后续接 |
| 核对失败 | 门禁失败；按 `outputs/task5/_failures/<run_key>/verify-try1.json` 的原因处理后换 run id 重跑 |
