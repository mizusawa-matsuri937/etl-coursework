# 任务 6 AC 完成情况（补全上映信息）

状态：2026-10-30 编写计划，待用户审批；实现未开始。AC00 已做 AI 文档检查，待用户审批后勾选；AC01–AC18 均未执行。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。除 AC00 外，命令都是**（计划）**：选择器与期望在实现后按实际运行结果改写，改写前不得勾选。

一次跑全部测试（计划）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全部通过，任务 1–5 的 691 项不减少。

## AC00 计划文件规范

- [ ] 2026-10-30 AI 文档检查通过（五个板块、R01–R35、AC00–AC18 一一对应）；待用户审批。

```bash
grep -n "^## " doc/task6/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task6/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task6/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task6/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（spec 与 checklist 的 AC 编号一一对应）；第三条为 `35`。

## AC01 输入版本与处理对象

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "inputs or scope"
```

期望（计划）：选中的案例全部通过。

## AC02 日期语义分离与精度

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_select.py -k "semantics or precision"
```

期望（计划）：选中的案例全部通过。

## AC03 IMDb 关联作品的年份候选

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "imdb_link"
```

期望（计划）：选中的案例全部通过。

## AC04 任务 5 升级线索的使用与记录

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "hint"
```

期望（计划）：选中的案例全部通过，含任务 4、任务 5 表不变的断言。

## AC05 商品标题年份候选

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "title_year"
```

期望（计划）：选中的案例全部通过。

## AC06 择优顺序、状态与分歧

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_select.py -k "order or status or disagreement"
```

期望（计划）：选中的案例全部通过。

## AC07 商品发售日期与标记

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_product_dates.py
```

期望（计划）：全部通过。

## AC08 最早评论时间

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_reviews.py
```

期望（计划）：全部通过。

## AC09 推断上界的条件与不覆盖

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_bounds.py -k "condition or override or contradiction"
```

期望（计划）：选中的案例全部通过。

## AC10 预售、合集与跨版本的报告

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_bounds.py -k "presale or collection or cross_edition"
```

期望（计划）：选中的案例全部通过。

## AC11 版本、重跑复用与版本差异

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py -k "reused or version or diff"
```

期望（计划）：选中的案例全部通过。

## AC12 分批恢复与锁

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py -k "batch or lock"
```

期望（计划）：选中的案例全部通过。

## AC13 Airflow DAG

- [ ] 未执行。真实 scheduler 端到端为可选复核，步骤见 runbook（计划，T6-04 编写）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_dag.py
```

期望（计划）：全部通过。

## AC14 报告、实例与查询

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_report.py
```

期望（计划）：全部通过。

## AC15 评价工具

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py
```

期望（计划）：全部通过。

## AC16 正式库全量运行与只读核对

- [ ] 未执行。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`（输出 JSON 的 `"problems": []`）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = 'rv0001' GROUP BY status ORDER BY status"
```

期望（计划）：四种状态合计 8,659；具体数字在运行后填写。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT count(*) FROM task6.release_selection WHERE release_date IS NOT NULL OR precision <> 'year'"
```

期望（计划）：`0`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, airflow_run_id, release_version_id FROM task6.run ORDER BY started_at"
```

期望（计划）：一行 `succeeded` 与一行 `reused`，版本都是 `rv0001`；run id 在运行后填写。

## AC17 评价标注与指标

- [ ] 未执行；需要付林轩核查 AI 草稿后导入。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT annotator, year_label, count(*) FROM task6.eval_label GROUP BY 1, 2 ORDER BY 2"
```

期望（计划）：标注人 `付林轩`，`known` 与 `unsure` 合计 120。

## AC18 迁移、测试与仓库内容

- [ ] 未执行。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration"
```

期望（计划）：`001,101,102,103,201,301,302,401,501`。

```bash
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_items|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望：没有匹配行，只有 `grep-exit=1`（规划时已可运行，正式核对在 T6-06）。
