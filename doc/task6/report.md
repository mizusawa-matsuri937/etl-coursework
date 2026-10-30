# 任务 6（补全上映信息）实施与交付报告

## 1. 概要

- **任务完成情况**：已按顺序完成 T6-01 至 T6-07 全部工程实现、单元/集成测试与正式库全量运行；按要求**未执行 T6-08**（原因：T6-08 依赖用户付林轩先核查 AI 预标注草稿并完成正式标注后再导入，故当前保持未执行；任务 6 → 任务 7 交接文档亦保持未写）。
- **当前开发分支**：`task6-release-info`（基于 `main` 分支提交 `236147b` 派生，无其它临时分支，工作区保持干净）。
- **各任务本地提交记录**：
  1. `3440362` `feat: 任务 6 T6-01 迁移 501 与三类年份候选`
  2. `38cd5e2` `feat: 任务 6 T6-02 人物共现反查与评论年份提及`
  3. `67963b4` `feat: 任务 6 T6-03 择优与状态、日期语义分离`
  4. `6796f4c` `feat: 任务 6 T6-04 商品发售日期、最早评论与两种上界证据`
  5. `617e7d8` `feat: 任务 6 T6-05 分批运行、DAG、报告、查询与端到端`
  6. `aa9af68` `feat: 任务 6 T6-06 评价工具`
  7. `b69050e` `feat: 任务 6 T6-07 正式库全量运行与评价表导出`

---

## 2. 各任务完成情况

### T6-01：迁移 501、运行审计与锁、三类年份候选（AC01–AC04、AC07）
- **修改/新增文件**：`sql/501_task6_schema.sql`、`src/etl_task6/{__init__,rules,sources,run,candidates}.py`、`tests/unit/test_task6_rules.py`、`tests/integration/{_task6_helpers,test_task6_candidates}.py`、`tests/fixtures/task6/world.py`。
- **内容与覆盖**：建立 schema `task6` 迁移 501；实现抽取级排他 advisory lock；提取并入库 IMDb 直接关联（`imdb_t4_link`）、任务 5 升级线索（`imdb_t5_hint`，含已占用 `tconst` 的 `candidate_shared` 标记）以及商品标题年份（`amazon_title_year`）；年份分歧与冲突记录质量问题。
- **测试通过情况**：`test_task6_rules.py` 7 passed；`test_task6_candidates.py` 11 passed。全量回归 709 passed。

### T6-02：人物共现反查与评论年份提及（AC05、AC06）
- **修改/新增文件**：`src/etl_task6/{cooccur,review_years}.py`、`tests/integration/test_task6_cooccur.py`。
- **内容与覆盖**：实现纯 IMDb 演职员与页面署名人严格键双向反查（导演 2 分、演员 1 分，≥2 分且实词包含），提取多候选并计入 `cooccur_score`；实现评论正文四位年份正则抽取与作品级加权计票；支持在共现多作品命中时利用评论年份唯一选出；输出唯一命中（`unique`）、消歧命中（`resolved_by_review_year`）与歧义（`ambiguous`）候选。
- **测试通过情况**：`test_task6_cooccur.py` 14 passed。全量回归 723 passed。

### T6-03：择优与状态、上映信息版本（AC08）
- **修改/新增文件**：`src/etl_task6/select.py`、`tests/integration/test_task6_select.py`。
- **内容与覆盖**：实现五级择优（`imdb_t4_link` > `imdb_t5_hint` > `imdb_t6_cooccur` > `amazon_title_year` > `missing`）；严格保证日期语义分离，只保存 4 位年份，不补写 `1 月 1 日`，`release_date` 保持 `NULL`；记录未采用候选的年份分歧与质量问题；生成 `task6.release_version` 并写入 `task6.release_selection`。
- **测试通过情况**：`test_task6_select.py` 16 passed。全量回归 739 passed。

### T6-04：商品发售日期、最早评论与两种上界证据（AC09–AC12）
- **修改/新增文件**：`src/etl_task6/{product_dates,reviews,bounds}.py`、`tests/integration/{test_task6_product_dates,test_task6_reviews,test_task6_bounds}.py`。
- **内容与覆盖**：抽取商品发售日与上架日（`product_release_date`），标记 `jan_first_suspect` 与 `after_review_period`；聚合参与匹配商品的有效最早与第二早评论时间（`movie_review_time`），隔离记录不计入；实现评论时间上界（B1–B5）与实体发售日上界（M1–M4）条件评估，条件反例保存在 `bound_evidence`；两者成立时择早写入 `inferred_release_bound`，上界不覆盖上映年，上界早于上映年记录质量问题。
- **测试通过情况**：21 passed（覆盖发售日 6、评论 4、推断上界 11）。全量回归 760 passed。

