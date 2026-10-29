# 任务 4 运行手册（定义并识别同一电影）

命令在项目根目录运行。任务 4 只读 `public`、`task2`、`task3`，结果写入 `etl_movies` 的 schema `imdb` 与 `task4`，导出写入 `outputs/task4/`（不入库）。IMDb 文件放在仓库外，只读挂载到容器的 `/data/imdb`，不进仓库、不进镜像。

## 1. 测试

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py tests/unit/test_task4_match.py tests/integration/test_task4_imdb.py tests/integration/test_task4_editions.py tests/integration/test_task4_match_db.py tests/integration/test_task4_cluster.py tests/integration/test_task4_dag.py tests/integration/test_task4_report.py tests/integration/test_task4_evaluate.py
```

集成测试会话开始时会清空 `etl_test`（含 `task4`、`imdb`、`task3`、`task2` schema）；下一节的端到端数据要在测试之后从头准备。

## 2. 端到端验证（真实 scheduler，写 etl_test）

两个 DAG 各跑三次：成功、同输入重跑、不存在的 IMDb 快照。合成数据来自测试夹具（手写，非真实商品或 IMDb 文件）。

1. 准备 `etl_test` 的合成任务 3 数据与合成 IMDb 快照（脚本经标准输入传入，会清空 `etl_test`），输出一行 JSON，其中有 `extraction_id`：

```bash
docker compose --profile test run --rm -T etl-test python - --imdb-out /opt/etl/outputs/e2e/imdb_fixture < scripts/seed_task4_e2e.py
```

2. 切换到端到端配置，并把 IMDb 挂载指向合成快照（命令行变量优先于 `.env`）：

```bash
IMDB_HOST_DIR="$PWD/outputs/e2e/imdb_fixture" docker compose -f compose.yaml -f compose.e2e.yaml up -d
```

3. IMDb 装载 DAG（Airflow 的 run id 不能重复，重做时换新的；以下 `COMPOSE_FILE` 让脚本用 e2e 配置）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id imdb_reference_task4_load --run-id t4_e2e_imdb_ok1 --conf '{"snapshot_id":"imdb_2026-10-30"}'
```

期望：四个任务 success，`DAG run t4_e2e_imdb_ok1: success`，退出码 0。

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id imdb_reference_task4_load --run-id t4_e2e_imdb_rerun1 --conf '{"snapshot_id":"imdb_2026-10-30"}'
```

期望：success（`load_snapshot` 的结果是 `reused`）。

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id imdb_reference_task4_load --run-id t4_e2e_imdb_missing1 --conf '{"snapshot_id":"imdb_2099-01-01"}'
```

期望：`check_files failed`（只尝试 1 次），`load_snapshot`、`verify_snapshot` 为 `upstream_failed`，`run_gate failed`，`DAG run ...: failed`，退出码 1；报告与 `check_files-try1.json` 在 `outputs/e2e/task4/_failures/t4_e2e_imdb_missing1/`。

4. 匹配 DAG（把 `<extraction_id>` 换成第 1 步输出的值）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task4_match --run-id t4_e2e_match_ok1 --conf '{"extraction_id":"<extraction_id>","imdb_snapshot_id":"imdb_2026-10-30"}'
```

期望：七个任务 success；`outputs/e2e/task4/<extraction_id>/map0001/t4_e2e_match_ok1/` 有 `summary.json`、`report.md`、`movies.csv`、`product_movie.csv`、`decisions_sample.csv`、`examples.md`。合成数据下：电影类商品 18 个 → 作品 13 部，多商品作品 3 部，待复核 4 个。

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task4_match --run-id t4_e2e_match_rerun1 --conf '{"extraction_id":"<extraction_id>","imdb_snapshot_id":"imdb_2026-10-30"}'
```

期望：success；`task4.run` 新增一行 `reused`，映射版本仍是 `map0001`。

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task4_match --run-id t4_e2e_match_missing1 --conf '{"extraction_id":"<extraction_id>","imdb_snapshot_id":"imdb_2099-01-01"}'
```

期望：`check_inputs failed`（只尝试 1 次），其余核心步骤 `upstream_failed`，`finalize_report success`（写失败汇总），`run_gate failed`，DAG failed；报告在 `outputs/e2e/task4/_failures/t4_e2e_match_missing1/`。

5. 暂时错误重试（可选；重做前先删除标记文件）：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task4_match --run-id t4_e2e_match_transient1 --conf '{"extraction_id":"<extraction_id>","imdb_snapshot_id":"imdb_2026-10-30","fault_injection":"transient_once:/opt/etl/outputs/e2e/t4_marker1"}'
```

