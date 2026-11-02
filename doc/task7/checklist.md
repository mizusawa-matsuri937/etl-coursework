# 任务 7 AC 完成情况（处理不同来源的信息冲突）

状态：2026-10-30 编写，2026-11-02 随 spec 修订为 AC00–AC22。2026-11-02 完成 T7-01 至 T7-08，并在用户授权下放宽姓名兼容（`t7r2`→`t7r3`，正式库 `cv0002`→`cv0003`，当前默认 `cv0003`）：AC01–AC18、AC20–AC22 已通过并勾选，AC19（用户审核标注、导入、指标）尚未执行，保持未勾选并标注 `（计划）`。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询（带 `SET default_transaction_read_only = on;`）。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：2026-11-02 实测最后一行 `1121 passed`（任务 1–6 的 792 项不减少，任务 7 新增 329 项，其中姓名放宽新增 39 项）；后续阶段会增加新测试，数字随之变化。

## AC00 计划文件规范

- [x] 2026-11-02 AI 文档检查通过（五个二级标题顺序正确，R01–R66 均被 AC 与任务覆盖，AC00–AC22 一一对应，未执行条目保持未勾选）；用户同日同意按复核结论修订。

```bash
grep -n "^## " doc/task7/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task7/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task7/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task7/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条为 `66`。

## AC01 输入版本、实体边界与处理范围

- [x] 2026-11-02 通过（AI 运行）：版本层与作品层——每部作品在 `movie_scalar_stage_result`、每个 matched 商品在 `edition_stage_result` 恰好一行，五类上映状态与只经 `contains` 出现的成员作品、无关联合集 `movie_ids = '{}'`、`tv_series` 不进入 `task7`、干扰版本不被读取、三类关联按 `0.95`/`0.80`/`0.75` 引入 IMDb 候选；物化（T7-05）——6 部作品各一行 `movie_resolved`、7 个 matched 商品各一行 `edition_resolved`（`B0TV` 不进入，`B0EMPTY` 的 `movie_ids` 为空数组），选择记录 54 条与 42 条，`check` 无问题。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py tests/integration/test_task7_movies_scalar.py tests/integration/test_task7_runs.py -k "ac01"
```

期望：`6 passed, 51 deselected`，退出码 `0`。

## AC02 候选保留、状态语义与版本属性隔离

- [x] 2026-11-02 通过（AI 运行）：候选定位与互斥状态、胜出值相同者都 `adopted`、`missing` 的 `considered_candidate_ids` 为空、冲突类型闭集、版本差异保留版本层、合集总片长与分级不下推、上游行摘要不变（版本层 + 作品层 4 项）；合集盒子的类型与面包屑不下推给成员作品，只经 `contains` 出现的成员只取 IMDb 类型（`movie_multivalue` 2 项）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py tests/integration/test_task7_movies_scalar.py tests/integration/test_task7_movies_multivalue.py -k "ac02"
```

期望：`6 passed, 66 deselected`，退出码 `0`。

## AC03 标题、原名与别名

- [x] 2026-11-02 通过（AI 运行）：M1 取 IMDb `Seven Samurai`/`Shichinin no samurai`，页面候选 `superseded_by_specific`（任务 4 规范键命中 IMDb 标题至少为 `alias`），别名表 3 条 IMDb 标题（`primary`、`original`、`aka` 的区域 `ES`、语言 `es`）；M2 取 `The Kid`、`original_title` 为 `missing`；M3 `conflict_unresolved`；M4 取页面 `Wrong Trousers`、IMDb 候选 `rejected`、`original_title` 为 `conflict_unresolved`、别名表无 IMDb 行。同名别名只保留先出现者（见 design“实现补充约定”）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac03"
```

期望：`5 passed, 28 deselected`，退出码 `0`。

## AC04 上映年份决策表与推断上界隔离

