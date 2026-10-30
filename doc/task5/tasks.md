# 任务 5 开发任务（人物姓名归一与消歧）

状态：2026-10-29 编写计划；2026-10-30 按用户要求加入作品年表反查、合作者佐证迭代、稀有度与发行商特征、待复核队列，AC 重新编号为 AC00–AC17。2026-10-30 用户审批通过计划，开始实现。分支 `task5-person-identity`（从 `main` 的 `bf5b19d` 建立）。T5-01 至 T5-08 已完成（T5-07 于 2026-10-30 在正式库经 Airflow 全量运行；T5-08 同日导入 AI 预标注、付林轩核查后全部采纳的标注，自动合并 P 1.000、R 0.706、F1 0.828）；各阶段已本地提交，尚未推送。

命令在项目根目录直接复制运行。T5-01 至 T5-07 的命令已实际运行核对（zsh）；标注“（计划）”的命令依赖尚未完成的步骤，期望值是预定口径，完成后实际运行核对再改写。每项 AC 的复核命令见 [checklist](checklist.md)。

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
  - 完成情况：2026-10-30 通过：`tests/integration/test_task5_external.py` 原 21 项；T5-07 因正式库暴露的两个缺陷增加 2 项，共 23 项（AC03 `-k credits` 15 项、AC04 `-k filmography` 9 项）。

  ```bash
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task5_external.py
  ```

  期望：`23 passed`。

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

- [x] T5-07 正式库：经 Airflow 全量运行，冻结开发池并在开发池上调整默认值、决定稀有度与发行商开关，冻结规则版本，只读核对，导出实例、查询结果与待复核队列，导出评价表与簇抽查表。关联 AC14、AC16、AC17。
  - 文件：`src/etl_task5/{external,report}.py`（两个缺陷修复与实例排序）、`tests/integration/test_task5_external.py`、`tests/fixtures/task5/links.py`、`doc/task5/design.md`（资源实测、规则调整记录、T5-07 修订）、README。
  - 完成情况：2026-10-30 通过：正式库 `t5_full_1c` 经 Airflow 8 分 47 秒成功（先后修复两个缺陷并加测试），56,809 条提及 → 29,635 个人物，核对 `exit=0`，任务 4 行数不变，重跑 `reused`，开发池观察后保持 `t5r1`。评价表、簇抽查表与待复核队列已导出，AI 草稿已生成未导入；691 项测试通过。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task5.check > /dev/null; echo "exit=$?"
  ```

  期望：`exit=0`（输出 JSON 的 `"problems": []`）。

  ```bash
  docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, airflow_run_id, person_mapping_version_id FROM task5.run ORDER BY started_at"
  ```

  期望：三行 `succeeded|t5_full_1c|pmap0001`、`reused|t5_full_2|pmap0001`、`reused|t5_full_3|pmap0001`。

  ```bash
  ls outputs/task5/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/pmap0001/eval
  ```

  期望：`LABELING.md`、`cluster_sample.csv`、`cluster_sample_draft.csv`、`eval_pairs.csv`、`eval_pairs_draft.csv`（草稿的 `ai_label` 等列由 AI 填写，`label`、`cluster_label` 空着等用户填；规则见 [runbook](runbook.md) 第 7 节）。

- [x] T5-08 导入标注（120 对评价样本与 20 个人物）并报告指标。关联 AC15。
  - 文件：`outputs/task5/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/pmap0001/eval/`（`eval_pairs_labeled.csv`、`cluster_sample_labeled.csv`、`metrics.json`、`metrics.md`；不入库）。
  - 完成情况：2026-10-30 通过。**标注来源：AI 预标注，付林轩核查后于 2026-10-30 全部采纳作为正式标注导入**（用户原话：AI 做的标签都是正确的，同意署名付林轩核查完毕）；不是用户独立逐条标注，未改判一条。正式标签即草稿的 `ai_label` / `ai_cluster_label`：评价对 same 85、different 33、unsure 2（unsure 保持 unsure，`note` 写入 AI 理由），簇 20 个全为 `same_person`；AI 存疑项（评价对 8、簇 5 个人物）一并采纳。导入 120 对与 20 个人物，无矛盾项。自动合并 Precision 1.000、Recall 0.706、F1 0.828（TP 60、FP 0、FN 25、TN 33，计入 118 对）；误合并 0；漏合并 25（S4 18、S5 5、S6 2，都是系统不按名字合并的同名或近似名）；簇抽查混入 0；人工复核覆盖率 0%、待复核率 20.4%。局限：正式标签来自 AI 预标注，AI 与系统依据相似的证据，指标可能偏乐观。
  - 导入工具无需改动：`evaluate import` 读取 `pair_id`、`label`、`note`，`clusters-import` 读取 `person_id`、`mention_key`、`cluster_label`、`outlier`、`note`，多出的列忽略；正式文件由原导出与草稿合成（列与顺序同原导出，只填 `label` / `cluster_label` 与 2 条 unsure 的 `note`）。

  ```bash
  docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT annotator, label, count(*) FROM task5.eval_label GROUP BY 1, 2 ORDER BY 2"
  ```

  期望：三行 `付林轩|different|33`、`付林轩|same|85`、`付林轩|unsure|2`。

  ```bash
  docker compose --profile crawl run --rm -T crawler python -m etl_task5.evaluate metrics 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); o=d['overall']; print(d['annotators'], d['labeled'], d['unsure']); print(o['tp'], o['fp'], o['fn'], o['tn'], round(o['precision'],3), round(o['recall'],3), round(o['f1'],3)); print(d['clusters']['labeled'], d['clusters']['mixed'])"
  ```

  期望：`['付林轩'] 120 2`；`60 0 25 33 1.0 0.706 0.828`；`20 0`。
