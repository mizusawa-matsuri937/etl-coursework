# 任务 6 AC 完成情况（补全上映信息）

状态：2026-10-30 编写计划；同日按用户要求补充来源；完成 T6-01 至 T6-07 初版后经独立审查（见 [review](review.md)），新增审查修复验收 AC21–AC23，并于同日完成 T6-08 至 T6-11 全部修复、测试与正式库清空重跑（AC01–AC18、AC20–AC23 均已验证通过，AC19 待付林轩复核评价草稿后于 T6-12 导入）。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。未执行的修复验收命令标注为**（计划）**：选择器与期望在实现后按实际运行结果改写，改写前不得勾选。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：`792 passed, 56 warnings`（任务 1–5 的 691 项不减少，任务 6 共 101 项）。

## AC00 计划文件规范

- [ ] 2026-10-30 AI 文档检查通过（五个板块、R01–R39、AC00–AC23 一一对应）；待用户审批。

```bash
grep -n "^## " doc/task6/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task6/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task6/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task6/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（spec 与 checklist 的 AC 编号一一对应）；第三条为 `39`。

## AC01 输入版本与处理对象

- [x] 2026-10-30 通过（夹具含两个任务 4 映射版本与两个人物映射版本、只经 `contains` 出现的成员作品、非参与匹配的剧集商品）。选择记录数等于作品数、旧版本多出的作品无记录、换人物映射版本后线索候选不产生。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "ac01"
```

期望：`3 passed, 10 deselected`。

## AC02 日期语义分离与精度

- [x] 2026-10-30 通过（IMDb 年份 1999 胜出且 `release_date` NULL、`precision=year`、`release_type=first_release`、区域 NULL；没有候选的作品保持 `missing`；全库无一年份补成日期、非 missing 的年份都等于采用候选的值；地区院线日期缺失原因 `no_permitted_dated_source`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_select.py -k "ac02"
```

期望：`3 passed, 7 deselected`。

## AC03 IMDb 关联作品的年份候选

- [x] 2026-10-30 通过（movie / tvMovie / video 三种 `titleType` 各一条候选，带快照、文件、行号、任务 4 关联依据、来源获取与抽取时间、置信度；`startYear` 为 NULL 的不产生候选）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "imdb_link"
```

期望：`1 passed, 12 deselected`。

## AC04 任务 5 升级线索的使用与记录

- [x] 2026-10-30 通过（未被占用的线索候选、`shared_tconst` 与质量问题、合集成员与多 `tconst` 只记录、`hint_ambiguous`；任务 4、任务 5 的表行数不变）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "hint or task4_and_task5"
```

期望：`4 passed, 9 deselected`。

## AC05 人物共现反查

- [x] 2026-10-30 通过（导演命中 + 标题实词子集 + 唯一 → 候选带命中姓名、断言 ID、演职员行号与标题键来源；得分 1、标题不吻合、编号排除各有评分记录写明未通过条件；两部命中且无评论年份 → `cooccur_ambiguous`；命中已占用作品 → `shared_tconst`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_cooccur.py -k "ac05"
```

期望：`7 passed, 5 deselected`。

## AC06 评论年份提及与消歧

- [x] 2026-10-30 通过（每条评论每个年份一票、晚于评论年份的不计、三个模式各计票；多部候选时经评论年份唯一选出并标 `review_year_tiebreak`、置信度 0.70；只有提及的作品仍为 `missing`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_cooccur.py -k "ac06"
```

期望：`4 passed, 8 deselected`。

## AC07 商品标题年份候选

- [x] 2026-10-30 通过（每个商品一条候选带原值、定位与断言 ID；一致 → `page_reported`、不一致 → `title_years_disagree`、单一 → `page_reported`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "title_year"
```

期望：`3 passed, 10 deselected`。

## AC08 择优顺序、状态与分歧

- [x] 2026-10-30 通过（F/G/H 都 `verified` 采用 IMDb 候选、G 相差 1 年只计数、H 有 `year_disagreement`（相差 4 年）与质量问题；I `hint_matched`；L 采用线索并有 `tconst_disagreement`；N `cooccur_matched`；每部作品恰好一条记录且状态互斥）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_select.py -k "order or status or disagreement"
```

期望：`6 passed, 19 deselected`。

## AC09 商品发售日期与标记

- [x] 2026-10-30 通过（`Release date` / `Publication date` → `media_release`，`Date First Available` → `listing_first_available`；`jan_first_suspect` 与 `after_review_period` 标记；原标签、原值、精度 day 与页面定位保留；日期值不进入年份候选与选择记录）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_product_dates.py
```