- [x] 2026-11-02 通过（AI 运行）：Y1 `1985`（`minor_tolerance`）；Y2 `1954`、Y5 `1991`（`cross_source_override` 并写质量问题）；Y3、Y4、Y6、Y7 为 `conflict_unresolved`（值 `NULL`，候选 `conflict_pending`）；Y8 为 `missing`，上界 `2001-05-01` 只在参考列，不产生年份候选。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac04"
```

期望：`11 passed, 21 deselected`，退出码 `0`。

## AC05 片长分层与逐版本差异分类

- [x] 2026-11-02 通过（AI 运行）：R1 作品层 `100`，`runtime_diff` 为 `tolerance_1_2`、`pal_evidence`、`variance_3_5`（`pal_formula_match = true`）、`cut_or_multidisc`，作品层 `framerate_or_edition`；R2 取 `90`、`unexplained_gt15`、写 `severe_runtime_conflict`；R3 取 `92`（`minor_tolerance`）；R4、R5 为 `conflict_unresolved`，版本层保留各自片长。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac05"
```

期望：`4 passed, 28 deselected`，退出码 `0`。

## AC06 内容分级规范化与跨版本消解

- [x] 2026-11-02 通过（AI 运行）：A1 版本层 `PG`（`formatting_or_alias`）；P1 作品层 `R`（`unrated_vs_rated`），未评级候选 `superseded_by_specific`；P2 `conflict_unresolved`，版本层各自保留 `G` 与 `PG`；A2 写 `unknown_mpaa_rating`；另覆盖已知与未知并存、同 ASIN 分级不一致取 `detail_bullets`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_scalar.py -k "ac06"
```

期望：`5 passed, 27 deselected`，退出码 `0`。

## AC07 类型映射、词面处置与多源并集

- [x] 2026-11-02 通过（AI 运行）：G1 `genres = {Action, Adventure, Animation, Anime, Fantasy, Sci-Fi}`，`Action`、`Sci-Fi` 为 `both`，`see more` 被去除，`Movies & TV`、`DVD` 记 `noise`，`faith & spirituality` 记 `unmapped` 并写质量问题；G2 已熔断不含 IMDb 类型；G3 回归：整串 `Comedy, Romance, Kids & Family/10-12 Years` 逐标签映射（读 `normalized_value`；改读 `raw_value` 时 5 项失败）；G4、G5 为 `missing`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac07"
```

期望：`7 passed, 21 deselected`，退出码 `0`。

## AC08 演职员并集、角色边界、顺序与覆盖范围

- [x] 2026-11-02 通过（AI 运行）：C1 `director` 含 D1（`both`）与 D2（`imdb_only`，`person_id` 按公式 `ps_` + sha256(`imdb:nm…`) 前 16 位），`cast` 含 S1 与 A2（A2 复用任务 5 已有的 `person_id`，不登记），`starring` 只含 S1，S1 序号 1、分布 `{1: 2, 2: 1}`、`billing_conflict_resolved`；C3 `John Doe` 为 `conflict_pending`、字段 `resolved`、`director_source_disagreement` 并写质量问题；`cast_coverage` 四种取值；C4 只有 IMDb 演员且 `starring` 为 `missing`；上游表摘要（含 `task5` 全部人物表）不变。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac08"
```

期望：`10 passed, 18 deselected`，退出码 `0`。

## AC09 版本层标量与多值特征

- [x] 2026-11-02 通过（AI 运行）：E1 发行商取 `Warner Home Video`（`formatting_or_alias`）、发售日取 `media_release` 并保留上架日（`semantic_distinction`）；E2 取 `Sony Pictures`（`intra_asin_publisher_conflict`）；E3 多值并集保留来源区域；E4 `date_flags` 与置信度 `0.60`；E5 `discs = 2` 有选择记录；另覆盖有断言时碟数优先、同 ASIN 多区域取区域优先级、上架日回退与 `missing`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py -k "ac09"
```

期望：`7 passed, 4 deselected`，退出码 `0`。

## AC10 共享 `tconst` 去重与分类

