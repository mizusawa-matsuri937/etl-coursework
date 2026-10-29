# 任务 4 合成夹具

全部为手写的合成数据，非真实商品，也不含任何 IMDb 真实文件；预期值按 spec 与 design 手算，不由实现生成。

- `imdb_files.py`：按 IMDb 格式在测试时现场写入五个 `.tsv.gz` 与下载记录 `download.json`（gzip 头时间固定为 0），不提交 `.tsv.gz`。
  - `BASIC`（AC02）：`movie`、`tvMovie`、`video`、`tvSeries`、`short`、`tvEpisode` 各类作品，`\N` 值，一个中文别名与一个法文别名，
    不在保留作品里的别名，writer 类演职员，无人引用的人。`BASIC_EXPECTED` 给出每个文件应读、应留的行数；`TITLE_TYPES`、
    `OTHER_TYPES_KEPT` 给出 `imdb.title` 与 `imdb.title_other` 的预期内容。
  - `MATCH`（AC05、AC06）：Casablanca、1960 与 1983 年两部 Breathless（含别名 “Sin aliento”）、Rocky 与 Rocky II、两部等价的
    Twin Peak（并列）、只有 `tvSeries` 的 Only Series、没有人物的 Lonely Title、Alien 与 Aliens。
  - `CONFLICT`（AC07）：有两位导演的 Duo Film，页面上只写其中一位的商品都能关联上，但彼此导演不重叠。
  - `flip_one_byte`：改动文件的一个字节（大小不变），用来验证 SHA-256 拒绝。
- `tests/integration/_task4_helpers.py`：在 `etl_test` 里建任务 3 结构的抽取、清单、`page_parse`、`page_field` 与 `product_classification`
  （直接写表，不经页面快照）。
  - `standard_products()`（AC01）：12 行，`movie` 4、`collection` 2、`tv_series`、`concert_other`、`non_movie` 各 1、有页面的 `pending` 1、无页面 2；
    预期匹配状态为参与匹配 6、`not_applicable` 4、`no_page` 2；评论数 3、2、1、1、4、5、6、1、1、1、7、1。
  - `match_products()`（AC05、AC06）：十个案例的商品（ASIN 末位字母是案例，数字是同案例内序号），预期判定写在
    `tests/integration/test_task4_match_db.py` 的 `IMDB_CASES` 与 `PAIR_CASES`。

| 案例 | 商品 | 预期 |
|---|---|---|
| 1 同一作品三个版本 | A01 DVD、A02 Blu-ray、A03 加长版（片长多 41%） | 都对 Casablanca 自动合并；加长版不因片长被拒 |
| 2 1960 与 1983 年版 | B01 / B02 | 各自对自己的 IMDb 作品自动合并，对另一部硬冲突 reject；B01 与 B02 的商品对 reject |
| 3 真人版与动画版 | C01 / C02（IMDb 里没有） | 商品对 reject，不自动合并 |
| 4 Rocky 与 Rocky II | D01 / D02 | 标题键 `rocky`、`rocky 2` 不同，互不为候选 |
| 5 无年份同导演 | E01 / E02 | 商品对 review（no_title_year） |
| 6 标题年份相同同导演 | F01 / F02 | 商品对 auto（page_cluster） |
| 7 只在 tvSeries 中有匹配 | G01 | G4 候选 review，原因 non_movie_title_type |
| 8 两个作品得分并列 | H01 | 两个候选都是 review（tie） |
| 9 仅标题一致 | I01 | reject，得分 2 |
| 10 西班牙语版 | J01 | 与英语版同属 Casablanca 作品，自动合并 |