期望：`6 passed`。

## AC10 最早评论时间

- [x] 2026-10-30 通过（作品 J 最早 2000-12-01、第二早 2001-03-02、评论数 3、有评论商品数 2；合集评论只在商品层不进成员作品；隔离评论不计入有效评论，K 无评论行）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_reviews.py
```

期望：`4 passed`。

## AC11 两种上界证据与推断上界

- [x] 2026-10-30 通过（B1–B5 与 M1–M4 条件反例分别记录；两者都成立取较早者；1 月 1 日降为 year 精度；仅上架日期无推断上界；上界早于上映年份记录 `bound_before_release_year` 且不影响年份与状态）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_bounds.py -k "ac11"
```

期望：`9 passed, 2 deselected`。

## AC12 预售、合集与跨版本的报告

- [x] 2026-10-30 通过（多版本作品跨版本最早评论早于全部发售日取评论为上界；合集商品评论数在商品层统计不进成员作品）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_bounds.py -k "ac12"
```

期望：`2 passed, 9 deselected`。

## AC13 版本、重跑复用与版本差异

- [x] 2026-10-30 通过（同一输入重跑为 `reused` 不新增数据行只增审计；规则变更保留前置抽取阶段、复用未受影响阶段；映射变更生成独立上映信息版本并支持差异比较）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py -k "reused or version or diff"
```

期望：`4 passed, 4 deselected`。

## AC14 分批恢复与锁

- [x] 2026-10-30 通过（候选与共现阶段批次故障注入后恢复重跑无遗漏无重复；并发抽取锁互斥阻断）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py -k "ac14"
```

期望：`3 passed, 5 deselected`。

## AC15 Airflow DAG

- [x] 2026-10-30 通过（DAG 任务拓扑与触发规则正确；真实 scheduler 端到端通过：`t6_e2e_ok1` 成功、`t6_e2e_rerun1` 复用、`t6_e2e_missing1` 阻断退出码 1）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_dag.py
```

期望：`7 passed, 7 warnings`。

## AC16 报告、实例与查询

- [x] 2026-10-30 通过（统计包含全貌口径；实例覆盖 10 类场景；`query movie` 与 `query gaps` 正确输出；导出的 6 个 CSV 与报告文件哈希完整记录）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_report.py
```

期望：`8 passed`。

## AC17 评价工具

- [x] 2026-10-30 通过（评价表不含开发池作品，各层数量符合设定且不显示系统年份、状态与层；导入后年份准确率（完全一致、相差 ≤1 年）、`tconst` 正确率、缺失可查到率、上界成立率与手算一致并按层、来源与证据类型给出，“不确定”单独计数；没有推断上界时上界成立率为“无定义”）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py
```

期望：`7 passed`。

## AC18 正式库全量运行与只读核对

- [x] 2026-10-30 通过（在 T6-08 至 T6-10 修复完成后清空 `task6`，先冻结开发池 `task6.dev_pool` 1,732 部，再经 Airflow 运行 `t6_full_1`（真实 `run_id = 2a6b07ff00e54bb39fcf86234a366cbf`，耗时 77.74s）与 `t6_full_2`（真实 `run_id = fb16afb798c046e599502af3a169dbdf`，`reused`）；只读核对 `etl_task6.check` 退出码 0、`"problems": []`、任务 1–5 全部 9 张主表行数不变；五种状态合计 8,659，推断上界 6,978）。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.check > /dev/null; echo "exit=$?"
```

期望：`exit=0`（输出 JSON 的 `"problems": []` 且 `"passed": true`）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = 'rv0001' GROUP BY status ORDER BY status"
```

期望：输出 `SET` 及五种状态合计 8,659：`cooccur_matched|641`、`hint_matched|293`、`missing|3202`、`page_reported|50`、`verified|4473`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT count(*) FROM task6.release_selection WHERE release_date IS NOT NULL OR precision <> 'year'"
```

期望：输出 `SET` 与 `0`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT chosen_evidence, count(*) FROM task6.inferred_release_bound WHERE release_version_id = 'rv0001' GROUP BY 1 ORDER BY 1"
```