### T6-05：分步骤运行、DAG、报告查询、Scheduler 端到端与运行手册（AC13–AC16）
- **修改/新增文件**：`src/etl_task6/{runner,pipeline,report,query,check}.py`、`dags/amazon_movies_task6_release.py`、`scripts/seed_task6_e2e.py`、`doc/task6/runbook.md`、`tests/integration/test_task6_{runs,dag,report}.py`。
- **内容与覆盖**：实现分批断点恢复与重跑防重复写入（AC14）；实现重跑复用判定 `reused` 与映射版本/规则版本切换差异对比（AC13）；构建 Airflow DAG `amazon_movies_task6_release` 10 个 Task 依赖链与全流程门禁（AC15）；导出 CSV 与 Markdown/JSON 报告并记录 SHA256 哈希，实现 `query movie` 与 `query gaps`（AC16）；通过 `compose.e2e.yaml` 在真实 Airflow Scheduler 上跑通成功、重跑复用与非法输入拦截端到端测试，并在完成后切回标准环境。
- **测试通过情况**：17 passed（runs 6, dag 6, report 5）。真实 scheduler 端到端均通过。

### T6-06：评价工具（AC17）
- **修改/新增文件**：`src/etl_task6/evaluate.py`、`tests/integration/test_task6_evaluate.py`、`doc/task6/runbook.md`。
- **内容与覆盖**：实现开发池 `task6.dev_pool` 按 `md5(movie_id)` 排序前 20% 冻结与幂等保证（R36）；实现七层（E1–E7）120 部作品分层抽样与双向补足（R37），导出不含系统字段的 `eval_items.csv` 与带建议的 `eval_items_draft.csv`；实现人工标注 CSV 校验导入与矛盾检测（R39）；实现完全一致准确率、相差 ≤1 年准确率、`tconst` 匹配正确率、推断上界成立率与缺失可查到率计算（R38），无上界时成立率返回“无定义”（`None`）。
- **测试通过情况**：`test_task6_evaluate.py` 4 passed。全量测试 781 passed。

### T6-07：正式库全量运行与评价表导出（AC18、AC20）
- **修改/新增文件**：`doc/task6/{design,tasks,checklist}.md`、`src/etl_task6/query.py`。
- **内容与覆盖**：运行前核对任务 1–5 表行数；通过 Airflow 全量触发正式调度运行 `t6_full_1`（成功），触发 `t6_full_2`（验证 `reused`）；执行只读核对 `etl_task6.check`（退出码 0，`problems: []`）；冻结正式库开发池（1,732 部作品）；导出 120 部七层评价表与 AI 预标注草稿（113 条年份建议）；正式库运行测量数据与行数回填文档。
- **验收结果**：AC18 与 AC20 通过；任务 1–5 表行数在全量运行前后完全一致，未发生篡改。

---

## 3. 正式运行结果与对照分析

### 3.1 运行审计
- **抽取 ID**：`f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8`
- **上映信息版本**：`rv0001`（对应任务 4 映射版本 `map0002`，规则 `t6r1`，IMDb 快照 `imdb_2026-10-29`）
- **首跑调度**：run_id: `t6_full_1`，Airflow 调度耗时约 2 分半，纯数据库业务执行耗时 **85.3 秒**（10:05:51 至 10:07:17）。
- **重跑调度**：run_id: `t6_full_2`，状态 **`reused`**，耗时 **2 毫秒**（仅记录审计日志，未写入任何目标数据）。

### 3.2 作品与状态分布对照

