# 任务 5 AC 完成情况（人物姓名归一与消歧）

状态：2026-10-29 编写计划；2026-10-30 按用户要求加强没有 IMDb 关联作品的跨作品人物匹配，AC 重新编号为 AC00–AC17。2026-10-30 用户审批通过计划；AC01–AC13 已用合成夹具通过（T5-01 至 T5-06）；AC14、AC16、AC17 于同日在正式库通过（T5-07）；AC15 于同日通过（T5-08）：标注来自 AI 预标注，付林轩核查后全部采纳并署名导入，不是用户独立逐条标注，指标可能偏乐观。AC00–AC17 均已有证据；AI 的检查不等于用户亲自复核。AC 的输入与预期只在 [spec](spec.md) 定义，本文只记完成情况、复核命令和应看到的结果。

复核方法：以下命令都在项目根目录直接复制运行（macOS 默认的 zsh 即可）。`pytest` 命令连接独立测试库 `etl_test`，不碰业务库；业务库只给只读查询。AC14–AC17 的命令已于 2026-10-30 在 zsh 中原样运行核对（AC16 的实例在导入标注后重新核对，见该节）。

一次跑全部测试：

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：2026-10-30 完成 T5-07 后为 `691 passed`（任务 1–4 的 505 项不变，任务 5 共 186 项；T5-07 因正式库暴露的两个缺陷新增 2 项）。

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

期望：`15 passed, 8 deselected`（T5-07 新增 1 项：同一人物既是导演又是演员）。

## AC04 外部人物 ID：作品年表反查与任务 4 升级线索

- [x] 2026-10-30 通过：自动关联、别名置信度 0.80、两部同名作品与两个同名已核实人物为复核、从未核实/角色不同不关联、年份差 3 年复核、升级线索、合集成员归属、任务 4 的表行数与内容不变。

```bash
docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_external.py -k "filmography"
```

期望：`9 passed, 14 deselected`（T5-07 新增 1 项：合集两个成员标题键相同）。

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

- [x] 2026-10-30 通过：经 Airflow 全量运行 `t5_full_1c`（8 分 47 秒）；56,809 条提及 → 29,635 个人物，核对 `exit=0`、`problems: []`，任务 4 的表行数前后相同，重跑为 `reused`。正式运行此前有两次失败尝试（`t5_full_1` 与 `t5_full_1b`），缺陷与修复见 [design](design.md)“实现中的修订（T5-07）”。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.check > /dev/null; echo "exit=$?"
```

期望：`exit=0`。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.check 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['problems'], d['persons'], d['mentions_expected'], d['auto_links_credits'], d['auto_links_filmography'], d['auto_pair_decisions'], len(d['rounds']), d['movie_person_rows'])"
```

期望：`[] 29635 56809 28903 1286 1671 4 50027`（无问题；人物数；提及数；途径一与途径二的自动外部 ID；自动合并边；迭代轮数；MoviePerson 行数）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT 'product_movie', count(*) FROM task4.product_movie UNION ALL SELECT 'movie', count(*) FROM task4.movie UNION ALL SELECT 'merge_decision', count(*) FROM task4.merge_decision UNION ALL SELECT 'mapping_version', count(*) FROM task4.mapping_version"
```

期望：`product_movie|17852`、`movie|17268`、`merge_decision|70424`、`mapping_version|2`，与运行前取的基线逐行相同（`check` 的输出 `t4_table_rows` 列出任务 4 全部 14 张表的行数，运行前后也相同）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, count(*) FROM task5.person GROUP BY status ORDER BY status"
```

期望：`external_verified|13233`、`page_only|10371`、`review_pending|6031`（待复核人物占 20.35%）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT c.route || '/' || d.tier, count(*) FROM task5.link_candidate c JOIN task5.link_decision d USING (link_candidate_id) GROUP BY 1 ORDER BY 1"
```

期望：`credits/auto|28903`、`credits/review|965`、`filmography/auto|1286`、`filmography/review|409`（按途径的外部 ID：作品演职员 28,903 条提及，作品年表 1,286 条）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT round, new_auto_edges FROM task5.iteration_round ORDER BY round"
```

期望：`1|1193`、`2|25`、`3|1`、`4|0`（迭代 4 轮，第 4 轮没有新增即停止；合作者佐证并入 2,284 条提及）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT count(*) FROM task5.t4_upgrade_hint"
```

期望：`335`（任务 4 升级线索，只记录）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, airflow_run_id, person_mapping_version_id FROM task5.run ORDER BY started_at"
```

