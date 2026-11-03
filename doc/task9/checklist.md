# 任务 9 AC 完成情况（字段级数据血缘）

状态：2026-11-03 编写。AC00 已由 AI 运行文档检查并勾选；T9-01 完成后 AC02、AC05 通过并勾选，AC01、AC03 部分完成（未勾选）；其余未执行。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询（带 `SET default_transaction_read_only = on;`，`psql -Atc` 输出首行会多一个 `SET`）。标注“（计划）”的命令在对应任务实现前不可运行，实现后实际核对再改写期望。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：2026-11-03 任务 8 完成时为 `1186 passed`；T9-01 完成后为 `1218 passed`（新增 32 项，原有测试不减少）。

## AC00 计划文件规范

- [x] 2026-11-03 AI 文档检查通过：五个二级标题顺序正确，R01–R49 均被 AC 与 T9-01 至 T9-07 覆盖，AC00–AC22 与 spec 一一对应，未执行条目保持未勾选；用户同日确认 H01–H04。

```bash
grep -n "^## " doc/task9/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task9/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task9/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task9/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条为 `49`。规则覆盖的检查命令见 [tasks](tasks.md) T9-00，期望 `49 [] []`。

## AC01 输入版本、分层与上游只读

- [ ] 部分完成（T9-01，2026-11-03）：原始层与暂存层通过——上游各 schema 全表行数与摘要不变、迁移 801 新对象只在 `task9`、只读指定抽取、IMDb 快照、`t3p1`/`t4n1`、`rv`、`cv` 阶段键，版本不一致或 cv 未核实时确定性失败，并发确定性失败。“范围外实体不生成选择”“评分统计无逐条依赖边”待 T9-02、T9-03，故不勾选。

```bash
# 原始层与暂存层部分：只读指定版本、上游不变、只写 task9
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py -k "ac01"
```

期望：`10 passed, 22 deselected`，退出码 `0`。T9-03 实现后再加入 `test_task9_selections.py` 的 AC01 用例（计划）。


## AC02 源记录定位与获取时间

- [x] 2026-11-03 通过（T9-01）：四类源记录各有完整定位，获取时间与来源列正确（页面 `fetched_at`、IMDb `downloaded_at`、评论 `download_completed_at`、人工修订 `decided_at`）；`fetched_at` 为空的页面获取时间为 `NULL` 并写 `missing_acquired_at`；同一定位两次构建 `source_record_id` 相同且等于 Python 公式；同阶段键重跑与注入故障后续跑结果一致。业务库副本实测见 design“T9-01 演练实测”。

```bash
# 四类源记录、获取时间来源列、缺获取时间的质量问题、ID 稳定
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py -k "ac02"
```

期望：`7 passed, 25 deselected`，退出码 `0`。


## AC03 快照哈希核验

- [ ] 部分完成（T9-01，2026-11-03）：源记录层通过——正常、内容被改、文件缺失三个快照依次为 `ok`、`snapshot_hash_mismatch`、`snapshot_missing`，质量问题带期望与实际哈希或路径，构建不中断；业务库副本 9,590 个快照全部 `ok`。“相关字段 `trace_status = 'evidence_unverified'`”属于字段选择，待 T9-03 验证，故不勾选。

```bash
# 正常、内容被改、文件缺失三种快照（源记录层）
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py -k "ac03"
```

期望：`5 passed, 27 deselected`，退出码 `0`。


## AC04 评论层视图

- [ ] 未执行（计划）。

```bash
# 有效、隔离、不完整评论与 D3 家族复制、合集评论（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_selections.py -k "ac04"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC05 字段断言与未采用候选

- [x] 2026-11-03 通过（T9-01）：页面字段、任务 4 无父（extract）与有父（derived）断言、人物提及、IMDb 五类行、推断上界依赖评论都登记为断言，各列与上游逐行一致；`assertion_id` 两次构建相同且等于 Python 公式；Legend 标题候选的 `adopted`、`rejected`、`superseded_by_specific` 与任务 6 的 `selected`、`not_selected` 都登记并保留上游原因。业务库副本上 Legend `release_year` 的 3 条任务 6 候选与 3 条任务 7 候选全部登记（见 design“T9-01 演练实测”）。

```bash
# 各上游来源的断言、ID 稳定、三种候选状态保留
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_sources.py tests/integration/test_task9_assertions.py -k "ac05"
```

期望：`9 passed, 23 deselected`，退出码 `0`。


