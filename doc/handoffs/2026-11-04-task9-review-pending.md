# 交接：任务 9（字段级数据血缘）正式库运行完成，评价待审核

日期：2026-11-04。写给接手任务 9 收尾的人（用户本人，或协助的 agent）。

- **任务 9 状态**：T9-00 至 T9-07 完成；T9-08 的正式库运行、核对、报告导出与 60 项审核表、AI 草稿导出完成，**付林轩尚未审核，没有导入任何标注**（AC21 未执行，T9-08 未勾选）。分支 `task9-field-lineage`，本地提交，**尚未推送，也未合并到 `main`**。
- **本文范围**：只写接手所需的事实；规则、设计与证据以 `doc/task9/` 为准，结果见 [`doc/task9/report.md`](../task9/report.md)，运行与审核方法见 [`doc/task9/runbook.md`](../task9/runbook.md)。
- **数字是快照**：2026-11-04 正式库只读查询结果，动手前用第 7 节命令核对，不一致以实际为准。

## 1. 先做什么

1. 读 `CLAUDE.md`、`README.md`、本文，再读 `doc/task9/report.md` 与 `doc/task9/checklist.md` 的 AC21。
2. 请付林轩审核 `eval_items_draft.csv`（第 4 节），审核完成后按第 4 节导入、计算指标，再补 report 第 7 节、checklist AC21、tasks T9-08、README。
3. 用户决定是否推送、开 PR、合并到 `main`。

## 2. 任务 9 做了什么

- **输入**（R01）：抽取 `f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`、`t3r2`/`t3p1`、`map0002`、`pmap0001`、`rv0001`、`cv0003`、`rd0001`、`imdb_2026-10-29`。只读任务 1–8 与 `imdb`，不重算上游。
- **四层**：源记录（页面快照、IMDb 文件行、评论文件记录、人工修订）→ 字段断言（extract、candidate、derived）→ 派生边（12 种操作，带上游运行与 OpenLineage 标注）→ 字段选择（每字段或成员一行，带 `trace_status`）与影响闭包；评论层用视图 `task9.v_review_lineage`、`task9.v_product_lineage`。
- **断链补定位**：类型词面、版本多值特征、评论上界第二早评论用上游同一规则函数只读重放，全部一致，标 `relocated`；推断上界的全部依赖评论登记为 `aggregate` 父节点。
- **查询**：`python -m etl_task9.query field|product|review|source|person|impact`，`python -m etl_task9.impact verify-sources`，`python -m etl_task9.report diff|export`。
- **AC15**：用户 2026-11-04 选定方案 C：新血缘版本全量重建（约 7.5 分钟、4.2 GB/版本），由“`diff` 的变化字段 = 事前 `impact` 定位”核对（spec H09）。

## 3. 结果与数据位置

- 数据库 `etl_movies` 的 schema `task9`（迁移 `801`；清单 `001,101,102,103,201,301,302,401,501,601,701,801`），体量 4,222 MB（超 design 的 1.5–2.5 GB 预算）；`etl_movies` 40 GB，postgres 卷空闲约 104 GB。
- 血缘版本 `lv0001`（规则 `t9r1`，`verified`）：Airflow `t9_full_1` 生成（449 秒），`t9_full_2` 为 `reused`。阶段短键 `s11f2e418823b9fe`、`a0d345df84d5280a`、`d8f913d99aa00f0e`、`l7292c0631b27f9a`、`if9ab0bf333bd203`（`task9.lineage_version` 里有完整对应）。

| 项目 | 数值 |
|---|---|
| 源记录 | 429,323（快照 9,590 全部 `ok`、IMDb 202,298、评论 217,435、人工修订 0） |
| 断言 | 1,548,754（extract 1,062,036、candidate 156,568、derived 330,150） |
| 派生边 / select 边 | 2,106,585 / 493,144（悬空、缺父、环 0） |
| 字段选择 | 531,371（非空 469,671：traced 410,309、relocated 59,362；覆盖率 100%，规划试算 85.6%） |
| 重放补定位 | 类型词面 34,531、版本特征 46,027、第二早评论 3,015，`replay_mismatch` 0 |
| 影响闭包 | 887,035 行 |
| 质量问题 | 1（`nm17757708` 缺 `imdb.name` 行，`dangling_edge`） |

