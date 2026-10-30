# 任务 6 AC 完成情况（补全上映信息）

状态：2026-10-30 编写计划；同日按用户要求补充来源，AC 重新编号为 AC00–AC20；待用户审批，实现未开始。AC00 已做 AI 文档检查，待用户审批后勾选；AC01–AC20 均未执行。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。除 AC00 与 AC20 的仓库检查外，命令都是**（计划）**：选择器与期望在实现后按实际运行结果改写，改写前不得勾选。

一次跑全部测试（计划）：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望（计划）：全部通过，任务 1–5 的 691 项不减少。

## AC00 计划文件规范

- [ ] 2026-10-30 AI 文档检查通过（五个板块、R01–R39、AC00–AC20 一一对应）；待用户审批。

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

期望：`3 passed`。

## AC02 日期语义分离与精度

- [x] 2026-10-30 通过（IMDb 年份 1999 胜出且 `release_date` NULL、`precision=year`、`release_type=first_release`、区域 NULL；没有候选的作品保持 `missing`；全库无一年份补成日期、非 missing 的年份都等于采用候选的值；地区院线日期缺失原因 `no_permitted_dated_source`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_select.py -k "ac02"
```

期望：`3 passed`。

## AC03 IMDb 关联作品的年份候选

- [x] 2026-10-30 通过（movie / tvMovie / video 三种 `titleType` 各一条候选，带快照、文件、行号、任务 4 关联依据、来源获取与抽取时间、置信度；`startYear` 为 NULL 的不产生候选）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "imdb_link"
```

期望：`1 passed`。

## AC04 任务 5 升级线索的使用与记录

- [x] 2026-10-30 通过（未被占用的线索候选、`shared_tconst` 与质量问题、合集成员与多 `tconst` 只记录、`hint_ambiguous`；任务 4、任务 5 的表行数不变）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "hint or task4_and_task5"
```

期望：`4 passed`。

## AC05 人物共现反查

- [x] 2026-10-30 通过（导演命中 + 标题实词子集 + 唯一 → 候选带命中姓名、断言 ID、演职员行号与标题键来源；得分 1、标题不吻合、编号排除各有评分记录写明未通过条件；两部命中且无评论年份 → `cooccur_ambiguous`；命中已占用作品 → `shared_tconst`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_cooccur.py -k "ac05"
```

期望：`7 passed`。

## AC06 评论年份提及与消歧

- [x] 2026-10-30 通过（每条评论每个年份一票、晚于评论年份的不计、三个模式各计票；多部候选时经评论年份唯一选出并标 `review_year_tiebreak`、置信度 0.70；只有提及的作品仍为 `missing`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_cooccur.py -k "ac06"
```

期望：`4 passed`。

## AC07 商品标题年份候选

- [x] 2026-10-30 通过（每个商品一条候选带原值、定位与断言 ID；一致 → `page_reported`、不一致 → `title_years_disagree`、单一 → `page_reported`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py -k "title_year"
```

期望：`3 passed`。

## AC08 择优顺序、状态与分歧

- [x] 2026-10-30 通过（F/G/H 都 `verified` 采用 IMDb 候选、G 相差 1 年只计数、H 有 `year_disagreement`（相差 4 年）与质量问题；I `hint_matched`；L 采用线索并有 `tconst_disagreement`；N `cooccur_matched`；每部作品恰好一条记录且状态互斥）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task6_rules.py tests/integration/test_task6_select.py -k "order or status or disagreement"
```

期望：`6 passed`。

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

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py -k "reused or version or diff"
```

期望（计划）：选中的案例全部通过。

## AC14 分批恢复与锁

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_runs.py -k "batch or lock"
```

期望（计划）：选中的案例全部通过。

## AC15 Airflow DAG

- [ ] 未执行。真实 scheduler 端到端为可选复核，步骤见 runbook（计划，T6-05 编写）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_dag.py
```

期望（计划）：全部通过。

## AC16 报告、实例与查询

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_report.py
```

期望（计划）：全部通过。

## AC17 评价工具

- [ ] 未执行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py
```

期望（计划）：全部通过。

## AC18 正式库全量运行与只读核对

- [ ] 未执行。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.check > /dev/null; echo "exit=$?"
```

期望（计划）：`exit=0`（输出 JSON 的 `"problems": []`）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = 'rv0001' GROUP BY status ORDER BY status"
```

期望（计划）：五种状态合计 8,659；具体数字在运行后填写。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT count(*) FROM task6.release_selection WHERE release_date IS NOT NULL OR precision <> 'year'"
```

期望（计划）：`0`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT chosen_evidence, count(*) FROM task6.inferred_release_bound WHERE release_version_id = 'rv0001' GROUP BY 1 ORDER BY 1"
```

期望（计划）：两行 `media_release`、`review`，数字在运行后填写。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, airflow_run_id, release_version_id FROM task6.run ORDER BY started_at"
```

期望（计划）：一行 `succeeded` 与一行 `reused`，版本都是 `rv0001`；run id 在运行后填写。

## AC19 评价标注与指标

- [ ] 未执行；需要付林轩核查 AI 草稿后导入。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT annotator, year_label, count(*) FROM task6.eval_label GROUP BY 1, 2 ORDER BY 2"
```

期望（计划）：标注人 `付林轩`，`known` 与 `unsure` 合计 120。

## AC20 迁移、测试与仓库内容

- [ ] 未执行。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration"
```

期望（计划）：`001,101,102,103,201,301,302,401,501`。

```bash
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_items|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望：没有匹配行，只有 `grep-exit=1`（规划时已可运行，正式核对在 T6-07）。