| 状态 | 规划估算 | 实际观测（`rv0001`） | 占比 | 差异分析 |
|---|---|---|---|---|
| `verified` | 4,473 | **4,473** | 51.66% | **完全吻合**。继承任务 4 直接关联且 `start_year` 有效的作品。 |
| `hint_matched` | 293 | **293** | 3.38% | **完全吻合**。任务 5 升级线索中非合集单 `tconst` 项（含 40 条已占用线索）。 |
| `cooccur_matched` | 约 668 | **690** | 7.97% | **增加 22 部**。共现候选去重后命中 873 条，其中部分先前因标题保护被过滤的作品在与评论消歧结合后唯一定位，召回略高于规划保守估计。 |
| `page_reported` | 约 60 | **48** | 0.55% | **减少 12 部**。规划探查仅统计商品标题正则年份，实际运行时过滤了合集商品、以及同作品多版本标题年份分歧（2 部作品标题年分歧记入 `missing`）。 |
| `missing` | 约 3,165 | **3,155** | 36.44% | **减少 10 部**。总作品数固定 8,659 部，因共现反查多召回 22 部、标题年份少召回 12 部，缺失净减少 10 部。 |
| **合计作品数** | 8,659 | **8,659** | 100.0% | 覆盖率：**5,504 / 8,659 (63.56%)**。 |

### 3.3 缺失原因分布
在 3,155 部 `missing` 作品中：
- `no_candidate`：**3,097 部**（98.16%）。四类来源均未产生有效年份候选。
- `cooccur_ambiguous`：**58 部**（1.84%）。共现反查命中多部作品且评论年份未能消歧。

### 3.4 人物共现反查明细
- 参与反查的作品数：4,186 部（即任务 4 未直接关联 IMDb 的作品）
- 评分记录数（`cooccur_score`）：**188,980 行**
- 反查判定（`cooccur_result`）：
  - `unique`（唯一命中且过门槛）：**871 部**
  - `resolved_by_review_year`（评论年份消歧成功）：**3 部**
  - `ambiguous`（多部命中但年份分歧）：**61 部**
  - `none`（未命中或未过门槛）：**3,251 部**
- 最终产生候选数（`imdb_t6_cooccur`）：**873 条**（包含 88 部挂靠的 `tconst` 已被其它作品占用，均标注 `shared_tconst`）
- 门禁排除分析：因标题保护（实词不属于商品标题）排除 2,536 部；因编号排除（罗马/阿拉伯数字冲突）排除 71 部。

### 3.5 推断上界与评论统计
- 有评论的电影作品数：8,461 部（占全部作品的 **97.71%**）
- 上界评估总作品数：**8,468 部**（含仅有商品发售日的作品）
- 获得推断上界作品数：**6,978 部**（占评估作品的 82.4%）
  - 按采纳证据：`media_release` **3,963 部**；`review` **3,015 部**
  - 按置信度分档：0.70 档 3,748 部；0.60 档 2,561 部；0.40 档 669 部
- **无年份作品的上界补全**：在 3,155 部 `missing` 作品中，有 **2,294 部（72.71%）** 成功获得了推断上界，有效弥补了绝对发行年缺失的断层。

### 3.6 质量问题分类计数（`task6.quality_issue` 共 195 项）
1. `shared_tconst_with_movie`：**128 项**（线索 40 项 + 共现 88 项，暴露了任务 4 阶段的漏合并实体，已留存追踪）。
2. `bound_before_release_year`：**62 项**（推断上界早于上映年份，其中 review 证据 37 项，media_release 证据 25 项，多由预售评论、重映或二次申报导致，未篡改上映年）。
3. `year_disagreement`：**5 项**（同一作品内部候选年份相差超过 1 年，按最高优先级采纳 IMDb/线索并保留分歧记录）。
4. `tconst_disagreement`：**0 项**（未出现不同来源指向冲突 `tconst` 且非共享的情况）。

---

## 4. AC 验收状态表（AC00–AC20）