- 导出（仓库外，`.gitignore` 排除）：`outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/`：`t9_full_1/`（`summary.json`、`report.md`、`examples.md`、`quality_issues.csv`、`checksums.json`、`run_summary.json`）、`t9_full_2/`、`manual/`、`eval/`。演练导出在 `outputs/rehearsal_t9/`；端到端种子快照在 `outputs/e2e/t9_seed/`。
- 业务库副本 `etl_t9_rehearsal` 已于 2026-11-04 删除（删前全链路演练结果见 design“业务库副本全链路演练实测”）。

## 4. 评价：审核表、导入与指标

- 文件（目录 `outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/eval/`）：
  - `eval_items.csv`：60 项审核表（sha256 `aef18344c641c27682dbb2484b29ac551b404565c0284873d6232683903d1791`），`L1` 作品标量 15、`L2` 作品多值成员 15、`L3` 版本字段 10、`L4` 人物 10、`L5` 评论与商品归属 10（商品 5、评论 5），无补位；数据库 `task9.eval_set` `ev_lv0001`、`task9.eval_item` 60 行。
  - `eval_items_draft.csv`：AI 草稿（sha256 `fdaeaf2fa8be7c8a74904816e134ff178e65312cfd593f237eb5cee6a715ebb9`），由 `evaluate draft` 只读审核表与原始证据文件生成：`correct` 47、`unsure` 13（`ai_doubt` 13）；分层 `L1` 14/1、`L2` 10/5、`L3` 8/2、`L4` 10/0、`L5` 5/5。草稿不给 `incorrect`。
  - `LABELING.md`：标注规则。
- 审核方法（R47–R49）：付林轩逐项看 `path` 与 `evidence`，填 `label`（`correct`/`incorrect`/`unsure`）；`incorrect` 必须填 `error_type`（`wrong_source`、`wrong_value`、`missing_step`、`wrong_reason`）；与 `ai_label` 不同或 `ai_doubt = true` 的项必须填 `human_evidence`（标 `unsure` 的除外）；`note` 不能原样粘贴 `ai_reason`。另存为同目录 `eval_labels.csv`。**AI 草稿不是审核结论，不能代填后导入。**
- 导入与指标（审核完成后执行，工作目录为项目根目录）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.evaluate import --version lv0001 --file /opt/etl/outputs/task9/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/lv0001/eval/eval_labels.csv --annotator 付林轩
```

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.evaluate metrics --version lv0001
```

  导入任何一条不合格会整批拒绝、不写入。之后把指标写进 report 第 7.2 节，如实写“AI 预填、付林轩审核”，勾选 checklist AC21 与 tasks T9-08。**导入后不要再用 `evaluate export --force`**（会清除标注）。

## 5. 已做的决定

| 日期 | 决定 | 来源 |
|---|---|---|
| 2026-11-03 | H01 血缘覆盖作品、版本、映射、人物与评论归属全部字段；评分统计只解释口径 | 用户 |
| 2026-11-03 | H02 实体字段物化在 `task9`，评论层用视图 | 用户 |
| 2026-11-03 | H03 断链只读重放补定位，不一致只记录 | 用户 |
| 2026-11-03 | H04 评论上界登记全部依赖评论；60 项抽查，AI 预填、付林轩审核 | 用户 |
| 2026-11-04 | H09 AC15 方案 C：新血缘版本全量重建，不按实体增量复用 | 用户 |
| 2026-11-02 | 任务 7 的 `cv0001`、`cv0002` 先不删，全部任务完成后由用户决定 | 用户 |

## 6. 环境用法与踩过的坑