- [x] 2026-11-02 通过（AI 运行）：4 条任务 6 共享问题（其中同一对作品两条）→ 3 对；已熔断为 `false_positive_link`、标题相等且导演兼容为 `likely_duplicate_movie`、标题相等但导演不兼容为 `undetermined`；任务 4 与其他上游表摘要不变；只取上映版本所属运行的 `shared_tconst_with_movie`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_link_assess.py -k "ac10"
```

期望：`2 passed, 8 deselected`，退出码 `0`。

## AC11 消解版本、阶段依赖、复用与差异

- [x] 2026-11-02 通过（AI 运行）：同输入第二次为 `reused`、业务行数不变、只多一条 `run` 审计；`t7r1tol`（片长容差 3 分钟）只重算 `movie_scalar`（新运行的 `run_batch` 只有该阶段，其余三个阶段键不变）；`t7r1pub`（发行商后缀）重算全部四个阶段；切换任务 4 映射版本（复制为 `map0003`、`pmap0002`、`rv0002`）生成新版本并全阶段重算；`report diff` 用 `FULL OUTER JOIN`，片长容差变化只列出 `D3` 的 `runtime_minutes`（冲突类型 `framerate_or_edition` → `minor_tolerance`），缺一边的实体显示 `ABSENT`；旧版本可查询。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py -k "ac11"
```

期望：`5 passed, 8 deselected`，退出码 `0`。

## AC12 分批事务恢复与并发咨询锁

- [x] 2026-11-02 通过（AI 运行）：批大小 2，分别在 `edition_resolve`、`link_assess`、`movie_scalar`、`movie_multivalue` 第 2 批提交前注入故障：只有第 1 批（2 个实体）落库、上游阶段已完成、下游阶段未开始、没有版本、运行为 `failed`；重跑跳过已完成批次，版本内容摘要（选择记录、物化表、候选、片长差异、别名、类型、署名对齐、成员）与一次跑完的完全相同；另一连接持有咨询锁时运行确定性抛 `RunBusy`，释放后可运行。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py -k "ac12"
```

期望：`5 passed, 8 deselected`，退出码 `0`。

## AC13 Airflow DAG 与核对后才成功

- [x] 2026-11-02 通过（AI 运行）：9 个节点按序连接，`finalize_report`、`gate` 为 `all_done`，重试上限 2，参数齐全，DAG 文件不在解析期导入 `etl_task7`，从 `airflow.sdk.exceptions` 导入 `AirflowFailException`、解析时无弃用警告；按 DAG 顺序执行：成功、重跑 `reused`、上游缺失时 `check_inputs` 失败且只尝试 1 次、`verify` 注入失败后运行为 `failed`、版本 `unverified`，重跑不是 `reused` 且在原版本 `cv0001` 上重写；故障注入只在 `ETL_ENV=test` 下生效。真实 scheduler（`compose.e2e.yaml`，`etl_test`）：`t7_e2e_ok1` success（`succeeded`/`verified`）；`t7_e2e_rerun2` success（`task7.run` 为 `reused`，六个执行步骤均为 `reused`）；`t7_e2e_missing1` failed（`check_inputs` 失败、仅 `try1` 记录、下游 `upstream_failed`、`finalize_report` success、`gate` failed）；`t7_e2e_vf2` failed（`verify` 失败、`gate` failed，运行 `failed`、版本 `unverified`）；`t7_e2e_vf3` success（运行 `succeeded`，版本 `verified`，没有新的 `run_batch`）。另有 `t7_e2e_rerun1`（success）与首次注入尝试 `t7_e2e_vf1`：后者输入已有通过核对的运行，被 `reused`，注入未生效，所以重新准备数据后用 `t7_e2e_vf2` 复核。已切回正常配置，见 runbook 第 2 节第 7 步。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_dag.py
```

期望：`10 passed`，退出码 `0`。真实 scheduler 复核步骤见 `doc/task7/runbook.md` 第 2 节（可选，会清空 `etl_test`）。

