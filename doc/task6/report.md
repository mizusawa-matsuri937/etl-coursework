# 任务 6（补全上映信息）实施与交付报告

## 1. 概要

- **任务完成情况**：已按顺序完成 T6-01 至 T6-07 初版实现，针对独立审查报告（[`doc/task6/review.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/review.md)）发现的 12 项问题完成 **T6-08 至 T6-11** 全部修复、回归测试与正式库清空重跑，并完成 **T6-12** 人工审核标注导入（付林轩核查非循环 AI 预标注草稿后全部采纳导入 120 部作品）、评价指标报告输出与任务 6 → 任务 7 交接文档（[`doc/handoffs/2026-10-30-task6-to-task7.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/handoffs/2026-10-30-task6-to-task7.md)）。
- **当前开发分支**：`task6-release-info`（基于 `main` 分支提交 `1625ad9` 派生，无其它临时分支，工作区保持干净）。
- **各任务本地提交记录**：
  1. `3440362` `feat: 任务 6 T6-01 迁移 501 与三类年份候选`
  2. `38cd5e2` `feat: 任务 6 T6-02 人物共现反查与评论年份提及`
  3. `67963b4` `feat: 任务 6 T6-03 择优与状态、日期语义分离`
  4. `6796f4c` `feat: 任务 6 T6-04 商品发售日期、最早评论与两种上界证据`
  5. `617e7d8` `feat: 任务 6 T6-05 分批运行、DAG、报告、查询与端到端`
  6. `aa9af68` `feat: 任务 6 T6-06 评价工具`
  7. `b69050e` `feat: 任务 6 T6-07 正式库全量运行与评价表导出`
  8. `179260a` `docs: 任务 6 独立审查与修复计划（T6-08 至 T6-12）`
  9. `2d791fe` `fix: 任务 6 T6-08 修复人物共现反查与运行阶段键`
  10. `a87e4d9` `fix: 任务 6 T6-09 修复报告统计、版本差异、只读查询、核对脚本与 DAG 弃用警告`
  11. `9c3a166` `fix: 任务 6 T6-10 修复评价分层键名、盲测导出与非循环 AI 预标注草稿`
  12. `b7e7271` `docs: 任务 6 T6-11 正式库清空重跑、重导产物并修正文档失实数据`

---

## 2. 各任务完成情况与真实测试统计

### T6-01：迁移 501、运行审计与锁、三类年份候选（AC01、AC03、AC04、AC07）
- **修改/新增文件**：`sql/501_task6_schema.sql`、`src/etl_task6/{__init__,rules,sources,run,candidates}.py`、`tests/unit/test_task6_rules.py`、`tests/integration/{_task6_helpers,test_task6_candidates}.py`、`tests/fixtures/task6/world.py`。
- **内容与覆盖**：建立 schema `task6` 迁移 501；实现抽取级排他 advisory lock；提取并入库 IMDb 直接关联（`imdb_t4_link`）、任务 5 升级线索（`imdb_t5_hint`，含已占用 `tconst` 的 `candidate_shared` 标记）以及商品标题年份（`amazon_title_year`）；年份分歧与冲突记录质量问题。
- **测试通过情况**：初版 `test_task6_rules.py` 14 passed、`test_task6_candidates.py` 13 passed（合计 27 passed，全量 718 passed；T6-08 补充 1 项单元测试后合计 28 passed）。

### T6-02：人物共现反查与评论年份提及（AC05、AC06）
- **修改/新增文件**：`src/etl_task6/{cooccur,review_years}.py`、`tests/integration/test_task6_cooccur.py`。
- **内容与覆盖**：实现 IMDb 演职员与页面署名人严格键共现反查（导演 2 分、演员 1 分，≥2 分且通过标题与续集编号条件），提取评分记录并写入 `cooccur_score`；实现评论正文按四类固定模式 `t6m1`（括号年份、`released/came out/filmed/made/shot in YYYY`、`YYYY movie/film/version/original/classic/release`、`from YYYY`）抽取四位年份，每条评论每一年份等权计 1 票（晚于评论年份的不计）；支持在共现多作品命中时利用评论年份唯一选出（票数 ≥2 且领先第 2 名 ≥2 票）；输出唯一命中（`unique`）、消歧命中（`resolved_by_review_year`）与歧义（`ambiguous`）结果。
- **测试通过情况**：初版 `test_task6_cooccur.py` 11 passed（全量 729 passed；T6-08 补充 1 项 AC21 集成测试后为 12 passed）。

### T6-03：择优与状态、上映信息版本（AC02、AC08）
- **修改/新增文件**：`src/etl_task6/select.py`、`tests/integration/test_task6_select.py`。
- **内容与覆盖**：实现固定顺序择优（`imdb_t4_link` > `imdb_t5_hint` > `imdb_t6_cooccur` > `amazon_title_year` > `missing`）；严格保证日期语义分离，只保存 4 位年份，不补写 `1 月 1 日`，`release_date` 保持 `NULL`；记录未采用候选的年份分歧（相差 1 年记 `year_diff_1`，相差 ≥2 年记 `year_disagreement` 质量问题）与 `tconst_disagreement`；生成 `task6.release_version` 并写入 `task6.release_selection`。
- **测试通过情况**：`test_task6_select.py` 10 passed（全量 739 passed）。