- 正式库只读查询带 `SET default_transaction_read_only = on;`；写入只经 runner 或 DAG。
- **评论文件只挂载在 Airflow 容器**（`/data/source/movies.txt.gz`）：`crawler` 容器里 `verify-sources` 把评论记为 `unavailable`（不算不一致），评论核验用 `docker compose exec -T airflow-scheduler python -m etl_task9.impact verify-sources --kinds review_record`。
- **快照证据在库外**：`outputs/task2/snapshots/`（28,913 个、4.7 GB）是 R09 的原始证据。**以后清理 `outputs/` 时必须保留 `outputs/task2/snapshots/`**，否则 `verify-sources` 与审核草稿会报快照缺失。
- **Airflow run-id 不能重复**：再触发 `amazon_movies_task9_lineage` 换新 run-id（同输入记 `reused`）。
- **测试只连 `etl_test`，不要并发跑两个 pytest**：两次 `docker compose --profile test run` 同时清空 `etl_test` 会互相破坏（本次误并发一次，出现 E/F，停掉后串行重跑全部通过）。全量测试 `docker compose --profile test run --rm etl-test pytest -q tests`，2026-11-04 为 `1287 passed`（约 6.5 分钟）。
- **演练副本**：`CREATE DATABASE … TEMPLATE etl_movies` 要先停 Airflow；建库、删库后都执行 `CHECKPOINT`（WAL 峰值，见任务 8 交接）。本次删副本后卷空闲 68 → 108 GB。
- Airflow 当前为正常配置（`ETL_ENV=prod`、`ETL_DB_NAME=etl_movies`），本次没有切换 compose 配置。
- **`CLAUDE.md` 未更新**：其中任务 9 的状态句仍写“T9-01 至 T9-08 未开始”等旧状态，本次由子代理执行，按安全约束没有改 `CLAUDE.md`，需用户自己更新或明确授权。

## 7. 核对命令

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration; SELECT lineage_version_id, status FROM task9.lineage_version; SELECT count(*) FROM task9.eval_item WHERE eval_set_id = 'ev_lv0001'; SELECT count(*) FROM task9.eval_label;"
```

期望：`SET` 之后为 `001,101,102,103,201,301,302,401,501,601,701,801`、`lv0001|verified`、`60`、`0`（导入后为 `60`）。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task9.check > /dev/null; echo "exit=$?"
```

期望：`exit=0`。

## 8. 遗留事项

1. 付林轩审核 60 项草稿 → 导入 → 指标 → 补 report 第 7.2 节、AC21、T9-08、README（第 4 节）。
2. 更新 `CLAUDE.md` 的任务 9 状态（第 6 节）。
3. 推送分支、PR 与合并由用户决定。
4. `outputs/` 清理时保留 `outputs/task2/snapshots/`；`cv0001`、`cv0002`、任务 8 体量、`outputs/rehearsal_t9/` 等演练输出，按用户要求待全部任务完成后统一处理。
5. `task9` 4.2 GB 超预算；如需减小，可考虑抽取边改视图或文本键改短（未估算、未实施）。

## 9. 文件索引

| 文件 | 内容 |
|---|---|
| `doc/task9/spec.md` | 规则 R01–R49、AC00–AC22、假设 H01–H09 |
| `doc/task9/design.md` | 设计、各阶段实现要点与演练、正式库实测 |
| `doc/task9/tasks.md`、`checklist.md` | 任务与 AC 状态、复核命令 |
| `doc/task9/runbook.md` | 测试、端到端、副本演练、正式运行、抽查评价 |
| `doc/task9/report.md` | 正式库运行与交付报告 |
| `sql/801_task9_schema.sql`、`sql/801_task9_views.sql` | 迁移与视图 |
| `src/etl_task9/`、`dags/amazon_movies_task9_lineage.py` | 源码与 DAG |
| `tests/integration/test_task9_*.py`、`tests/fixtures/task9/README.md` | 测试与夹具说明 |