## AC14 报告、实例与三个只读查询

- [x] 2026-11-02 通过（AI 运行）：`summary.json` 的作品与商品总数、分字段三种状态（合计等于作品数与 matched 商品数，即分母）、`inferred`、关联评估、片长类别、候选状态、署名对齐、类型词面、共享 `tconst` 等计数与数据库一致；`examples.md` 覆盖 R60 的 19 个类别（含片长 7 类），每类 1–3 例并附候选 ID、ASIN、`tconst` 或人物 ID；`query movie`（胜出值、规则理由、全部候选含未采用的、关联评估、版本摘要）、`query edition`（版本属性、内部冲突、所属作品、评论统计）、`query conflicts`（按类型、字段、层过滤）与命令行入口；导出文件的 SHA-256 与记录一致，不含整页 HTML 与评论正文。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_report.py -k "ac14"
```

期望：`7 passed, 4 deselected`，退出码 `0`。

## AC15 盲测评价工具

- [x] 2026-11-02 通过（AI 运行）：16 项测试——真实 120 项配额（七层、E4 = 8 + 8 + 4、E7 = 10 + 10）、`conflict_unresolved` 保底为 `min(可用数, 4)`、署名对齐至少一半 `L4`、补足顺序 `E3 → E4 → E1 → E7`；指标与手算一致，分母为 0 记“无定义”；开发池严格排除（含关联作品在开发池的 ASIN）；盲测表无系统结果，打乱候选状态与顺序后导出字节相同，证据生成时执行的 SQL 不含 `task7.`；草稿可仅凭盲测行复现，`eval_label` 无 `ai_reason` 列；缺依据的存疑项、改判项、照抄 `ai_reason` 的备注被拒绝，拒绝时不写任何标注。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_evaluate.py -k "ac15"
```

期望：`16 passed`，退出码 `0`。

## AC16 规则纯函数与边界情况

- [x] 2026-11-02 通过（AI 运行）：标题清洁化与信号（单实词与括号年份、载体词与首尾冠词、`Emma VHS`、`Departed, The`）、PAL 公式与证据、年份决策表、熔断公式、发行商、碟数、多人串拆分、`NR/Unrated` 写法、未知 MPAA、类型词面处置、署名顺序、规则签名独立性与冲突类型闭集同 SQL CHECK。文件含 AC20 写语句静态检查，共 169 项（含规范键命中 `alias` 的 R20 用例；姓名放宽新增 28 项：格式预规范化、`t7r1`/`t7r2` 阶段签名与 `cv0001`/`cv0002` 存下的阶段键一致、`LS` 正负例、`t7r3` 方向性、页面拼接串与连字符名不触发）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py
```

期望：`169 passed`，退出码 `0`。

## AC17 多运行与多阶段键共存

- [x] 2026-11-02 通过（AI 运行）：不存在或属于别的版本的 `--run-id` 被拒绝；有 `reused` 运行时默认读 `succeeded` 运行的计数，显式传入 `reused` 运行时读它复用的那次计数；两个版本共存时各版本的候选与片长类别只数自己阶段键下的行；`check` 检出注入的孤儿候选、`conflict_unresolved` 字段的 `adopted` 候选（状态不一致）、`movie_resolved` 被篡改，以及未核实版本（命令行要求 `verified`，`verify` 步骤不要求）；核对失败的运行为 `failed`，版本 `unverified`，重跑不是 `reused`，在原版本上重写且阶段结果不重算。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_report.py tests/integration/test_task7_runs.py -k "ac17"
```

期望：`6 passed, 18 deselected`，退出码 `0`。

## AC18 正式库全量运行、复用与只读核对