### T6-04：商品发售日期、最早评论与两种上界证据（AC09–AC12）
- **修改/新增文件**：`src/etl_task6/{product_dates,reviews,bounds}.py`、`tests/integration/{test_task6_product_dates,test_task6_reviews,test_task6_bounds}.py`。
- **内容与覆盖**：抽取商品发售日与上架日（`product_release_date`），标记 `jan_first_suspect` 与 `after_review_period`；聚合参与匹配商品的有效最早与第二早评论时间（`movie_review_time`），隔离记录不计入；实现评论时间上界（B1–B5）与实体发售日上界（M1–M4）条件评估，条件反例保存在 `bound_evidence`；两者成立时择早写入 `inferred_release_bound`，上界不覆盖上映年，上界早于上映年记录 `bound_before_release_year` 质量问题。
- **测试通过情况**：21 passed（`test_task6_product_dates.py` 6、`test_task6_reviews.py` 4、`test_task6_bounds.py` 11；全量 760 passed）。

### T6-05：分步骤运行、DAG、报告查询、Scheduler 端到端与运行手册（AC13–AC16）
- **修改/新增文件**：`src/etl_task6/{runner,pipeline,report,query,check}.py`、`dags/amazon_movies_task6_release.py`、`scripts/seed_task6_e2e.py`、`doc/task6/runbook.md`、`tests/integration/test_task6_{runs,dag,report}.py`。
- **内容与覆盖**：实现分批断点恢复与重跑防重复写入（AC14）；实现重跑复用判定 `reused` 与映射版本/规则版本切换差异对比（AC13）；构建 Airflow DAG `amazon_movies_task6_release` 10 个 Task 依赖链与全流程门禁（AC15）；导出 CSV 与 Markdown/JSON 报告并记录 SHA256 哈希，实现 `query movie` 与 `query gaps`（AC16）；通过 `compose.e2e.yaml` 在真实 Airflow Scheduler 上跑通成功（`t6_e2e_ok1`）、重跑复用（`t6_e2e_rerun1`）与非法输入拦截（`t6_e2e_missing1`）端到端测试，并在完成后切回标准环境。
- **测试通过情况**：初版 17 passed（`runs` 6、`dag` 6、`report` 5；全量 777 passed；T6-08 与 T6-09 补充 6 项回归测试后为 23 passed）。

### T6-06：评价工具（AC17）
- **修改/新增文件**：`src/etl_task6/evaluate.py`、`tests/integration/test_task6_evaluate.py`、`doc/task6/runbook.md`。
- **内容与覆盖**：实现开发池 `task6.dev_pool` 按 `md5(movie_id)` 排序前 20% 冻结与幂等保证（R36）；实现七层（E1–E7）120 部作品分层抽样与补足（R37），导出不含系统字段的 `eval_items.csv` 与带建议的 `eval_items_draft.csv`；实现人工标注 CSV 校验导入与矛盾检测（R39）；实现完全一致准确率、相差 ≤1 年准确率、`tconst` 匹配正确率、推断上界成立率与缺失可查到率计算（R38），无上界时成立率返回“无定义”（`None`）。
- **测试通过情况**：初版 `test_task6_evaluate.py` 4 passed（全量 781 passed；T6-10 补充 3 项 AC23 回归测试后为 7 passed）。

