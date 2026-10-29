# 任务 4 AC 完成情况（定义并识别同一电影）

状态：2026-10-29 编写计划。AC00 由 AI 做文档检查，已通过；AC01–AC17 都未执行。用户已确认全部假设（H01–H11），尚未审阅计划全文。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。标注“（计划）”的命令依赖尚未实现的代码或测试，期望值是预定口径，实现后实际运行核对再改写。

一次跑全部测试（计划）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全部通过；任务 1–3 的 336 项不减少，另加任务 4 的测试。

## AC00 计划文件规范

- [x] 2026-10-29 通过（AI 文档检查；用户尚未审阅）。

```bash
grep -n "^## " doc/task4/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task4/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task4/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task4/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（spec 与 checklist 的 AC 编号一一对应）；第三条为 `42`。

## AC01 商品版本与匹配范围

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_editions.py -k "product_edition or match_scope"
```

期望（计划）：`2 passed`。

## AC02 IMDb 快照校验与装载

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_imdb.py
```

期望（计划）：全部通过，覆盖类型过滤、行号、`\N`、哈希不符拒绝且无部分行、重复装载 `reused`。

## AC03 标题键、说明分类与别名

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py -k "title"
```

期望（计划）：全部通过；spec AC03 的 8 个标题与 1 个别名逐个参数化。

## AC04 姓名键与片长

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py -k "name or runtime"
```

期望（计划）：全部通过。

## AC05 候选与三档判定

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_match.py tests/integration/test_task4_match_db.py -k "decision"
```

期望（计划）：全部通过；10 个案例逐个参数化，另有一项检查每个判定都有决策行。

## AC06 合集成员与评论口径

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_editions.py tests/integration/test_task4_match_db.py -k "collection"
```

期望（计划）：全部通过。

## AC07 聚类一致性

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py -k "consistency or no_chain"
```

期望（计划）：`2 passed`。

## AC08 稳定 movie_id、映射历史与人工纠正

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py -k "stable_id or human"
```

期望（计划）：全部通过。

## AC09 幂等与规则版本差异

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py -k "reused or rule_version_diff"
```

期望（计划）：`2 passed`。

## AC10 分批恢复与锁

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_dag.py -k "resume or lock"
```

期望（计划）：`2 passed`。

## AC11 Airflow DAG

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_dag.py -k "dag"
```

期望（计划）：全部通过。真实 scheduler 的成功、重跑、不存在快照三次运行，按 runbook（待写）复核，可选。

## AC12 报告导出与实例

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_report.py
```

期望（计划）：全部通过。

## AC13 评价工具与指标

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_evaluate.py
```

期望（计划）：全部通过，含无预测合并时 Precision 与 F1 为“无定义”。

## AC14 正式库全量匹配

- [ ] 未执行。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.check
```

期望（计划）：退出码 0，`"problems": []`，`product_edition_rows` 为 `253059`，`movie_edition_relations` 为 `8730`，`tv_series_relations` 为 `0`。

## AC15 正式评价与簇抽查

- [ ] 未执行（需要用户标注 120 对与 20 个簇）。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate metrics
```

期望（计划）：退出码 0，输出 Precision、Recall、F1（或“无定义”）、`unsure` 数、复核覆盖率、待复核率与簇抽查结果，并注明标注人与样本范围。

## AC16 跨版本合并与易混淆实例

- [ ] 未执行。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.report examples
```

期望（计划）：退出码 0，跨版本合并与易混淆未合并各至少 5 组，每组有源记录定位。

## AC17 迁移、测试与仓库内容

- [ ] 未执行。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration"
git ls-files | grep -Ei "imdb.*\.tsv|eval_pairs|\.env$"
```

期望（计划）：第一条含 `001,101,102,103,201,301,302`；第二条无输出。