- [x] 2026-11-02 通过（AI 运行）：先在 `etl_movies` 冻结开发池 1,732 部，再经 Airflow 触发 `t7_full_1`（运行 242 秒）生成 `cv0001`，`t7_full_2` 为 `reused`；姓名放宽后 `t7_full_3`（`t7r2`，278 秒）生成 `cv0002`、`t7_full_4` 为 `reused`；`LS` 改为方向性后 `t7_full_5`（`t7r3`，256 秒）生成 `cv0003` 并 `verified`、`t7_full_6` 为 `reused`（0 秒）；三个版本都 `verified`，`etl_task7.check`（默认检查最近版本 `cv0003`）退出码 0、`"problems": []`；任务 1–6 全部表行数与运行前逐表一致（只有 `public.schema_migration` 多一行 `601`）。没有失败的运行记录。`cv0001`、`cv0002` 保留，`task7` 三版本并存约 1,408 MB。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task7.check > /dev/null; echo "exit=$?"
```

期望：`exit=0`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT field_name, status, count(*) FROM task7.movie_field_selection WHERE conflict_version_id = 'cv0003' GROUP BY field_name, status ORDER BY field_name, status;"
```

期望：9 个字段的三种状态合计各为 `8659`，例如 `title|resolved|8659`、`release_year` 为 `conflict_unresolved|7`、`missing|3202`、`resolved|5450`，`runtime_minutes` 为 `7`、`528`、`8124`，`mpaa_rating` 为 `5`、`1657`、`6997`，`director` 为 `42`、`2017`、`6600`；`conflict_unresolved` 与 `missing` 的胜出值均为空由 `check` 核对。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT count(*) FROM task7.edition_resolved WHERE conflict_version_id = 'cv0003'; SELECT status, conflict_version_id, airflow_run_id FROM task7.run ORDER BY started_at; SELECT conflict_version_id, status FROM task7.conflict_version;"
```

期望：`9590`；按顺序 `succeeded|cv0001|t7_full_1`、`reused|cv0001|t7_full_2`、`succeeded|cv0002|t7_full_3`、`reused|cv0002|t7_full_4`、`succeeded|cv0003|t7_full_5`、`reused|cv0003|t7_full_6`；`cv0001|verified`、`cv0002|verified`、`cv0003|verified`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT cross_source_mismatch_halt, count(*) FROM task7.movie_resolved WHERE conflict_version_id = 'cv0003' GROUP BY 1 ORDER BY 1;"
```

期望：`f|8652` 与 `t|7`，合计 `8659`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT (SELECT min(frozen_at) FROM task7.dev_pool) < (SELECT min(started_at) FROM task7.run); SELECT count(*) FROM task7.dev_pool;"
```

期望：`t`（开发池早于首次运行冻结）与 `1732`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT 'review_record', count(*) FROM public.review_record UNION ALL SELECT 'product_manifest', count(*) FROM task2.product_manifest UNION ALL SELECT 'page_field', count(*) FROM task3.page_field UNION ALL SELECT 'movie', count(*) FROM task4.movie UNION ALL SELECT 'product_movie', count(*) FROM task4.product_movie UNION ALL SELECT 'person', count(*) FROM task5.person UNION ALL SELECT 'movie_person', count(*) FROM task5.movie_person UNION ALL SELECT 'release_selection', count(*) FROM task6.release_selection ORDER BY 1"
```

期望：`movie|17268`、`movie_person|50027`、`page_field|449000`、`person|29635`、`product_manifest|253059`、`product_movie|17852`、`release_selection|8659`、`review_record|7931696`（与运行前相同）。

## AC19 正式库盲测标注与指标

- [ ] 未执行（计划）。评价对象为 `cv0003`：盲测表与 AI 草稿已于 2026-11-02 导出到 `outputs/task7/<抽取>/cv0003/eval/`（`ev_cv0003`，120 项：`E1` 15、`E2` 15、`E3` 20、`E4` 20、`E5` 15、`E6` 15、`E7` 20，`conflict_unresolved` 15 项，署名对齐含 `L4` 4 项、`LS` 1 项），等待付林轩审核；`ev_cv0001` 被取代，不导入。

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