期望：`succeeded|t5_full_1c|pmap0001`、`reused|t5_full_2|pmap0001`、`reused|t5_full_3|pmap0001`；`SELECT (SELECT count(*) FROM task5.person_mention), (SELECT count(*) FROM task5.link_decision), (SELECT count(*) FROM task5.person), (SELECT count(*) FROM task5.movie_person)` 重跑前后都是 `56809|31563|29635|50027`。

## AC15 用户标注与指标

- [x] 2026-10-30 通过。**标注来源：AI 预标注，付林轩核查后于 2026-10-30 全部采纳作为正式标注导入**（用户原话：AI 做的标签都是正确的，同意署名付林轩核查完毕）；不是用户独立逐条标注，未改判一条。评价集 `ev_pmap0001`（规则 `t5r1` 冻结后导出，开发池姓氏键已排除），导入 120 对（same 85、different 33、unsure 2，unsure 保持 unsure，AI 存疑的 8 对一并采纳）与 20 个人物（全为 `same_person`，AI 存疑的 5 个人物一并采纳），无矛盾项。自动合并 Precision 1.000、Recall 0.706、F1 0.828（TP 60、FP 0、FN 25、TN 33，计入 118 对）；误合并 0；漏合并 25（S4 18、S5 5、S6 2）；簇抽查混入 0；人工复核覆盖率 0%（0 / 19,787）、待复核率 20.4%（有待复核候选的人物占比 28.0%）。
- 分层：S1、S2、S7、S8 全部 TP（25、10、15、10），P = R = F1 = 1.000；S3 TN 15；S4 TP 0、FN 18、TN 1；S5 FN 5、TN 9；S6 FN 2、TN 8（S3–S6 的 Precision 与 F1 无定义，S4–S6 Recall 0）。途径：`credits` 40/0/0、`filmography` 10/0/0、`co_credit` 10/0/0 都是 P = R = 1.000；`none`（系统未合并的同名与近似名）FN 25、TN 33。
- 局限：正式标签来自 AI 预标注，AI 与系统依据相似的证据，指标可能偏乐观；25 条漏合并是 AI 判同一人而系统不按名字单独合并的对（见 design），如果去掉其中 2 条 AI 存疑的 same（`683def41ea42`、`1d932fb092bf`），Recall 为 0.723；指标只代表评价样本，不代表全量准确率；没有第二标注人。

标注入库（只读）：

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT annotator, label, count(*) FROM task5.eval_label GROUP BY 1, 2 ORDER BY 2"
```

期望：三行 `付林轩|different|33`、`付林轩|same|85`、`付林轩|unsure|2`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT annotator, mixed, count(*) FROM task5.cluster_label GROUP BY 1, 2 ORDER BY 2"
```

期望：一行 `付林轩|f|20`（20 个人物都没有混入）。

指标（重新计算并写 `metrics.json`、`metrics.md`）：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.evaluate metrics 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); o=d['overall']; print(d['annotators'], d['labeled'], d['unsure']); print(o['tp'], o['fp'], o['fn'], o['tn'], round(o['precision'],3), round(o['recall'],3), round(o['f1'],3)); print({s:(b['tp'],b['fp'],b['fn'],b['tn']) for s,b in d['by_stratum'].items()}); print({r:(b['tp'],b['fp'],b['fn']) for r,b in d['by_route'].items()}); print(len(d['false_merges']), len(d['missed_merges']), d['review']['review_coverage'], d['review']['review_pending_rate'], d['clusters']['labeled'], d['clusters']['mixed'])"
```

期望：第一行 `['付林轩'] 120 2`；第二行 `60 0 25 33 1.0 0.706 0.828`；第三行 `{'S1': (25, 0, 0, 0), 'S2': (10, 0, 0, 0), 'S3': (0, 0, 0, 15), 'S4': (0, 0, 18, 1), 'S5': (0, 0, 5, 9), 'S6': (0, 0, 2, 8), 'S7': (15, 0, 0, 0), 'S8': (10, 0, 0, 0)}`；第四行 `{'co_credit': (10, 0, 0), 'credits': (40, 0, 0), 'filmography': (10, 0, 0), 'none': (0, 0, 25)}`；第五行 `0 25 0.0 0.203509 20 0`。

```bash
cat outputs/task5/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/pmap0001/eval/metrics.md
```

期望：含“标注人：付林轩；已标注 120 / 120 对，不确定 2”“合计 | 60 | 0 | 25 | 33 | 100.0% | 70.6% | 82.8%”、误合并实例“无”、25 条漏合并实例、“抽查 20 个人物，已标注 20 个，混入不同的人的人物 0 个”。

导入后核对与任务 4 表不变：

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.check 2>/dev/null | grep '"problems"'; docker compose --profile crawl run --rm -T crawler python -m etl_task5.check >/dev/null 2>&1; echo "exit=$?"
```