### T6-07：正式库初版运行与独立审查（AC18、AC20）
- **内容与发现**：完成初版正式库运行（`8ec587275f974c1489b7f903a1976a98`）；经独立审查（[`doc/task6/review.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/review.md)）发现共现反查排序与单实词别名误挂、报告 `title_types`/`candidate_shared_count` 统计失真、评价分层键名错误与 AI 草稿循环复用系统值、以及文档数字失真等 12 项问题（ISSUE-01 至 ISSUE-12），制定 T6-08 至 T6-12 修复计划。

### T6-08：修复人物共现反查与运行阶段键（AC05、AC13、AC21；修复 ISSUE-04、ISSUE-05、ISSUE-08）
- **修改文件**：`src/etl_task6/{rules,run,cooccur}.py`、`tests/unit/test_task6_rules.py`、`tests/integration/test_task6_{cooccur,runs}.py`。
- **内容与覆盖**：`cooccur._title_keys_of` 改为按 `primary → original → aka` 排序，`score_movies` 遍历寻找同时满足 `cooccur_title_ok` 与 `cooccur_number_guard` 的标题键（防止排在前面的无编号 `aka` 提前 `break` 误杀带编号续集）；`rules.cooccur_title_ok` 对单实词标题键仅允许与单实词商品标题相等匹配（消除单实词别名跨作品误挂长标题）；`run.Inputs.stage_key("cooccur")` 纳入 `RuleSet` 共现评分参数。
- **测试通过情况**：新增 3 项 AC21 测试通过，全量 784 passed。

### T6-09：修复报告统计、版本差异、只读查询、核对脚本与 DAG 弃用警告（AC13、AC15、AC16、AC22；修复 ISSUE-06、ISSUE-09、ISSUE-10、ISSUE-12）
- **修改文件**：`src/etl_task6/{report,query,check}.py`、`dags/amazon_movies_task6_release.py`、`tests/integration/test_task6_{report,runs,dag}.py`。
- **内容与覆盖**：`report.py` 修正 `source_ref->>'title_type'`、独立 `COUNT(*)` 统计 `candidate_shared_count`、校验真实 `run_id` 并按 `status = 'succeeded'` 解析最新成功运行、各阶段表查询补齐当前版本的 `stage_key`/`parser_version` 过滤、`diff_versions` 改为 `FULL OUTER JOIN` 覆盖跨版本增删作品、`tconst` 变化与上界变化；`query.py` 补齐 `product_review_time` 与 `cooccur_score` 标题匹配证据；`check.py` 改为比较 `EXTRACT(YEAR FROM b.bound_time)::int` 与 `s.release_year` 并核查当前共现阶段键下 `cooccur_score.passed = TRUE`；`dags/amazon_movies_task6_release.py` 优先从 `airflow.sdk.exceptions` 导入 `AirflowFailException`。
- **测试通过情况**：新增 5 项 AC22 测试通过，全量 789 passed。

### T6-10：修复评价分层键名、盲测导出与非循环 AI 预标注草稿（AC17、AC23；修复 ISSUE-01、ISSUE-02、ISSUE-03、ISSUE-11）
- **修改文件**：`src/etl_task6/evaluate.py`、`tests/integration/test_task6_evaluate.py`、`doc/task6/{design,tasks,checklist,runbook}.md`。
- **内容与覆盖**：`_classify_movie_stratum` 修复读取 `source_ref.get("title_type")` 与 `disagreement` 的 `year_diff_1`/`year_disagreement`/`tconst_disagreement`；`_fetch_product_details` 从 `task4.field_assertion` 读取商品原始标题（保留括号年份供 `E5` 核查）；`export_eval_set` 在七层抽样后按 `md5(movie_id)` 全局确定性打乱再顺序分配 `t6_eval_0001`–`t6_eval_0120`；`_generate_ai_draft` 彻底移除对系统年份/状态/上界的读取，仅基于盲测公开信息与 `imdb` 公开表独立比对，严禁用商品发售日、评论时间或推断上界填充 `ai_true_year`。
- **测试通过情况**：新增 3 项 AC23 测试通过，`test_task6_evaluate.py` 7 passed，全量 **792 passed, 56 warnings**（任务 1–5 共 691 项，任务 6 共 101 项）。

### T6-11：正式库清空 `task6` 重跑、重导产物并修正文档（AC18、AC20；修复 ISSUE-07）
- **修改文件**：`doc/task6/{design,report,tasks,checklist,runbook}.md`、`README.md`；导出目录 `outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/`（不入库）。
- **内容与覆盖**：清空正式库 `task6` 前后核对任务 1–5 全部 9 张主表行数不变；在全量运行前先冻结 `task6.dev_pool`（1,732 部，`13:12:56+00`）；通过 Airflow 触发 `t6_full_1`（真实 `run_id = 2a6b07ff00e54bb39fcf86234a366cbf`）完成全量运行，触发 `t6_full_2`（真实 `run_id = fb16afb798c046e599502af3a169dbdf`）验证全部阶段 `reused`；运行 `python -m etl_task6.check` 确认退出码 0、`"problems": []`；重新导出真实 `run_id` 报告与 120 部盲测评价集 `eval_items.csv`、非循环 AI 预标注草稿 `eval_items_draft.csv`；全面核对并修正文档数字与命令期望。

### T6-12：人工审核标注导入、评价指标报告与交接文档（AC19）
- **修改/新增文件**：`doc/task6/{report,tasks,checklist}.md`、`doc/handoffs/2026-10-30-task6-to-task7.md`、`README.md`；导出目录 `outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/eval/`（`eval_items_labeled.csv`、`metrics.json`、`metrics.md`；不入库）。
- **内容与覆盖**：付林轩对 120 部盲测样本的非循环 AI 预标注草稿核查通过后生成正式标注文件 `eval_items_labeled.csv`（SHA256 `39b4a22c59a0f06b6832afb0831fce4053dd5e394f57456efc2d3ee3f34abe7d`），导入 `task6.eval_label`（标注人 `付林轩`，`known` 75、`unsure` 45，矛盾数 0）；计算并导出正式评价指标；编写任务 6 → 任务 7 交接文档。

---

## 3. 正式运行结果与对照分析（T6-11 修复后重跑实测）

### 3.1 运行审计与分阶段真实耗时
- **抽取 ID**：`f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`
- **上映信息版本**：`rv0001`（对应任务 4 映射版本 `map0002`、任务 5 人物映射版本 `pmap0001`、页面解析版本 `t3p1`、规则版本 `t6r1`、IMDb 快照 `imdb_2026-10-29`）
- **开发池冻结时间**：`2026-10-30 13:12:56.708883+00`（1,732 部作品，早于首次全量运行开始时间 `13:15:35.181093+00`）
- **首次全量运行**：Airflow `run_id = t6_full_1`，数据库 `task6.run.run_id = 2a6b07ff00e54bb39fcf86234a366cbf`，状态 `succeeded`，数据库起止时间 `13:15:35.181093+00` 至 `13:16:52.919886+00`（跨度 **77.74 秒**，各阶段纯业务执行累计 **74.37 秒** + `verify` **0.70 秒**）：
  - `candidates`：**0.86 秒**（8,659 部作品，产生候选 4,922 条：`imdb_t4_link` 4,473、`imdb_t5_hint` 293、`amazon_title_year` 156）
  - `cooccur`：**38.56 秒**（4,186 部非直接关联作品，写入 `cooccur_score` 188,980 行，`passed = true` 927 行，产生 `imdb_t6_cooccur` 候选 826 条）
  - `product_dates`：**7.61 秒**（9,590 个参与匹配商品，抽取 `product_release_date` 9,558 行）
  - `review_times`：**16.30 秒**（商品层评论时间 9,578 行、作品层评论时间 8,461 行、评论年份提及 `t6m1` 8,814 行覆盖 2,765 部作品）
  - `select`：**0.96 秒**（生成 `rv0001` 与 8,659 条 `release_selection`）
  - `bounds`：**10.08 秒**（评估 8,468 部作品，生成 `inferred_release_bound` 6,978 行）
  - `verify`：**0.70 秒**（只读核对通过，`problems: []`）
- **二次复用运行**：Airflow `run_id = t6_full_2`，数据库 `task6.run.run_id = fb16afb798c046e599502af3a169dbdf`，状态 **`reused`**（`13:18:16.674042+00` 至 `13:18:16.674943+00`，耗时 **0.9 毫秒**，全部阶段复用 `2a6b07ff00e54bb39fcf86234a366cbf`，未新增或修改任何业务行）。

### 3.2 作品与状态分布对照

| 状态 | 规划估算 | 初版运行（修复前） | T6-11 修复后实测（`rv0001`） | 占比 | 差异分析 |
|---|---|---|---|---|---|
| `verified` | 4,473 | 4,473 | **4,473** | 51.66% | **完全吻合**。继承任务 4 直接关联且 `start_year` 有效的作品（`title_type` 分布：`movie` **3,863**、`tvMovie` **335**、`video` **275**）。 |
| `hint_matched` | 293 | 293 | **293** | 3.38% | **完全吻合**。任务 5 升级线索中非合集单 `tconst` 项（含未占用 253 条、已占用 `candidate_shared` **40 条**；另有 1 条 `collection_member` 不产生候选）。 |
| `cooccur_matched` | 约 668 | 690 | **641** | 7.40% | T6-08 修复单实词别名跨作品子集误匹配（ISSUE-05）剔除了 50 部假阳性作品，同时修复续集编号回退（ISSUE-04）挽回了 `mv_02f5be7f1018409b`（`Puppet Master 6`，1998），净采用 641 部（其余共现候选与更高优的线索或标题重叠）。 |
| `page_reported` | 约 60 | 48 | **50** | 0.58% | 非 `verified` 作品中有商品标题年份的共 62 部（`title_years_disagree = 0`，无同作品多版本标题年冲突），其中 **2 部被更高优先级的 `hint_matched` 覆盖、10 部被 `cooccur_matched` 覆盖**，剩余 **50 部**采用商品标题年份成为 `page_reported`。 |
| `missing` | 约 3,165 | 3,155 | **3,202** | 36.98% | 剔除单实词假阳性共现匹配后，无可靠外部年份证据的作品如实归入 `missing`（其中 2,337 部配有推断上界）。 |
| **合计作品数** | 8,659 | 8,659 | **8,659** | 100.0% | 作品覆盖率：**5,457 / 8,659 (63.02%)**；参与匹配商品覆盖率：**5,528 / 9,590 (57.64%)**；全部商品覆盖率：**5,528 / 253,059 (2.18%)**。 |

### 3.3 缺失原因分布
在 3,202 部 `missing` 作品中：
- `no_candidate`：**3,157 部**（98.59%）。四类来源均未产生有效年份候选（`hint_ambiguous` 与 `title_years_disagree` 均为 0 部）。
- `cooccur_ambiguous`：**45 部**（1.41%）。共现反查命中多部不同年份作品且评论年份提及未能唯一消歧。

### 3.4 人物共现反查与标题键来源明细
- 参与反查的作品数：**4,186 部**（任务 4 未直接关联 IMDb 的全部作品）
- 评分记录数（`cooccur_score`）：**188,980 行**（其中 `passed = true` **927 行**；因标题保护未通过 187,544 行 / 涉及 2,537 部作品；标题通过但编号条件排除 509 行 / 涉及 56 部作品）
- 标题匹配键来源分布（修复 ISSUE-04 后按 `primary → original → aka` 优先序）：
  - `passed = true`（927 行）：`primary` **645 行**、`original` **9 行**、`aka` **273 行**
  - `passed = false` 且有标题键（509 行）：`primary` **339 行**、`aka` **170 行**
- 反查结果（`cooccur_result`）：
  - `unique`（唯一命中且过门槛）：**824 部**
  - `resolved_by_review_year`（评论年份消歧成功）：**3 部**
  - `ambiguous`（多部命中但年份分歧）：**47 部**
  - `none`（未命中或未过门槛）：**3,312 部**
- 最终产生候选数（`imdb_t6_cooccur`）：**826 条**（824 + 3 = 827 部命中作品中有 1 部在 IMDb 中 `start_year IS NULL` 不产生候选；826 条候选中包含 **86 条** `shared_tconst`）。

### 3.5 推断上界与评论统计
- 有有效评论的作品数：**8,461 部**（占全部作品的 **97.71%**）
- 上界评估总作品数：**8,468 部**（含 7 部仅有商品发售日而无评论的作品）
- 获得推断上界作品数：**6,978 部**（占评估作品的 82.40%）
  - 按采纳证据：`media_release` **3,963 部**；`review` **3,015 部**
  - 按置信度分档：`0.70` 档 **3,748 部**；`0.60` 档 **2,561 部**；`0.40` 档 **669 部**
- **无年份作品的上界补全**：在 3,202 部 `missing` 作品中，有 **2,337 部（72.99%）** 获得了推断上界。
- **预售、合集与跨版本统计（R26）**：合集商品评论数（保持在商品层）**16,241 条**；多版本作品 **240 部**；最早评论早于全部 `media_release` 日期的作品 **4,713 部**（数据中无预售标记，不宣称已排除预售）。

### 3.6 质量问题分类计数（`task6.quality_issue` 共 193 项）
1. `shared_tconst_with_movie`：**126 项**（任务 5 线索 `candidate_shared` **40 项** + 共现反查 `shared_tconst` **86 项**，暴露任务 4 阶段漏合并实体，已留存追踪）。
2. `bound_before_release_year`：**62 项**（推断上界早于上映年份，其中 `review` 证据 **37 项**、`media_release` 证据 **25 项**，未覆盖或篡改上映年）。
3. `year_disagreement`：**5 项**（同一作品内部候选年份相差 ≥2 年，按最高优先级采纳并记录分歧）。
4. `tconst_disagreement`：**0 项**（不同来源候选指向不同 `tconst` 的作品为 0 部）。

### 3.7 任务 1–5 主表行数只读保护核对
正式库清空 `task6` 重跑前后及 `python -m etl_task6.check` 校验确认，任务 1–5 全部 9 张主表行数分毫不差：

| Schema.Table | 运行前行数 | 运行后行数（`check` 实测） | 状态 |
|---|---:|---:|---|
| `public.review_record` | 7,931,696 | 7,931,696 | 完全一致 |
| `public.review_staging` | 7,869,446 | 7,869,446 | 完全一致 |
| `task2.product_manifest` | 253,059 | 253,059 | 完全一致 |
| `task3.page_field` | 449,000 | 449,000 | 完全一致 |
| `task4.movie` | 17,268 | 17,268 | 完全一致 |
| `task4.product_movie` | 17,852 | 17,852 | 完全一致 |
| `task4.merge_decision` | 70,424 | 70,424 | 完全一致 |
| `task5.person` | 29,635 | 29,635 | 完全一致 |
| `task5.movie_person` | 50,027 | 50,027 | 完全一致 |

### 3.8 评价集盲测导出、人工审核标注导入与正式指标（T6-12，AC19）
- **评价集 `ev_rv0001`**：从排除开发池（1,732 部）后的评价池（6,927 部）中按七层严格抽样 **120 部**（`E1: 15, E2: 15, E3: 15, E4: 20, E5: 15, E6: 20, E7: 20`，各层候选充足，`filled: 0`）。
- **盲测防泄露**：抽样后按 `md5(movie_id)` 全局确定性打乱再顺序分配 `t6_eval_0001`–`t6_eval_0120`；`eval_items.csv` 补充任务 4 商品原始标题断言（保留括号年份），不含系统年份、状态、分层与推断上界。
- **非循环 AI 草稿与人工审核导入**：草稿 `eval_items_draft.csv` 仅基于盲测表公开字段与 `imdb` 公开表独立比对生成建议；用户 **付林轩** 于 2026-10-30 逐条审核 120 个样本后确认与草稿一致并审核通过，导出正式标注文件 `eval_items_labeled.csv`（SHA256 `39b4a22c59a0f06b6832afb0831fce4053dd5e394f57456efc2d3ee3f34abe7d`）并导入 `task6.eval_label`（标注人 `付林轩`，`known: 75`、`unsure: 45`，矛盾数 `0`）。
- **正式评价指标汇总（`metrics.json` / `metrics.md`）**：

| 指标维度 | 范围 | 有效样本数 | 命中 / 成立数 | 比率 |
|---|---|---:|---:|---:|
| **年份准确率（完全一致 / 相差 ≤1 年）** | **总体** | **75** | **75 / 75** | **100.0% / 100.0%** |
| 年份准确率（按层） | `E1` / `E2` / `E3` / `E4` / `E5` | 15 / 15 / 13 / 20 / 12 | 15 / 15 / 13 / 20 / 12 | **100.0%** |
| 年份准确率（按层） | `E6` / `E7`（系统 `missing`） | 0 / 0 | 0 / 0 | 无定义 |
| 年份准确率（按来源） | `imdb_t4_link` / `imdb_t5_hint` / `imdb_t6_cooccur` / `amazon_title_year` | 30 / 13 / 20 / 12 | 30 / 13 / 20 / 12 | **100.0%** |
| **IMDb `tconst` 匹配正确率** | **总体** | **65** | **63**（`different: 2`） | **96.9%** |
| `tconst` 正确率（按来源） | `imdb_t4_link` / `imdb_t6_cooccur` | 30 / 20 | 30 / 20 | **100.0%** |
| `tconst` 正确率（按来源） | `imdb_t5_hint` | 15 | 13（`different: 2`） | **86.7%** |
| **推断上界成立率（`true_year ≤ bound_year`）** | **总体** | **53** | **53** | **100.0%** |
| 推断上界成立率（按证据类型） | `media_release` / `review` | 40 / 13 | 40 / 13 | **100.0%** |
| **缺失作品可查到率** | `E6` + `E7`（共 40 部 `missing`） | 40 | 0（40 部均为 `unsure`） | **0.0%** |

- **标注来源与方法局限（如实说明）**：
  1. 本次标注由非循环 AI 草稿先基于盲测表公开字段（商品原始标题括号年份、页面公开演职员）与 `imdb` 公开表独立比对给出建议，付林轩审核 120 个样本后全部采纳导入（未改判）。由于 AI 草稿与系统共享同一套本地 IMDb 快照与页面标题断言，尽管草稿未读取系统输出，其证据基础仍与系统高度同源，因此已确认年份的准确率（100.0%）可能偏乐观；但独立演职员交叉校验成功揪出了 `E3`（`imdb_t5_hint`）中的 2 例异片错配（`t6_eval_0061` *Ernest Goes to Jail* 与 `t6_eval_0087` *The Red Baron*，使 `imdb_t5_hint` 的 `tconst` 正确率为 86.7%）。
  2. 40 部 `missing` 作品（`E6`、`E7`）在严格禁用 DVD/VHS 发售日、最早评论时间和推断上界作为首映年的前提下，仅凭本地页面与 IMDb 快照无法确认首映年份，故全部标为 `unsure`（缺失可查到率为 0.0%）；只有一位标注人（付林轩），无第二标注人交叉复核；样本为刻意多取难例的分层抽样，指标仅代表评价样本而非全量分布。

---

## 4. AC 验收状态表（AC00–AC23）

| 验收项 | 覆盖规则 | 状态 | 验证依据 / 观测结果位置 |
|---|---|---|---|
| **AC00** | 计划文件规范 | **待用户审批** | `doc/task6/spec.md` 五个板块顺序正确，R01–R39 与 AC00–AC23 一一对应 |
| **AC01** | R01, R02 | **通过** | `tests/integration/test_task6_candidates.py`（`-k ac01`：`3 passed, 10 deselected`） |
| **AC02** | R03–R05, R17 | **通过** | `tests/integration/test_task6_select.py`（`-k ac02`：`3 passed, 7 deselected`） |
| **AC03** | R06, R13 | **通过** | `tests/integration/test_task6_candidates.py`（`-k imdb_link`：`1 passed, 12 deselected`） |
| **AC04** | R07, R08 | **通过** | `tests/integration/test_task6_candidates.py`（`-k "hint or task4_and_task5"`：`4 passed, 9 deselected`） |
| **AC05** | R08–R10 | **通过** | `tests/integration/test_task6_cooccur.py`（`-k ac05`：`7 passed, 5 deselected`） |
| **AC06** | R10, R11 | **通过** | `tests/integration/test_task6_cooccur.py`（`-k ac06`：`4 passed, 8 deselected`） |
| **AC07** | R12, R13 | **通过** | `tests/integration/test_task6_candidates.py`（`-k title_year`：`3 passed, 10 deselected`） |
| **AC08** | R14–R16 | **通过** | `tests/unit/test_task6_rules.py` 与 `tests/integration/test_task6_select.py`（`-k "order or status or disagreement"`：`6 passed, 19 deselected`） |
| **AC09** | R18, R19 | **通过** | `tests/integration/test_task6_product_dates.py`（`6 passed`） |
| **AC10** | R20 | **通过** | `tests/integration/test_task6_reviews.py`（`4 passed`） |
| **AC11** | R21–R25 | **通过** | `tests/integration/test_task6_bounds.py`（`-k ac11`：`9 passed, 2 deselected`） |
| **AC12** | R26 | **通过** | `tests/integration/test_task6_bounds.py`（`-k ac12`：`2 passed, 9 deselected`） |
| **AC13** | R27, R28 | **通过** | `tests/integration/test_task6_runs.py`（`-k "reused or version or diff"`：`4 passed, 4 deselected`） |
| **AC14** | R29 | **通过** | `tests/integration/test_task6_runs.py`（`-k ac14`：`3 passed, 5 deselected`） |
| **AC15** | R30 | **通过** | `tests/integration/test_task6_dag.py`（`7 passed, 7 warnings`）及真实 scheduler 端到端验证 |
| **AC16** | R33–R35 | **通过** | `tests/integration/test_task6_report.py`（`8 passed`）及 `query.py` 实测 |
| **AC17** | R36–R38 | **通过** | `tests/integration/test_task6_evaluate.py`（`7 passed`） |
| **AC18** | R01–R35 | **通过** | 正式库 `2a6b07ff00e54bb39fcf86234a366cbf`（`succeeded`）与 `fb16afb798c046e599502af3a169dbdf`（`reused`）；`python -m etl_task6.check` 退出码 0、`problems: []` |
| **AC19** | R36–R39 | **通过** | 正式库 `task6.eval_label` 导入 120 条（标注人 `付林轩`，`known: 75, unsure: 45`）；导出 `metrics.json`、`metrics.md` 并完成 `doc/handoffs/2026-10-30-task6-to-task7.md` |
| **AC20** | R31, R32 | **通过** | 正式库迁移 `001,101,102,103,201,301,302,401,501`；全量 `792 passed, 56 warnings`；受限文件检查 `grep-exit=1` |
| **AC21** | R09, R10, R27, R28 | **通过** | `tests/unit/test_task6_rules.py`、`tests/integration/test_task6_{cooccur,runs}.py`（`-k ac21`：`3 passed, 32 deselected`） |
| **AC22** | R24, R27, R30, R33, R34 | **通过** | `tests/integration/test_task6_{report,runs,dag}.py`（`-k "ac22 or diff or dag"`：`12 passed, 11 deselected, 7 warnings`） |
| **AC23** | R36–R39 | **通过** | `tests/integration/test_task6_evaluate.py`（`-k "ac23 or ac17"`：`7 passed`） |

---

## 5. 与计划不一致之处及审查修复说明

1. **R09/R10 共现反查单实词别名保护与标题键优先序（T6-08，修复 ISSUE-04、ISSUE-05）**：
   - *原实现问题*：`_title_keys_of` 按字母序把 `aka` 排在最前且 `score_movies` 遇首个 `title_ok` 即 `break` 误杀续集；`cooccur_title_ok` 允许单实词别名（如 `legend`、`wallace`）跨作品子集匹配含额外核心实词的长标题。
   - *实际修复*：固定 `primary → original → aka` 排序并遍历寻找同时满足 `cooccur_title_ok` 与 `cooccur_number_guard` 的标题键；对单实词 IMDb 标题键仅允许与单实词商品标题相等匹配，保留 ≥2 个实词且字符数 ≥6 按子集匹配。
   - *是否需用户确认*：已按审查修复计划执行并通过 AC21 验证。
2. **R20 最早第二早评论的跨商品全局计算（T6-04）**：
   - *实际做法*：在多版本作品下，将所有成员商品各自的最早和第二早有效评论汇总后统一按时间排序，取全局最早与第二早评论（设计修订 9 已记入 `design.md`）。
3. **R23 上界评估对象范围（T6-04）**：
   - *实际做法*：评估具有有效评论或具有商品发售日期的全部作品（8,468 部，含 7 部仅有商品发售日而无评论的作品；设计修订 10 已记入 `design.md`）。

---

## 6. 遇到的问题与处理

1. **独立审查发现的共现反查、报告统计、评价分层与 AI 草稿循环问题（ISSUE-01 至 ISSUE-12）**：
   - *处理*：在 T6-08 至 T6-10 逐项修复代码并补充 11 项回归测试（AC21–AC23），在 T6-11 清空正式库 `task6`、先冻结开发池再通过 Airflow 重跑全量与复用验证、重新导出报告与盲测评价集，并全面订正文档。
2. **`evaluate freeze` 在 `task6.release_version` 为空时的调用顺序问题（T6-10）**：
   - *现象*：`evaluate.py:main()` 原先在判断 `args.cmd == "freeze"` 前无条件调用 `latest_rv(conn, eid)`，导致在首次全量运行前执行 `freeze` 会因尚无 `release_version` 报错。
   - *处理*：将 `args.cmd == "freeze"` 分支提前，直接调用 `apply_task6_schema(conn)` 与 `default_t4_mapping(conn, eid)` 冻结 `task6.dev_pool`，确保严格遵守“全量运行前先冻结开发池”的顺序。

---

## 7. 未解决的问题与风险

1. **预售评论与重映导致的推断上界偏早（62 项 `bound_before_release_year`）**：
   - 现存 62 部作品的推断上界早于上映年（`review` 37 项、`media_release` 25 项），多由重映版年份、早期点映或零售商预售评论所致；系统严格遵守“推断上界不覆盖已采用上映年”并记入质量问题。
2. **漏合并实体（126 项 `shared_tconst_with_movie`）**：
   - 线索（40 项）与共现反查（86 项）暴露了任务 4 未合并但指向同一 IMDb `tconst` 的作品，已标记 `shared_tconst` 留作任务 4 升级输入。
3. **冷门无外部 ID 作品的真实年份核查难度与分号连接署名**：
   - 评价集 `E6`、`E7` 的 40 部 `missing` 作品无法仅凭本地 IMDb 快照与商品标题括号确认首次公开发行年（标为 `unsure`）；部分冷门外语片在原网页 `Product details` 中使用分号 `;` 和 `and ` 连接多名导演或仅写演员姓氏（如 `B000NDIB4G`），留待后续跨任务冲突与归一化改进。

---

## 8. 交付状态说明

- **T6-01 至 T6-12 已全部完成**：付林轩已于 2026-10-30 审核通过 120 部评价样本并导入 `task6.eval_label`，正式指标报告（`metrics.json`、`metrics.md`）与任务 6 → 任务 7 交接文档（[`doc/handoffs/2026-10-30-task6-to-task7.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/handoffs/2026-10-30-task6-to-task7.md)）均已就绪，可合并 `main` 并进入任务 7。

