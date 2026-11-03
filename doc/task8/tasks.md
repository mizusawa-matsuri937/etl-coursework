# 任务 8 开发任务（评论清洗与重复识别）

状态：2026-11-03 编写计划；T8-00 已执行并通过（AC00）；T8-01 至 T8-04 于同日实现并在业务库副本演练，T8-05、T8-06 同日实现（DAG 经真实 scheduler 在 `etl_test` 验证，演练库已删除）。T8-07 同日在业务库副本按“先冻结、再运行”重做全链路并通过（演练库已删除）；T8-08 同日在正式库运行完成（`rd0001` `verified`，报告与盲测表、AI 草稿已导出），评价待付林轩审核标注，故未勾选。每完成一个任务在本地提交一次，未经用户要求不推送。

命令均在项目根目录 `/Users/aqua_night/codex_project/数据存储与管理/ETL作业模板`（macOS 默认 zsh）下直接复制运行，不使用变量缩写。每项 AC 的逐条复核命令见 [checklist](checklist.md)。每个实现阶段完成后先在业务库副本 `etl_t8_rehearsal` 演练（design“业务库副本演练”），再进入下一阶段。

- [x] T8-00 计划：正式库只读探查，编写 `doc/task8/{spec,design,tasks,checklist}.md`，执行 AC00 规范自检。规则：全部；关联 AC00。
  - 文件：`doc/task8/spec.md`、`doc/task8/design.md`、`doc/task8/tasks.md`、`doc/task8/checklist.md`；`README.md`、`CLAUDE.md`（任务 7 合并状态与任务 8 状态）。
  - 完成情况：2026-11-03 完成。正式库只读探查（试算基线见 design），用户确认 H01–H04；AC00 自检通过。

  ```bash
  grep -n "^## " doc/task8/spec.md
  diff <(grep -o "^| AC[0-9][0-9]" doc/task8/spec.md | tr -d '| ') <(grep -o "^## AC[0-9]*" doc/task8/checklist.md | tr -d '# ')
  grep -c "^- R[0-9][0-9] \[" doc/task8/spec.md
  python3 -c "import re; s=open('doc/task8/spec.md').read(); t=open('doc/task8/tasks.md').read(); ex=lambda x: {i for a,b in re.findall(r'R(\d\d)(?:–R(\d\d))?', x) for i in range(int(a), int(b or a)+1)}; rules={int(n) for n in re.findall(r'^- R(\d\d) \[', s, re.M)}; ac=ex(''.join(re.findall(r'^\| AC\d\d \| ([^|]*)\|', s, re.M))); tk=ex(''.join(re.findall(r'规则：([^；]*)；', t))); print(len(rules), sorted(rules-ac), sorted(rules-tk))"
  ```

  期望：第一条只输出 `目标、范围、规则、AC测试、假设` 五行；第二条无输出；第三条输出 `49`；第四条输出 `49 [] []`（49 条规则，没有未被 AC 或 T8-01 至 T8-07 覆盖的规则；T8-08 的正式运行另检验全部规则，不计入这项检查）。

- [x] T8-01 迁移 701、运行审计与咨询锁、规则纯函数、清洗阶段 `review_clean`。规则：R01、R02、R06–R09、R39、R40；关联 AC02、AC14，部分 AC03。
  - 文件：`sql/701_task8_schema.sql`、`src/etl_task8/{__init__,rules,sources,run,clean,runner}.py`（`runner` 目前只有分阶段运行 `stages`，完整运行由 T8-04 补充）、`tests/fixtures/task8/`、`tests/unit/test_task8_rules.py`、`tests/integration/{_task8_helpers,test_task8_clean}.py`；`tests/integration/conftest.py` 增加 `DROP SCHEMA IF EXISTS task8`。
  - 目标：建立 schema `task8` 全部表与闭集 CHECK；输入键、阶段键、审计与锁；helpfulness 状态、内容键与核验、ID 生成等纯函数；夹具与任务 1、3、7 真实表同形；在 `etl_t8_rehearsal` 演练清洗阶段并对照试算（helpfulness 各状态、不完整 12 条）。
  - 完成情况：2026-11-03 通过。29 项测试通过（单元 25、清洗集成 4），全量 1,158 项通过（同日运行，含 T8-02、T8-03 的 8 项；原 1,121 项不减少）；夹具由任务 1 装载器生成。演练库清洗 7,849,565 行 16 批 581 秒：`ok` 5,869,509、`zero_zero` 1,980,001、分子大于分母 43、`missing` 12、负数与无法解析 0，不完整 12，问题 67 条，与试算完全一致（design“演练实测”）。

  ```bash
  # 规则单元测试与清洗阶段集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task8_rules.py tests/integration/test_task8_clean.py
  ```

  期望：`29 passed`，退出码 `0`。

