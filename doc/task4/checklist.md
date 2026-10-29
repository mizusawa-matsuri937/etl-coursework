# 任务 4 AC 完成情况（定义并识别同一电影）

状态：2026-10-29 编写计划并实现；AC00–AC17 全部通过：AC15 由用户付林轩审核标注后导入、计算指标；其余为 AI 运行的自动化测试与对正式库的只读核对，用户尚未复核。用户已确认全部假设（H01–H11），尚未审阅计划全文。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。标注“（计划）”的命令依赖尚未实现的代码或测试，期望值是预定口径，实现后实际运行核对再改写。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：`505 passed`（任务 1–3 的 336 项不变，任务 4 共 169 项；后续任务继续增加）。2026-10-29 实际 `505 passed`。

## AC00 计划文件规范

- [x] 2026-10-29 通过（AI 文档检查；用户尚未审阅）。

```bash
grep -n "^## " doc/task4/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task4/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task4/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task4/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出（spec 与 checklist 的 AC 编号一一对应）；第三条为 `42`。

## AC01 商品版本与匹配范围

- [x] 2026-10-29 通过（`2 passed`）：12 行商品版本，匹配范围 6/4/2，只读指定抽取与规则版本。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_editions.py -k "product_edition or match_scope"
```

期望：`2 passed, 10 deselected`。

## AC02 IMDb 快照校验与装载

- [x] 2026-10-29 通过（`18 passed`）：类型过滤、行号、`\N`、哈希不符拒绝（五个文件各一例）且无部分行、重复装载 `reused`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_imdb.py
```

期望：`18 passed`。

## AC03 标题键、说明分类与别名

- [x] 2026-10-29 通过（`16 passed`）：8 个标题的键、年份、说明段分类逐项相等；别名 “À bout de souffle” 的键与地区、语言、来源行在 `test_task4_match_db.py` 验证。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py -k "title"
```

期望：`16 passed, 11 deselected`；spec AC03 的 8 个标题逐个参数化。

## AC04 姓名键与片长

- [x] 2026-10-29 通过（`7 passed`）：署名行倒序、有署名行时不拆详情要点、宽松键与严格键、占位署名、片长 110 与 90、发售日期只作版本属性（落库部分在 `test_task4_editions.py`）。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py -k "name or runtime"
```

期望：`7 passed, 20 deselected`。

## AC05 候选与三档判定

- [x] 2026-10-29 通过（`50 passed`）：10 个案例在纯规则层与数据库层各验证一遍；每个候选恰好一条决策（含得分、置信度、阈值、特征、规则版本），未采用候选同样保存。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_match.py tests/integration/test_task4_match_db.py -k "decision"
```

期望：`50 passed, 6 deselected`。

## AC06 合集成员与评论口径

- [x] 2026-10-29 通过（`11 passed`）：成员 2 个与 4 个、原文与序号、`contains` 关系、`collection_unsplit` 无关系，评论口径之和等于清单记录总数、合集评论单列。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py tests/integration/test_task4_editions.py tests/integration/test_task4_match_db.py tests/integration/test_task4_cluster.py -k "collection"
```

期望：`11 passed, 81 deselected`。

## AC07 聚类一致性

- [x] 2026-10-29 通过（`2 passed`）：C 被移出并改为人工复核，一条 `cluster_check`；D、E、F 各自成作品。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py -k "consistency or no_chain"
```

期望：`2 passed, 14 deselected`。

## AC08 稳定 movie_id、映射历史与人工纠正

- [x] 2026-10-29 通过（`8 passed`）：两次运行 `movie_id` 相同且不含原文 ID；导入“不得合并”与“必须合并”后生成新版本，拆出的商品得到新 ID、原作品 ID 不变，历史里 1 条 `split`、1 条 `merge`；前后数量由表重数核对；旧版本仍可查询；非法导入整体失败。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py -k "stable_id or human"
```

期望：`8 passed, 8 deselected`。

## AC09 幂等与规则版本差异

- [x] 2026-10-29 通过（`2 passed`）：第二次运行 `reused`，所有目标表行数不变；自动阈值改为 9 的新规则版本不重新标准化、不新增候选，差异列表恰好是受影响的商品并带原因。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py -k "reused or rule_version_diff"
```

期望：`2 passed, 14 deselected`。

## AC10 分批恢复与锁