期望：输出 `SET` 及两行 `media_release|3963` 与 `review|3015`（合计 6,978）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT status, run_id, release_version_id FROM task6.run ORDER BY started_at"
```

期望：输出 `SET` 及两行 `succeeded|2a6b07ff00e54bb39fcf86234a366cbf|rv0001` 与 `reused|fb16afb798c046e599502af3a169dbdf|rv0001`。

## AC19 评价标注与指标

- [ ] 未执行；T6-11 已重新导出盲测评价集 `eval_items.csv`（120 部，`t6_eval_0001`–`t6_eval_0120`）与非循环 AI 草稿 `eval_items_draft.csv`，待付林轩核查后于 T6-12 导入（当前 `task6.eval_label` 为 0 行）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT annotator, year_label, count(*) FROM task6.eval_label GROUP BY 1, 2 ORDER BY 2"
```

期望（计划）：标注人 `付林轩`，`known` 与 `unsure` 合计 120。

## AC20 迁移、测试与仓库内容

- [x] 2026-10-30 通过（迁移版本 001–501 齐全；全量 792 项测试全部通过，任务 1–5 的 691 项不减少；仓库不含受限数据文件、导出文件、评价表或 `.env`）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration"
```

期望：输出 `SET` 与 `001,101,102,103,201,301,302,401,501`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：`792 passed, 56 warnings`（含 T6-08 至 T6-10 新增回归测试，任务 1–5 的 691 项不减少）。

```bash
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_items|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望：`grep-exit=1`。

## AC21 共现反查排序、续集编号回退、单实词别名保护与阶段键重算

- [x] 2026-10-30 通过（验证 `_title_keys_of` 按 `primary → original → aka` 排序；带编号续集作品不因排在前面的无编号标题键提前 `break` 误杀；单实词别名仅允许与单实词相等匹配、保留 ≥2 个实词且字符数 ≥6 按子集匹配；修改 `RuleSet` 共现参数后 `stage_key("cooccur")` 变化并触发重算）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_cooccur.py tests/integration/test_task6_runs.py -k "ac21"
```

期望：`3 passed, 32 deselected`。

## AC22 报告统计口径、真实 run_id、跨版本差异、血缘查询与核对强化

- [x] 2026-10-30 通过（验证 `summary.json` 的 `title_types` 与全量 `candidate_shared_count` 准确；不存在的 `--run-id` 报错拒绝；存在 `reused` 运行时按版本导出仍取 `succeeded` 运行计数，`build_and_write_report` 默认采用真实 `run_id`；`diff_versions` 用 `FULL OUTER JOIN` 覆盖增删作品、`tconst` 变化与上界变化；`query movie` 返回 `product_review_time` 与共现标题证据；`check` 比较 `EXTRACT(YEAR FROM b.bound_time)::int` 与 `s.release_year` 并校验当前共现阶段键下 `cooccur_score.passed = TRUE`；无来自 `amazon_movies_task6_release.py` 的 `AirflowFailException` 弃用警告）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_report.py tests/integration/test_task6_runs.py tests/integration/test_task6_dag.py -k "ac22 or diff or dag"
```

期望：`12 passed, 11 deselected, 7 warnings`。

## AC23 评价分层键名修复、商品原始标题导出、盲测打乱与非循环 AI 草稿

- [x] 2026-10-30 通过（验证 `tvMovie`/`video`/`year_diff_1`/`year_disagreement`/`tconst_disagreement` 的 `verified` 作品均分入 `E2`；`eval_items.csv` 的 `product_titles` 保留任务 4 商品原始标题中的括号年份；`item_id` 按 `md5(movie_id)` 全局打乱分配 `t6_eval_0001`–`t6_eval_0120` 不泄露分层与状态；`eval_items_draft.csv` 独立比对生成且不读取系统采用年份或推断上界、`missing` 作品不填入禁用来源年份；`metrics` / `compute_eval_metrics` 完整断言 `by_stratum`、`by_source` 与 `by_evidence_type` / `bounds_by_evidence`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py -k "ac23 or ac17"
```

期望：`7 passed`。