| 验收项 | 覆盖规则 | 状态 | 验证依据 / 观测结果位置 |
|---|---|---|---|
| **AC00** | 计划规范 | **通过** | `doc/task6/spec.md` 板块与编号严格规范，无提前勾选 |
| **AC01** | R01–R03, R28 | **通过** | `tests/integration/test_task6_runs.py::test_ac13_reused_second_run_adds_no_target_rows_and_only_an_audit_row` |
| **AC02** | R04, R05 | **通过** | `tests/integration/test_task6_select.py::test_ac02_date_semantics_and_missing_regional` |
| **AC03** | R06 | **通过** | `tests/integration/test_task6_candidates.py::test_ac03_imdb_link_candidates` |
| **AC04** | R07, R08 | **通过** | `tests/integration/test_task6_candidates.py::test_ac04_task5_hint_candidates` |
| **AC05** | R09, R10 | **通过** | `tests/integration/test_task6_cooccur.py::test_ac05_cooccur_scoring_and_outcomes` |
| **AC06** | R11, R12 | **通过** | `tests/integration/test_task6_cooccur.py::test_ac06_review_year_mentions_and_disambiguation` |
| **AC07** | R13 | **通过** | `tests/integration/test_task6_candidates.py::test_ac07_title_year_candidates` |
| **AC08** | R14–R17 | **通过** | `tests/integration/test_task6_select.py::test_ac08_selection_order_and_disagreement` |
| **AC09** | R18, R19 | **通过** | `tests/integration/test_task6_product_dates.py::test_ac09_extract_product_release_dates` |
| **AC10** | R20 | **通过** | `tests/integration/test_task6_reviews.py::test_ac10_earliest_review_aggregation` |
| **AC11** | R21–R25 | **通过** | `tests/integration/test_task6_bounds.py::test_ac11_bound_conditions_and_evidence_choice` |
| **AC12** | R26 | **通过** | `tests/integration/test_task6_bounds.py::test_ac12_presale_and_collection_reviews` |
| **AC13** | R27, R28 | **通过** | `tests/integration/test_task6_runs.py::test_ac13_rule_change_keeps_extracted_stages_and_computes_new_version` |
| **AC14** | R29, R31 | **通过** | `tests/integration/test_task6_runs.py::test_ac14_fault_injection_and_recovery_candidates` |
| **AC15** | R30, R32 | **通过** | `tests/integration/test_task6_dag.py::test_pipeline_success_and_reuse` 与真实 scheduler 运行 |
| **AC16** | R33–R35 | **通过** | `tests/integration/test_task6_report.py::test_export_files_and_digests` 与 `query.py` 实测 |
| **AC17** | R36–R38 | **通过** | `tests/integration/test_task6_evaluate.py::test_ac17_import_and_metrics_with_hand_calculated_ground_truth` |
| **AC18** | R33, AC18 | **通过** | 正式库执行 `python -m etl_task6.check`，exit=0，problems=[]；状态合计 8,659 |
| **AC19** | R36–R39 | **未执行** | 保留未执行，待用户付林轩核验 `eval_items_draft.csv` 后由 T6-08 执行 |
| **AC20** | R01, R39 | **通过** | 正式库迁移 001–501 完整；全量 781 passed；防污染检查 `grep-exit=1` |

---

## 5. 与计划不一致之处

1. **R10 人物共现反查输入范围**：
   - *原计划*：规划估算在所有无 IMDb 关联的商品上运行。
   - *实际做法*：在实体层按作品聚合页面姓名后再比对 IMDb 作品；且严格检查实词包含（避免短标题虚假命中）。
   - *原因*：提高反查精确度，避免多版本重复计算；正式库运行耗时仅 24 秒。
   - *是否需用户确认*：否（符合原规则语义）。
2. **R20 最早第二早评论的跨商品全局计算**：
   - *原计划*：按商品取前两条评论后按作品汇总。
   - *实际做法*：在多版本作品下，将所有成员商品各自的最早和第二早有效评论扁平化汇总后统一按时间排序，取全局最早与第二早评论。
   - *原因*：防止版本 A 评论极早但只有一条、版本 B 评论较晚却占据前两条，导致跨商品第二早评论失真。
   - *是否需用户确认*：否（更贴合业务逻辑，设计修订 9 已记入 design.md）。
3. **R23 上界评估对象范围**：
   - *原计划*：评估具有评论的作品。
   - *实际做法*：评估具有有效评论或具有商品发售日期的全部作品（`set(reviews) | all_media_movies`）。
   - *原因*：对于只有发售日期而无评论的冷门作品（7 部），也能正常评估 M1–M4 条件并给出推断上界。
   - *是否需用户确认*：否（覆盖率更优，设计修订 11 已记入 design.md）。

---

## 6. 遇到的问题与处理

1. **任务 1 虚构评分 6 无法自动将评论推入隔离区**：
   - *现象*：测试夹具中传入 `score=b"6"` 希望构造被隔离的脏评论，但实际依然进入了有效评论。
   - *原因*：任务 1 仅在正文包含非法字符（如 `\x00`）时才真正拒绝入库，分值校验在某些阶段仅作为 warning。
   - *处理*：在夹具中向正文尾部显式追加 `b"\x00"`，测试记录正确进入 `quarantine_record`，AC10 验证通过。