- [x] 2026-10-29 通过（`5 passed`）：批大小 2，在标准化、判定、商品对判定三个阶段的第 2 批提交前注入故障，故障后只有第 1 批的行（`entity_stage` 2 个实体、`run_batch` 1 批、没有映射版本），第 2 批没有部分行；同一 DAG run 重试接着用同一条运行，只处理剩余批次，最终各表行数与 `movie_id` 映射和一次跑完相同、没有重复；另一个连接持有抽取锁时匹配步骤抛 `RunBusy`，非暂时错误、库里不留行，锁释放后可运行。第 5 个是装载 DAG 已有的锁用例。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_dag.py -k "resume or lock"
```

期望：`5 passed, 19 deselected`。

## AC11 Airflow DAG

- [x] 2026-10-29 通过。两个 DAG 的任务依次相连（装载 DAG 4 个任务、匹配 DAG 7 个任务）、收尾与门禁 `all_done`、重试 2 次、参数集合正确；按 DAG 顺序执行的成功、重跑 `reused`、不存在的快照（`check_files`/`check_inputs` 失败、只尝试 1 次、下游 `upstream_failed`、门禁失败、失败记录在 `_failures`）均有测试。真实 scheduler 经 `compose.e2e.yaml` 在 `etl_test` 跑了装载 DAG 与匹配 DAG 各三次：成功 `success`、重跑 `success`（`task4.run` 为 `reused`）、不存在快照 `failed`（检查步骤失败，其余 `upstream_failed`，匹配 DAG 的 `finalize_report` 仍 success 并写失败汇总）；另一次暂时错误重试 `normalize` 第 2 次成功。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_dag.py
```

期望：`24 passed`（装载 DAG 10、匹配 DAG 14）。真实 scheduler 的步骤与期望在 [runbook](runbook.md) 第 2 节，可选复核（会清空 `etl_test`，之后要重新准备）。

## AC12 报告导出与实例

- [x] 2026-10-29 通过（`4 passed`）：`summary.json` 的商品版本与匹配范围、合并前后数量、IMDb 关联与 `page_only` 数、判定档、合集成员数、剧集数、评论口径都与库里重数一致（评论口径之和等于清单记录合计，再加无产品标识的记录等于任务 1 读取数）；实例列出夹具里的跨版本合并（Casablanca 四个版本、Breathless 别名版、页面簇）和易混淆未合并（Breathless 1960/1983 硬冲突等），每个商品带页面地址、快照位置与哈希，IMDb 作品带文件与行号；导出文件哈希与记录一致，不含 HTML 或评论正文；用户标注过的实例注明结论。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_report.py -k "summary or examples or export"
```

期望：`4 passed, 3 deselected`。

## AC13 评价工具与指标

- [x] 2026-10-29 通过（`8 passed`）：开发池为按 `md5` 排序的前 20% 标题键（向上取整）且冻结后不变；评价表不含开发池的商品、不显示系统判定、各层数量与补足记录正确；导入后 Precision、Recall、F1 与手算一致（3 TP、1 FP、1 FN），误合并与漏合并实例列出，“不确定”单独计数；无预测合并时 Precision 与 F1 为“无定义”；复核覆盖率与待复核率单独输出；矛盾标注只列出不改动；簇抽查表的导出、导入与结果。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_evaluate.py
```

期望：`8 passed`。

## AC14 正式库全量匹配

- [x] 2026-10-29 通过。经 Airflow 全量运行（IMDb 快照 `imdb_2026-10-29`，抽取 `f1e8b845…`，任务 3 规则 `t3r2`）：t4r1 为 `map0001`（346 秒），开发池上调整权重后 t4r2 为 `map0002`（52 秒）；只读核对退出码 0、无问题：商品版本 253,059 行（`matched` 9,590、`no_page` 232,398、`not_applicable` 11,071）；8,730 个电影类商品各恰好一条 `edition` 关系；剧集 835 个无关系；自动合并 5,037 条判定（IMDb 关联 4,775、商品对 262）均满足 R22/R25；一致性检查移出 1 个商品（Halo Legends），无未处理的硬冲突；`map0002`：8,730 个商品 → 8,472 部作品，多商品作品 240 部，待复核 899 个（10.30%），IMDb 关联商品 4,544 个（52.05%）；判定档 auto 5,037、review 1,629、reject 28,546；同参数重跑（`t4_full_r2_rerun1`）为 `reused`。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.check > /dev/null; echo "exit=$?"
```

期望：`exit=0`。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.check | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['problems'], d['mapping_version_id'], d['rule_version'], d['product_edition_rows'], d['movie_edition_relations'], d['tv_series_relations'], d['auto_imdb_violating_r22'], d['auto_pair_violating_r25'], d['unresolved_cluster_conflicts'])"
```

