# 任务 3 AC 完成情况（电影页面筛选）

状态：2026-10-29 由 AI 执行：AC00–AC11、AC13 通过；同日用户按本文命令人工复核（规则 t3r1），全部通过。之后规则升为 t3r2，受影响的 AC00、AC04、AC11、AC12 已由 AI 重新核对，用户尚未复核这部分。AC12：用户同日对照抽查表做了人工抽检，口头结论为“已分类的基本没有问题”，但没有逐条填写标注、也没有导入，因此没有一致率数字，保持未勾选。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。标注“（计划）”的命令依赖尚未实现的代码或测试，期望值是预定口径，实现后实际运行核对再改写。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：`336 passed`（2026-10-29 规则 t3r2；任务 1 196、任务 2 89、任务 3 51。用户复核 t3r1 时为 333）。

## AC00 计划文件规范

- [x] 2026-10-29 通过（AI 文档检查；实现中修订 R11、R14、R16、AC04 后复查通过；用户复核后按 t3r2 修订 R11、R14、R15、AC04，再次复查通过）。

```bash
grep -n "^## " doc/task3/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task3/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task3/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task3/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（spec 与 checklist 的 AC 编号一一对应）；第三条为 `30`。

## AC01 输入分组与无页面的待确认

- [x] 2026-10-29 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_classify.py -k "input_groups or no_page_pending"
```

期望：`2 passed`。

## AC02 快照缺失与哈希不一致

- [x] 2026-10-29 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_classify.py -k "missing_and_hash"
```

期望：`1 passed`。

## AC03 证据区域与字段抽取

- [x] 2026-10-29 通过（单元测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task3_extract.py
```

期望：`6 passed`。

## AC04 分类规则

- [x] 2026-10-29 通过（单元测试；18 个合成页逐个参数化，后 3 个为 t3r2 修复的已知问题；用户复核的是 t3r1 时的 15 页版本）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task3_rules.py
```

期望：`22 passed`。

## AC05 分类依据与冲突证据

- [x] 2026-10-29 通过（集成测试；含测试库端到端发现并修正的“无信号页面也要有证据”）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_classify.py -k evidence
```

期望：`2 passed`。

## AC06 评论保留与不写上游表

- [x] 2026-10-29 通过（集成测试；正式库见 AC11）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_classify.py -k "reviews or upstream_untouched"
```

期望：`1 passed`。

## AC07 同版本复用与规则版本差异

- [x] 2026-10-29 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_classify.py -k "reused or rule_version"
```

期望：`2 passed`。

## AC08 分批提交、续跑与并发锁

- [x] 2026-10-29 通过（集成测试）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_classify.py -k "batch or lock or limit or abandoned"
```

期望：`4 passed`。

## AC09 Airflow DAG

- [x] 2026-10-29 通过（集成测试＋真实 scheduler 经 `compose.e2e.yaml` 在 `etl_test` 运行成功、重跑、暂时错误、不存在的抽取各一次）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_dag.py
```

期望：`9 passed`（另有 4 条 Airflow 弃用警告，与任务 1、任务 2 的 DAG 相同）。

真实 scheduler 的运行记录（只读，Airflow 元数据库）：

```bash
docker compose exec -T postgres psql -U postgres -d airflow_meta -c "SELECT run_id, task_id, state, try_number FROM task_instance WHERE dag_id = 'amazon_products_task3_classify' AND run_id IN ('t3_e2e_ok2', 't3_e2e_rerun2', 't3_e2e_transient2', 't3_e2e_bad_extraction2') AND task_id IN ('check_input', 'classify_products', 'run_gate') ORDER BY run_id, task_id"
```

期望：12 行；`t3_e2e_bad_extraction2` 为 `check_input failed 1`、`classify_products upstream_failed 0`、`run_gate failed 1`；`t3_e2e_ok2`、`t3_e2e_rerun2` 三项均 `success 1`；`t3_e2e_transient2` 的 `classify_products` 为 `success 2`。重做步骤见 [runbook](runbook.md) 第 2 节（会把 Airflow 临时切到测试库）。

## AC10 统计报告与导出