---

## 9. 复核命令（只读命令，可在 zsh 中直接复制）

### 9.1 只读核对整体上映信息与任务 1–5 行数
```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.check
```
**期望输出**：退出码 0，JSON 中 `"problems": []`, `"passed": true`, `"selections_total": 8659`。

### 9.2 核对五类状态分布
```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = 'rv0001' GROUP BY status ORDER BY status"
```
**期望输出**：
```text
SET
cooccur_matched|641
hint_matched|293
missing|3202
page_reported|50
verified|4473
```

### 9.3 核对推断上界证据分布
```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT chosen_evidence, count(*) FROM task6.inferred_release_bound WHERE release_version_id = 'rv0001' GROUP BY 1 ORDER BY 1"
```
**期望输出**：
```text
SET
media_release|3963
review|3015
```

### 9.4 核对运行审计与开发池冻结顺序
```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SET default_transaction_read_only = on; SELECT status, run_id, release_version_id, airflow_run_id FROM task6.run ORDER BY started_at"
```
**期望输出**：
```text
SET
succeeded|2a6b07ff00e54bb39fcf86234a366cbf|rv0001|t6_full_1
reused|fb16afb798c046e599502af3a169dbdf|rv0001|t6_full_2
```

### 9.5 查询单部共现反查作品实例（含续集编号回退召回案例）
```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.query movie mv_02f5be7f1018409b
```
**期望输出**：输出完整 JSON，展示通过 `imdb_t6_cooccur` 反查出 `tt0132451`（`Puppet Master 6`，1998 年）及 `matched_title_key: "puppet master 6"`、商品层与作品层评论时间。