- [x] 2026-11-02 通过（AI 运行）：正式库迁移为 `001,101,102,103,201,301,302,401,501,601`；全量测试 `1121 passed`（任务 1–6 的 792 项不减少）；写语句测试通过；迁移 601 的 `credit_alignment.level` CHECK 在已应用的正式库幂等升级为含 `LS`（`check_inputs` 触发，未新增迁移号，清单不变）；`git ls-files` 不含 `.env`、`outputs/`、评价表或 IMDb 数据。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration;"
```

期望：`001,101,102,103,201,301,302,401,501,601`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py -k "ac20"
```

期望：`1 passed, 168 deselected`，退出码 `0`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py -k "ac20"
```

期望：`1 passed, 13 deselected`（旧版迁移 601 的 CHECK 被幂等升级为含 `LS`），退出码 `0`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：最后一行 `1121 passed`（另有来自 Airflow 的弃用警告），任务 1–6 的 792 项不减少。

```bash
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_items|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望：无匹配行，输出 `grep-exit=1`。

## AC21 关联评估与错配熔断

- [x] 2026-11-02 通过（AI 运行）：L1–L10 的四信号、熔断条件、单信号不熔断、强关联不熔断、`Emma VHS` 与 `Departed, The` 不亮 T、信号与证据持久化、`movie_scalar` 读取同一判定（`link_assess` 10 项，含 T 信号读任务 4 规范键的回归）；`movie_multivalue` 读取同一判定：把一条未熔断评估改成已熔断后重跑，IMDb 类型与演职员随之消失（1 项）。姓名放宽（`t7r1`/`t7r3` 对照运行）：L11 `J.P.Dutta`、L12 `Rakesh Chaturvedi` 在 `t7r1` 下 `D` 亮并 `Y+D` 熔断，在 `t7r3` 下 `D` 不亮、只剩 `Y` 单信号；L13 单个词、L14 `Michael Jordan`（`L3`）符合预期；`director_signal` 单元测试另验证方向性（页面名较长时 `t7r2` 兼容、`t7r3` 不兼容）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_link_assess.py tests/integration/test_task7_movies_multivalue.py -k "ac21"
```

期望：`12 passed, 38 deselected`，退出码 `0`。

## AC22 同片署名对齐与多人串

- [x] 2026-11-02 通过（AI 运行）：C5 `Garey Busey` 对齐为 `Gary Busey`（`both`、`L4`、原署名保留）；C6 取严格命中 `John Cusack`，`Joan Cusack` 为 `imdb_only`；C7 同等级两个命中不对齐、页面行 `conflict_pending`；C8 两段都对齐，原行被取代且保留在 `credit_alignment`；C9 部分命中、C10 无 IMDb、C11 `Black and Blue Productions` 保留原行、`conflict_pending` 并写 `unsplit_multi_person`；任何分段都未新造 `person_id`。纯函数 `align_name` 的单元测试另有 5 项。姓名放宽（`t7r1`/`t7r2`/`t7r3` 对照运行）：C12 `J.P.Dutta`（`L1`）、C13 `Rakesh Chaturvedi`（`LS`）在 `t7r1` 下为 `conflict_pending`、`t7r3` 下对齐为同一人；C14 单个词不对齐；C15 页面较短且两人并列 `LS` 不对齐；C16 `L3`；C17 多人串两段 `L1`/`LS` 全部命中；C18 `Whoopi Goldberg Justin Kirk`、C19 `George Meeker Trevor Bardette` 在 `t7r2` 下误对齐/并列、在 `t7r3` 下无兼容成员；C20 `Carla-Rae` 连字符名不触发 `LS`；C21 `Wai-Man Chan` 对齐为 `LS`；AC22 另有纯函数 `LS` 方向性、拼接串与连字符名参数用例。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_movies_multivalue.py -k "ac22"
```

期望：`17 passed, 20 deselected`，退出码 `0`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task7_rules.py tests/unit/test_task7_multivalue.py -k "ac22"
```

期望：`29 passed, 155 deselected`，退出码 `0`。
