# 任务 5 AC 完成情况（人物姓名归一与消歧）

状态：2026-10-29 编写计划；2026-10-30 按用户要求加强没有 IMDb 关联作品的跨作品人物匹配，AC 重新编号为 AC00–AC17。2026-10-30 用户审批通过计划；AC01–AC13 已用合成夹具通过（T5-01 至 T5-06），AC14–AC17 针对正式库，未执行（T5-07、T5-08）。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。标注“（计划）”的命令依赖尚未实现的代码或测试，期望值是预定口径，实现后实际运行核对再改写（包括 `-k` 选择器与通过数）。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：2026-10-30 完成 T5-06 后为 `689 passed`（任务 1–4 的 505 项不变，任务 5 新增 184 项）。

## AC00 计划文件规范

- [x] 2026-10-30 AI 文档检查通过（五个板块、R01–R47、AC00–AC17 一一对应）；同日用户审批通过。

```bash
grep -n "^## " doc/task5/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task5/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task5/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task5/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（spec 与 checklist 的 AC 编号一一对应）；第三条为 `47`。

## AC01 人物提及的范围与来源

- [x] 2026-10-30 通过：选中 7 项（范围、来源、占位署名、边界不确定、旧映射版本不串读）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_mentions.py -k "scope or source or placeholder or ambiguous"
```

期望：`7 passed, 5 deselected`（另 5 项为输入检查、分批恢复、阶段键、锁、姓名拆分入库，同文件全部运行为 `12 passed`）。

## AC02 姓名规范化与比较键

- [x] 2026-10-30 通过：AC02 的每个姓名一个参数化案例，另覆盖兼容等级与详情要点逗号串。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task5_names.py
```

期望：`39 passed`。

## AC03 外部人物 ID：作品演职员

- [x] 2026-10-30 通过：8 个等级/判定案例参数化，另有改名与作品外同名、未关联商品与决策证据、至多一个 auto、重跑不新增。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_external.py -k "credits"
```

期望：`14 passed, 7 deselected`。

## AC04 外部人物 ID：作品年表反查与任务 4 升级线索

- [x] 2026-10-30 通过：自动关联、别名置信度 0.80、两部同名作品与两个同名已核实人物为复核、从未核实/角色不同不关联、年份差 3 年复核、升级线索、合集成员归属、任务 4 的表行数与内容不变。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_external.py -k "filmography"
```

期望：`8 passed, 13 deselected`。

## AC05 同作品归并与跨作品消歧

- [x] 2026-10-30 通过：规则层判定表 15 行 + 数据库层 7 个案例（同作品、导演兼演员、无佐证、共同合作者、不同 nconst、年代冲突、链、缩写）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task5_rules.py tests/integration/test_task5_resolve.py -k "ac05"
```

期望：`22 passed, 26 deselected`。

## AC06 合作者佐证迭代与只记录的特征

- [x] 2026-10-30 通过：迭代 3 轮，每轮新增合并边 2、1、0；输入乱序重跑得到相同的 `person_id`；开关关闭时判定档不变、特征照存，打开稀有度后只有“稀有名 + 同名合作者”升为自动。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task5_rules.py tests/integration/test_task5_resolve.py -k "ac06"
```

期望：`5 passed, 43 deselected`。

## AC07 决策、标识、别名、映射版本、人工纠正与待复核队列

- [x] 2026-10-30 通过：`person_id` 稳定且不含 nconst/姓名、重复运行为 `reused`、别名类型与次数、队列按影响降序、导入生成新版本且原版本可查、差异带原因、合并两个不同 nconst 的文件整体拒绝。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_resolve.py -k "ac07"
```

期望：`14 passed, 10 deselected`。

## AC08 MoviePerson 的角色、顺序与来源

- [x] 2026-10-30 通过：署名行演员有 `starring` 与 `cast`、详情要点演员只有 `cast`，导演兼演员有两种角色，署名顺序取最小并标 `order_conflict`，每条关系列出全部来源提及与标签，合集演员关联到成员作品、另一位为 `unattributed`，重复构建不重写，故障注入回滚后重跑完成。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_movie_person.py
```

期望：`11 passed`。

## AC09 幂等、改规则与版本差异

- [x] 2026-10-30 通过：第二次运行为 `reused` 且目标表行数不变；换规则版本不重新抽取提及、差异不含无关作品且每个变化带原因；换任务 4 映射版本生成新人物映射版本，差异恰为被拆出的那条提及，锚点未变的 `person_id` 不变。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_runs.py -k "reused or rule_change or mapping_change"
```