- [x] T8-02 评论家族 `review_family`：签名、分区重叠计数、Jaccard 连通分量、`weak_support`。规则：R12、R13；关联 AC04。
  - 文件：`src/etl_task8/family.py`、`tests/integration/test_task8_family.py`。
  - 目标：签名与重叠计数分批落库，连通分量单事务写入；演练库上家族数、`weak_support` 数与试算对照并解释差异。
  - 完成情况：2026-11-03 通过。3 项测试通过（AC04 两项、分区中断恢复与连通分量无半写入一项）。演练库 32 分区 53 秒：家族 140,584 个（试算 140,587）、多 ASIN 家族 45,953（试算 45,956）、签名组对 12,230、相连边 1,419、最大家族 57 个 ASIN；`weak_support` 61,054 个家族，其中多 ASIN 的 12,054 个（30,290 个 ASIN），差异解释见 design“演练实测”。

  ```bash
  # 评论家族集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_family.py
  ```

  期望：`3 passed`，退出码 `0`。

- [x] T8-03 去重与目标层 `review_dedup`：分组核验、`D1`–`D3`、逻辑评论、`ReviewMapping`、`Reviewer`、候选 `C1`、`C2`。规则：R03–R05、R10、R11、R14–R25；关联 AC01、AC03、AC05、AC06。
  - 文件：`src/etl_task8/dedup.py`、`tests/integration/test_task8_dedup.py`。
  - 目标：按用户哈希分区提交；隔离与不完整记录写映射；哈希碰撞拆组；演练库上逻辑评论数、各层次记录数、候选数与试算对照，抽查各层实例。
  - 完成情况：2026-11-03 通过。5 项测试通过（AC01、AC03、AC05、AC06 家族内部分、分区与写入批与中断恢复），全量 1,158 项通过。演练库 34 个单元 388 秒：7,911,696 条记录各一行映射（`mapped` 7,849,553、`quarantined` 62,131、`incomplete` 12），逻辑评论 1,908,386（试算约 1,908,873），`D1` 4,501、`D2` 9,432、`D3` 5,927,234，C1 4,202 组（试算 4,204）、家族内 C2 3,470 对，哈希碰撞 0；差异解释见 design“演练实测”。统计组内的 C2（AC06 的 X4）由 T8-04 补充。

  ```bash
  # 去重与目标层集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py
  ```

  期望：`5 passed`，退出码 `0`。

