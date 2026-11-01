# 任务 7 AC 完成情况（处理不同来源的信息冲突）

状态：2026-10-30 在分支 `task7-conflict-resolution`（从 `main` 的 `649ac06` 建立）编写计划并通过 AC00 自检；同日用户确认作品层与版本层字段边界、IMDb 关联范围、演职员可信并集、标量容差与保留冲突状态边界、以及 120 项七层盲测评价方案。AC01–AC20 尚未执行，全部保持未勾选并标注 `（计划）`。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询（带 `SET default_transaction_read_only = on;`）。

一次跑全部测试（计划）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全量通过（任务 1–6 的 `792 passed` 不减少，外加任务 7 新增单元与集成测试）。

## AC00 计划文件规范

- [x] 2026-10-30 AI 文档检查通过（`spec.md` 严格只有五个二级标题且顺序正确，R01–R39 全部在 AC 表中引用，`spec.md` 与 `checklist.md` 的 AC00–AC20 编号一一对应，未执行条目保持未勾选并标注计划）；同日用户确认核心策略方案。

```bash
grep -n "^## " doc/task7/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task7/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task7/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task7/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（`spec.md` 与 `checklist.md` 的 AC 编号一一对应）；第三条为 `39`。

## AC01 输入版本、实体边界与 IMDb 关联范围

- [ ] 未执行。

```bash
# （计划）验证只读指定上游版本、作品层与版本层实体边界及三类 IMDb 关联可靠度
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py tests/integration/test_task7_movies_scalar.py -k "ac01"
```

期望（计划）：测试通过，退出码 `0`。

## AC02 原始候选保留、选择优先级与版本属性隔离

- [ ] 未执行。

```bash
# （计划）验证全部候选状态互斥保留、禁止无理由最后抓取/简单多数票、版本物理差异与评论原样可查
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py tests/integration/test_task7_movies_scalar.py -k "ac02"
```

期望（计划）：测试通过，退出码 `0`。

## AC03 作品标题与原名冲突消解

- [ ] 未执行。

```bash
# （计划）验证主标题与原名分离、页面载体后缀剥离、别名变体标记与无 IMDb 跨版本标题实质冲突保留
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac03"
```

期望（计划）：测试通过，退出码 `0`。

## AC04 上映年份分歧复核、跨字段错配熔断与推断上界隔离

- [ ] 未执行。

```bash
# （计划）验证 1 年容差、强关联重映年覆盖、弱匹配跨字段错配熔断及推断上界不混入上映年
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac04"
```

期望（计划）：测试通过，退出码 `0`。

## AC05 作品层标准片长与版本层片长冲突分类及消解

- [ ] 未执行。

```bash
# （计划）验证 1-2 分钟微差、PAL 24/25 帧率转换、剪辑版/多碟花絮、严重片长冲突及无 IMDb 跨版冲突保留
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac05"
```

期望（计划）：测试通过，退出码 `0`。

## AC06 内容分级（`mpaa_rating`）规范化与跨版本冲突消解

- [ ] 未执行。

```bash
# （计划）验证同 ASIN 简称/全称归一、具体分级胜过 NR/Unrated 及两种不同具体分级保留 conflict_unresolved
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac06"
```

期望（计划）：测试通过，退出码 `0`。

## AC07 电影类型（`genres`）受控词表映射、噪声过滤与多源并集

- [ ] 未执行。

```bash
# （计划）验证 30 词受控词表映射、复合词拆分、DVD/Movies & TV 噪声过滤与多源并集来源标记
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac07"
```

期望（计划）：测试通过，退出码 `0`。

## AC08 演职员跨源可信并集、角色边界、分号拆分与署名顺序消解

- [ ] 未执行。

```bash
# （计划）验证导演/演员并集、主演仅取页面 byline、imdb_person_ext 稳定 person_id、分号串拆分与署名顺序众数消解
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac08"
```

期望（计划）：测试通过，退出码 `0`。

## AC09 商品版本层（`ProductEdition`）内部冲突消解与多值特征并集

- [ ] 未执行。

```bash
# （计划）验证单 ASIN 内 publisher 词根归一与区域优先级、media_release 与 listing_first_available 区分、标量 discs 及 7 项多值版本特征并集
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py -k "ac09"
```

期望（计划）：测试通过，退出码 `0`。

## AC10 跨作品共享 `tconst`（126 项）对比与分类记录

- [ ] 未执行。

```bash
# （计划）验证 shared_tconst_conflict 对漏合并（likely_duplicate_movie）与弱匹配误挂（false_positive_link）的对比分类
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac10"
```

期望（计划）：测试通过，退出码 `0`。

## AC11 冲突消解版本、同参重跑复用与跨版本差异 `diff`

- [ ] 未执行。

```bash
# （计划）验证同参重跑 reused、修改规则或映射版本增量重算生成新版本及 FULL OUTER JOIN 跨版本 diff
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py -k "ac11"
```

期望（计划）：测试通过，退出码 `0`。

## AC12 分批事务恢复与并发咨询锁

- [ ] 未执行。

```bash
# （计划）验证版本层与作品层分批提交前故障注入后的断点恢复及并发抽取锁互斥
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py -k "ac12"
```

期望（计划）：测试通过，退出码 `0`。

## AC13 Airflow DAG `amazon_movies_task7_conflict` 与端到端验证

- [ ] 未执行。

```bash
# （计划）验证 DAG 拓扑、重试配置、无弃用警告及测试库成功/复用/失败阻断流程
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_dag.py
```

期望（计划）：测试通过，退出码 `0`。

## AC14 统计报告、典型冲突实例与三个只读查询

- [ ] 未执行。

```bash
# （计划）验证 summary.json 覆盖率与冲突分布、examples.md 实例、query movie|edition|conflicts 及无评论正文导出
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_report.py -k "ac14"
```

期望（计划）：测试通过，退出码 `0`。

## AC15 盲测评价工具与非循环 AI 预标注草稿验证

- [ ] 未执行。

```bash
# （计划）验证开发池排除、七层抽样、MD5 打乱盲测编号、非循环 AI 草稿生成及准确率/冲突保留指标计算
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_evaluate.py -k "ac15"
```

期望（计划）：测试通过，退出码 `0`。

## AC16 规则纯函数与边界情况单元测试

- [ ] 未执行。

```bash
# （计划）验证标题剥离、错配熔断、PAL 帧率公式、MPAA 规范化、类型映射、分号拆分与署名顺序众数纯函数
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py
```

期望（计划）：测试通过，退出码 `0`。

## AC17 多运行与多阶段键共存下的报告、查询与核对健壮性

- [ ] 未执行。

```bash
# （计划）验证真实 run_id 校验、reused 存在时按 succeeded 导出、阶段键过滤防串读及 check 异常检出
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_report.py tests/integration/test_task7_runs.py -k "ac17"
```

期望（计划）：测试通过，退出码 `0`。

## AC18 正式库全量运行、重跑复用与只读核对

- [ ] 未执行。

```bash
# （计划）正式库只读核对脚本检查
docker compose --profile crawl run --rm -T crawler python -m etl_task7.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`（JSON 中 `"problems": []` 且 `"passed": true`）。

```bash
# （计划）核对正式库作品层各字段消解状态分布（每字段合计 8,659 部）
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT field_name, status, count(*) FROM task7.movie_field_selection WHERE conflict_version_id = 'cv0001' GROUP BY field_name, status ORDER BY field_name, status;"
```

期望（计划）：输出 `SET` 及 9 个作品层字段的 `resolved`、`conflict_unresolved`、`missing` 分布，每个字段合计均为 `8659`。

```bash
# （计划）核对正式库版本层消解行数（合计 9,590 个商品）与运行审计记录（succeeded + reused）
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT count(*) FROM task7.edition_resolved WHERE conflict_version_id = 'cv0001'; SELECT status, conflict_version_id, airflow_run_id FROM task7.run ORDER BY started_at;"
```

期望（计划）：输出 `9590`，以及 `t7_full_1`（`succeeded|cv0001`）与 `t7_full_2`（`reused|cv0001`）两条运行记录。

## AC19 正式库 120 项盲测评价标注导入与正式指标

- [ ] 未执行。

```bash
# （计划）核对正式库已导入的 120 条评价标注
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT annotator, human_action, count(*) FROM task7.eval_label GROUP BY 1, 2 ORDER BY 2;"
```

期望（计划）：输出标注人 `付林轩` 及各 `human_action`（`resolve`、`keep_conflict`、`unsure`）计数，合计 `120`。

```bash
# （计划）输出正式库评价指标
docker compose --profile crawl run --rm -T crawler python -m etl_task7.evaluate metrics
```

期望（计划）：输出包含 `labeled_total: 120`、`action_accuracy`、`value_accuracy` 与 `conflict_retention` 的完整指标 JSON。

## AC20 迁移版本、全量测试与仓库卫生

- [ ] 未执行。

```bash
# （计划）核对正式库迁移版本
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration;"
```

期望（计划）：输出 `SET` 与 `001,101,102,103,201,301,302,401,501,601`。

```bash
# （计划）运行全量测试套件
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全量通过（任务 1–6 的 792 项不减少）。

```bash
# （计划）检查 Git 仓库卫生
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_items|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望（计划）：无匹配行，输出 `grep-exit=1`。