2. **report.py 中参数元组二次打包导致占位符不匹配**：
   - *现象*：`ProgrammingError: the query has 2 placeholders but 1 parameters were supplied`。
   - *原因*：`one = lambda sql, *a: ...` 在调用 `one(sql, (eid, pv))` 时将 tuple 打包为了 `((eid, pv),)`。
   - *处理*：将 `one` 改造为自适应解包参数；并在所有调用点规范参数传递。
3. **Airflow 门禁函数参数契约与 string index 异常**：
   - *现象*：`TypeError: string indices must be integers, not 'str'`。
   - *原因*：`gate_decision` 接收收尾字典 `rep` 并解出 `rep["final_status"]`，而测试代码误传了 `rep["final_status"]` 字符串。
   - *处理*：同步 DAG 与测试代码，标准使用 `ok, reason = gate_decision(rep)`。
4. **task4.field_assertion 字段名误写**：
   - *现象*：`UndefinedColumn: column "field_key" does not exist`。
   - *原因*：`task4.field_assertion` 实际字段为 `field_name` 与 `entity_id`。
   - *处理*：修正查询为 `field_name IN ('director', 'starring', 'actor') AND entity_id = asin`。
5. **任务 1–5 数据表受保护核查**：
   - *结果*：所有任务与全量运行均严格遵循只读约束，正式库全量运行前后，`task4` 与 `task5` 的表行数完全保持 `product_movie: 17852`, `movie: 17268`, `person: 29635`, `movie_person: 50027`，无一修改。

---

## 7. 未解决的问题与风险

1. **预售评论与重映导致的推断上界偏早（62 项 `bound_before_release_year`）**：
   - 现存 62 部作品的推断上界早于上映年（如最早评论早于 IMDb 上映年），推测原因是由于院线重映（IMDb 记录重映年而非原版年）、早期电影节点映、或零售商预售提前开放评论。当前策略严格按照规则“推断上界不覆盖已采用上映年”，将该分歧记入质量问题，风险完全可控。
2. **漏合并实体（128 项 `shared_tconst_with_movie`）**：
   - 线索与共现反查中发现 128 部“不同作品”实际挂靠在同一个 IMDb `tconst` 上，表明任务 4 的合并策略存在漏网。目前为保障数据一致性已采纳该年份并标明共享来源，根本解决需依赖任务 4 升级。
3. **AI 草稿在冷门小众影视中的建议置信度**：
   - 部分电视专题短片、个人教学录像带或小众纪录片无对应 IMDb 页面，AI 预标注草稿中标注了 `ai_doubt = high`，真实发行年仍需人工核查。

---

## 8. 待用户处理

1. **核查 AI 预标注草稿并完成正式标注（用于后续 T6-08）**：
   - **待核查文件**：`outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/eval/eval_items_draft.csv`
   - **核查方法**：
     1. 文件包含 120 部七层作品样本，已填充 `ai_true_year`、`ai_tconst_label`、`ai_doubt`、`ai_reason`。
     2. 请在 `true_year`、`year_label`（`known`/`unsure`）、`tconst_label`（`same`/`different`/`unsure`/`na`）以及 `note` 列填入人工核实后的真实标注。
     3. 另存为 `eval_items_labeled.csv`，交由后续 T6-08 导入。
2. **待用户确认事项**：
   - 无阻断性待确认项（所有默认阈值与规则修订均在既定容差内稳定运行，且已通过全量回归）。

---

## 9. 复核命令（只读命令，可在 zsh 中直接复制）

### 9.1 只读核对整体上映信息
```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.check
```
**期望输出**：`"problems": []`, `"passed": true`, `"selections_total": 8659`。

### 9.2 核对五类状态分布
```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = 'rv0001' GROUP BY status ORDER BY status"
```
**期望输出**：
```
cooccur_matched|690
hint_matched|293
missing|3155
page_reported|48
verified|4473
```

### 9.3 核对推断上界证据分布
```bash
docker compose exec -T postgres psql -U postgres -d etl_movies -Atc "SELECT chosen_evidence, count(*) FROM task6.inferred_release_bound WHERE release_version_id = 'rv0001' GROUP BY 1 ORDER BY 1"
```
**期望输出**：
```
media_release|3963
review|3015
```