- [x] T8-04 归属与统计 `review_attribution`、版本物化与差异、完整运行与核对、分批恢复与并发锁。规则：R26–R36、R38；关联 AC07–AC10，补齐 AC01、AC06。
  - 文件：`src/etl_task8/{attribution,versions,check,runner}.py`（`runner` 补 `execute_dedup`、DAG 步骤 `run_stage`、`run` 子命令；`check` 原计划在 T8-05，因 `verify` 依赖它提前实现）、`src/etl_task8/{rules,sources,dedup}.py`（统计组纯函数、上游取数、核验单元关闭并行哈希）、`tests/integration/{_task8_helpers,test_task8_attribution,test_task8_runs}.py`、`tests/unit/test_task8_rules.py`。
  - 目标：作品归属只经 `edition`、合集单列、评分统计组、三类统计表；`finalize_version` 只标 `unverified`；阶段键增量重算；`diff_versions`（`report diff` 的命令行在 T8-05）；故障注入与锁。演练库上统计组数、作品评分去重前后差异与试算对照。
  - 完成情况：2026-11-03 通过。新增 12 项测试（归属 4、运行 6、单元 2），全量 1,170 项通过（原 1,158 项不减少）；演练库完整运行生成 `rd0001` 并通过核对：归属逻辑评论 257,733、多作品统计组 259 个（541 部）、去重后评论数变化的作品 542 部，与试算一致（design“业务库副本演练实测（T8-04）”）。

  ```bash
  # 归属、统计与运行恢复集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_attribution.py tests/integration/test_task8_runs.py
  ```

  期望：`10 passed`，退出码 `0`。

- [x] T8-05 报告、查询、只读核对、Airflow DAG 与 runbook。规则：R37、R42–R44；关联 AC11、AC12。
  - 文件：`src/etl_task8/{report,query,pipeline}.py`（`check` 已在 T8-04 实现）、`dags/amazon_reviews_task8_dedup.py`、`scripts/seed_task8_e2e.py`、`tests/integration/test_task8_{report,dag}.py`、`doc/task8/runbook.md`。
  - 目标：9 节点 DAG，`verify` 通过才 `succeeded`；`compose.e2e.yaml` 下经真实 scheduler 跑通成功、重跑 `reused`、上游缺失、`verify` 失败四种情况后切回正常配置；`report export`、`report diff`、四个查询与 `check`。
  - 完成情况：2026-11-03 通过。新增 11 项测试（DAG 8、报告 3）；真实 scheduler 四种情况均符合预期（成功 9 个任务 success；重跑 `reused`；`cv9999` 时 `check_inputs` 失败、其后 6 个 `upstream_failed`、`finalize_report` 仍写失败报告、`gate` 失败；`verify_fail` 时 `rd0001` 留 `unverified`，无注入重跑后 `verified`）；之后切回正常配置，`etl_movies` 无 `task8` schema。演练库 `report export` 7 个文件、12 类实例各 3 条，用时约 26 秒。

  ```bash
  # 报告、查询与 DAG 集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_report.py tests/integration/test_task8_dag.py
  ```

  期望：`11 passed`，退出码 `0`。

  真实 scheduler 端到端（可选，会清空 `etl_test`，运行前后切换配置见 [runbook](runbook.md) 第 2 节；以下命令在切到 `compose.e2e.yaml` 后执行）：

  ```bash
  docker compose --profile test run --rm -T etl-test python - < scripts/seed_task8_e2e.py
  ```

  期望：`{"extraction_id": "6214025170700962bf5d7e8cf164f9c66c38b3090f8eb20f327edb5740155e61", "records": 47, "products": 14}`。

  ```bash
  COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_reviews_task8_dedup --run-id t8_e2e_ok1 --conf '{"extraction_id":"6214025170700962bf5d7e8cf164f9c66c38b3090f8eb20f327edb5740155e61","batch_size":10,"partitions":2}'
  ```

  期望：9 个任务 `success`，末行 `DAG run t8_e2e_ok1: success`，退出码 `0`。

  ```bash
  COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_reviews_task8_dedup --run-id t8_e2e_rerun1 --conf '{"extraction_id":"6214025170700962bf5d7e8cf164f9c66c38b3090f8eb20f327edb5740155e61","batch_size":10,"partitions":2}'
  ```

  期望：`DAG run t8_e2e_rerun1: success`；随后 `docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT status, review_dedup_version_id FROM task8.run ORDER BY started_at"` 输出 `succeeded|rd0001` 与 `reused|rd0001` 两行。

  ```bash
  COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_reviews_task8_dedup --run-id t8_e2e_missing1 --conf '{"extraction_id":"6214025170700962bf5d7e8cf164f9c66c38b3090f8eb20f327edb5740155e61","conflict_version_id":"cv9999","batch_size":10,"partitions":2}'
  ```

  期望：`check_inputs` `failed`，`review_clean` 至 `verify` 6 个 `upstream_failed`，`finalize_report` `success`，`gate` `failed`，`DAG run t8_e2e_missing1: failed`，退出码 `1`；`ls outputs/e2e/task8/_failures/t8_e2e_missing1` 列出 `check_inputs-try1.json`、`report.md`、`run_summary.json`。

  ```bash
  docker compose --profile test run --rm -T etl-test python - < scripts/seed_task8_e2e.py
  ```

  ```bash
  COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_reviews_task8_dedup --run-id t8_e2e_vf1 --conf '{"extraction_id":"6214025170700962bf5d7e8cf164f9c66c38b3090f8eb20f327edb5740155e61","batch_size":10,"partitions":2,"fault_injection":"verify_fail"}'
  ```

  期望：`verify` 与 `gate` `failed`，其余 7 个 `success`，退出码 `1`；`ls outputs/e2e/task8/_failures/t8_e2e_vf1` 列出 `report.md`、`run_summary.json`、`verify-try1.json`；`task8.run` 为 `failed|rd0001`，`task8.dedup_version` 为 `rd0001|unverified`。

  ```bash
  COMPOSE_FILE=compose.yaml:compose.e2e.yaml python3 scripts/run_dag_e2e.py --dag-id amazon_reviews_task8_dedup --run-id t8_e2e_vf2 --conf '{"extraction_id":"6214025170700962bf5d7e8cf164f9c66c38b3090f8eb20f327edb5740155e61","batch_size":10,"partitions":2}'
  ```

  期望：`DAG run t8_e2e_vf2: success`，退出码 `0`；`docker compose exec -T postgres psql -U postgres -d etl_test -Atc "SELECT status, review_dedup_version_id, airflow_run_id FROM task8.run ORDER BY started_at; SELECT review_dedup_version_id, status FROM task8.dedup_version"` 输出 `failed|rd0001|t8_e2e_vf1`、`succeeded|rd0001|t8_e2e_vf2`、`rd0001|verified`。