### 9.6 运行全量测试套件
```bash
docker compose --profile test run --rm etl-test pytest -q tests
```
**期望输出**：`792 passed, 56 warnings`。

---

## 10. 文件索引

### 10.1 新增/修改的代码与文档
- **核心逻辑**：
  - `src/etl_task6/__init__.py`：基础常量与包声明
  - `src/etl_task6/rules.py`：规则集、单实词别名保护与版本定义
  - `src/etl_task6/sources.py`：数据源抽象与 IMDb/任务 4 结构访问
  - `src/etl_task6/run.py`：迁移 501、运行审计、阶段键（含共现评分参数）、分批状态与排他锁
  - `src/etl_task6/candidates.py`：直接关联、线索与商品标题年份抽取
  - `src/etl_task6/cooccur.py`：演职员严格键共现反查、标题键优先序与续集编号完整遍历
  - `src/etl_task6/review_years.py`：评论正文年份模式 `t6m1` 抽取与计票
  - `src/etl_task6/select.py`：择优策略、状态仲裁与版本创建
  - `src/etl_task6/product_dates.py`：商品发售日与上架日抽取
  - `src/etl_task6/reviews.py`：有效评论聚合与第二早评论计算
  - `src/etl_task6/bounds.py`：推断上界两种证据条件判定与择优
  - `src/etl_task6/runner.py`：分批断点调度器
  - `src/etl_task6/pipeline.py`：Airflow Task 纯 Python 执行体
  - `src/etl_task6/report.py`：全貌统计、CSV 导出、真实 `run_id` 校验与 `FULL OUTER JOIN` 差异比对
  - `src/etl_task6/query.py`：单作品血缘查询與缺失上界查询
  - `src/etl_task6/check.py`：只读完整性核对工具
  - `src/etl_task6/evaluate.py`：开发池前置冻结、七层抽样与全局 MD5 打乱盲测导出、非循环 AI 草稿生成与指标计算
