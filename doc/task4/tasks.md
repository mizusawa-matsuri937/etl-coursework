# 任务 4 开发任务（定义并识别同一电影）

状态：2026-10-29 编写计划，实现未开始；分支 `task4-movie-identity`（从 `main` 的 `428c205` 建立）。

命令在项目根目录直接复制运行。标注“（计划）”的命令依赖尚未实现的代码，期望值是预定口径，实现后实际运行核对再改写。每项 AC 的复核命令见 [checklist](checklist.md)。

- [x] T4-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：2026-10-29 AI 文档检查通过（五个板块、R01–R42、AC00–AC17 一一对应）；用户确认 H01–H04，同意 H05–H11 按建议执行；用户尚未审阅计划全文。

  ```bash
  grep -n "^## " doc/task4/spec.md
  ```

  期望：只有 `目标、范围、规则、AC测试、假设` 五行。

- [ ] T4-01 IMDb 快照：下载脚本、迁移 301、校验与流式装载、装载 DAG。关联 AC02、AC11。
  - 文件：`scripts/fetch_imdb.py`、`sql/301_imdb_schema.sql`、`src/etl_task4/imdb.py`、`dags/imdb_reference_task4_load.py`、`compose.yaml`（`IMDB_HOST_DIR` 只读挂载）、`.env.example`、`tests/fixtures/task4/imdb/`、`tests/integration/test_task4_imdb.py`。
  - 下载需用户在实施时再次许可；测试只用合成文件。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_imdb.py
  ```

  期望（计划）：全部通过，覆盖类型过滤、行号、`\N`、哈希不符拒绝、重复装载 `reused`。

- [ ] T4-02 迁移 302、商品版本、标准化与字段断言、合集成员拆分。关联 AC01、AC03、AC04、AC06。
  - 文件：`sql/302_task4_schema.sql`、`src/etl_task4/{normalize,editions}.py`、`tests/unit/test_task4_normalize.py`、`tests/integration/{_task4_helpers,test_task4_editions}.py`、`tests/fixtures/task4/`。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py tests/integration/test_task4_editions.py
  ```

  期望（计划）：全部通过。

- [ ] T4-03 候选、特征、得分与三档判定，合集成员关联。关联 AC05、AC06。
  - 文件：`src/etl_task4/match.py`、`tests/unit/test_task4_match.py`、`tests/integration/test_task4_match_db.py`。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_match.py tests/integration/test_task4_match_db.py
  ```

  期望（计划）：全部通过；AC05 的 10 个案例逐个参数化。

- [ ] T4-04 聚类与一致性、`movie_id`、映射版本与历史、人工纠正、版本差异。关联 AC07、AC08、AC09。
  - 文件：`src/etl_task4/cluster.py`、`src/etl_task4/review.py`、`tests/integration/test_task4_cluster.py`。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py
  ```

  期望（计划）：全部通过。

- [ ] T4-05 分批恢复与锁、报告导出、匹配 DAG、真实 scheduler 端到端。关联 AC10、AC11、AC12。
  - 文件：`src/etl_task4/{pipeline,report,check}.py`、`dags/amazon_movies_task4_match.py`、`tests/integration/test_task4_{dag,report}.py`、`doc/task4/runbook.md`（新建）。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_dag.py tests/integration/test_task4_report.py
  ```

  期望（计划）：全部通过；真实 scheduler 的三次运行步骤写入 runbook。

- [ ] T4-06 评价工具：开发池冻结、评价表与簇抽查表导出、标注导入、指标。关联 AC13。
  - 文件：`src/etl_task4/evaluate.py`、`tests/integration/test_task4_evaluate.py`。
  - 完成情况：未执行。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_evaluate.py
  ```

  期望（计划）：全部通过，含分母为零的“无定义”案例。

- [ ] T4-07 正式库：装载 IMDb 快照，在开发池上调整阈值并冻结规则版本，经 Airflow 全量匹配，只读核对，导出实例。关联 AC14、AC16、AC17。
  - 文件：`src/etl_task4/check.py`；design“资源预算”与“特征、得分与判定”写回实测；README、交接文档。
  - 完成情况：未执行。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task4.check
  ```

  期望（计划）：退出码 0，`"problems": []`，`product_edition_rows` 为 `253059`，`movie_edition_relations` 为 `8730`。

- [ ] T4-08 用户标注 120 对评价样本与 20 个簇，导入并报告指标。关联 AC15。
  - 文件：`outputs/task4/<抽取>/<映射版本>/eval/`（不入库）。
  - 由用户标注，AI 不标注；时间由用户安排。
  - 完成情况：未执行。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate export
  ```

  期望（计划）：退出码 0，`"pairs": 120`，`by_stratum` 与 design 的分层一致（不足时注明补足来源）。