- [x] T8-06 评价工具：开发池、六层盲测表、AI 草稿、导入校验、指标。规则：R45–R49；关联 AC13。
  - 文件：`src/etl_task8/evaluate.py`、`tests/integration/test_task8_evaluate.py`、`doc/task8/runbook.md`（评价章节）。
  - 完成情况：2026-11-03 通过（只做工具与测试，未在正式库冻结或导出）。新增 5 项测试，全量 1,186 项通过；演练库 `freeze` 开发池 177,261 / 886,308 名评论者（运行后才冻结，`frozen_before_first_run` 为 `false`），`export` 120 项六层全满、`V2` 中 `weak_support` 10 项、盲测表无系统结论，用时约 92 秒。

  ```bash
  # 评价工具集成测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_evaluate.py
  ```

  期望：`5 passed`，退出码 `0`。

- [x] T8-07 业务库副本全链路演练。规则：R41；关联 AC15。
  - 文件：`doc/task8/design.md`（“业务库副本全链路演练实测（T8-07）”）、`doc/task8/runbook.md`（第 3 节补 `CHECKPOINT`、评价导出与重跑）、`doc/task8/tasks.md`、`doc/task8/checklist.md`。
  - 完成情况：2026-11-03 通过。先 `freeze`（`frozen_before_first_run: true`）再 `runner run`：`rd0001` `succeeded`/`verified`，887 秒；映射 7,911,696、逻辑评论 1,908,386、重复 5,941,167、家族 140,584、归属逻辑评论 257,733，与第 2 阶段演练逐项相同；`check` 退出码 0；`report export` 7 个文件；`evaluate export` 120 项六层全满；重跑 `reused`。建库时 WAL 峰值约 27 GB，`CHECKPOINT` 后回收；演练库已删除。

  ```bash
  # 演练导出的关键数字（演练库已删除，读本地 outputs/rehearsal_t8，不入 Git）
  python3 -c "import json; s=json.load(open('outputs/rehearsal_t8/task8/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rd0001/rehearsal1/summary.json')); print(s['version_status'], s['records']['read'], s['logical_reviews'], s['duplicates']['total'], s['families']['families'], s['movies']['logical_distinct'], s['stat_groups']['multi_movie_groups'])"
  ```

  期望：`verified 7911696 1908386 5941167 140584 257733 259`。

  ```bash
  # 演练结束后确认演练库已删除
  docker compose exec -T postgres psql -U postgres -Atc "SELECT datname FROM pg_database ORDER BY 1"
  ```

  期望：只有 `airflow_meta`、`etl_movies`、`etl_test`、`postgres`、`template0`、`template1`，没有 `etl_t8_rehearsal`。