## AC06 多值成员与实体合并边

- [ ] 未执行（计划）。

```bash
# 对齐成员、同人多角色、多人串、商品到作品、人物归并（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_derivations.py -k "ac06"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC07 派生边闭集、无环与终止

- [ ] 未执行（计划）。

```bash
# 操作类型闭集、边属性、无环、路径终止于源记录、注入环与悬空边（计划）
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task9_rules.py tests/integration/test_task9_derivations.py -k "ac07"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC08 只读重放补定位

- [ ] 未执行（计划）。

```bash
# 类型词面、版本特征、第二早评论补定位与 replay_mismatch（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_relocate.py -k "ac08"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC09 推断上界的全部依赖

- [ ] 未执行（计划）。

```bash
# 评论上界 7 条 aggregate 父节点、直接依赖标记、合集排除、发售日期上界（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_derivations.py -k "ac09"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC10 字段选择、状态与一致性

- [ ] 未执行（计划）。

```bash
# 四种状态、trace_status、NULL 缺失原因、值不一致检出、人工修订路径（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_selections.py -k "ac10"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC11 查询 field

- [ ] 未执行（计划）。

```bash
# movie_id + 字段名 → 最终值、采用与未采用候选、原始证据、理由（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac11"
```

期望（计划）：相关用例全部通过，退出码 `0`。正式库演示命令见 design“演示查询”。

## AC12 查询 product 与 review

- [ ] 未执行（计划）。

```bash
# ASIN 与源评论 ID → 版本、作品、匹配状态（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac12"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC13 查询 source

- [ ] 未执行（计划）。

```bash
# 快照、tconst、nconst、评论 → 受影响实体与字段（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac13"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC14 查询 person

- [ ] 未执行（计划）。

```bash
# person_id → 原始署名、别名归并过程、参与作品（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_query.py -k "ac14"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC15 来源与规则变化定位、版本差异

- [ ] 未执行（计划）。

```bash
# verify-sources、impact --rule、增量重建与 report diff（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_impact.py -k "ac15"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC16 版本、幂等、恢复、并发与 DAG

- [ ] 未执行（计划）。

```bash
# 重跑 reused、注入故障后恢复、并发锁、DAG 拓扑与 verify 失败重跑（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_runs.py tests/integration/test_task9_dag.py -k "ac16"
```

期望（计划）：相关用例全部通过，退出码 `0`；真实 scheduler 端到端步骤见 runbook（计划，可选）。

## AC17 报告与实例

- [ ] 未执行（计划）。

```bash
# summary.json 与数据库一致、覆盖率分列、examples.md 覆盖四个查询与四类作业示例（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_report.py -k "ac17"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC18 抽查评价工具

- [ ] 未执行（计划）。

```bash
# 五层配额、审核表、AI 草稿、导入校验、指标（计划）
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_evaluate.py -k "ac18"
```

期望（计划）：相关用例全部通过，退出码 `0`。

## AC19 业务库副本全链路演练

- [ ] 未执行（计划）。步骤见 design“业务库副本演练”与 runbook（计划）；演练结论写入 design“业务库副本全链路演练实测”。

## AC20 正式库全量运行与核对

- [ ] 未执行（计划）。

```bash
# 正式库只读核对（计划）
docker compose --profile crawl run --rm -T crawler python -m etl_task9.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`。

```bash
# 血缘版本与 trace_status 分布（只读，计划）
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT lineage_version_id, status FROM task9.lineage_version; SELECT trace_status, count(*) FROM task9.field_selection WHERE lineage_version_id = 'lv0001' AND final_value IS NOT NULL GROUP BY 1 ORDER BY 1;"
```

期望（计划）：`lv0001|verified`；非空最终值的 `trace_status` 只有 `traced`、`relocated`（与 `manual`，正式库应为 0）。四个演示查询命令见 design“演示查询”。

## AC21 正式库抽查评价

- [ ] 未执行（计划）。审核表由 AI 预填、付林轩审核后导入；指标写入 report（计划）。

## AC22 迁移、测试与仓库内容

- [ ] 未执行（计划）。

```bash
# 迁移清单（只读，计划）
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration;"
```

期望（计划）：`SET` 之后为 `001,101,102,103,201,301,302,401,501,601,701,801`。

```bash
git ls-files | grep -E "^outputs/|\.env$|eval_items|\.tsv\.gz$|\.html\.gz$" | wc -l
```

期望（计划）：`0`。
