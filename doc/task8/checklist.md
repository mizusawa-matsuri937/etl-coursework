# 任务 8 AC 完成情况（评论清洗与重复识别）

状态：2026-11-03 编写。AC00 已由 AI 运行文档检查并勾选；AC01–AC18 尚未执行，全部保持未勾选并标注 `（计划）`。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询（带 `SET default_transaction_read_only = on;`，`psql -Atc` 输出首行会多一个 `SET`）。

一次跑全部测试（计划）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全量通过（任务 1–7 的 `1121 passed` 不减少，外加任务 8 新增测试）。

## AC00 计划文件规范

- [x] 2026-11-03 AI 文档检查通过：五个二级标题顺序正确，R01–R49 均被 AC 与 T8-01 至 T8-07 覆盖，AC00–AC18 与 spec 一一对应，未执行条目保持未勾选；用户同日确认 H01–H04。

```bash
grep -n "^## " doc/task8/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task8/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task8/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task8/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条为 `49`。规则覆盖的检查命令见 [tasks](tasks.md) T8-00，期望 `49 [] []`。

## AC01 输入版本、分层与原始记录全部保留

- [ ] 未执行。

```bash
# （计划）验证只读指定抽取与 cv0003、每条原始记录一行映射、隔离与不完整记录的状态、lr_ 前缀、public 不变
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py -k "ac01"
```

期望（计划）：测试通过，退出码 `0`。

## AC02 helpfulness 状态与不完整记录

- [x] 2026-11-03 通过（T8-01）：六种原值依次为 `ok`、`zero_zero`、`numerator_exceeds_denominator`、`negative`、`unparseable`、`missing`，SQL 判定与 Python 判定逐条一致；后四类各一条 `review_issue`，`0/0` 不写；缺评分与时间的记录 `key_complete = false` 并写 `incomplete_review`。演练库实测与试算一致（`ok` 5,869,509、`zero_zero` 1,980,001、分子大于分母 43、`missing` 12、不完整 12）。

```bash
# 验证六种 helpfulness 状态、0/0 不算异常、不完整记录
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task8_rules.py tests/integration/test_task8_clean.py -k "ac02"
```

期望：`12 passed, 17 deselected`，退出码 `0`。

## AC03 内容键只用原值、核验与 Reviewer

- [ ] 未执行。

```bash
# （计划）验证大小写、空白、摘要差异不合并，哈希碰撞拆组，显示名变体
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py -k "ac03"
```

期望（计划）：测试通过，退出码 `0`。

## AC04 评论家族

- [ ] 未执行。

```bash
# （计划）验证签名合组、Jaccard 0.5 连通、weak_support、family_id 与阈值变化
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_family.py -k "ac04"
```

期望（计划）：测试通过，退出码 `0`。

## AC05 去重层次、代表记录与稳定 ID

- [ ] 未执行。

```bash
# （计划）验证 D1、D2、D3、代表记录、helpfulness 选择、跨家族不合并、重跑 ID 不变
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py -k "ac05"
```

期望（计划）：测试通过，退出码 `0`。

## AC06 不合并的候选

- [ ] 未执行。

```bash
# （计划）验证 cross_family_same_content 与 near_duplicate 候选，候选不改变逻辑评论
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py tests/integration/test_task8_attribution.py -k "ac06"
```

期望（计划）：测试通过，退出码 `0`。

## AC07 商品与作品归属、合集口径

- [ ] 未执行。

```bash
# （计划）验证只经 edition 归属作品、合集只在商品层、其他类别不进作品
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_attribution.py -k "ac07"
```

期望（计划）：测试通过，退出码 `0`。

## AC08 评分统计组与评分口径

- [ ] 未执行。

```bash
# （计划）验证统计组、组内只计一次、共享数、去重前后平均分与敏感性数字
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_attribution.py -k "ac08"
```

期望（计划）：测试通过，退出码 `0`。

## AC09 版本、阶段依赖与复用

- [ ] 未执行。

```bash
# （计划）验证 reused、改阈值重算家族及下游、改 cv 只重算归属、report diff
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_runs.py -k "ac09"
```

期望（计划）：测试通过，退出码 `0`。

## AC10 分批恢复与并发锁

- [ ] 未执行。

```bash
# （计划）验证三个阶段第 2 批前故障后重跑一致、家族无半写入、并发失败
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_runs.py -k "ac10"
```

期望（计划）：测试通过，退出码 `0`。

## AC11 Airflow DAG 与核对后才成功

- [ ] 未执行。

```bash
# （计划）验证 9 节点拓扑、重试参数、上游缺失与 verify 失败分支；真实 scheduler 见 runbook
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dag.py
```

期望（计划）：测试通过，退出码 `0`；真实 scheduler 四种情况的运行记录写入完成情况。

## AC12 报告、实例与查询

- [ ] 未执行。

```bash
# （计划）验证 summary 与数据库一致、examples 覆盖全部类别、四个查询与口径说明、导出无正文全文
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_report.py
```

期望（计划）：测试通过，退出码 `0`。

## AC13 评价工具

- [ ] 未执行。

```bash
# （计划）验证开发池排除、六层配额、盲测不泄露、草稿不读结论、缺依据改判被拒、指标手算
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_evaluate.py
```

期望（计划）：测试通过，退出码 `0`。

## AC14 纯函数单元测试

- [x] 2026-11-03 通过（T8-01）：内容键构造（大小写、末尾空格、摘要、空与缺失、长度前缀、Unicode 按字符计）、分区桶、签名与 Jaccard 连通分量（单条评论家族、阈值 0.7）、层次与 helpfulness 并列、碰撞拆组与 ID、词集 Jaccard（空正文、ASCII 分词）、候选；另有 1 项集成测试核对 SQL 内容键哈希与分区桶与 Python 版本一致。

```bash
# 内容键、签名与连通分量、词集 Jaccard、ID 生成；SQL 与 Python 一致
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task8_rules.py tests/integration/test_task8_clean.py -k "ac14"
```

期望：`14 passed, 15 deselected`，退出码 `0`。

## AC15 业务库副本演练

- [ ] 未执行。

```bash
# （计划）演练结束后确认演练库已删除
docker compose exec -T postgres psql -U postgres -Atc "SELECT datname FROM pg_database ORDER BY 1"
```

期望（计划）：输出 `airflow_meta`、`etl_movies`、`etl_test`、`postgres`、`template0`、`template1`，没有 `etl_t8_rehearsal`；演练的运行记录、`check` 结果与试算差异解释写入完成情况与 design。

## AC16 正式库全量运行

- [ ] 未执行。

```bash
# （计划）只读核对
docker compose --profile crawl run --rm -T crawler python -m etl_task8.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`。

```bash
# （计划）每条原始记录一行映射（期望 SET 之后为 7911696）
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT count(*) FROM task8.review_mapping m JOIN task8.dedup_version v ON v.dedup_stage_key = m.dedup_stage_key WHERE v.review_dedup_version_id = 'rd0001';"
```

期望（计划）：`7911696`。

## AC17 正式库评价

- [ ] 未执行。

```bash
# （计划）导入标注后的指标
docker compose --profile crawl run --rm -T crawler python -m etl_task8.evaluate metrics
```

期望（计划）：`labeled_total` 为 120，标注人为付林轩，输出总体与六层指标。

## AC18 迁移、测试与仓库卫生

- [ ] 未执行。

```bash
# （计划）迁移版本
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration;"
```

期望（计划）：`001,101,102,103,201,301,302,401,501,601,701`。

```bash
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_items|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望：没有匹配行，只有 `grep-exit=1`（2026-11-03 T8-00 时实测如此；实现后再运行一次）。