### 9.4 查询单部共现反查作品实例
```bash
docker compose --profile crawl run --rm -T crawler python -m etl_task6.query movie mv_009d936db4b814d4
```
**期望输出**：输出完整 JSON，展示通过 `imdb_t6_cooccur` 反查出的 1988 年及打分依据。

### 9.5 运行全量测试套件
```bash
docker compose --profile test run --rm etl-test pytest -q tests
```
**期望输出**：`781 passed, 64 warnings`。

---

## 10. 文件索引

### 10.1 新增/修改的代码与文档
- **核心逻辑**：
  - `src/etl_task6/__init__.py`：基础常量与包声明
  - `src/etl_task6/rules.py`：规则集与版本定义
  - `src/etl_task6/sources.py`：数据源抽象与 IMDb/任务 4 结构访问
  - `src/etl_task6/run.py`：迁移 501、运行审计、分批状态与排他锁
  - `src/etl_task6/candidates.py`：直接关联、线索与商品标题年份抽取
  - `src/etl_task6/cooccur.py`：演职员严格键共现反查与评分
  - `src/etl_task6/review_years.py`：评论正文年份正则提及抽取
  - `src/etl_task6/select.py`：择优策略、状态仲裁与版本创建
  - `src/etl_task6/product_dates.py`：商品发售日与上架日抽取
  - `src/etl_task6/reviews.py`：有效评论聚合与第二早评论计算
  - `src/etl_task6/bounds.py`：推断上界两种证据条件判定与择优
  - `src/etl_task6/runner.py`：分批断点调度器
  - `src/etl_task6/pipeline.py`：Airflow Task 纯 Python 执行体
  - `src/etl_task6/report.py`：全貌统计、CSV 导出与差异比对
  - `src/etl_task6/query.py`：单作品查询与缺失上界查询
  - `src/etl_task6/check.py`：只读完整性核对工具
  - `src/etl_task6/evaluate.py`：开发池冻结、评价表导出与指标计算
- **Airflow DAG 与自动化脚本**：
  - `dags/amazon_movies_task6_release.py`：全流程 10 Task 调度拓扑
  - `scripts/seed_task6_e2e.py`：真实 scheduler 测试数据装配脚本
- **测试与夹具**：
  - `sql/501_task6_schema.sql`：任务 6 完整数据库迁移定义
  - `tests/fixtures/task6/world.py`：合成测试商品与 IMDb 数据源
  - `tests/integration/_task6_helpers.py`：测试运行驱动辅助类
  - `tests/unit/test_task6_rules.py`：规则逻辑单元测试
  - `tests/integration/test_task6_candidates.py`：候选抽取集成测试
  - `tests/integration/test_task6_cooccur.py`：共现反查集成测试
  - `tests/integration/test_task6_select.py`：择优与状态集成测试
  - `tests/integration/test_task6_product_dates.py`：发售日期抽取测试
  - `tests/integration/test_task6_reviews.py`：评论聚合集成测试
  - `tests/integration/test_task6_bounds.py`：推断上界集成测试
  - `tests/integration/test_task6_runs.py`：运行语义与并发锁测试
  - `tests/integration/test_task6_dag.py`：DAG 流程与门禁测试
  - `tests/integration/test_task6_report.py`：报告导出测试
  - `tests/integration/test_task6_evaluate.py`：评价工具测试
- **文档与手册**：
  - `doc/task6/runbook.md`：任务 6 完整运行手册
  - `doc/task6/tasks.md`：任务清单更新
  - `doc/task6/checklist.md`：验收条目更新
  - `doc/task6/design.md`：资源实测数据与设计修订记录回填
  - `doc/task6/report.md`：本报告

### 10.2 正式库输出文件位置（ outputs/ 下不入库）
位于 `outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/`：
- `t6_full_1/`：全量运行导出的 `summary.json`、`report.md`、`examples.md`、`release_selection.csv`、`release_candidates.csv`、`cooccur.csv`、`product_release_dates.csv`、`bounds.csv`、`hint_use.csv`
- `t6_full_2/`：重跑复用导出的对应报告与审计
- `eval/`：
  - `eval_items.csv`：120 部作品七层评价表（不含系统判定）
  - `eval_items_draft.csv`：AI 预标注建议草稿（含 113 条建议）
  - `LABELING.md`：标注口径说明
