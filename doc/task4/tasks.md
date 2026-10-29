# 任务 4 开发任务（定义并识别同一电影）

状态：2026-10-29 编写计划；同日完成 T4-01 至 T4-07（全部测试 505 项通过，其中任务 1–3 的 336 项不变，任务 4 共 169 项）；T4-08 于同日由付林轩审核 AI 预标注后导入，指标 P 1.000、R 0.487、F1 0.655；分支 `task4-movie-identity`（从 `main` 的 `428c205` 建立），未提交。

命令在项目根目录直接复制运行。标注“（计划）”的命令依赖尚未实现的代码，期望值是预定口径，实现后实际运行核对再改写。每项 AC 的复核命令见 [checklist](checklist.md)。

- [x] T4-00 计划：spec、design、tasks、checklist。关联 AC00。
  - 完成情况：2026-10-29 AI 文档检查通过（五个板块、R01–R42、AC00–AC17 一一对应）；用户确认 H01–H04，同意 H05–H11 按建议执行；用户尚未审阅计划全文。

  ```bash
  grep -n "^## " doc/task4/spec.md
  ```

  期望：只有 `目标、范围、规则、AC测试、假设` 五行。

- [x] T4-01 IMDb 快照：下载脚本、迁移 301、校验与流式装载、装载 DAG。关联 AC02、AC11。
  - 文件：`scripts/fetch_imdb.py`、`sql/301_imdb_schema.sql`、`src/etl_task4/imdb.py`、`dags/imdb_reference_task4_load.py`、`compose.yaml`（`IMDB_HOST_DIR` 只读挂载）、`.env.example`、`tests/fixtures/task4/imdb/`、`tests/integration/test_task4_imdb.py`。
  - 下载需用户在实施时再次许可；`scripts/fetch_imdb.py` 只写好、未运行，需要 `--confirm`；测试只用合成文件（测试时现场生成，不提交 `.tsv.gz`）。
  - 完成情况：2026-10-29 通过。五个文件一个事务装载，哈希或大小不符在写库前拒绝；`imdb.title_other` 为 R15 新增（spec R05、AC02 已同步修订）。装载 DAG 的定义与步骤测试在 `test_task4_dag.py`（10 项，属 AC11 的一部分）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_imdb.py
  ```

  期望：`18 passed`，覆盖类型过滤、行号、`\N`、哈希不符拒绝（五个文件各一例）、重复装载 `reused`、失败回滚与并发锁。

- [x] T4-02 迁移 302、商品版本、标准化与字段断言、合集成员拆分。关联 AC01、AC03、AC04、AC06。
  - 文件：`sql/302_task4_schema.sql`、`src/etl_task4/{normalize,editions}.py`、`tests/unit/test_task4_normalize.py`、`tests/integration/{_task4_helpers,test_task4_editions}.py`、`tests/fixtures/task4/`。
  - 完成情况：2026-10-29 通过。商品版本 12 行、匹配范围 6/4/2、标题键与说明分类、姓名键与片长、合集成员拆分、断言血缘、分批恢复均有测试。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_normalize.py tests/integration/test_task4_editions.py
  ```

  期望：`39 passed`（单元 27、集成 12）。

- [x] T4-03 候选、特征、得分与三档判定，合集成员关联。关联 AC05、AC06。
  - 文件：`src/etl_task4/match.py`、`src/etl_task4/candidates.py`（新增，取数与落库）、`tests/unit/test_task4_match.py`、`tests/integration/test_task4_match_db.py`。
  - 完成情况：2026-10-29 通过。AC05 的十个案例在纯规则层与数据库层各验证一遍，每个候选恰好一条决策，合集成员单独匹配。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/unit/test_task4_match.py tests/integration/test_task4_match_db.py
  ```

  期望：`56 passed`（单元 19、集成 37）；AC05 的 10 个案例逐个参数化。

- [x] T4-04 聚类与一致性、`movie_id`、映射版本与历史、人工纠正、版本差异。关联 AC07、AC08、AC09。
  - 文件：`src/etl_task4/cluster.py`、`src/etl_task4/review.py`、`src/etl_task4/runner.py`、`src/etl_task4/run.py`（后两个新增：运行编排与公共部分）、`tests/integration/test_task4_cluster.py`。
  - 完成情况：2026-10-29 通过。一致性检查、不连带合并、稳定 `movie_id`、人工纠正（含与硬冲突矛盾的合并）、映射事件与版本差异、`reused` 均有测试；分批故障后重跑已验证（AC10 的正式测试在 T4-05）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_cluster.py
  ```

  期望：`16 passed`。