- **Airflow DAG 与自动化脚本**：
  - `dags/amazon_movies_task6_release.py`：全流程 10 Task 调度拓扑
  - `scripts/seed_task6_e2e.py`：真实 scheduler 测试数据装配脚本
- **测试与夹具（共 101 项测试）**：
  - `sql/501_task6_schema.sql`：任务 6 完整数据库迁移定义
  - `tests/fixtures/task6/world.py`：合成测试商品与 IMDb 数据源
  - `tests/integration/_task6_helpers.py`：测试运行驱动辅助类
  - `tests/unit/test_task6_rules.py`：规则逻辑单元测试（15 项）
  - `tests/integration/test_task6_candidates.py`：候选抽取集成测试（13 项）
  - `tests/integration/test_task6_cooccur.py`：共现反查集成测试（12 项）
  - `tests/integration/test_task6_select.py`：择优与状态集成测试（10 项）
  - `tests/integration/test_task6_product_dates.py`：发售日期抽取测试（6 项）
  - `tests/integration/test_task6_reviews.py`：评论聚合集成测试（4 项）
  - `tests/integration/test_task6_bounds.py`：推断上界集成测试（11 项）
  - `tests/integration/test_task6_runs.py`：运行语义与并发锁测试（8 项）
  - `tests/integration/test_task6_dag.py`：DAG 流程与门禁测试（7 项）
  - `tests/integration/test_task6_report.py`：报告导出与版本差异测试（8 项）
  - `tests/integration/test_task6_evaluate.py`：评价分层、盲测导出、非循环 AI 草稿与指标测试（7 项）
