# 任务 8 开发任务（评论清洗与重复识别）

状态：2026-11-03 编写计划；T8-00 已执行并通过（AC00）。T8-01 至 T8-08 尚未实现，其命令与期望均标注 `（计划）`，实现后按实际运行结果改写。每完成一个任务在本地提交一次，未经用户要求不推送。

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

- [ ] T8-03 去重与目标层 `review_dedup`：分组核验、`D1`–`D3`、逻辑评论、`ReviewMapping`、`Reviewer`、候选 `C1`、`C2`。规则：R03–R05、R10、R11、R14–R25；关联 AC01、AC03、AC05、AC06。
  - 文件：`src/etl_task8/dedup.py`、`tests/integration/test_task8_dedup.py`。
  - 目标：按用户哈希分区提交；隔离与不完整记录写映射；哈希碰撞拆组；演练库上逻辑评论数、各层次记录数、候选数与试算对照，抽查各层实例。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）去重与目标层集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_dedup.py
  ```

  期望（计划）：全部通过，退出码 `0`。

- [ ] T8-04 归属与统计 `review_attribution`、版本物化与差异、分批恢复与并发锁。规则：R26–R36、R38；关联 AC07–AC10。
  - 文件：`src/etl_task8/{attribution,versions,runner}.py`、`tests/integration/test_task8_{attribution,runs}.py`。
  - 目标：作品归属只经 `edition`、合集单列、评分统计组、三类统计表；`finalize_version` 只标 `unverified`；阶段键增量重算；`report diff`；故障注入与锁。演练库上统计组数、作品评分去重前后差异与试算对照。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）归属、统计与运行恢复集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_attribution.py tests/integration/test_task8_runs.py
  ```

  期望（计划）：全部通过，退出码 `0`。

- [ ] T8-05 报告、查询、只读核对、Airflow DAG 与 runbook。规则：R37、R42–R44；关联 AC11、AC12。
  - 文件：`src/etl_task8/{report,query,check,pipeline}.py`、`dags/amazon_reviews_task8_dedup.py`、`scripts/seed_task8_e2e.py`、`tests/integration/test_task8_{report,dag}.py`、`doc/task8/runbook.md`。
  - 目标：9 节点 DAG，`verify` 通过才 `succeeded`；`compose.e2e.yaml` 下经真实 scheduler 跑通成功、重跑 `reused`、上游缺失、`verify` 失败四种情况后切回正常配置；`report export`、四个查询与 `check`。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）报告、查询与 DAG 集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_report.py tests/integration/test_task8_dag.py
  ```

  期望（计划）：全部通过，退出码 `0`。

- [ ] T8-06 评价工具：开发池、六层盲测表、AI 草稿、导入校验、指标。规则：R45–R49；关联 AC13。
  - 文件：`src/etl_task8/evaluate.py`、`tests/integration/test_task8_evaluate.py`、`doc/task8/runbook.md`（评价章节）。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）评价工具集成测试
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_evaluate.py
  ```

  期望（计划）：全部通过，退出码 `0`。

- [ ] T8-07 业务库副本全链路演练。规则：R41；关联 AC15。
  - 文件：`doc/task8/design.md`（以演练实测替换试算或注明差异）、`doc/task8/tasks.md`、`doc/task8/checklist.md`。
  - 目标：建 `etl_t8_rehearsal`，全链路运行、`check`、`report export`、`evaluate freeze`/`export` 演练；逐项解释与试算基线的差异；删除演练库。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）演练结束后确认演练库已删除
  docker compose exec -T postgres psql -U postgres -Atc "SELECT datname FROM pg_database ORDER BY 1"
  ```

  期望（计划）：只有 `airflow_meta`、`etl_movies`、`etl_test`、`postgres`、`template0`、`template1`，没有 `etl_t8_rehearsal`。

- [ ] T8-08 正式库运行与报告：冻结开发池、经 Airflow 生成 `rd0001`、重跑 `reused`、核对、导出报告与盲测表；审核标注导入后计算指标，编写 `report.md` 与交接文档。规则：正式库运行检验全部规则（见 AC16–AC18 的 Rule 列）；关联 AC16–AC18。
  - 文件：`doc/task8/{report,spec,design,tasks,checklist}.md`、`doc/handoffs/<日期>-task8-to-task9.md`、`README.md`、`CLAUDE.md`；`outputs/task8/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rd0001/`（不入 Git）。
  - 完成情况：未执行（计划）。

  ```bash
  # （计划）正式库只读核对
  docker compose --profile crawl run --rm -T crawler python -m etl_task8.check > /dev/null; echo "exit=$?"
  ```

  期望（计划）：`exit=0`。