期望：success，`normalize` 的 `try_number` 为 2，`outputs/e2e/task4/_failures/t4_e2e_match_transient1/normalize-try1.json` 记着第 1 次失败。

6. 回到正常配置（`.env` 里的 `IMDB_HOST_DIR` 指向真实快照的父目录）：

```bash
docker compose up -d
```

## 3. 正式运行

### 3.1 下载 IMDb（联网，需用户许可）

下载五个 `.tsv.gz`（约 1.9 GB）到仓库外的 `../imdb/<下载日期>/`，并写 `download.json`（地址、起止时间、`Last-Modified`、字节数、SHA-256）。IMDb 数据限个人、非商业使用，不得再分发：

```bash
python3 scripts/fetch_imdb.py --confirm --date 2026-10-29
```

在 `.env` 里加一行 `IMDB_HOST_DIR=<imdb 目录的绝对路径>`（包含 `<下载日期>/` 的那一层），然后 `docker compose up -d` 让挂载生效。快照编号为 `imdb_<下载日期>`。

### 3.2 装载 IMDb 快照

```bash
python3 scripts/run_dag_e2e.py --dag-id imdb_reference_task4_load --run-id t4_imdb_load_1 --conf '{"snapshot_id":"imdb_2026-10-29"}' --timeout 10800
```

期望：四个任务 success。同一快照再触发（换 run id）只记 `reused`。命令行等价用法（不经 Airflow）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.imdb load --snapshot-id imdb_2026-10-29
```

### 3.3 全量匹配

```bash
python3 scripts/run_dag_e2e.py --dag-id amazon_movies_task4_match --run-id t4_full_1 --conf '{"imdb_snapshot_id":"imdb_2026-10-29"}' --timeout 10800
```

抽取默认取最近一次全量清单（`f1e8b845…`），任务 3 规则 `t3r2`，标准化 `t4n1`，匹配规则默认 `t4r1`（其他版本用 `rule_version` 参数）。同参数重跑（换 run id）为 `reused`。`limit` 只限制标准化的商品数，之后的步骤会失败、不生成映射，仅用于试跑。

### 3.4 只读核对与实例

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.check
```

期望：退出码 0，`"problems": []`。（核对输出里的 `product_edition_rows`、`movie_edition_relations`、`tv_series_relations`、`decisions_by_tier` 等数字见 [checklist](checklist.md) AC14。）

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.report examples
```

期望：`cross_edition_merges` 与 `confusable_not_merged` 各 5 组，每组带特征与源记录定位。统计与导出：`report summary`、`report export --run-key <名>`。

## 4. 结果位置

| 内容 | 位置 |
|---|---|
| IMDb 快照 | `etl_movies` 的 `imdb.snapshot`、`source_file`、`title`、`title_other`、`title_aka`、`title_director`、`title_principal`、`name` |
| 商品版本、断言、候选、决策 | `task4.product_edition`、`field_assertion`、`source_record`、`imdb_title_key`、`candidate`、`merge_decision` |
| 作品、映射、历史 | `task4.movie_identity`、`mapping_version`、`movie`、`product_movie`、`product_match_status`、`mapping_event`、`cluster_check` |
| 运行审计 | `task4.run`、`run_batch`、`entity_stage`、`quality_issue` |
| 评价 | `task4.dev_pool`、`eval_set`、`eval_pair`、`eval_label`、`cluster_sample`、`cluster_label` |
| 报告与导出 | `outputs/task4/<抽取>/<映射版本>/<run_key>/`：`summary.json`、`report.md`、`movies.csv`、`product_movie.csv`、`decisions_sample.csv`、`examples.md` |
| IMDb 装载汇总 | `outputs/task4/imdb/<快照>/<run_key>/summary.json` |
| 评价表与簇抽查表 | `outputs/task4/<抽取>/<映射版本>/eval/`（`eval_pairs.csv`、`cluster_sample.csv`、`LABELING.md`、导入后的 `metrics.json`、`metrics.md`） |
| 失败记录 | `outputs/task4/_failures/<run_key>/` |

## 5. 改规则与比较版本

- 改 `src/etl_task4/match.py` 的权重、阈值时，新建一个 `RuleSet` 并用新版本号登记到 `RULESETS`（不改已登记版本），再用 DAG 参数 `rule_version` 运行。只改规则版本时不重新标准化、不重新生成候选，只新增该规则版本的决策和一个新的映射版本。
- 改 `src/etl_task4/normalize.py` 要升 `NORM_VERSION`，从标准化开始重算。
- 比较两个映射版本（容器内，列出 `movie_id` 变化的商品、事件与新旧判定档）：

```bash
docker compose --profile crawl run --rm -T crawler python -c "import json; from etl_task1.config import load_settings; from etl_task1.db import connect; from etl_task4.cluster import diff_versions; c = connect(load_settings()).__enter__(); print(json.dumps(diff_versions(c, 'map0001', 'map0002'), ensure_ascii=False, indent=1))"
```

## 6. 人工纠正

CSV 列：`kind`（`must_link`、`cannot_link`、`detach`）、`left`、`right`（`detach` 留空）、`note`、`annotator`。导入整体一个事务，随后自动重算并生成新的映射版本：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.review import /opt/etl/outputs/task4/decisions.csv --extraction-id f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8 --imdb-snapshot imdb_2026-10-29 --annotator <姓名>
```