- **文档与手册**：
  - `doc/task6/spec.md`：规格说明（R01–R39、AC00–AC23）
  - `doc/task6/design.md`：设计说明、开发池观察、资源实测与审查修复记录
  - `doc/task6/tasks.md`：开发任务清单（T6-00 至 T6-12）
  - `doc/task6/checklist.md`：验收清单（AC00 至 AC23）
  - `doc/task6/runbook.md`：运行手册
  - `doc/task6/review.md`：独立审查报告（ISSUE-01 至 ISSUE-12）
  - `doc/task6/report.md`：本实施与交付报告
  - `doc/handoffs/2026-10-30-task6-to-task7.md`：任务 6 → 任务 7 交接文档

### 10.2 正式库输出文件位置（`outputs/` 下不入库）
位于 `outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/`：
- `2a6b07ff00e54bb39fcf86234a366cbf/`（及 Airflow `run_key` 目录 `t6_full_1/`、`t6_full_2/`）：全量运行导出的 `summary.json`、`report.md`、`examples.md`、`release_selection.csv`、`release_candidates.csv`、`cooccur.csv`、`product_release_dates.csv`、`bounds.csv`、`hint_use.csv`
- `eval/`：
  - `eval_items.csv`：120 部作品七层盲测评价表（`t6_eval_0001`–`t6_eval_0120` 全局打乱，保留任务 4 商品原始标题括号年份，不含系统判定）
  - `eval_items_draft.csv`：非循环 AI 预标注建议草稿（75 条独立年份建议、45 条留空待查）
  - `eval_items_labeled.csv`：付林轩审核通过的正式标注文件（SHA256 `39b4a22c59a0f06b6832afb0831fce4053dd5e394f57456efc2d3ee3f34abe7d`）
  - `metrics.json`、`metrics.md`：正式评价指标 JSON 与 Markdown 报告
  - `LABELING.md`：标注口径说明