期望：`3 passed, 7 deselected`。

## AC10 分批恢复与并发锁

- [x] 2026-10-30 通过：批大小 2，在提及、途径一、途径二、跨作品判定的第 2 批提交前注入故障，只有第 1 批的行，重跑只补剩余批次，行数与一次跑完相同；聚类与 MoviePerson 故障无部分行；另一个连接持锁时运行 `RunBusy`、不留行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_runs.py -k "fault or lock"
```

期望：`7 passed, 3 deselected`。

## AC11 Airflow DAG

- [x] 2026-10-30 通过（测试库）：DAG 定义与依赖顺序、`all_done`、重试 2 次、成功与重跑（`reused`）、不存在映射版本在检查步骤失败且不重试、下游未运行、暂时错误可重试、锁冲突确定性失败、故障注入后同一 run_key 重试沿用同一条运行。
- [x] 2026-10-30 真实 scheduler（可选复核）：经 `compose.e2e.yaml` 触发 `t5_e2e_ok1`（success）、`t5_e2e_rerun1`（success，`reused`）、`t5_e2e_missing1`（failed，`check_inputs` 只试 1 次、下游 `upstream_failed`、门禁失败、失败记录在 `_failures`），另有暂时错误重试 `t5_e2e_transient1`（success）；步骤与期望见 [runbook](runbook.md) 第 2 节。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_dag.py
```

期望：`17 passed`。

## AC12 报告、实例与查询

- [x] 2026-10-30 通过：统计与库中逐项一致（提及、合并前后、按途径的外部 ID、迭代、状态、MoviePerson、特征开关影响数、升级线索），实例含别名合并与不合并（附外部 ID 或冲突证据、源记录定位，用户标注的优先并注明结论），两个查询返回原始署名、别名、决策链、作品，同名的两个导演分别列出；导出文件哈希与记录一致且无 HTML；只读核对对篡改的库报问题。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_report.py
```

期望：`18 passed`。

## AC13 评价工具

- [x] 2026-10-30 通过：开发池为按 `md5(姓氏键)` 的前 20% 且冻结后不变；评价表两边姓氏键都不在开发池、不含系统判定与途径，各层数量符合计划（S7 候选都在开发池，由 S4 补足并记实际层数）；导入后 Precision、Recall、F1 与逐对手算一致并按层与按途径给出，误合并与漏合并实例被列出，“不确定”单独计数，矛盾项只列出；无预测合并时 Precision、Recall、F1 为“无定义”；复核覆盖率与待复核率单独输出；簇抽查优先抽合作者佐证产生的人物。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_evaluate.py
```

期望：`8 passed`。

## AC14 正式库全量运行

- [ ] 未执行。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`；输出 JSON 的 `"problems": []`。逐项只读查询（按途径的外部 ID、迭代轮数、任务 4 升级线索数、任务 4 表未被写入）在 T5-07 实际运行后补写。

## AC15 用户标注与指标

- [ ] 未执行。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.evaluate metrics
```

期望（计划）：退出码 0；输出 Precision、Recall、F1（按层与按途径）、误合并与漏合并实例、不确定数、复核覆盖率、待复核率与簇抽查混入数，并注明标注人与样本范围。

## AC16 正式实例与查询

- [ ] 未执行。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.report examples
```

期望（计划）：至少 5 组别名合并、5 组同名或近似名不合并，每组有源记录定位；`query person` 与 `query director` 的复核命令在 T5-07 用正式库中的实际 ID 补写。

## AC17 迁移、测试与仓库内容

- [ ] 未执行。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration"
```

期望（计划）：`001,101,102,103,201,301,302,401`（2026-10-30 计划时实际为 `001,101,102,103,201,301,302`）；另跑全部测试与 `git ls-files`，仓库中没有 IMDb 文件、导出、评价表与 `.env`。