## 7. 评价与标注（AC13、AC15）

AI 只做预标注草稿（2026-10-29 应用户要求）：`eval_pairs_draft.csv` 与 `cluster_sample_draft.csv` 已填好 `label`/`cluster_label`，另有 `ai_doubt`（低/中/高）与 `ai_reason` 两列；用户逐条审核修正后，用草稿文件导入并以用户为 `--annotator`，多出的两列导入时忽略。原始导出文件保持空白。顺序固定：先冻结开发池，调参只看开发池，冻结规则版本之后再导出评价表。

1. 冻结开发池（按 `md5(标题键)` 排序的前 20% 标题键，只做一次）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate freeze
```

2. 导出 120 对评价表和 20 簇抽查表（已存在时不覆盖，避免丢失标注）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate export
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate clusters-export
```

文件在 `outputs/task4/f1e8b845…/<映射版本>/eval/`，同目录的 `LABELING.md` 是标注口径。
3. 标注口径（用户一人标注）：
   - `eval_pairs.csv` 的 `label` 填 `same`（同一部创作作品：各种载体、语言、地区、包装、剪辑版、修复版、加长版等版本）、`different`（翻拍、续集、同名不同作品、完全不同的作品），或 `unsure`（页面与 IMDb 的证据都无法判断，并写 `note`；不计入指标）。
   - 依据页面（`*_amazon_url`）与 IMDb（`*_imdb_url`），不要凭记忆。表里不显示系统的判定。
   - 合集成员行（`left_entity` 形如 `ASIN#序号`）问的是这个成员是不是右边商品所代表的作品。
   - `cluster_sample.csv`：逐簇在该簇任一行的 `cluster_label` 填 `same_work` 或 `mixed`；`mixed` 时在混入商品所在行的 `outlier` 列填 `x`。
4. 导入标注（路径为容器内路径；`<姓名>` 换成标注人）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate import /opt/etl/outputs/task4/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/<映射版本>/eval/eval_pairs.csv --annotator <姓名>
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate clusters-import /opt/etl/outputs/task4/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/<映射版本>/eval/cluster_sample.csv --annotator <姓名>
```

导入会列出明显矛盾项（例如双方关联同一部 IMDb 作品、导演一致，却标为 `different`），只供复看，不改标注。
5. 指标（写 `metrics.json`、`metrics.md`）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate metrics
```

输出 Precision、Recall、F1（分母为 0 记“无定义”）、误合并与漏合并实例、不确定数、复核覆盖率、待复核率和簇抽查结果。指标只代表评价样本。

## 8. 失败与恢复

| 情况 | 处理 |
|---|---|
| IMDb 文件大小或哈希与 `download.json` 不符 | `check_files` 确定性失败，库里什么都不写；重新下载到新的日期目录 |
| IMDb 装载失败（单事务回滚） | 快照标 `failed`，重跑从头重读；同一快照的并发装载拿不到锁时按暂时错误重试 |
| 抽取不存在、任务 3 分类不完整、IMDb 快照未装载、规则版本未登记 | `check_inputs` 确定性失败，不重试 |
| 同一抽取已有另一个匹配运行持有锁 | 步骤确定性失败（`RunBusy`），不自动重试；等对方结束后换 run id 重跑 |
| 数据库暂时断连 | Airflow 最多重试 2 次；已提交的批次保留，重试只处理没有标记的实体，聚类步骤整体重做 |
| 进程被杀 | 未提交批次无部分行；运行停在 `running`，下次拿到锁时改为 `interrupted` 后续接 |
| 核对失败 | 门禁失败；按 `outputs/task4/_failures/<run_key>/verify-try1.json` 的原因处理后换 run id 重跑 |