期望：`[] map0002 t4r2 253059 8730 0 0 0 0`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT mapping_version_id, rule_version, counts->>'participating_products', counts->>'movies_with_editions', counts->>'multi_product_movies', counts->>'review_pending_products', counts->>'imdb_linked_movies', counts->>'page_only_movies' FROM task4.mapping_version ORDER BY 1"
```

期望两行：`map0001|t4r1|8730|8481|232|1013|4309|4300` 与 `map0002|t4r2|8730|8472|240|899|4473|4186`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT d.tier, count(*) FROM task4.merge_decision d WHERE d.rule_version = 't4r2' GROUP BY 1 ORDER BY 1"
```

期望：`auto|5037`、`reject|28546`、`review|1629`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, count(*) FROM task4.run WHERE kind = 'match' GROUP BY 1 ORDER BY 1"
```

期望：`reused|1`、`succeeded|2`（t4r1 一次、t4r2 一次、t4r2 同参数重跑一次 `reused`）。

## AC15 正式评价与簇抽查

- [x] 2026-10-29 通过：标注人付林轩审核 AI 预标注草稿后定稿（改判 4 对，其余认可；AI 只出草稿，见 spec H04），导入 120 对与 20 个簇。评价集 `ev_map0002`（规则 t4r2 冻结后导出，开发池 1,967 个标题键已排除）。自动合并 P 1.000、R 0.487、F1 0.655（TP 38、FP 0、FN 40、TN 41，计入 119 对，unsure 1）；误合并 0；漏合并 40 对（S3 复核档 15、S6 合集成员 10、S4 同名 8、S5 未分块 7）。S1 35 对全部正确；S3、S4、S5 的 Precision 与 F1、S7 的全部指标分母为 0，记“无定义”。复核覆盖率 0%（899 个待复核商品都没有人工决策），待复核率 10.30%。簇抽查 20 个簇，混入不同作品 0 个。指标只代表分层评价样本。

评价集状态（只读）：

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT eval_set_id, (SELECT count(*) FROM task4.eval_pair WHERE eval_set_id = 'ev_map0002'), (SELECT count(*) FROM task4.cluster_sample WHERE eval_set_id = 'ev_map0002'), (SELECT count(*) FROM task4.eval_label WHERE eval_set_id = 'ev_map0002') FROM task4.eval_set"
```

期望：`ev_map0002|120|20|120`。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate metrics
```

期望：退出码 0；`annotators` 为 `["付林轩"]`；`overall` 中 `tp` 38、`fp` 0、`fn` 40、`tn` 41，`precision` 1.0、`recall` 约 0.487、`f1` 约 0.655；`false_merges` 为空，`missed_merges` 40 条；`review.review_coverage` 0.0、`review_pending_rate` 约 0.103；`clusters.mixed` 0。

## AC16 跨版本合并与易混淆实例

- [x] 2026-10-29 通过：`map0002` 的实例 5 组跨版本合并（The Aristocats、Blue Tiger、The Adventures of Priscilla, Queen of the Desert、The Hoodlum、Dude, Where's My Car?，各 2 个版本，带 IMDb 文件与行号）和 5 组易混淆未合并（Ring / The Ring、Puss in Boots、Something to Talk About、Darkness Falls、Munchhausen，均为硬冲突拒绝），每个商品带页面地址、快照位置与哈希；尚无用户标注，均注明“未标注”。完整实例见 `outputs/task4/f1e8b845…/map0002/t4_full_r2_1/examples.md`。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task4.report examples | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['cross_edition_merges']), len(d['confusable_not_merged']))"
```

期望：`5 5`。

## AC17 迁移、测试与仓库内容

- [x] 2026-10-29 通过：迁移版本含 `001,101,102,103,201,301,302`；全部测试 `505 passed`（任务 1–3 的 336 项不变）；`git ls-files` 没有 IMDb 文件、导出、评价表与 `.env`（IMDb 文件在 `../imdb/2026-10-29/`，导出在被忽略的 `outputs/`）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration"
```

期望：`001,101,102,103,201,301,302`。

```bash
git ls-files | grep -Ei "imdb.*\.tsv|eval_pairs|cluster_sample|\.env$"; echo "grep_exit=$?"
```

期望：只输出 `grep_exit=1`（没有匹配的已跟踪文件）。全部测试见本文开头。
