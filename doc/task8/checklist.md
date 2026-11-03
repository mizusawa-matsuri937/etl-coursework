# 任务 8 AC 完成情况（评论清洗与重复识别）

状态：2026-11-03 编写。AC00 已由 AI 运行文档检查并勾选；T8-01 至 T8-03 实现后 AC02、AC03、AC04、AC05、AC14 通过并勾选；T8-04 实现后 AC01、AC06（补齐）与 AC07–AC10 通过并勾选；T8-05 实现后 AC11、AC12 通过并勾选；其余尚未执行，保持未勾选并标注 `（计划）`。AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板` 直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询（带 `SET default_transaction_read_only = on;`，`psql -Atc` 输出首行会多一个 `SET`）。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：`1170 passed`（2026-11-03 T8-04 后实测：任务 1–7 的 1,121 项不减少，任务 8 共 49 项）。

## AC00 计划文件规范

- [x] 2026-11-03 AI 文档检查通过：五个二级标题顺序正确，R01–R49 均被 AC 与 T8-01 至 T8-07 覆盖，AC00–AC18 与 spec 一一对应，未执行条目保持未勾选；用户同日确认 H01–H04。

```bash
grep -n "^## " doc/task8/spec.md
diff <(grep -o "^| AC[0-9][0-9]" doc/task8/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task8/checklist.md | tr -d '# ')
grep -c "^- R[0-9][0-9] \[" doc/task8/spec.md
```

期望：第一条只列出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条为 `49`。规则覆盖的检查命令见 [tasks](tasks.md) T8-00，期望 `49 [] []`。

## AC01 输入版本、分层与原始记录全部保留

- [x] 2026-11-03 通过（T8-01、T8-03、T8-04）：两个抽取（全量与样本）中只读指定抽取；有效、`invalid_utf8`、`unknown_line`、无产品 ID 的隔离与缺 5 个字段的记录在 `review_mapping` 各恰好一行，隔离为 `quarantined`、不完整为 `incomplete` 且 `logical_review_id` 为空；`lr_` 前缀且不等于任何 `source_review_id`；`public` 各表行数与摘要不变；同时有 `cv0003` 与 `cv0002` 时只读 `cv0003`（`cv0002` 把单品映射到另一部作品、另一对重复作品，都不出现；其他 `link_stage_key` 与其他 `link_class` 的作品对不被读到）。演练库 7,911,696 条记录各一行映射。

```bash
# 抽取限定、每条原始记录一行映射、隔离与不完整记录、lr_ 前缀、public 不变、只读指定 cv
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_clean.py tests/integration/test_task8_dedup.py tests/integration/test_task8_attribution.py -k "ac01"
```

期望：`3 passed, 10 deselected`，退出码 `0`。

## AC02 helpfulness 状态与不完整记录

- [x] 2026-11-03 通过（T8-01）：六种原值依次为 `ok`、`zero_zero`、`numerator_exceeds_denominator`、`negative`、`unparseable`、`missing`，SQL 判定与 Python 判定逐条一致；后四类各一条 `review_issue`，`0/0` 不写；缺评分与时间的记录 `key_complete = false` 并写 `incomplete_review`。演练库实测与试算一致（`ok` 5,869,509、`zero_zero` 1,980,001、分子大于分母 43、`missing` 12、不完整 12）。

```bash
# 验证六种 helpfulness 状态、0/0 不算异常、不完整记录
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task8_rules.py tests/integration/test_task8_clean.py -k "ac02"
```

期望：`12 passed, 17 deselected`，退出码 `0`。

## AC03 内容键只用原值、核验与 Reviewer

- [x] 2026-11-03 通过（T8-03）：K1 只差大小写、K2 只差末尾空格（任务 1 保留末尾空格）、K3 同正文不同摘要、K4 注入哈希碰撞都不合并；K4 在核验时拆成两条逻辑评论（子组序号 0、1）并各写一条 `hash_collision`；`reviewer` 中 U5 一行含 `Ann` 2 次与 `Ann "A"` 1 次，同名的 U6 单独一行。演练库 1,294 个用户有多个显示名（与试算一致），哈希碰撞 0。

```bash
# 验证大小写、空白、摘要差异不合并，哈希碰撞拆组，显示名变体
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py -k "ac03"
```

期望：`1 passed, 4 deselected`，退出码 `0`。

## AC04 评论家族

- [x] 2026-11-03 通过（T8-02）：A、B、C 同一家族（A–C 的 Jaccard 0.6），D、E 各自成家族，F、G 同一家族且 `weak_support`；`family_id` 为 `rf_` + sha256(家族内最小 ASIN) 前 16 位；阈值改为 0.7（新规则版本）时 C 自成家族，清洗阶段原样复用、只新增家族阶段键。

```bash
# 验证签名合组、Jaccard 0.5 连通、weak_support、family_id 与阈值变化
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_family.py -k "ac04"
```

期望：`2 passed, 1 deselected`，退出码 `0`。

## AC05 去重层次、代表记录与稳定 ID

- [x] 2026-11-03 通过（T8-03）：同 ASIN 八字段全同为 `D1_exact_same_asin`，只 helpfulness 不同为 `D2_same_asin_variant`（`diff_fields = {helpfulness}`，逻辑评论 helpfulness 取 `3/4`），同一家族 3 个 ASIN 为代表 + 2 条 `D3_family_copy`；代表记录为最小 `record_no`；跨家族同内容为两条逻辑评论；重跑（含换分区数）`logical_review_id` 不变；重复评论数 8 = `D1` + `D2` + `D3` = 键完整记录 21 − 逻辑评论 13。

```bash
# 验证 D1、D2、D3、代表记录、helpfulness 选择、跨家族不合并、重跑 ID 不变
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py -k "ac05"
```

期望：`1 passed, 4 deselected`，退出码 `0`。

## AC06 不合并的候选

- [x] 2026-11-03 通过（T8-03、T8-04）：X1 记一组 `cross_family_same_content`，X2（Jaccard 0.9286）记一对 `near_duplicate`，X3（0.6）无候选；X4 同一用户在同一统计组两部作品（不同家族）上 Jaccard 0.95 的两条评论记一对 `near_duplicate`（`stage = 'review_attribution'`、`basis.scope = 'stat_group'`），两条仍各为代表记录；关掉近似阈值后映射完全相同。演练库家族内 C2 3,470 对、统计组内跨家族 C2 7 对。

```bash
# X1–X4 与候选不改变映射
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py tests/integration/test_task8_attribution.py -k "ac06"
```

期望：`2 passed, 7 deselected`，退出码 `0`。

## AC07 商品与作品归属、合集口径

- [x] 2026-11-03 通过（T8-04）：A1、A2、C1 同一家族；商品层 L1 在 A1、A2 各算一条（A1 6、A2 5、C1 6 条逻辑评论）；M1 归属 L1（`via_asins = {A1,A2}`）与 L3（`via_asins = {A1}`、`also_on_collection`）各一次；L2 只在合集、L4 剧集、L5 未采集都不归属作品；M2 只在合集里，归属 0；类别为 `movie`、`collection_with_movies`、`tv_series`、`not_collected`；`check` 无问题。演练库归属逻辑评论 257,733 条，只在合集上的 13,994 条、合集与单品都有的 1,827 条。

```bash
# 只经 edition 归属作品、合集只在商品层、其他类别不进作品
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_attribution.py -k "ac07"
```

期望：`1 passed, 3 deselected`，退出码 `0`。

## AC08 评分统计组与评分口径

- [x] 2026-11-03 通过（T8-04）：统计组 `{M1,M2}`（`likely_duplicate_movie`）、`{M3,M4}`（共享 1 条）、`{M5}`；作品与组的去重前后记录数、平均分与手算一致（如组 `{M3,M4}` 去重前 3 条 3.6667、去重后 2 条 3.0；M5 去重前 3 条 3.3333、去重后 2 条 3.0）；共享数 M3、M4 各 1；`{M1,M2}` 有一对近似评论，各计一次（5 条 4.0），敏感性去掉较晚一条后 4 条 4.25；任务 2–7 的表摘要不变；另有 2 项单元测试（连通分量、敏感性）。演练库多作品组 259 个（541 部），与试算一致。

```bash
# 统计组、组内只计一次、共享数、去重前后平均分、敏感性数字
docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task8_rules.py tests/integration/test_task8_attribution.py -k "ac08"
```

期望：`3 passed, 28 deselected`，退出码 `0`。

## AC09 版本、阶段依赖与复用

- [x] 2026-11-03 通过（T8-04）：同输入第二次运行为 `reused`、指向 `rd0001`，`task8` 除 `run` 外全部表行数与摘要不变；家族阈值改为 0.7（新规则版本）生成 `rd0002`，`review_clean` 复用、家族及下游三个阶段键变化，原先共享评论的两部作品分为两组；改用 `cv0002` 生成 `rd0003`，只有 `review_attribution` 阶段键变化；`diff_versions` 列出阶段键、计数与作品统计的变化；三个版本都是 `verified`，`rd0001` 重新核对无问题。

```bash
# reused、改阈值重算家族及下游、改 cv 只重算归属、版本差异
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_runs.py -k "ac09"
```

期望：`1 passed, 5 deselected`，退出码 `0`。

## AC10 分批恢复与并发锁

- [x] 2026-11-03 通过（T8-04）：完整运行中分别在 `review_clean`（每批 3 条）、`review_family`、`review_dedup` 第 2 批提交前与 `review_family` 连通分量提交前注入故障：只有已提交单元落库（如清洗只有 1–3 号记录），运行 `failed`、没有版本，家族表在连通分量失败时为空；重跑跳过已提交单元，16 张内容表的摘要与一次跑完一致；另一连接持有抽取咨询锁时完整运行与分阶段运行都确定性失败（`RunBusy`），不留运行记录。演练中去重核验单元报 `DiskFull` 后重跑从该单元继续，结果通过核对。

```bash
# 三个阶段第 2 批前故障后重跑一致、家族无半写入、并发失败
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_runs.py -k "ac10"
```

期望：`5 passed, 1 deselected`，退出码 `0`。

## AC11 Airflow DAG 与核对后才成功

- [x] 2026-11-03 通过（T8-05）：9 节点拓扑、重试参数、`verify` 通过才 `succeeded`、上游缺失与 `verify` 失败分支的 8 项测试通过；真实 scheduler（`compose.e2e.yaml`，写 `etl_test`）四种情况符合预期：`t8_e2e_ok1` 成功、`t8_e2e_rerun1` 记 `reused`、`t8_e2e_missing1`（`cv9999`）在 `check_inputs` 失败并写失败报告、`t8_e2e_vf1` 留 `unverified` 后 `t8_e2e_vf2` 重跑 `verified`；随后切回正常配置。逐条命令见 tasks.md T8-05。

```bash
# 验证 9 节点拓扑、重试参数、上游缺失与 verify 失败分支；真实 scheduler 见 runbook 第 2 节
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_report.py tests/integration/test_task8_dag.py -k "ac11"
```

期望：`8 passed, 3 deselected`，退出码 `0`。

## AC12 报告、实例与查询

- [x] 2026-11-03 通过（T8-05）：`summary.json` 与数据库计数一致、`examples.md` 覆盖全部类别、四个查询带口径说明、导出无评论正文全文（只留 80 字前缀）、`checksums.json` 校验；演练库导出 7 个文件，12 类实例各 3 条。

```bash
# 验证 summary 与数据库一致、examples 覆盖全部类别、四个查询与口径说明、导出无正文全文
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_report.py tests/integration/test_task8_dag.py -k "ac12"
```

期望：`3 passed, 8 deselected`，退出码 `0`。

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
