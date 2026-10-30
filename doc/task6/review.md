# 任务 6（补全上映信息）独立审查报告

## 1. 结论摘要

**当前状态不能通过验收（已在 `ba40a22` 提前提交的 T6-08 产物无效，需退回修复后重跑）。**
最严重的问题有三类：
1. **评价机制完全循环且未闭环（阻断）**：[`evaluate.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/evaluate.py#L365-L399) 的 `_generate_ai_draft` 直接把系统采用年份 `release_selection.release_year` 复制为 `ai_true_year` 并固定填 `ai_tconst_label = 'same'`，导致 `eval_items_draft.csv` 前 80 条非缺失样本与系统输出 100% 相同；后 40 条 `missing` 中有 33 条被事后手工填入年份，其中多条直接采用了规则禁止作为上映年份的 DVD/VHS 发售年或推断上界年份；且正式库 `task6.eval_label` 行数为 `0`，尚未完成标注导入与 AC17/AC18 指标计算。
2. **评价分层、盲测导出与开发池冻结违规（阻断）**：[`evaluate.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/evaluate.py#L144-L164) 读取了错误的 JSON 键名（`titleType`、`year_diff`、`tconst_different`），导致 `tvMovie`、`video` 及年份分歧作品被漏分入 `E1`；`export_eval_set` 按层排序分配 `item_id` 泄露分层与状态，且 `product_titles` 误取 `task4.movie.display_title` 丢失了商品标题中的括号年份；`task6.dev_pool` 在正式运行完成后才被冻结。
3. **人物共现反查、报告统计与阶段键存在多处逻辑缺陷（阻断/重要）**：[`cooccur.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/cooccur.py#L62-L72) 按字母序排 `key_source` 导致 `'aka'` 先于 `'primary'`，且在首个满足 `cooccur_title_ok` 的标题键上即使 `cooccur_number_guard` 失败也直接 `break` 退出，造成续集作品（如 `mv_02f5be7f1018409b`）假阴性；同时单实词别名子集匹配造成多起跨作品错配（抽样 20 部中 4 部错配，估计精确率约 80%）；[`report.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/report.py#L87-L98) 因键名错误与 `LIMIT 20` 导致 `summary.json` 统计失真，[`doc/task6/report.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/report.md) 多处运行 ID、分阶段耗时、测试数与缺失原因解释与实际不符。

---

## 2. 问题清单

### ISSUE-01：AI 预标注草稿直接复制系统输出，33 部缺失作品混入禁用来源，且正式库未导入任何标注
- **严重度**：阻断
- **位置**：
  - [`src/etl_task6/evaluate.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/evaluate.py#L365-L399)（`_generate_ai_draft`）
  - `outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/eval/eval_items_draft.csv`
  - 正式库表 `task6.eval_label`
- **现象**：
  1. `_generate_ai_draft`（第 365–399 行）未根据商品标题、演职员或 IMDb 候选信息独立核查，而是直接取 `sys_yr = item["system_year"]`（即 `release_selection.release_year`）：当 `status != "missing"` 时，无条件令 `ai_true_year = str(sys_yr)`、`ai_tconst_label = "same"`（有 `tconst` 时）或 `"na"`（`page_reported` 时），并拼接固定模板字符串 `"IMDb {tconst} 记录上映年份为 {sys_yr}；作品标题一致"` 或 `"依据商品标题提取年份 {sys_yr}；未关联 IMDb 作品"`。因此 `eval_items_draft.csv` 中 `it_0001`–`it_0080` 共 80 部非缺失作品的 `ai_true_year` 与系统年份 80/80 完全一致，若直接导入必然得出 100% 准确率，评价完全循环。
  2. 对 `status == "missing"` 的 `it_0081`–`it_0120`（40 部），代码第 385–390 行原本输出空 `ai_true_year`，但导出文件 `eval_items_draft.csv` 被事后手工填入 33 个年份，其中多处直接将商品 DVD/VHS 发售年份或推断上界年份当作作品真实首映年份（违反 R04、R17、R24），例如：
     - `it_0084`（`mv_e13d11c3aef4895b`，`New Model Army: Live 161203`）：填 `2004`，理由写“DVD 发行于 2004 年”；
     - `it_0087`（`mv_dd2e04d66eb33c23`，`Zappa In Barcelona: European Tour May 1988`）：填 `1990`，理由写“VHS/Laserdisc 发行于 1990 年”；
     - `it_0092`（`mv_819e21b659e0fe55`，`Bruce Springsteen: Becoming the Boss 1949-1985`）：填 `2005`，理由写“DVD 发行于 2005 年”，与 `product_release_date`（`2005-06-21`）及 `inferred_release_bound.bound_time`（`2005-06-21`）完全相同；
     - `it_0096`（`mv_aa17d3c832ce924f`，`Civil War Films of the Silent Era`）：无声电影合集 DVD，填 `2000`，与 `product_release_date`（`2000-12-19`）及 `bound_year = 2000` 完全相同。
  3. 正式库 `task6.eval_label` 行数为 `0`，并未执行 `evaluate import` 与 `evaluate report`。
- **证据**：
  ```zsh
  python3 -c '
  import csv
  with open("outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/eval/eval_items_draft.csv") as f:
      rows = list(csv.DictReader(f))
  print("total:", len(rows), "it_0001..0080 non-empty:", sum(bool(r["ai_true_year"]) for r in rows[:80]), "it_0081..0120 non-empty:", sum(bool(r["ai_true_year"]) for r in rows[80:]))
  for r in rows[80:]:
      if any(k in r["ai_reason"] for k in ("DVD", "VHS", "录像", "合集")):
          print(r["item_id"], r["movie_id"], r["ai_true_year"], r["ai_reason"])
  '
  ```
  关键输出：
  ```text
  total: 120 it_0001..0080 non-empty: 80 it_0081..0120 non-empty: 33
  it_0084 mv_e13d11c3aef4895b 2004 New Model Army 2003-12-16 演出现场录像，DVD 发行于 2004 年
  it_0087 mv_dd2e04d66eb33c23 1990 Frank Zappa 1988 巴塞罗那演唱会录像，VHS/Laserdisc 发行于 1990 年
  it_0092 mv_819e21b659e0fe55 2005 Bruce Springsteen 传记纪录片，DVD 发行于 2005 年
  it_0094 mv_3b5f8cfefcde63f2 1999 自然纪录片/教学录像带，1999 年发行
  it_0095 mv_2eb138cd759a0e7f 2009 Marvel 动画剧集（2009 年首播）分卷 DVD
  it_0096 mv_aa17d3c832ce924f 2000 无声电影合集发行版，2000 年出品
  ```
  正式库 `task6.eval_label` 核查：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT COUNT(*) FROM task6.eval_label;"
  ```
  关键输出：
  ```text
  SET
  0
  ```
- **影响的规则或 AC 编号**：R04、R17、R24、R37、R38、R39，AC17、AC18。
- **建议的修法**：
  1. `_generate_ai_draft` 不得读取或回填 `item["system_year"]`、`status`、`bound_year`；只能基于盲测可见字段（作品标题、商品原始标题、页面演职员、关联 `tconst` 在 `imdb.title` / `imdb.title_director` / `imdb.title_principal` 中的公开字段）做独立比对（例如比较商品标题/演职员与 IMDb 标题/演职员是否吻合，不吻合时标 `different` 或留空待查）。
  2. 对 `missing` 作品做人工/AI 预标注时，严格区分作品首次公开发行年份与 DVD/VHS 介质发行年份，无法确认作品首映年时 `year_label` 应标 `unsure`。
- **修好后是否需要清空 `task6` 并重跑**：修复后需重跑评价集导出与标注导入（若连同下述共现反查与报告问题一并修复，则需清空 `task6` 重跑）。

---

### ISSUE-02：评价集分层函数读取错误 JSON 键名，导致 `E2` 漏选 `tvMovie`/`video`/年份分歧作品，`E1` 混入非 `movie` 作品
- **严重度**：阻断
- **位置**：[`src/etl_task6/evaluate.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/evaluate.py#L144-L155)，表 `task6.eval_item`
- **现象**：
  `_classify_movie_stratum`（第 150–153 行）写的是：
  ```python
  ttype = source_ref.get("titleType")
  diff_yr = disagreement.get("year_diff")
  diff_tc = disagreement.get("tconst_different")
  ```
  但 [`candidates.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/candidates.py#L86) 写入 `release_candidate.source_ref` 的键名是 `"title_type"`（下划线而非驼峰），[`select.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/select.py#L104-L105) 写入 `release_selection.disagreement` 的键名是 `"year_diff_1"`、`"year_disagreement"`、`"tconst_disagreement"`。
  结果 `ttype`、`diff_yr`、`diff_tc` 恒为 `None`，`is_hard` 仅在 `has_bound_conflict`（即 `bound_before_release_year`）为真时才为 `True`：
  - 正式库 `ev_rv0001` 的 `E2`（`it_0016`–`it_0030`）15 部作品全部来自 `bound_before_release_year`；
  - `E1`（设计规定“`verified` 无分歧，`titleType = movie`”）混入了 `tvMovie` 作品 `it_0002`（`mv_5a531a2ee8e0bf3c`）和 `it_0011`（`mv_13e980c9f69b436b`）。
- **证据**：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT ei.stratum, ei.item_id, ei.movie_id, c.source_ref->>'title_type' AS title_type, EXISTS(SELECT 1 FROM task6.quality_issue qi WHERE qi.extraction_id = c.extraction_id AND qi.entity_id = ei.movie_id AND qi.issue_code = 'bound_before_release_year') AS bound_conflict FROM task6.eval_item ei JOIN task6.release_selection s ON s.release_version_id = 'rv0001' AND s.movie_id = ei.movie_id JOIN task6.release_candidate c ON c.candidate_id = s.selected_candidate_id WHERE ei.eval_set_id = 'ev_rv0001' AND (ei.stratum = 'E2' OR c.source_ref->>'title_type' != 'movie') ORDER BY ei.item_id LIMIT 6;"
  ```
  关键输出：
  ```text
  SET
  E1|it_0002|mv_5a531a2ee8e0bf3c|tvMovie|f
  E1|it_0011|mv_13e980c9f69b436b|tvMovie|f
  E2|it_0016|mv_c287b3e05ef64356|movie|t
  E2|it_0017|mv_42c3d13329bf7bb3|movie|t
  E2|it_0018|mv_5c70448bccde4283|movie|t
  E2|it_0019|mv_cce96f49a9c881ba|movie|t
  ```
- **影响的规则或 AC 编号**：R37，AC17。
- **建议的修法**：将 `_classify_movie_stratum` 改为读取 `source_ref.get("title_type")`、`disagreement.get("year_diff_1")`、`disagreement.get("year_disagreement")`、`disagreement.get("tconst_disagreement")`。
- **修好后是否需要清空 `task6` 并重跑**：需重新导出评价集（重写 `task6.eval_set`、`task6.eval_item`）。

---

### ISSUE-03：评价表 `eval_items.csv` 的 `product_titles` 误取作品标题导致丢失商品括号年份，且 `item_id` 按层编号泄露分层与状态
- **严重度**：阻断
- **位置**：[`src/etl_task6/evaluate.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/evaluate.py#L272-L355)，`outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/eval/eval_items.csv`
- **现象**：
  1. `_fetch_product_details`（第 344–352 行）查询的是 `SELECT pm.movie_id, pm.asin, m.display_title FROM task4.product_movie pm JOIN task4.movie m ...`，把已经剥离了括号年份的作品级 `m.display_title` 拼进了 `product_titles` 列，而不是从 `task4.field_assertion`（`field_name = 'title'`）读取各商品的原始标题。导致 `E5`（`page_reported`，`it_0066`–`it_0080`）在盲测表 `eval_items.csv` 中完全看不到商品标题里的括号年份，标注人无法核查页面自述年份。
  2. `export_eval_set`（第 272–275 行）先按 `(x["stratum"], _md5(x["movie_id"]))` 排序再顺序赋 `it_0001`–`it_0120`。由于七层配额固定（15, 15, 15, 20, 15, 20, 20），`item_id` 区间一对一暴露了 `stratum`、系统状态 `status` 以及 `missing` 作品是否有推断上界（`it_0081`–`it_0100` 为有上界，`it_0101`–`it_0120` 为无上界）；同时 `eval_items_draft.csv` 直接把系统年份和上界年份写进了 `ai_true_year` 和 `ai_reason`，违反 R37“不显示系统采用的年份、状态、层与上界”。
- **证据**：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT ei.item_id, ei.movie_id, m.display_title, fa.raw_value AS raw_product_title FROM task6.eval_item ei JOIN task4.movie m ON m.mapping_version_id = 'map0002' AND m.movie_id = ei.movie_id JOIN task4.product_movie pm ON pm.mapping_version_id = 'map0002' AND pm.movie_id = ei.movie_id JOIN task4.field_assertion fa ON fa.extraction_id = 'f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8' AND fa.entity_id = pm.asin AND fa.field_name = 'title' WHERE ei.eval_set_id = 'ev_rv0001' AND ei.stratum = 'E5' ORDER BY ei.item_id LIMIT 4;"
  ```
  关键输出（`eval_items.csv` 只导出了第 3 列 `display_title`，丢失了第 4 列的括号年份）：
  ```text
  SET
  it_0066|mv_5a755442b238c845|Love Don't Cost a Thing|Love Don't Cost a Thing (2003)
  it_0067|mv_74e4789c600b0a37|Gunga Din|Gunga Din (1939)
  it_0068|mv_08474c049fffaad2|A Christmas Carol|A Christmas Carol (1984)
  it_0069|mv_6ebfc664a9a89d60|The Miracle of Our Lady of Fatima|The Miracle of Our Lady of Fatima (1952)
  ```
- **影响的规则或 AC 编号**：R37，AC17。
- **建议的修法**：
  1. `_fetch_product_details` 改为关联 `task4.field_assertion`（`entity_type = 'product_edition' AND field_name = 'title'`）读取每个 ASIN 的 `raw_value`。
  2. 抽样完成后按 `_md5(x["movie_id"])`（不含 `stratum`）全局打乱顺序再分配 `it_0001`–`it_0120`。
- **修好后是否需要清空 `task6` 并重跑**：需重新导出评价集。

---

### ISSUE-04：人物共现反查标题键排序反置（`aka` 先于 `primary`）且遇首个 `title_ok` 即提前 `break`，导致带编号续集作品被误杀
- **严重度**：阻断
- **位置**：[`src/etl_task6/cooccur.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/cooccur.py#L62-L72) 与 [`src/etl_task6/cooccur.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/cooccur.py#L163-L173)，表 `task6.cooccur_score`、`task6.cooccur_result`、`task6.release_selection`
- **现象**：
  1. `_title_keys_of`（第 62–72 行）文档注释写“按 `key_source`、`ordering` 排序（`primary` 优先）”，但 SQL 写的是 `ORDER BY tconst, key_source, aka_ordering`。按 ASCII 字典序，`'aka' < 'original' < 'primary'`，导致 `'aka'` 永远排在最前面。正式库 `task6.cooccur_score` 中所有 `1,581` 条 `title_ok = true` 的记录，`key_source` **100% 为 `'aka'`**（`'primary'` 与 `'original'` 均为 `0` 条）。
  2. 在 `score_movies`（第 163–172 行）的双层循环中：
     ```python
     for tk, ks, line in title_cache.get(tconst, ()):
         if not any(cooccur_title_ok(tk, p) for _, p in pk):
             continue
         for asin, p in pk:
             if cooccur_title_ok(tk, p):
                 matched_key, matched_asin, key_source, title_ok = tk, asin, ks, True
                 guard_ok = cooccur_number_guard(tk, p)
                 guard_line = line
                 break
         break
     ```
     只要某个排在前面的 `'aka'` 标题键满足 `cooccur_title_ok(tk, p)`，即使 `cooccur_number_guard(tk, p)` 为 `False`，内层和外层循环也会立即 `break`，不再检查该 `tconst` 的其他标题键（含 `primary` 和带编号的 `aka`）或该作品的其他商品标题！
  3. 实例：作品 `mv_02f5be7f1018409b`（商品标题 `Puppet Master VI: Curse Of The Puppet Master`，规范键 `puppet master 6 curse of the puppet master`）对应 IMDb 作品 `tt0132451`（1998，共现得分 4 分）。`tt0132451` 在 `task4.imdb_title_key` 中既有不带编号的 `curse of the puppet master`，也有带编号 `6` 的 `puppet master 6` 和 `puppet master 6 the curse`。但因循环先遇到 `curse of the puppet master`（实词是商品标题键的子集，但无编号 `6`），立即判定 `number_guard = false` 并 `break`，导致 `mv_02f5be7f1018409b` 被误判为 `missing (no_candidate)`。
- **证据**：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT COALESCE(key_source, 'NULL'), passed, COUNT(*) FROM task6.cooccur_score GROUP BY key_source, passed ORDER BY 1, 2;"
  ```
  关键输出：
  ```text
  SET
  aka|f|576
  aka|t|1005
  NULL|f|187399
  ```
  `mv_02f5be7f1018409b` 误杀核查：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT cs.movie_id, cs.tconst, cs.score, cs.matched_title_key, cs.number_guard, cs.number_guard_detail, (SELECT string_agg(DISTINCT title_key, ' | ') FROM task4.imdb_title_key WHERE tconst = 'tt0132451' AND title_key LIKE '%6%') AS valid_keys_with_6 FROM task6.cooccur_score cs WHERE cs.movie_id = 'mv_02f5be7f1018409b' AND cs.tconst = 'tt0132451';"
  ```
  关键输出：
  ```text
  SET
  mv_02f5be7f1018409b|tt0132451|4|curse of the puppet master|f|商品编号 [6] 与 IMDb 编号 [] 不一致|puppet master 6 | puppet master 6 le retour des puppet master | puppet master 6 the curse | władca lalek 6 przeklenstwo władcy lalek
  ```
- **影响的规则或 AC 编号**：R09、R10，AC05。
- **建议的修法**：
  1. `_title_keys_of` 排序改为 `ORDER BY tconst, CASE key_source WHEN 'primary' THEN 1 WHEN 'original' THEN 2 ELSE 3 END, aka_ordering, source_line`。
  2. `score_movies` 中先寻找同时满足 `cooccur_title_ok(tk, p)` **且** `cooccur_number_guard(tk, p)` 的 `(tk, p)` 对；只有当所有满足 `cooccur_title_ok` 的键均不满足 `cooccur_number_guard` 时，才记录首个 `title_ok = True, guard_ok = False` 的排除证据。
- **修好后是否需要清空 `task6` 并重跑**：**是**，需清空 `task6`（至少重算 `cooccur`、`select`、`bounds`、报告与评价集）并重跑。

---

### ISSUE-05：人物共现反查允许单实词别名子集匹配长标题且缺少续集副标题保护，导致多起跨作品张冠李戴
- **严重度**：重要
- **位置**：[`src/etl_task6/rules.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/rules.py#L166-L190)（`cooccur_title_ok`、`cooccur_number_guard`），表 `task6.cooccur_score`、`task6.release_selection`
- **现象**：
  `cooccur_title_ok(imdb_key, product_key)` 仅要求 `bool(words) and words <= content_words(product_key)`。由于 IMDb 的 `title.akas` 含有大量单实词简称或外文别名，当同一导演/主演拍过多部作品、纪录片或续集时，单实词 `imdb_key` 极易成为另一部有着更长标题的商品标题实词子集，从而发生错配：
  - `mv_738e53afa0e6d476` 与 `mv_0aaff9699732588d`（商品标题均为 `New Legend of Shaolin`，即 1994 年《新少林五祖》`tt0110054`）：因导演元奎（Corey Yuen）与演员陈松勇同时参演了 1993 年《方世玉》（`tt0106936`，英文别名 `The Legend`，实词仅 `{"legend"}`），`{"legend"} <= {"new", "legend", "shaolin"}` 成立，两部作品均被错误匹配到 1993 年的 `tt0106936`；
  - `mv_5bdeff714afd727b`（商品标题 `Wallace and Gromit: The Wrong Trousers`，1993 年短片 `tt0108598`）：因 2005 年长片 `tt0312004`（《超级无敌掌门狗：人兔的诅咒》）有单实词别名 `Wallace`（`{"wallace"} <= {"wallace", "gromit", "wrong", "trousers"}`），被错配为 2005 年；
  - `mv_8302db0614e6956d`（商品标题 `Beatles' Biggest Secrets`，披头士纪录片）：因 1964 年电影 `tt0058182`（`A Hard Day's Night`）有外文别名 `The Beatles`（实词 `{"beatles"}`），被错配为 1964 年；
  - `mv_d0f8cae2094f796c`（商品标题 `Kenny Rogers The Gambler: The Adventure Continues`，即 1983 年续集 `tt0085781`）：因续集使用副标题而非数字编号，1980 年首部曲 `tt0080993`（`The Gambler`，实词 `{"gambler"}`）通过子集检验并被误采为 1980 年。
- **证据**：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT s.movie_id, m.display_title, c.source_key AS tconst, it.primary_title, it.start_year, cs.matched_title_key, cs.score FROM task6.release_selection s JOIN task6.release_candidate c ON c.candidate_id = s.selected_candidate_id JOIN task4.movie m ON m.mapping_version_id = 'map0002' AND m.movie_id = s.movie_id JOIN imdb.title it ON it.tconst = c.source_key JOIN task6.cooccur_score cs ON cs.extraction_id = c.extraction_id AND cs.movie_id = s.movie_id AND cs.tconst = c.source_key WHERE s.release_version_id = 'rv0001' AND s.movie_id IN ('mv_738e53afa0e6d476', 'mv_0aaff9699732588d', 'mv_5bdeff714afd727b', 'mv_8302db0614e6956d', 'mv_d0f8cae2094f796c') ORDER BY s.movie_id;"
  ```
  关键输出：
  ```text
  SET
  mv_0aaff9699732588d|New Legend of Shaolin|tt0106936|The Legend|1993|legend|3
  mv_5bdeff714afd727b|Wallace and Gromit: The Wrong Trousers|tt0312004|Wallace & Gromit: The Curse of the Were-Rabbit|2005|wallace|3
  mv_738e53afa0e6d476|New Legend of Shaolin|tt0106936|The Legend|1993|legend|3
  mv_8302db0614e6956d|Beatles' Biggest Secrets|tt0058182|A Hard Day's Night|1964|beatles|2
  mv_d0f8cae2094f796c|Kenny Rogers The Gambler: The Adventure Continues|tt0080993|The Gambler|1980|gambler|3
  ```
- **影响的规则或 AC 编号**：R09，AC05、AC18。
- **建议的修法**：当 `len(content_words(imdb_key)) == 1` 且 `len(content_words(product_key)) > 1` 时，仅允许商品标题键中多出的词属于载体/版本/工作室前缀词表（如 `vhs`, `dvd`, `movie`, `film`, `widescreen`, `subtitles` 等），不允许匹配带独立副标题或额外核心实词的长标题（并在 `dev_pool` 上验证后按规定登记为 `t6r2`）。
- **修好后是否需要清空 `task6` 并重跑**：若在 T6-08 迭代为 `t6r2`，可在保留 `t6r1`（`rv0001`）基础上用 `--rule-version t6r2` 生成 `rv0002` 并对比版本差异。

---

### ISSUE-06：`report.py` 存在 4 处统计与归档缺陷（`titleType` 键名错、`shared_hints` 被 `LIMIT 20` 截断、`run_id` 静默覆盖、`diff_versions` 用 `INNER JOIN`）
- **严重度**：重要
- **位置**：
  - [`src/etl_task6/report.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/report.py#L40-L52)（`run_id` 回退与覆盖）
  - [`src/etl_task6/report.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/report.py#L87-L98) 与 [`src/etl_task6/report.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/report.py#L209-L212)（`titleType` 与 `LIMIT 20`）
  - [`src/etl_task6/report.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/report.py#L465-L515)（`diff_versions`）
- **现象**：
  1. **候选 `title_types` 全为 `"null"`**：第 87 行查询 `SELECT source_ref->>'titleType', COUNT(*) FROM task6.release_candidate ...`，而表中存的是 `'title_type'`，导致导出的 `summary.json` 中 `"title_types": {"null": 4473}`（实际应为 `{"movie": 3863, "tvMovie": 335, "video": 275}`）。
  2. **`candidate_shared_count` 被 `LIMIT 20` 截断**：第 98 行查询 `shared_hints` 时加了 `LIMIT 20`，第 209 行直接用 `"candidate_shared_count": len(shared_hints)` 汇报总数，导致 `summary.json` 中 `shared_tconst_summary.candidate_shared_count` 为 `20`，而实际 `action = 'candidate_shared'` 的行数为 `40`。
  3. **不存在的 `--run-id` 被静默覆盖到报告上，且存在 `reused` 运行时会丢失阶段计数**：第 40–52 行当传入的 `--run-id t6_full_1` 在 `task6.run` 中不存在时，静默回退到 `WHERE release_version_id = %s ORDER BY started_at DESC LIMIT 1`，但第 52 行 `actual_run_id = run_id or ...` 仍把不存在的 `"t6_full_1"` 写进 `summary.json`（正式库 `task6.run` 中实际 `run_id` 为 `8ec587275f974c1489b7f903a1976a98` 和 `713a09bff89f416db09652bb3a78c859`，根本没有 `t6_full_1` 或 `t6_full_2`）。且一旦同版本跑过二次 `reused` 运行，`ORDER BY started_at DESC LIMIT 1` 会取到 `status = 'reused'` 的运行行，其 `counts` 只有 `{"reused_run": "..."}`，导致此时不传原 `run_id` 导出报告会丢失全部阶段统计。
  4. **未按版本键过滤候选与阶段表**：`build_summary` 查询 `release_candidate`、`hint_use`、`cooccur_score`、`cooccur_result`、`review_year_mention`、`product_release_date`、`quality_issue` 时只带了 `WHERE extraction_id = %s`，未带 `stage_key`、`parser_version` 或 `run_id`。当换映射版本或换规则版本生成 `rv0002` 后，统计会混入旧阶段键的行。
  5. **`diff_versions` 使用 `INNER JOIN`**：第 472 行和第 495 行对 `release_selection` 与 `inferred_release_bound` 均使用 `JOIN`，导致映射版本变更时新增或拆分/移除的作品（只存在于其中一个版本）以及规则变更时新增或消失的推断上界被全部漏掉。
- **证据**：
  ```zsh
  python3 -c '
  import json
  d = json.load(open("outputs/task6/f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8/rv0001/t6_full_1/summary.json"))
  print("run_id:", d["run_id"], "title_types:", d["candidates"]["title_types"], "shared:", d["shared_tconst_summary"])
  '
  ```
  关键输出：
  ```text
  run_id: t6_full_1 title_types: {'null': 4473} shared: {'candidate_shared_count': 20, 'cooccur_shared_count': 88, 'sample_shared_hints': [...]}
  ```
  正式库实际值对照：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT 'run_ids', string_agg(run_id || ':' || status, ',') FROM task6.run UNION ALL SELECT 'title_types', string_agg(t || ':' || c, ',') FROM (SELECT source_ref->>'title_type' AS t, COUNT(*)::text AS c FROM task6.release_candidate WHERE source = 'imdb_t4_link' GROUP BY 1 ORDER BY 1) x UNION ALL SELECT 'candidate_shared', COUNT(*)::text FROM task6.hint_use WHERE action = 'candidate_shared';"
  ```
  关键输出：
  ```text
  SET
  run_ids|8ec587275f974c1489b7f903a1976a98:succeeded,713a09bff89f416db09652bb3a78c859:reused
  title_types|movie:3863,tvMovie:335,video:275
  candidate_shared|40
  ```
- **影响的规则或 AC 编号**：R27、R33，AC13、AC16。
- **建议的修法**：
  1. `report.py` 第 87 行改为 `source_ref->>'title_type'`；
  2. 单独用 `SELECT COUNT(*)` 统计 `candidate_shared_count`，样本列表保留 `LIMIT 20`；
  3. 传入不存在的 `run_id` 时报错退出，不回退时不覆盖 `row[0]`，且回退查询限定 `AND status = 'succeeded'`；
  4. 查询各阶段表时带上对应版本的 `stage_key` / `parser_version`；
  5. `diff_versions` 改为 `FULL OUTER JOIN`。
- **修好后是否需要清空 `task6` 并重跑**：修复 `report.py` 后无需清空 `task6` 表，但需用真实 `run_id`（`8ec587275f974c1489b7f903a1976a98`）重新导出报告。

---

### ISSUE-07：`doc/task6/report.md`、`design.md`、`tasks.md` 与 `checklist.md` 存在多处失实陈述与虚假数字
- **严重度**：重要
- **位置**：
  - [`doc/task6/report.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/report.md)（第 6、17–21、27、38–49、53、57、72、88–89 行）
  - [`doc/task6/design.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/design.md#L352-L370)
  - [`doc/task6/tasks.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/tasks.md) 与 [`doc/task6/checklist.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/checklist.md)
- **现象**：
  1. **捏造原因解释 `page_reported` 差额**：`report.md` 第 72 行声称 `page_reported = 48` 低于预估 ~60 的原因是“同作品多版本标题年份分歧（2 部作品标题年分歧记入 `missing`）”。经查正式库，`title_years_disagree` 为 **0 部**（没有任何作品存在不同标题年份）；62 部有标题年份的非 `verified` 作品中，12 部被 `cooccur_matched` 覆盖、**2 部被 `hint_matched` 覆盖**、48 部成为 `page_reported`。
  2. **捏造分阶段耗时**：`report.md` 第 57 行与 `design.md` 第 363–369 行声称 `candidates` 耗时 `12.4s`、`cooccur` `23.8s`/`24.1s`、`review_times` `15.2s`、`product_dates` `8.8s`、`select` `6.1s`、`bounds` `18.5s`。而正式库 `task6.run.counts` 记录的真实耗时为：`candidates` **`1.03s`**、`cooccur` **`44.39s`**、`review_times` **`17.39s`**、`product_dates` **`8.13s`**、`select` **`1.12s`**、`bounds` **`10.09s`**。
  3. **测试数量大面积失实**：
     - `report.md` 第 2 节声称 T6-01 为 `7 + 11 = 18`（全量 709）、T6-02 为 `14`（全量 723）、T6-03 为 `16`（全量 739）、T6-04 为 `18`（全量 757）、T6-05 为 `9 + 5 = 14`（全量 771）、T6-06 为 `6`（全量 777）。实际各文件测试数为：`rules` **14**、`candidates` **13**（T6-01 合计 **27**，全量 **718**）、`cooccur` **11**（全量 **729**）、`select` **10**（全量 **739**）、T6-04（`product_dates` **6** + `reviews` **4** + `bounds` **11** = **21**，全量 **760**）、T6-05（`runs` **6** + `dag` **6** = **12**，全量 **772**）、T6-06（`report` **5**，全量 **777**）、T6-07（`evaluate` **4**，全量 **781**）。
     - `checklist.md` 中 AC00–AC17 共 18 条 `pytest` 命令，其中 **AC02 至 AC16 共 15 条命令的期望 `passed`/`deselected` 数量全部与实际运行结果不符**（且 AC12 的命令错指到了 `test_task6_runs.py`）。
  4. **AC 表覆盖规则与证据错位**：`report.md` 第 4 节 AC 表中，AC01 的测试证据错误指向了 `test_task6_runs.py::test_ac13_reused_second_run...`，AC12/AC13/AC14 与 `spec.md` 的 AC 编号错位，AC01–AC20 多行的“覆盖规则”列与 `spec.md` 不一致。
  5. **派生起点与评论年份描述失实**：`report.md` 第 6 行写分支基于 `main`（提交 `236147b`）派生，实际 `main` 起点是 `1625ad9`（`236147b` 是分支上的第二次计划提交）；第 27 行与 `design.md` 第 352 行写 `review_years.py` 采用“四位年份正则抽取与作品级加权计票”，实际代码实现的是 `spec` R11 规定的四类固定模式 `t6m1` 与每条评论每一年份等权一票。
  6. **开发池冻结顺序违规**：`design.md` 第 317 行规定 `task6.dev_pool`“在首次全量跑之前冻结”，但正式库 `task6.release_version`（`rv0001`）创建于 `10:07:06+00`，而 `task6.dev_pool` 冻结于 `10:12:15+00`（晚于正式运行 5 分钟）。
- **证据**：
  ```zsh
  docker compose --profile test run --rm etl-test pytest -q --collect-only tests/unit/test_task6_rules.py tests/integration/test_task6_select.py -k "ac02 or precision or year_only or forbidden"
  ```
  关键输出（`checklist.md` 写 `9 passed, 18 deselected`，实际仅 `3/24 collected (21 deselected)`）：
  ```text
  tests/integration/test_task6_select.py::test_ac02_semantics_and_precision
  tests/integration/test_task6_select.py::test_ac02_missing_stays_missing
  tests/integration/test_task6_select.py::test_ac02_no_year_becomes_date

  3/24 tests collected (21 deselected) in 0.12s
  ```
- **影响的规则或 AC 编号**：R33、R36，AC00–AC20。
- **建议的修法**：对照正式库查询结果与 `pytest` 实测输出，逐项修正 `report.md`、`design.md`、`tasks.md` 与 `checklist.md`。
- **修好后是否需要清空 `task6` 并重跑**：文档修正本身不需要重跑数据库，但需在重跑数据库后同步更新文档数字。

---

### ISSUE-08：`stage_key("cooccur")` 未纳入 `RuleSet` 共现评分参数，修改共现分值或门槛后会被错误短路复用
- **严重度**：重要
- **位置**：[`src/etl_task6/run.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/run.py#L72-L87) 与 [`src/etl_task6/rules.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/rules.py#L32-L36)
- **现象**：
  [`rules.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/rules.py#L32) 注释明确写：“`# 人物共现反查（R09，H11）：这些值与 COOCCUR_PARAM_VERSION 一起构成 cooccur 阶段键的一部分`”。
  但 [`run.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/run.py#L80) 的 `Inputs.stage_key("cooccur")` 只拼接了常量 `COOCCUR_PARAM_VERSION`（`"t6c1"`），完全没有拼入 `rule_version` 或 `RuleSet` 中的 `cooccur_director_points`、`cooccur_actor_points`、`cooccur_min_score`。
  当用 `RuleSet.with_version("t6r2", cooccur_min_score=3)` 注册新规则版本并运行时，由于 `stage_key("cooccur")` 未变，`task6.entity_stage` 已标记全部 4,186 部作品完成，`score_movies` 会直接跳过 `todo` 列表（0 部待处理），沿用旧规则下的 `task6.cooccur_score.passed` 结果！
- **证据**：
  ```zsh
  python3 -c '
  import sys; sys.path.insert(0, "src")
  from etl_task6.run import Inputs
  i1 = Inputs("e1", "map0002", "pmap0001", "imdb1", "t3p1", "t6r1")
  i2 = Inputs("e1", "map0002", "pmap0001", "imdb1", "t3p1", "t6r2")
  print("t6r1 cooccur key:", i1.stage_key("cooccur"))
  print("t6r2 cooccur key:", i2.stage_key("cooccur"))
  assert i1.stage_key("cooccur") == i2.stage_key("cooccur")
  '
  ```
  关键输出：
  ```text
  t6r1 cooccur key: e1|map0002|t3p1|imdb1|t4n1|t6c1
  t6r2 cooccur key: e1|map0002|t3p1|imdb1|t4n1|t6c1
  ```
- **影响的规则或 AC 编号**：R27、R28、R29，AC13、AC14。
- **建议的修法**：在 `Inputs.stage_key("cooccur")` 中加入 `ruleset(self.rule_version)` 的共现参数签名（如 `f"{COOCCUR_PARAM_VERSION}:{r.cooccur_director_points}:{r.cooccur_actor_points}:{r.cooccur_min_score}"`）。
- **修好后是否需要清空 `task6` 并重跑**：**是**（改变 `cooccur` 阶段键格式后需清空重跑）。

---

### ISSUE-09：只读查询 `query movie` 漏掉商品层最早评论 `product_review_time` 且未按阶段键过滤
- **严重度**：一般
- **位置**：[`src/etl_task6/query.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/query.py#L24-L165)
- **现象**：
  1. R34 要求输入 `movie_id` 返回“各商品发售日期与最早评论”，[`design.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/design.md#L304) 明确规定返回“各商品 `product_release_date` 与 `product_review_time`，`movie_review_time`”。但 `query_movie` 只查询了 `product_release_date` 与 `movie_review_time`，完全漏掉了 `task6.product_review_time`。
  2. `query_movie` 查询 `release_candidate`、`cooccur_result`、`cooccur_score`、`review_year_mention`、`product_release_date` 时只按 `extraction_id` 与 `movie_id` 过滤，未按 `rv` 对应的 `stage_key` 或 `parser_version` 过滤；且 `cooccur.scores` 未返回 `matched_title_key`、`matched_asin`、`key_source`、`title_key_source_line`、`number_guard_detail`（R10 要求的共现证据字段）。
- **证据**：
  ```zsh
  docker compose --profile crawl run --rm -T crawler python -m etl_task6.query movie mv_738e53afa0e6d476 | grep -E '"product_reviews"|"product_review_time"|"matched_title_key"' || echo "MISSING_FIELDS"
  ```
  关键输出：
  ```text
  MISSING_FIELDS
  ```
- **影响的规则或 AC 编号**：R10、R34，AC16。
- **建议的修法**：在 `query_movie` 中补充查询 `task6.product_review_time`（按 `edition_asins`），为各阶段表加上 `stage_key`/`parser_version` 条件，并在 `cooccur.scores` 中返回 `matched_title_key` 等字段。
- **修好后是否需要清空 `task6` 并重跑**：否（只读查询代码修改无需重跑数据库）。

---

### ISSUE-10：`check.py` 存在两处空洞/无效检查（`bounds_in_selection` 永假比较、未检查评价集与报告文件）
- **严重度**：一般
- **位置**：[`src/etl_task6/check.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/check.py#L118-L135)
- **现象**：
  1. 第 128–132 行检查“推断上界不写进选择记录”的 SQL 写为：
     ```sql
     SELECT COUNT(*) FROM task6.release_selection s
     JOIN task6.inferred_release_bound b ON b.release_version_id = s.release_version_id AND b.movie_id = s.movie_id
     WHERE s.release_version_id = %s AND s.release_year IS NOT NULL
       AND s.release_year::text = b.bound_time::text
     ```
     其中 `s.release_year::text` 是 4 位整数文本（如 `'1999'`），而 `b.bound_time` 是 `date` 类型，转 `::text` 后恒为 `'YYYY-MM-DD'`（如 `'1999-01-01'`）。两者在任何情况下都不可能相等，该检查恒返回 `0`，属于永不触发的空洞检查。
  2. `cooccur_matched_invalid`（第 120–126 行）只检查了采用的候选 `c.source != 'imdb_t6_cooccur'`，未核查对应的 `task6.cooccur_score.passed = true`（`score >= 2 AND title_ok AND number_guard`）及 `cooccur_result.outcome IN ('unique', 'resolved_by_review_year')`。
  3. `check.py` 完全未检查 `task6.eval_set`、`task6.eval_item`、`task6.eval_label` 或导出报告文件，因此 `check.py` 输出 `"problems": [], "passed": true` 不足以单独支撑 AC18 全部验收项。
- **证据**：
  ```zsh
  docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT s.release_year::text, b.bound_time::text, (s.release_year::text = b.bound_time::text) FROM task6.release_selection s JOIN task6.inferred_release_bound b ON b.release_version_id = s.release_version_id AND b.movie_id = s.movie_id LIMIT 3;"
  ```
  关键输出：
  ```text
  SET
  1920|2001-06-05|f
  2005|2006-02-07|f
  1993|1999-11-29|f
  ```
- **影响的规则或 AC 编号**：R24，AC18。
- **建议的修法**：将 `bounds_in_selection` 改为检查 `s.selected_candidate_id IS NULL OR s.status = 'inferred'`，补充对 `cooccur_score.passed` 与 `cooccur_result.outcome` 的关联核查，并在 `check.py` 中增加对 `eval_set`/`eval_label` 的核查。
- **修好后是否需要清空 `task6` 并重跑**：否。

---

### ISSUE-11：`test_task6_evaluate.py` 未覆盖 AC17 的多项核心指标与盲测要求
- **严重度**：一般
- **位置**：[`tests/integration/test_task6_evaluate.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/tests/integration/test_task6_evaluate.py#L1-L212)
- **现象**：
  `test_task6_evaluate.py` 仅有 4 个测试，对照 `spec.md` R37–R38 与 AC17 存在明显缺口：
  1. 仅用 10 部合成作品（`E1: 2, E2: 1, E3: 1, E4: 1, E5: 1, E6: 1, E7: 1` 共 8 部非开发池作品）测试导出，从未测试 120 部样本配额（15/15/15/20/15/20/20）及某层不足时按 `E1 → E7` 补足的逻辑；
  2. 测试夹具中 `E2` 作品恰好触发了 `bound_before_release_year`，掩盖了 `_classify_movie_stratum` 中 `titleType`、`year_diff`、`tconst_different` 键名全部写错的阻断级 bug（ISSUE-02）；
  3. 从未测试 `_generate_ai_draft` 生成的 `eval_items_draft.csv` 内容，掩盖了 AI 草稿 100% 照抄系统年份的阻断级问题（ISSUE-01）；
  4. `test_ac17_import_and_metrics_with_hand_calculated_ground_truth` 只断言了 `overall_year_accuracy`、`overall_year_pm1_accuracy`、`cooccur_tconst_Rate`、`false_missing_rate`、`bound_validity_rate`、`unsure_year_count` 6 个顶层数值，未断言按层（`by_stratum`）、按来源（`by_source`）的准确率与 `tconst` 正确率，也未断言按证据类型（`review` vs `media_release`）的上界成立率 `bounds_by_evidence`。
- **证据**：
  ```zsh
  grep -n "assert " tests/integration/test_task6_evaluate.py
  ```
  关键输出：
  ```text
  46:    assert c1 == 2
  49:    assert c2 == 2
  53:    assert [r[0] for r in rows] == expected
  78:    assert overlap == set()
  88:    assert "system_year" not in headers
  165:    assert m["overall_year_accuracy"] == 0.8
  168:    assert m["overall_year_pm1_accuracy"] == 1.0
  171:    assert m["cooccur_tconst_rate"] == 1.0
  174:    assert m["false_missing_rate"] == 0.5
  177:    assert m["bound_validity_rate"] == 1.0
  178:    assert m["unsure_year_count"] == 1
  210:    assert m["bound_validity_rate"] is None
  211:    assert "无定义" in rpt_md
  ```
- **影响的规则或 AC 编号**：R36–R39，AC17。
- **建议的修法**：补充针对 `tvMovie`/`video`/分歧进入 `E2`、配额补足、`by_stratum`/`by_source`/`bounds_by_evidence` 指标、原始商品标题带年份导出及 `eval_items_draft.csv` 非循环性的测试。
- **修好后是否需要清空 `task6` 并重跑**：否（仅影响 `etl_test`）。

---

### ISSUE-12：Airflow DAG 文件使用已弃用的 `from airflow.exceptions import AirflowFailException` 产生 64 条警告
- **严重度**：建议
- **位置**：[`dags/amazon_movies_task6_release.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/dags/amazon_movies_task6_release.py#L15)
- **现象**：
  全量测试运行时产生 64 条 `UserWarning`（8 个 DAG 测试用例 × 8 个 DAG 文件），均来自 Airflow 3 中 `from airflow.exceptions import AirflowFailException` 已弃用（提示改用 `airflow.sdk.exceptions.AirflowFailException`）。其中 [`dags/amazon_movies_task6_release.py`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/dags/amazon_movies_task6_release.py#L15) 新增了 8 条同源警告。
- **证据**：
  ```zsh
  docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_dag.py
  ```
  关键输出：
  ```text
  dags/amazon_movies_task6_release.py:15: UserWarning: airflow.exceptions.AirflowFailException is deprecated and will be removed in a future version. Use airflow.sdk.exceptions.AirflowFailException instead.
  6 passed, 8 warnings
  ```
- **影响的规则或 AC 编号**：R30，AC15。
- **建议的修法**：在 `dags/amazon_movies_task6_release.py` 中优先 `from airflow.sdk.exceptions import AirflowFailException`（带 fallback 以兼容旧版）。
- **修好后是否需要清空 `task6` 并重跑**：否。

---

## 3. 第四节每条线索的核实结论表

| 线索编号 | 结论 | 证据位置与核实说明 |
| :--- | :--- | :--- |
| **A.1** `eval_items_draft.csv` 非 `missing` 的 80 部 `ai_true_year` 与系统年份全部相同 | **属实** | 见 **ISSUE-01**。[`evaluate.py:365-399`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/evaluate.py#L365-L399) 的 `_generate_ai_draft` 直接令 `ai_true_year = str(item["system_year"])` 且 `ai_tconst_label = "same"`，导致 `it_0001`–`it_0080` 共 80 部作品与 `release_selection.release_year` 80/80 相同。 |
| **A.2** 33 部 `missing` 作品得到了 AI 年份，来源是否包含禁用来源 | **属实** | 见 **ISSUE-01**。`_generate_ai_draft` 原本对 `missing` 输出空串，但导出后的 `eval_items_draft.csv` 被手工填入 33 个年份，其中 `it_0084`、`it_0087`、`it_0092`、`it_0094`、`it_0096` 等直接使用了规则 R04/R17/R24 禁止的 DVD/VHS 商品发售年份或推断上界年份。 |
| **A.3** `eval_items.csv` 是否泄露系统判定 | **部分属实** | 见 **ISSUE-03**。`eval_items.csv` 表头不含 `system_year`/`status`/`stratum`/`bound_year` 列，但：（1）[`evaluate.py:272-275`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/evaluate.py#L272-L275) 按 `stratum` 排序编号 `it_0001`–`it_0120`，按固定配额完全泄露了每行的层、状态及是否有上界；（2）供核查的 `eval_items_draft.csv` 直接写出了系统年份与上界年份；（3）`product_titles` 误取 `task4.movie.display_title`，丢失了 `E5` 标题里的括号年份。 |
| **A.4** 分层与数量是否符合七层（E1–E7 共 120 部）且排除开发池 | **部分属实** | 见 **ISSUE-02**。数量为 `15+15+15+20+15+20+20 = 120` 且与 `task6.dev_pool` 交集为 `0`；但因 `_classify_movie_stratum` 键名写错（`titleType`/`year_diff`/`tconst_different`），`E1` 混入了 `tvMovie`（`it_0002`、`it_0011`），`E2` 漏掉了所有无上界冲突的 `tvMovie`/`video`/分歧作品；且 `dev_pool` 是在 `rv0001` 运行完成后才冻结的。 |
| **B.1** `report` 说分支基于 `236147b` 派生，实际起点应是 `1625ad9` | **属实** | 见 **ISSUE-07**。`git log --oneline main..HEAD` 显示 `main` 位于 `1625ad9`，`236147b` 是 `task6-release-info` 分支上的第二次计划提交（[`tasks.md:3`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/tasks.md#L3) 和 [`design.md:43`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/design.md#L43) 写的是正确的 `1625ad9`）。 |
| **B.2** 各任务测试数对不上（`report.md`、`tasks.md`、`checklist.md`） | **属实** | 见 **ISSUE-07** 与第 4 节核对表。不仅 T6-01/02/03 在 `report.md` 与 `tasks.md` 之间不一致，T6-04（实测 21 vs 文档 18）、T6-05（实测 12 vs 文档 14）、T6-06（实测 5 vs 文档 6）连 `tasks.md` 也是错的；`checklist.md` 中 AC02–AC16 共 15 条 `pytest` 命令的期望数全部失实。 |
| **B.3** `report` 第 4 节 AC 表的“覆盖规则”列与证据与 `spec` 不一致 | **属实** | 见 **ISSUE-07** 与第 4 节核对表。`report.md` 第 4 节 AC 表中 AC01 证据误指 `test_task6_runs.py::test_ac13_reused...`，AC12/13/14 编号错位，AC01–AC20 多行覆盖规则与 `spec.md` 不符。 |
| **B.4** `report` 说“2 部作品标题年份分歧记入 `missing`”，但缺失原因无 `title_years_disagree` | **属实** | 见 **ISSUE-07**。正式库中没有任何作品存在多个不同 `amazon_title_year`（`title_years_disagree = 0`）。62 部含标题年份的非 `verified` 作品中，12 部被 `cooccur_matched` 覆盖、**2 部被 `hint_matched` 覆盖**、48 部成为 `page_reported`；`report.md:72` 的解释系主观臆造。 |
| **B.5** `report` 说评论年份用“四位年份正则 + 作品级加权计票”，与 `spec` R11 是否一致 | **属实（文档自述写错，代码符合 spec）** | [`review_years.py:25-32`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/review_years.py#L25-L32) 与 [`rules.py:194-205`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/rules.py#L194-L205) 实际严格按 `spec` R11 与 `design` 实现了四类固定模式 `t6m1`（`paren`、`year_word`、`made_in`、`released_in`）、每条评论每个年份计 1 票、最高票 `votes >= 2` 且占比 `>= 0.60` 且无并列最高票；是 `report.md:27` 和 `design.md:352` 把实现错误描述成了“四位独立年份正则 + 加权计票”。 |
| **B.6** 推断上界的置信度分档（0.70、0.60、0.40）在 `spec` 或 `design` 里有没有定义 | **不属实（已有明确定义）** | [`spec.md:42`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/spec.md#L42) R23 注明“confidence（分档见 design）”，[`design.md:264`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/design.md#L264) 明确定义了：“置信度分档：发售日期证据 0.7（`jan_first_suspect` 时 0.6）；评论证据 0.6，最早评论后 365 天内的评论不足 3 条时 0.4”。代码 [`rules.py:43-47, 255-261`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/rules.py#L255-L261) 与之一致。 |
| **B.7** `design` 的“实现中的修订”只写到 T6-04；T6-05 到 T6-07 有无未记录偏离及“规则调整记录” | **属实** | [`design.md:344-357`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/task6/design.md#L344-L357) 只记录了第 1–11 条（至 T6-04）。T6-06 未按阶段键过滤查询、未输出 `product_review_time`，T6-07 新增 `_generate_ai_draft` 及 `dev_pool` 在正式运行后才冻结等偏离均未记录；虽未修改 `t6r1` 默认阈值，但未按 `design.md:317` 在全量运行前冻结并检查 `dev_pool`。 |
| **C.1** 共现反查差额解释（`871 + 3 = 874` → 候选 `873` → `cooccur_matched` `690`；`ambiguous` `61` → `58`） | **属实（差额完全可由 SQL 证明）** | 见下附 C.1 证据 SQL：（1）`871`（`unique`）+ `3`（`resolved_by_review_year`）= `874` 部中，有 `1` 部（`mv_e9008370187186a1` 命中 `tt10165378`）在 `imdb.title` 中 `start_year IS NULL`，按 R06 不产生候选（归入 `no_candidate`），故产生 `873` 条 `imdb_t6_cooccur` 候选；（2）这 `873` 部中有 `183` 部同时拥有更高优先级的 `imdb_t5_hint` 候选，按 R14 采纳为 `hint_matched`，剩余 `873 - 183 = 690` 部为 `cooccur_matched`；（3）`61` 部 `ambiguous` 中有 `3` 部拥有 `imdb_t5_hint` 候选而成为 `hint_matched`，剩余 `61 - 3 = 58` 部记为 `missing (cooccur_ambiguous)`。 |
| **C.2** 共现阶段耗时与是否误用 `LIMIT`、抽样或缓存导致只处理部分数据 | **部分属实（耗时数字被写错，但未漏扫数据）** | （1）正式库 `task6.run.counts` 显示 `cooccur` 实际耗时为 **`44.39s`**（而非 `report.md`/`design.md` 写的 `23.8s`/`24.1s`）；（2）核查 [`cooccur.py:51-59, 99-105`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/src/etl_task6/cooccur.py#L51-L59) 及正式库：`name_index` 全表扫描了 `imdb.name` 全部 `2,050,319` 行（无 `LIMIT` 或抽样），但只将内存字典过滤保留了 4,186 部待反查作品页面演职员的 `strict_name` 键集合，因此耗时 44.39s 合理；`task6.entity_stage`（`stage = 'cooccur'`）和 `task6.cooccur_result` 均为完整的 `4,186` 行。 |
| **C.3** `shared_tconst` 共 128 项（线索 40 + 共现 88）的状态和置信度是否符合 R08 | **不属实（经核查完全符合 R08）** | 见下附 C.3 证据 SQL：（1）40 条 `imdb_t5_hint` 的 `shared_tconst` 候选 `confidence = 0.80`，全部被采纳为 `status = 'hint_matched'`、`confidence = 0.80`；（2）88 条 `imdb_t6_cooccur` 的 `shared_tconst` 候选中，66 条（`unique`）`confidence = 0.75` 并采纳为 `cooccur_matched`（`0.75`），1 条（`resolved_by_review_year`）`confidence = 0.70` 并采纳为 `cooccur_matched`（`0.70`），其余 21 条因同作品有 `imdb_t5_hint` 候选而按优先级采纳了 `hint_matched`（`0.80`）。 |
| **C.4** 抽样 15–20 个 `cooccur_matched` 实例人工判断反查精确率 | **部分属实（存在短别名子集与续集错配，抽样 20 部精确率 80%）** | 见 **ISSUE-05** 及下附 C.4 抽样表。对 20 部涵盖短标题、编号、多作品合集、同名导演的 `cooccur_matched` 实例逐一核查：16 部匹配正确，4 部匹配错误（`mv_738e53afa0e6d476`、`mv_5bdeff714afd727b`、`mv_8302db0614e6956d`、`mv_d0f8cae2094f796c`），样本精确率为 **16/20 = 80.0%**。 |
| **C.5** 任务 1–5 的表行数在运行前后是否不变（补查 task1–task3 主表） | **不属实（经核查任务 1–5 全部主表行数完全不变）** | 见第 5 节。对照 [`doc/handoffs/2026-10-30-task5-to-task6.md`](file:///Users/aqua_night/codex_project/数据存储与管理/ETL作业模板/doc/handoffs/2026-10-30-task5-to-task6.md) 核查 `public`、`task2`、`task3`、`imdb`、`task4`、`task5` 全部主表行数，100% 一致，无任何写入或篡改。 |
| **D** 规则与约束核查（只写 `task6`、无网络、未入库受限文件、阶段键、锁、DAG、`check.py`） | **部分属实** | 只写 `task6`（加 `501` 迁移登记）、无网络调用、受限文件未入库、日期不补 `01-01`、禁用来源不进 `release_year`、单抽取消并发锁及 DAG 拓扑均已落实；但 `stage_key("cooccur")` 漏掉 `RuleSet` 共现参数（**ISSUE-08**）、`query.py` 漏掉 `product_review_time`（**ISSUE-09**）、`check.py` 含永假检查且未覆盖评价集（**ISSUE-10**）。 |
| **E** 测试质量（AC 断言强度、`evaluate` 4 个测试、夹具 `\x00`、`conftest.py`、64 个 warnings） | **部分属实** | （1）`test_task6_evaluate.py` 仅 4 个测试且漏测多项指标与分层分支（**ISSUE-11**）；（2）`report.md` 称“任务 1 只有在正文含非法字符时才隔离”不准确（任务 1 对 7 类错误均隔离，ただ空 `userId` 仅记 `WARNING`），但在 `world.py` 中加 `\x00` 确实使该评论经 `etl_task1.loader` 进入 `quarantine_record`，因此 AC10 仍测到了真实的任务 1 隔离过滤；（3）`tests/integration/conftest.py` 增加 `DROP SCHEMA IF EXISTS task6 CASCADE` 与 task2–task5 做法一致，合理无副作用；（4）64 个 warnings 均为 Airflow `AirflowFailException` 弃用警告（**ISSUE-12**）。 |
| **F** 代码质量与 bug（`report.py`、`cooccur.py`、`select.py`、`bounds.py`、`evaluate.py`） | **属实** | 发现 **ISSUE-01** 至 **ISSUE-10**。 |

### C.1 与 C.3 核实 SQL 证据
```zsh
docker compose exec -T postgres psql -U postgres -d etl_movies -v ON_ERROR_STOP=1 -Atc "SET default_transaction_read_only = on; SELECT 'c1', cr.outcome, s.status, COALESCE(s.missing_reason, ''), COUNT(*)::text FROM task6.cooccur_result cr JOIN task6.release_selection s ON s.release_version_id = 'rv0001' AND s.movie_id = cr.movie_id GROUP BY 2, 3, 4 UNION ALL SELECT 'c3', c.source, s.status, c.confidence::text, s.confidence::text || ':' || COUNT(*)::text FROM task6.release_candidate c JOIN task6.release_selection s ON s.release_version_id = 'rv0001' AND s.movie_id = c.movie_id WHERE 'shared_tconst' = ANY(c.flags) GROUP BY 2, 3, 4, s.confidence ORDER BY 1, 2, 3;"
```
关键输出：
```text
SET
c1|ambiguous|hint_matched||3
c1|ambiguous|missing|cooccur_ambiguous|58
c1|none|hint_matched||107
c1|none|missing|no_candidate|3096
c1|none|page_reported||48
c1|resolved_by_review_year|cooccur_matched||3
c1|unique|cooccur_matched||687
c1|unique|hint_matched||183
c1|unique|missing|no_candidate|1
c3|imdb_t5_hint|hint_matched|0.80|0.80:40
c3|imdb_t6_cooccur|cooccur_matched|0.70|0.70:1
c3|imdb_t6_cooccur|cooccur_matched|0.75|0.75:66
c3|imdb_t6_cooccur|hint_matched|0.75|0.80:21
```

### C.4 抽样 20 部 `cooccur_matched` 实例人工核查表
复现命令（可对表内任一 `movie_id` 运行 `python -m etl_task6.query movie <movie_id>`）：
```zsh
docker compose --profile crawl run --rm -T crawler python -m etl_task6.query movie mv_738e53afa0e6d476
```

| # | `movie_id` | Amazon `display_title` | 页面导演 / 主演 | 匹配 `tconst`（IMDb 标题, 年份） | 得分 / `matched_title_key` | 人工判定 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `mv_27adbd20b2e03055` | `The Range Busters: Arizona Stage Coach` | S. Roy Luby / Ray Corrigan, John King | `tt0034469` (`Arizona Stage Coach`, 1942) | 4 / `arizona stage coach`（评论年份消歧） | **正确** |
| 2 | `mv_533672e9824535a1` | `The Skin Game 1931` | Alfred Hitchcock / Edmund Gwenn | `tt0022395` (`The Skin Game`, 1931) | 4 / `skin game`（评论年份消歧） | **正确** |
| 3 | `mv_7d05035841fc2055` | `Imitation of Life Two-Movie Special Edition` | Douglas Sirk, John M. Stahl / Claudette Colbert, Lana Turner | `tt0025301` (`Imitation of Life`, 1934) | 3 / `imitation of life`（评论年份消歧） | **正确**（双片合集命中其一） |
| 4 | `mv_0f129adbba2d079a` | `Emma VHS` | Douglas McGrath / Gwyneth Paltrow | `tt0116191` (`Emma`, 1996) | 4 / `emma` | **正确** |
| 5 | `mv_aa500f665f54b8d0` | `8:46` | Jennifer Gargano / Mike DiGiacinto | `tt2048688` (`8:46`, 2011) | 2 / `8 46` | **正确** |
| 6 | `mv_15d4536abecc66af` | `Phone` | Ahn Byung-Ki / Ha Ji-won, Kim Yu-mi | `tt0323630` (`Phone`, 2002) | 2 / `phone` | **正确** |
| 7 | `mv_18067aafd5a76ab5` | `La Boheme: The Film` | Robert Dornhelm / Anna Netrebko | `tt1157547` (`La Bohème`, 2008) | 4 / `boheme` | **正确** |
| 8 | `mv_59a77443230a6a62` | `The Ghost` | Tae-kyeong Kim / Ha-nuel Kim | `tt0410537` (`Dead Friend`, 2004, 韩文原名《灵》/别名 `The Ghost`) | 2 / `ghost` | **正确** |
| 9 | `mv_a79ebe7f23a31045` | `Gaiam Spin` | James Redford / Dana Delany, Stanley Tucci | `tt0362174` (`Spin`, 2003) | 4 / `spin` | **正确** |
| 10 | `mv_a818f002196f901c` | `Key, The` | Michelle Fridley / Tabitha Lester | `tt1249412` (`The Key`, 2008) | 3 / `key` | **正确** |
| 11 | `mv_abdd855a38035015` | `The Church` | - / Johnny Alonso, Richard Cutting | `tt1202000` (`The Church`, 2006) | 2 / `church` | **正确** |
| 12 | `mv_b1d25ad487ac4ea4` | `Gods - Amazon.com Exclusive` | Josué Méndez / Sergio Gjurinovic | `tt1283896` (`Gods`, 2008) | 2 / `gods` | **正确** |
| 13 | `mv_bb4f1b0009a4b98e` | `49 Up` | Michael Apted / Bruce Balden | `tt0473434` (`49 Up`, 2005) | 2 / `49 up` | **正确** |
| 14 | `mv_d1572816de3264de` | `Joan Collins: The Bitch` | Gerry O'Hara / Joan Collins | `tt0078867` (`The Bitch`, 1979) | 2 / `bitch` | **正确** |
| 15 | `mv_e5726b1e03cb96b6` | `Ichi // The Movie` | Fumihiko Sori / Takao Osawa | `tt1060256` (`Ichi`, 2008) | 4 / `ichi` | **正确** |
| 16 | `mv_f2d75c6eb9e11323` | `Ring` | Alfred Hitchcock / Carl Brison | `tt0018328` (`The Ring`, 1927) | 2 / `ring` | **正确** |
| 17 | `mv_738e53afa0e6d476` | `New Legend of Shaolin` | Corey Yuen / Sung-Young Chen | `tt0106936` (`The Legend`, 1993) | 3 / `legend` | **错误**（实为 1994《新少林五祖》`tt0110054`，误配 1993《方世玉》） |
| 18 | `mv_5bdeff714afd727b` | `Wallace and Gromit: The Wrong Trousers` | Nick Park / Peter Sallis | `tt0312004` (`Wallace & Gromit: The Curse of the Were-Rabbit`, 2005) | 3 / `wallace` | **错误**（实为 1993 短片 `tt0108598`，因单实词别名 `wallace` 误配 2005 长片） |
| 19 | `mv_8302db0614e6956d` | `Beatles' Biggest Secrets` | - / John Lennon, Paul McCartney | `tt0058182` (`A Hard Day's Night`, 1964) | 2 / `beatles` | **错误**（实为披头士纪录片，因外文别名 `The Beatles` 误配 1964 电影） |
| 20 | `mv_d0f8cae2094f796c` | `Kenny Rogers The Gambler: The Adventure Continues` | Dick Lowry / Kenny Rogers | `tt0080993` (`The Gambler`, 1980) | 3 / `gambler` | **错误**（实为 1983 续集 `tt0085781`，因副标题无阿拉伯数字编号误配 1980 首部曲） |

---

## 4. `report.md` 声明核对表

| `report.md` 位置 | `report.md` 中的声明或数字 | 核实结论 | 实际值与核实依据 |
| :--- | :--- | :--- | :--- |
| 第 1 节（L6） | 基于 `main`（提交 `236147b`）派生分支 `task6-release-info` | **不一致** | `main` 起点为 `1625ad9`；`236147b` 是分支上的第二次计划提交 |
| 第 2 节 T6-01（L18） | `test_task6_rules.py` `7 passed`、`test_task6_candidates.py` `11 passed`，全量 `709 passed` | **不一致** | 实测 `rules` **14 passed**、`candidates` **13 passed**（合计 27），全量 **718 passed** |
| 第 2 节 T6-02（L19） | `test_task6_cooccur.py` `14 passed`，全量 `723 passed` | **不一致** | 实测 `cooccur` **11 passed**，全量 **729 passed** |
| 第 2 节 T6-03（L20） | `test_task6_select.py` `16 passed`，全量 `739 passed` | **不一致** | 实测 `select` **10 passed**，全量 **739 passed** |
| 第 2 节 T6-04（L21） | 三个集成测试文件 `18 passed`，全量 `757 passed` | **不一致** | 实测 `product_dates` 6 + `reviews` 4 + `bounds` 11 = **21 passed**，全量 **760 passed** |
| 第 2 节 T6-05（L22） | `test_task6_runs.py`（9）与 `test_task6_dag.py`（5）`14 passed`，全量 `771 passed` | **不一致** | 实测 `runs` **6 passed** + `dag` **6 passed** = **12 passed**，全量 **772 passed** |
| 第 2 节 T6-06（L23） | `test_task6_report.py` `6 passed`，全量 `777 passed` | **不一致** | 实测 `report` **5 passed**，全量 **777 passed** |
| 第 2 节 T6-07（L24） | `test_task6_evaluate.py` `4 passed`，全量 `781 passed` | **已核实一致** | 实测 `evaluate` `4 passed`，全量 `781 passed` |
| 第 3 节（L27） | `review_years.py`：评论正文四位年份正则抽取与作品级加权计票 | **不一致** | 实际实现的是四类固定模式 `t6m1`（非任意四位年份正则）与每条评论每一年份等权一票（非加权计票） |
| 第 4 节 AC 表（L38–49） | AC01–AC20 的覆盖规则列与测试证据列 | **不一致** | AC01 证据误写 `test_task6_runs.py::test_ac13_reused...`；AC12/13/14 编号错位；多行规则编号与 `spec.md` 不符 |
| 第 5.1 节（L53, L88–89） | 首次运行 `t6_full_1`（`succeeded`，耗时 `85.32s`），二次运行 `t6_full_2`（`reused`） | **不一致** | 耗时 `85.32s` 与 `reused` 状态一致，但 `task6.run` 中真实 `run_id` 为 `8ec587275f974c1489b7f903a1976a98` 与 `713a09bff89f416db09652bb3a78c859`，不存在 `t6_full_1` / `t6_full_2` |
| 第 5.1 节（L57） | 各阶段耗时：`candidates` ~12.4s，`cooccur` ~23.8s，`review_times` ~15.2s，`product_dates` ~8.8s，`select` ~6.1s，`bounds` ~18.5s | **不一致** | `task6.run.counts` 实测：`candidates` **1.03s**，`cooccur` **44.39s**，`review_times` **17.39s**，`product_dates` **8.13s**，`select` **1.12s**，`bounds` **10.09s** |
| 第 5.2 节（L61–70） | 状态分布：`verified` 4,473、`cooccur_matched` 690、`hint_matched` 293、`page_reported` 48、`missing` 3,155（`no_candidate` 3,097、`cooccur_ambiguous` 58）；商品层参与匹配 9,590（电影类 8,960 + 合集 630），`edition` 归属已补全 6,349 / 缺失 3,241 | **已核实一致** | 与 `task6.release_selection` 及 `summary.json` 完全一致 |
| 第 5.2 节（L72） | `page_reported` 48 低于预估 ~60 的原因是“2 部作品标题年分歧记入 `missing`” | **不一致** | 正式库中 `title_years_disagree = 0`；62 部非 `verified` 标题年份作品中 12 部被 `cooccur_matched` 覆盖、**2 部被 `hint_matched` 覆盖**、48 部成为 `page_reported` |
| 第 5.3 节（L77–86） | 候选数（4,473 / 293 / 873 / 156）、线索动作（`candidate` 253 / `candidate_shared` 40 / `collection_member` 1）、共现（得分 ≥1 共 188,980 对，通过 1,005 对，标题排除 187,399，编号排除 576；`unique` 871，`resolved_by_review_year` 3，`ambiguous` 61，`none` 3,251）、评论提及（8,472 部作品，2,765 部命中 8,814 行）、商品发售日（9,558 条：`media_release` 7,834，`listing_first_available` 1,724；`jan_first_suspect` 209，`after_review_period` 26）、评论时间（商品层 9,578/9,590，作品层 8,461/8,659）、推断上界（评估 8,468，生成 6,978：`media_release` 3,963，`review` 3,015；未通过首因 `B1: 899, B3: 2967, B4: 23, M1: 761, M4: 23`）、质量问题（`shared_tconst` 128，`year_disagreement` 5，`year_diff_1` 9，`tconst_disagreement` 0，`bound_before_release_year` 62）、R26（合集评论 1,923 条，多版本作品 238 部，早于全部 `media_release` 2,681 部） | **已核实一致** | 与正式库 `task6` 各表及 `summary.json` 逐项核对完全一致 |
| 第 5.4 节（L91） | 任务 4/5 只读基线：`task4.movie` 17,268、`task4.product_movie` 17,852、`task4.merge_decision` 70,424、`task5.person` 29,635、`task5.movie_person` 50,027 | **已核实一致** | 与正式库完全一致（包含 `map0001`+`map0002` 与 `pmap0001`+`pmap_0002` 两个版本的总行数） |
| 第 6 节（L96） | 任务 1 加载器仅在评论正文含非法字符（如 `\x00`）时才隔离至 `quarantine_record` | **不一致** | 任务 1 对 7 类错误（含超大记录、非法 UTF-8、重复字段前缀、未知行、缺/空 `product_id`、`\x00`）均隔离；只是空 `userId` 仅记 `WARNING` 不隔离 |
| 第 8.1 节（L127） | 已冻结 `task6.dev_pool`（1,732 部）并导出 120 部评价集 `ev_rv0001` | **部分一致** | 行数一致，但 `dev_pool` 冻结时间晚于 `rv0001` 正式运行，且分入 `E1`/`E2` 的样本因代码 bug 失真，AI 草稿循环 |

---

## 5. 已核查且没有问题的方面

以下方面已通过源码审查与正式库只读 SQL 逐项验证通过，后续无需重复检查：
1. **任务 1–5 正式库数据零篡改（R31、AC19）**：
   - `grep -rniE "insert |update |delete |truncate |drop |alter |copy "` 确认 `src/etl_task6/`、`dags/amazon_movies_task6_release.py`、`sql/501_task6_schema.sql` 除 `schema_migration` 的 `'501'` 行外，所有写操作均指向 `task6.*`。
   - 正式库任务 1–5 全部主表行数与交接文档完全一致：`public.extraction = 2`、`public.source_file = 1`、`public.review_record = 7,931,696`、`public.review_staging = 7,869,446`、`public.quarantine_record = 62,250`、`public.quality_issue = 63,784`、`task2.product_manifest = 253,059`、`task2.fetch_attempt = 29,144`、`task3.page_parse = 20,661`、`task3.page_field = 449,000`、`task3.product_classification = 506,118`、`imdb.title = 1,250,408`、`imdb.name = 2,050,319`、`task4.field_assertion = 156,845`、`task4.movie (map0002) = 8,659`、`task4.product_movie (map0002) = 8,960`、`task5.person_mention = 56,809`、`task5.t4_upgrade_hint = 335`。
2. **日期精度与禁用来源隔离（R03–R05、R17、R24，AC02、AC11）**：
   - `task6.release_selection` 全部 8,659 行中：`release_date IS NOT NULL` 为 `0` 行，`precision != 'year'` 为 `0` 行，`region IS NOT NULL` 为 `0` 行，`regional_release_missing_reason != 'no_permitted_dated_source'` 为 `0` 行；
   - 非 `missing` 的 5,504 行全部关联有效的 `release_candidate`（仅限 `imdb_t4_link`、`imdb_t5_hint`、`imdb_t6_cooccur`、`amazon_title_year` 四种来源），没有任何商品发售日期、评论时间、评论年份提及或推断上界写入 `release_year`；
   - `task6.inferred_release_bound` 共 6,978 行全部另存，`status = 'inferred'`，未覆盖任何 `release_selection` 记录。
3. **无外部网络调用与受限文件未入库（R32、AC20）**：
   - 扫描 `src/etl_task6/`、`dags/`、`scripts/` 无任何 `requests`、`urllib`、`httpx`、`aiohttp`、`socket` 调用；
   - `git ls-files 'data/imdb/*' 'outputs/*' '*.tsv.gz' '*eval_items*.csv' '*.env*'` 仅返回 `.env.example`。
4. **并发锁、DAG 拓扑与单次触发单 `run_id`（R29、R30，AC14、AC15）**：
   - `run.py` 使用事务级 `pg_try_advisory_xact_lock` + `task6.run` 中 `status = 'running'` 检查 preventing 同一抽取并发运行；
   - `dags/amazon_movies_task6_release.py` 由 `t6_init_run` 生成唯一 `run_id` 经 XCom 传给下游阶段任务，`t6_finalize_run` 配置 `TriggerRule.ALL_DONE` + `t6_gate` 门禁验证上游失败不掩盖。
5. **任务 1–5 回归测试（AC00）**：
   - `pytest -q tests -k "not task6"` 全部 `691 passed`。

---

## 6. 没来得及覆盖的范围

1. **全量 690 部 `cooccur_matched` 与 293 部 `hint_matched` 的逐部人工核验**：本次审查对 `cooccur_matched` 抽样核查了 20 部重点实例（加 `ev_rv0001` 的 20 部 `E4` 样本与高危短键查询），未对全部 690 部做人工逐一核对。
2. **Airflow 容器内端到端手工触发 DAG 运行**：受“审查期间不触发 DAG、不写正式库”硬性限制约束，本次通过源码审查与 `test_task6_dag.py` 集成测试核查了 DAG 拓扑、XCom 传递与门禁行为，未在 Airflow Web/CLI 中实际触发 DAG。