- [x] T4-05 分批恢复与锁、报告导出、匹配 DAG、核对命令、真实 scheduler 端到端、运行手册。关联 AC10、AC11、AC12。
  - 文件：`src/etl_task4/{runner,pipeline,report,check}.py`（`runner.run_stage` 新增）、`dags/amazon_movies_task4_match.py`、`scripts/seed_task4_e2e.py`、`tests/integration/test_task4_{dag,report}.py`、`doc/task4/runbook.md`。
  - 完成情况：2026-10-29 通过。两个测试文件 `31 passed`（匹配 DAG 定义与顺序、成功、重跑 `reused`、不存在快照、限定运行、暂时错误、锁被占用、三个阶段的故障恢复、报告与实例、核对）；真实 scheduler 在 `etl_test` 跑了两个 DAG 的成功、重跑、不存在快照三次，另有一次暂时错误重试（`normalize` 第 2 次成功），步骤与期望见 runbook 第 2 节。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_dag.py tests/integration/test_task4_report.py
  ```

  期望：`31 passed`。

- [x] T4-06 评价工具：开发池冻结、评价表与簇抽查表导出、标注导入、指标。关联 AC13。
  - 文件：`src/etl_task4/evaluate.py`、`tests/integration/test_task4_evaluate.py`。
  - 完成情况：2026-10-29 通过，`8 passed`：开发池按 `md5` 取前 20% 且冻结后不变；评价表不含开发池商品、不显示系统判定、各层数量与补足记录；导入与手算的 Precision、Recall、F1 一致，误合并与漏合并实例、“不确定”单独计数；无预测合并时 Precision 与 F1 为“无定义”；簇抽查导出与导入。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task4_evaluate.py
  ```

  期望：`8 passed`。

- [x] T4-07 正式库：装载 IMDb 快照，在开发池上调整权重并冻结规则版本，经 Airflow 全量匹配，只读核对，导出实例。关联 AC14、AC16、AC17。
  - 文件：`src/etl_task4/{check,match,imdb}.py`（规则 t4r2、IMDb 引用缺失的容忍）、`doc/task4/design.md`（“规则调整记录”“资源预算”实测）、README、交接文档。
  - 完成情况：2026-10-29 通过。IMDb 下载经用户授权（五个文件 1,933,753,541 字节，放仓库外 `../imdb/2026-10-29/`）；装载 287 秒（保留 1,250,408 部电影类作品）；全量匹配 t4r1 用 346 秒，开发池上调整为 t4r2（导演严格重叠的权重 3 → 4）后映射版本 `map0002`：8,730 个电影类商品 → 8,472 部作品，多商品作品 240 部，待复核 899 个（10.30%），IMDb 关联商品 4,544 个（52.05%）；`check` 退出码 0、无问题；同参数重跑为 `reused`；实例 5 + 5 组。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task4.check > /dev/null; echo "exit=$?"
  ```

  期望：`exit=0`；输出 JSON 的 `"problems": []`，`product_edition_rows` 为 `253059`，`movie_edition_relations` 为 `8730`，`tv_series_relations` 为 `0`（逐项复核命令见 checklist AC14）。

- [x] T4-08 用户标注 120 对评价样本与 20 个簇，导入并报告指标。关联 AC15。
  - 文件：`outputs/task4/<抽取>/map0002/eval/`（`eval_pairs.csv`、`cluster_sample.csv`，以及 AI 预标注草稿 `*_draft.csv`；不入库）。
  - 完成情况：2026-10-29 通过。AI 先出预标注草稿，付林轩审核后改判 4 对，导入 120 对与 20 簇；自动合并 P 1.000、R 0.487、F1 0.655（TP 38、FP 0、FN 40、TN 41，计入 119 对，unsure 1），误合并 0，簇抽查混入 0。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task4.evaluate metrics
  ```

  期望：退出码 0，`overall` 中 `tp` 38、`fp` 0、`fn` 40，`clusters.mixed` 0。
