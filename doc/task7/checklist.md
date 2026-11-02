# 任务 7 AC 完成情况（处理不同来源的信息冲突）

状态：2026-10-30 编写，2026-11-02 随 spec 修订为 AC00–AC22。AC01–AC22 尚未执行，全部保持未勾选并标注 `（计划）`。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询（带 `SET default_transaction_read_only = on;`）。

一次跑全部测试（计划）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全量通过（任务 1–6 的 `792 passed` 不减少，外加任务 7 新增测试）。

## AC00 计划文件规范

- [x] 2026-11-02 AI 文档检查通过（五个二级标题顺序正确，R01–R66 均被 AC 与任务覆盖，AC00–AC22 一一对应，未执行条目保持未勾选）；用户同日同意按复核结论修订。

```bash
grep -n "^## " doc/task7/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task7/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task7/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task7/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条为 `66`。

## AC01 输入版本、实体边界与处理范围

- [ ] 未执行。

```bash
# （计划）验证只读指定版本、两层实体边界、无关联合集与三类 IMDb 关联可靠度
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py tests/integration/test_task7_movies_scalar.py -k "ac01"
```

期望（计划）：测试通过，退出码 `0`。

## AC02 候选保留、状态语义与版本属性隔离

- [ ] 未执行。

```bash
# （计划）验证候选互斥状态、missing 语义、冲突类型闭集、合集属性不下推与版本差异保留
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py tests/integration/test_task7_movies_scalar.py -k "ac02"
```

期望（计划）：测试通过，退出码 `0`。

## AC03 标题、原名与别名

- [ ] 未执行。

```bash
# （计划）验证主标题与原名分离、别名表、无 IMDb 跨版本冲突保留与熔断回退页面标题
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac03"
```

期望（计划）：测试通过，退出码 `0`。

## AC04 上映年份决策表与推断上界隔离

- [ ] 未执行。

```bash
# （计划）验证 8 个年份场景与推断上界不混入上映年
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac04"
```

期望（计划）：测试通过，退出码 `0`。

## AC05 片长分层与逐版本差异分类

- [ ] 未执行。

```bash
# （计划）验证 runtime_diff 分类、PAL 证据要求、作品层汇总与无 IMDb 跨版本冲突
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac05"
```

期望（计划）：测试通过，退出码 `0`。

## AC06 内容分级规范化与跨版本消解

- [ ] 未执行。

```bash
# （计划）验证简称全称归一、具体分级胜过 NR/Unrated、双具体分级保留冲突与未知分级
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac06"
```

期望（计划）：测试通过，退出码 `0`。

## AC07 类型映射、词面处置与多源并集

- [ ] 未执行。

```bash
# （计划）验证词面规整、复合词拆分、noise 与 unmapped 逐行保留、并集来源标记
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac07"
```

期望（计划）：测试通过，退出码 `0`。

## AC08 演职员并集、角色边界、顺序与覆盖范围

- [ ] 未执行。

```bash
# （计划）验证导演与演员并集、主演只取页面、稳定 person_id、署名顺序与 cast_coverage
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac08"
```

期望（计划）：测试通过，退出码 `0`。

## AC09 版本层标量与多值特征

- [ ] 未执行。

```bash
# （计划）验证发行商归一、发售日语义与标记、discs 选择记录、7 项多值并集
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py -k "ac09"
```

期望（计划）：测试通过，退出码 `0`。

## AC10 共享 `tconst` 去重与分类

- [ ] 未执行。

```bash
# （计划）验证重复问题合并与三类分类
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_link_assess.py -k "ac10"
```

期望（计划）：测试通过，退出码 `0`。

## AC11 消解版本、阶段依赖、复用与差异

- [ ] 未执行。

```bash
# （计划）验证同参复用、按依赖链增量重算与 report diff
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py -k "ac11"
```

期望（计划）：测试通过，退出码 `0`。

## AC12 分批事务恢复与并发咨询锁

- [ ] 未执行。

```bash
# （计划）验证四个分批阶段的故障恢复与并发互斥
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py -k "ac12"
```

期望（计划）：测试通过，退出码 `0`。

## AC13 Airflow DAG 与核对后才成功

- [ ] 未执行。

```bash
# （计划）验证 DAG 拓扑、重试配置、成功、复用、上游缺失与 verify 失败后重跑
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_dag.py
```

期望（计划）：测试通过，退出码 `0`。真实 scheduler 复核见 `doc/task7/runbook.md`（可选，T7-06 编写）。

## AC14 报告、实例与三个只读查询

- [ ] 未执行。

```bash
# （计划）验证 summary.json、examples.md、三个查询与无评论正文导出
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_report.py -k "ac14"
```

期望（计划）：测试通过，退出码 `0`。

## AC15 盲测评价工具

- [ ] 未执行。

```bash
# （计划）验证开发池排除、配额与保底、证据不读候选状态、存疑项依据、指标手算
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_evaluate.py -k "ac15"
```

期望（计划）：测试通过，退出码 `0`。

## AC16 规则纯函数与边界情况

- [ ] 未执行。

```bash
# （计划）验证标题、PAL、MPAA、类型词面与多人串纯函数
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py
```

期望（计划）：测试通过，退出码 `0`。

## AC17 多运行与多阶段键共存

- [ ] 未执行。

```bash
# （计划）验证 run_id 校验、reused 时读 succeeded 计数、阶段键防串读与 check 异常检出
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_report.py tests/integration/test_task7_runs.py -k "ac17"
```

期望（计划）：测试通过，退出码 `0`。

## AC18 正式库全量运行、复用与只读核对

- [ ] 未执行。

```bash
# （计划）正式库只读核对
docker compose --profile crawl run --rm -T crawler python -m etl_task7.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`。

```bash
# （计划）作品层各字段状态分布
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT field_name, status, count(*) FROM task7.movie_field_selection WHERE conflict_version_id = 'cv0001' GROUP BY field_name, status ORDER BY field_name, status;"
```

期望（计划）：9 个字段的三种状态合计各为 `8659`。

```bash
# （计划）版本层行数与运行审计
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT count(*) FROM task7.edition_resolved WHERE conflict_version_id = 'cv0001'; SELECT status, conflict_version_id, airflow_run_id FROM task7.run ORDER BY started_at;"
```

期望（计划）：`9590`，以及 `t7_full_1`（`succeeded|cv0001`）与 `t7_full_2`（`reused|cv0001`）。

```bash
# （计划）错配熔断与署名对齐统计
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT cross_source_mismatch_halt, count(*) FROM task7.movie_resolved WHERE conflict_version_id = 'cv0001' GROUP BY 1 ORDER BY 1;"
```

期望（计划）：`t|<熔断数>` 与 `f|<其余>`，合计 `8659`；数字在 T7-08 实测后写入。

## AC19 正式库盲测标注与指标

- [ ] 未执行。

```bash
# （计划）核对已导入标注
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT annotator, human_action, count(*) FROM task7.eval_label GROUP BY 1, 2 ORDER BY 2;"
```

期望（计划）：标注人 `付林轩`，各 `human_action` 合计 `120`。

```bash
# （计划）输出评价指标
docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate metrics
```

期望（计划）：含 `labeled_total: 120`、`action_accuracy`、`value_accuracy`、`conflict_retention`。

## AC20 迁移版本、全量测试与仓库卫生

- [ ] 未执行。

```bash
# （计划）正式库迁移版本
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration;"
```

期望（计划）：`001,101,102,103,201,301,302,401,501,601`。

```bash
# （计划）写语句目标只在 task7
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py -k "ac20"
```

期望（计划）：测试通过，退出码 `0`。

```bash
# （计划）全量测试
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全量通过，792 项不减少。

```bash
# （计划）仓库卫生
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_items|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望（计划）：无匹配行，输出 `grep-exit=1`。

## AC21 关联评估与错配熔断

- [ ] 未执行。

```bash
# （计划）验证四信号、姓名兼容比较、熔断条件、单信号不熔断与强关联不熔断
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_link_assess.py -k "ac21"
```

期望（计划）：测试通过，退出码 `0`。

## AC22 同片署名对齐与多人串

- [ ] 未执行。

```bash
# （计划）验证错别字对齐、最高等级优先、并列不对齐、多人串全段与部分命中、不新造 person_id
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac22"
```

期望（计划）：测试通过，退出码 `0`。