期望：`  "problems": []` 与 `exit=0`。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT 'product_movie', count(*) FROM task4.product_movie UNION ALL SELECT 'movie', count(*) FROM task4.movie UNION ALL SELECT 'merge_decision', count(*) FROM task4.merge_decision UNION ALL SELECT 'mapping_version', count(*) FROM task4.mapping_version"
```

期望：`product_movie|17852`、`movie|17268`、`merge_decision|70424`、`mapping_version|2`。

## AC16 正式实例与查询

- [x] 2026-10-30 通过：报告列出 5 组别名合并（含 Tim Robbins、Rubén Blades）与 5 组同名不合并（Paul Guilfoyle、Bruce Lee、Federico Fellini、Marc Lawrence、Charles Lane，都有外部 ID 冲突或年代冲突），每组带原始署名、作品、外部 ID 或冲突证据与页面快照定位；T5-08 导入标注后重新核对：实例改为优先取已标注的，5 组别名合并都标 `same`、5 组不合并都标 `different`（标注人付林轩，来源是 AI 预标注经核查采纳，见 AC15）。两个查询在正式库返回非空结果。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.report examples 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['alias_merges']), len(d['not_merged'])); print([(e['canonical_name'], e['external_id']['nconst']) for e in d['alias_merges']]); print([(e['left']['raw'], e['right']['raw'], e['reason']) for e in d['not_merged']])"
```

期望：第一行 `5 5`；第二行 `[('Josh Lucas', 'nm0524197'), ('Robert Duvall', 'nm0000380'), ('John Turturro', 'nm0001806'), ('Julianne Moore', 'nm0000194'), ('Lawrence Dane', 'nm0199325')]`；第三行 `[('Paul Guilfoyle', 'Paul Guilfoyle', 'different_external_id'), ('Charles Lane', 'Charles Lane', 'different_external_id'), ('David Bradley', 'David Bradley', 'different_external_id'), ('John Vernon', 'John Vernon', 'different_external_id'), ('Robert Taylor', 'Robert Taylor', 'different_external_id')]`（导入标注前的实例是 Olivia de Havilland、Tim Robbins 等与 Bruce Lee、Federico Fellini 等，选择规则优先取已标注的实例，所以 T5-08 之后换了一批）。含标注结论与源记录定位的完整实例在 `outputs/task5/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/pmap0001/t5_final_labeled/examples.md`（导入标注后用 `report export --run-key t5_final_labeled` 重新导出；`t5_full_3/examples.md` 与 `pmap0001/examples.json` 是导入前的版本，实例标“未标注”）。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.query person ps_e29f65eda4d251ef 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['found'], d['canonical_name'], d['external_id']['nconst'], [a['alias'] for a in d['aliases']], d['mentions_total'], len(d['works']), len(d['decision_chain']))"
```

期望：`True Tim Robbins nm0000209 ['Tim Robbins', 'Robbins, Tim'] 8 5 5`（有倒序别名的人物：原始署名 8 条、5 部作品、决策链 5 条）。

```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task5.query director "Alfred Hitchcock" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['matched_persons'], [(p['person_id'], p['status'], len(p['works'])) for p in d['persons']])"
```

期望：`5 [('ps_1c22cf4d8a2a9b3d', 'review_pending', 1), ('ps_3e754811ebc66e3b', 'review_pending', 1), ('ps_886f4b237a4c4f5e', 'external_verified', 16), ('ps_d5d249d5e3f4df44', 'review_pending', 1), ('ps_ff05ed7834fc4018', 'review_pending', 0)]`（同名的 5 个人物分别列出：有 `nm0000033` 的一个、四个待复核；这是 R17 的预期行为：没有外部 ID 的同名导演不凭姓名合并）。

## AC17 迁移、测试与仓库内容

- [x] 2026-10-30 通过：迁移版本含 `401`；全部测试 `691 passed`（任务 1–4 的 505 项不变）；`git ls-files` 没有 IMDb 文件、导出、评价表与 `.env`（`outputs/` 在 `.gitignore` 中）。

```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT string_agg(version, ',' ORDER BY version) FROM public.schema_migration"
```

期望：`001,101,102,103,201,301,302,401`。

```bash
docker compose --profile test run --rm etl-test pytest -q tests
```

期望：`691 passed`（含若干 Airflow 弃用警告）。

```bash
git ls-files | grep -Ei '(^|/)\.env$|^outputs/|eval_pairs|cluster_sample|review_queue|name\.basics|title\.(basics|akas|crew|principals|ratings)|\.tsv(\.gz)?$|movies\.txt'; echo "grep-exit=$?"
```

期望：没有匹配行，只有 `grep-exit=1`；`git check-ignore -v outputs/task5/x.csv` 输出 `.gitignore:16:/outputs/	outputs/task5/x.csv`。