- [x] 2026-10-29 通过（集成测试；正式报告见 AC11）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task3_report.py
```

期望：`2 passed`（另一项为 AC12 的抽查导出导入）。

## AC11 正式库全量分类

- [x] 2026-10-29 通过。t3r1：Airflow 运行 `t3_full_1` 约 5.5 分钟，重跑 `t3_full_2_rerun` 为 `reused`，用户同日复核通过；dp_page 20,661 个快照哈希全部一致；上游表除 `schema_migration` 多一行 `201` 外没有任何写入（`outputs/task3-run/upstream-*.txt`）。t3r2（当前版本）：运行 `t3_full_r2_1` 35 秒，只重算分类、未重新解析；电影 8,730（结构化 5,931、推断 2,799）、待确认 239,361；与 t3r1 相比 212 个产品变化（见 design“规则版本 t3r2”）。t3r2 结果由 AI 核对，用户尚未复核。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.check
```

期望（当前版本 t3r2）：退出码 0；`"rule_version": "t3r2"`，`"problems": []`，`manifest_rows`、`classification_rows`、`category_sum` 均为 `253059`，`missing_classification`、`classified_without_evidence`、`rows_without_any_evidence`、`snapshot_hash_mismatch`、`snapshot_missing`、`parse_error` 均为 `0`，`reviews_by_category_total` 为 `7911684`、`reviews_without_asin` 为 `12`、`records_read` 为 `7911696`。加 `--rule-version t3r1` 核对旧版本，同样退出码 0。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT input_group, category, count(*) FROM task3.product_classification WHERE extraction_id = 'f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8' AND rule_version = 't3r2' GROUP BY 1, 2 ORDER BY 1, 2"
```

期望：13 行；`dp_page` 下 `collection 860`、`concert_other 3256`、`movie 8730`、`non_movie 17`、`pending 6963`、`tv_series 835`；其余七组都只有 `pending`：`access_restricted 4152`、`automation_title 1493`、`not_collected 223933`、`not_found 2378`、`other_site 167`、`parse_failed 62`、`timeout 213`。（t3r1 时 dp_page 为 861、3256、8808、25、6876、835。）

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT airflow_run_id, rule_version, status, parsed, classified FROM task3.classify_run ORDER BY started_at"
```

期望：`t3_full_1 | t3r1 | succeeded | 20661 | 253059`，`t3_full_2_rerun | t3r1 | reused | 0 | 0`，`t3_full_r2_1 | t3r2 | succeeded | 0 | 253059`。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.classify diff --from t3r1 --to t3r2
```

期望：退出码 0，开头为 `"changed": 212`。

```bash
shasum -a 256 outputs/task3/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/t3r1/t3_full_1/classification.csv outputs/task3/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/t3r1/t3_full_2_rerun/classification.csv outputs/task3/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/t3r2/t3_full_r2_1/classification.csv
```

期望：前两行（t3r1 两次运行）哈希相同，均为 `42e2bbdd3cf1c5d467fa5caf8a729bcd488fdb03d97139a605ead2c6748e6260`；第三行（t3r2）为 `d9d260fecb352c4518629d2cc53bd964a00f341f2eefed2f23281ed7232eb96e`。完整报告在各目录的 `report.md`。

## AC12 人工抽查

- [ ] 2026-10-29 用户对照 t3r1 抽查表（120 行）人工抽检，口头结论“已分类的基本没有问题”，但未逐条填写标注、`task3.manual_review` 为 0 行，没有一致率数字，不勾选。规则升为 t3r2 后已重新导出抽查表（115 行，非电影只有 15 个可抽；与开发样本重叠 0、与 t3r1 抽查表重叠 16）。导出导入程序已由集成测试验证（AC10 命令中的第 2 项）。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task3.report review-export
```

期望：退出码 0；`"rows": 115`，`by_stratum` 为 `non_movie` 15、其余五类各 `20`，`sha256` 为 `11aae69304d9c01b2e990fde6488a4ed54bddd315d68639a04d4e4bae80f6d0c`（重复导出结果相同）。标注方法与导入命令见 [runbook](runbook.md) 第 6 节。

## AC13 迁移、测试与交付

- [x] 2026-10-29 通过。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -c "SELECT version FROM schema_migration ORDER BY 1"
git ls-files | grep -cE '^outputs/|\.html\.gz$|review_sample'
```

期望：第一条为 `001`、`101`、`102`、`103`、`201` 五行；第二条输出 `0`。全部测试见本文开头（`336 passed`）。