- [ ] T8-08 正式库运行与报告：冻结开发池、经 Airflow 生成 `rd0001`、重跑 `reused`、核对、导出报告与盲测表；审核标注导入后计算指标，编写 `report.md` 与交接文档。规则：正式库运行检验全部规则（见 AC16–AC18 的 Rule 列）；关联 AC16–AC18。
  - 文件：`doc/task8/{report,design,runbook,tasks,checklist}.md`、`src/etl_task8/evaluate.py`（`V5` 按作品对去重）、`tests/integration/test_task8_evaluate.py`、`README.md`、`CLAUDE.md`；交接文档 `doc/handoffs/<日期>-task8-to-task9.md` 待评价完成后编写；`outputs/task8/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rd0001/`（不入 Git）。
  - 完成情况：2026-11-03 **正式库运行完成，评价待付林轩审核标注**（因此不勾选）。先 `freeze`（`frozen_before_first_run: true`），`t8_full_1` 9 个任务 success（15 分 13 秒）生成 `rd0001` `verified`，`t8_full_2` `reused`；`check` 退出码 0；数字与 T8-07 演练逐项相同（逻辑评论 1,908,386、重复 5,941,167），见 [report](report.md)。发现并修正评价 `V5` 重复抽同一对作品（全量 1,186 项通过），重导 120 项；AI 草稿 `eval_items_ai_draft.csv` 已生成（41 项拿不准），未导入任何标注，指标未计算。

  ```bash
  # 正式库只读核对
  docker compose --profile crawl run --rm -T crawler python -m etl_task8.check > /dev/null; echo "exit=$?"
  ```

  期望：`exit=0`。

  ```bash
  # 两次 Airflow 运行与版本状态（只读）
  docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT status, review_dedup_version_id, airflow_run_id FROM task8.run ORDER BY started_at; SELECT review_dedup_version_id, status FROM task8.dedup_version;"
  ```

  期望：`SET` 之后为 `succeeded|rd0001|t8_full_1`、`reused|rd0001|t8_full_2`、`rd0001|verified`。

  ```bash
  # 评价集与 AI 草稿（本地 outputs，不入 Git）：120 项、item_key 不重复、尚未导入标注
  docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT count(*), count(DISTINCT item_key) FROM task8.eval_item WHERE eval_set_id = 'ev_rd0001'; SELECT count(*) FROM task8.eval_label WHERE eval_set_id = 'ev_rd0001';"
  ```

  期望：`SET` 之后为 `120|120` 与 `0`。

  ```bash
  python3 -c "import csv, collections; r=list(csv.DictReader(open('outputs/task8/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rd0001/eval/eval_items_ai_draft.csv', encoding='utf-8'))); print(len(r), len({x['item_id'] for x in r}), dict(collections.Counter(x['ai_label'] for x in r)), sum(x['ai_doubt'] == 'true' for x in r), sum(bool(x['human_label']) for x in r))"
  ```

  期望：`120 120 {'same': 96, 'different': 23, 'unsure': 1} 41 0`（`human_label` 全空，待审核）。

  ```bash
  # V5 按作品对去重的回归测试（工作目录：项目根目录）
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_evaluate.py
  ```

  期望：`5 passed`，退出码 `0`。
