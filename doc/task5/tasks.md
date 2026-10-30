# 任务 5 开发任务（人物姓名归一与消歧）

状态：2026-10-29 编写计划；2026-10-30 按用户要求加入作品年表反查、合作者佐证迭代、稀有度与发行商特征、待复核队列，AC 重新编号为 AC00–AC17。2026-10-30 用户审批通过计划，开始实现。分支 `task5-person-identity`（从 `main` 的 `bf5b19d` 建立），未提交。

命令在项目根目录直接复制运行。T5-01 至 T5-06 的命令已实际运行核对；标注“（计划）”的命令依赖尚未实现的代码，期望值是预定口径，实现后实际运行核对再改写。每项 AC 的复核命令见 [checklist](checklist.md)。

- [x] T5-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：2026-10-30 AI 文档检查通过（五个板块、R01–R47、AC00–AC17 一一对应，每条规则都有 AC）；用户已确认 H01–H04，提出 H13–H15 的加强方向；2026-10-30 用户审批通过计划全文，其余假设按建议执行。

  ```bash
  grep -n "^## " doc/task5/spec.md
  ```

  期望：只有 `目标、范围、规则、AC测试、假设` 五行。

- [x] T5-01 迁移 401、人物提及与姓名规范化。关联 AC01、AC02。
  - 文件：`sql/401_task5_schema.sql`、`src/etl_task5/{names,mentions,run}.py`、`tests/unit/test_task5_names.py`、`tests/integration/{_task5_helpers,test_task5_mentions}.py`、`tests/fixtures/task5/`。
  - 完成情况：2026-10-30 通过：新增 `tests/unit/test_task5_names.py` 39 项、`tests/integration/test_task5_mentions.py` 12 项；迁移 401 随后随 T5-02、T5-03 追加表。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task5_names.py tests/integration/test_task5_mentions.py
  ```

  期望：`51 passed`（AC01 的选择器与通过数见 checklist）。

- [x] T5-02 外部 ID：途径一（关联作品演职员的兼容等级与判定）、途径二（已核实人物的作品年表反查、任务 4 升级线索）。关联 AC03、AC04。
  - 文件：`src/etl_task5/external.py`、`tests/integration/test_task5_external.py`。
  - 完成情况：2026-10-30 通过：`tests/integration/test_task5_external.py` 21 项（AC03 `-k credits` 14 项、AC04 `-k filmography` 8 项）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_external.py
  ```

  期望：`21 passed`。

- [x] T5-03 作品内人物组、跨作品候选与判定、合作者佐证迭代、稀有度与发行商特征及开关、聚类与一致性、`person_id`、规范姓名、别名、人物映射版本、人工纠正与待复核队列。关联 AC05、AC06、AC07。
  - 文件：`src/etl_task5/{rules,resolve,cluster,review}.py`、`tests/unit/test_task5_rules.py`、`tests/integration/test_task5_resolve.py`。
  - 完成情况：2026-10-30 通过：`tests/unit/test_task5_rules.py` 24 项、`tests/integration/test_task5_resolve.py` 24 项；全量 `625 passed`。`runner.execute_resolve` 是 T5-05 之前的串联函数，分步骤运行与 DAG 留给 T5-05。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task5_rules.py tests/integration/test_task5_resolve.py
  ```

  期望：`48 passed`。

- [x] T5-04 MoviePerson 与来源、合集归属。关联 AC08。
  - 文件：`src/etl_task5/movie_person.py`、`sql/401_task5_schema.sql`（`movie_person`、`movie_person_source`）、`tests/integration/test_task5_movie_person.py`、`tests/fixtures/task5/movie_person.py`。
  - 完成情况：2026-10-30 通过：`test_task5_movie_person.py` 11 项；`execute_resolve` 在聚类之后生成 MoviePerson，`reused` 时不重写；聚类步骤新增 `before_commit("cluster", 1)` 注入点，上一次在生成人物映射版本之后失败时重跑沿用该版本。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_movie_person.py
  ```

  期望：`11 passed`。

- [x] T5-05 分步骤运行与锁、分批恢复、报告与查询、DAG、核对命令、真实 scheduler 端到端、运行手册。关联 AC09、AC10、AC11、AC12。
  - 文件：`src/etl_task5/{runner,pipeline,report,query,check,review}.py`、`dags/amazon_movies_task5_person.py`、`scripts/seed_task5_e2e.py`、`tests/integration/test_task5_{runs,dag,report}.py`、`doc/task5/runbook.md`。
  - 完成情况：2026-10-30 通过：`test_task5_runs.py` 10 项、`test_task5_dag.py` 17 项、`test_task5_report.py` 18 项；真实 scheduler 经 `compose.e2e.yaml` 跑了成功、重跑（`reused`）、不存在映射版本（check_inputs 失败只试 1 次）与暂时错误重试，结果与步骤见 [runbook](runbook.md)，随后已回到正常配置；不在正式库触发 DAG。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_runs.py tests/integration/test_task5_dag.py tests/integration/test_task5_report.py
  ```

  期望：`45 passed`（含 7 条 Airflow 弃用警告）。

- [x] T5-06 评价工具：开发池冻结、评价表（含作品年表层 S8）与簇抽查表导出、标注导入、按层与按途径的指标。关联 AC13。
  - 文件：`src/etl_task5/evaluate.py`、`tests/integration/test_task5_evaluate.py`、`doc/task5/runbook.md`（第 7 节）。
  - 完成情况：2026-10-30 通过：`test_task5_evaluate.py` 8 项（开发池 20% 按 md5 冻结且不变、评价表不含开发池姓氏键与系统判定、层数与补足、导入与矛盾项、指标与手算一致、无预测合并时“无定义”、复核覆盖率、簇抽查）；全量 `689 passed`。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_evaluate.py
  ```

  期望：`8 passed`。

- [ ] T5-07 正式库：经 Airflow 全量运行，冻结开发池并在开发池上调整默认值、决定稀有度与发行商开关，冻结规则版本，只读核对，导出实例、查询结果与待复核队列，导出评价表与簇抽查表。关联 AC14、AC16、AC17。
  - 文件：`src/etl_task5/{rules,check}.py`（如有规则调整）、`doc/task5/design.md`（“规则调整记录”、资源实测）、README、交接文档。
  - 完成情况：未执行。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task5.check > /dev/null; echo "exit=$?"
  ```

  期望（计划）：`exit=0`，输出 JSON 的 `"problems": []`。

- [ ] T5-08 用户标注 120 对评价样本与 20 个人物，导入并报告指标。关联 AC15。
  - 文件：`outputs/task5/<抽取>/<人物映射版本>/eval/`（`eval_pairs.csv`、`cluster_sample.csv` 与 AI 预标注草稿 `*_draft.csv`；不入库）。
  - 完成情况：未执行。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task5.evaluate metrics
  ```

  期望（计划）：退出码 0，输出 `overall` 的 `tp`、`fp`、`fn`，按层与按途径的指标，`clusters.mixed`。
