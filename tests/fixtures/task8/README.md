# 任务 8 合成夹具

全部为手写的合成评论（非真实用户与商品），预期值按 spec 的 AC 表手算，不由实现生成。`world.py` 只定义源记录规格 `Rec`（ASIN、用户、显示名、helpfulness、评分、时间、摘要、正文，`None` 表示省略该行，`raw_text` 可写入非法 UTF-8，`extra` 可追加无法识别的行）；`tests/integration/_task8_helpers.load` 把一组 `Rec` 写成 movies.txt 格式的 gzip，再用**任务 1 的装载器**（`etl_task1.loader.load_extraction`）装进 `etl_test`。所以 `review_record`、`review_staging`、`quarantine_record`、`quality_issue` 的列、规范值（`helpful_votes`、`helpfulness_ratio`、`score`、`review_time_utc`）、隔离分类与质量问题都由任务 1 的代码生成，与正式库同形，而不是手写成恰好能通过的形态（任务 7 交接第 6 节第 1 条）。

与正式库对照过的形态（2026-11-03 只读查询）：评论时间是 UTC 零点的 Unix 秒、评分写作 `5.0`；`8/1` 这类分子大于分母在任务 1 里票数为空并记 `invalid_helpfulness`；`0/0` 的票数为 0、比例为空；12 条不完整记录只有产品、用户与显示名三行；隔离记录为 `invalid_utf8`、`unknown_line`、`unknown_line` + `missing_product_id`（无产品行且带一条无法识别的行）。

`_task8_helpers.load(..., sample_max=N)` 用同一份数据按样本范围再装一个抽取（另一个 `extraction_id`），用来证明任务 8 只读指定抽取（AC01）。归属阶段（T8-04）需要的任务 3 分类与任务 7 `edition_resolved`、`shared_tconst_result` 由后续阶段补充，形状须与正式库同形（`movie_ids` 为 `text[]`，合集可为空数组）。

## 案例与 AC 场景

| 测试文件 | 案例 | 对应 AC 场景 |
|---|---|---|
| `test_task8_clean.py` | helpfulness `3/5`、`0/0`、`8/1`、`-1/2`、`abc`、缺失；缺评分与时间的记录 | AC02 |
| `test_task8_clean.py` | 大小写、末尾空格、Unicode、HTML 链接与实体、摘要缺失与空摘要：SQL 内容键哈希与分区桶与 Python 一致 | AC14（SQL 部分） |
| `test_task8_clean.py` | 有效、`invalid_utf8`、`unknown_line`、无产品 ID 的隔离记录；同一数据的样本抽取 | AC01（清洗部分） |
| `test_task8_family.py` | A、B 同 10 条；C 是其中 6 条；D 与 A 共 1 条；E 无共享；F、G 各 1 条且相同；阈值 0.7 | AC04 |
| `test_task8_dedup.py` | 有效、D1、隔离、不完整（缺 5 个字段）、无产品 ID；样本抽取 | AC01 |
| `test_task8_dedup.py` | K1 大小写、K2 末尾空格、K3 同正文不同摘要、K4 注入哈希碰撞；用户 U5 有显示名 `Ann` 与 `Ann "A"`，另一用户也叫 `Ann` | AC03 |
| `test_task8_dedup.py` | 同 ASIN 八字段全同；只 helpfulness 不同（`1/2`、`3/4`）；同一家族 3 个 ASIN；跨家族同内容；重跑 | AC05 |
| `test_task8_dedup.py` | X1 跨家族同内容、X2 同一家族 Jaccard 0.93、X3 Jaccard 0.6 | AC06（家族内部分） |
