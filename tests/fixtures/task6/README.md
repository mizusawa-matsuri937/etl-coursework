# 任务 6 合成夹具

全部为手写的合成数据（非真实商品与作品），预期值按 spec 与 design 手算，不由实现生成。`world.py` 一个文件装下全部案例：IMDb 一侧由 `tests/fixtures/task4/imdb_files.build_snapshot` 在测试时现场写成 `.tsv.gz` 并装载；任务 4 映射版本由 `_task6_helpers.make_world` 运行 `etl_task4.runner.execute_match`（规则 **t4r2**，与正式库 `map0002` 同规则）得到；任务 5 的两个人物映射版本与升级线索由 helper 直接写成 `task5` 的行。每个测试开始时 `reset_task6` 清空 `task6` 再跑。

任务 4 无法自动产生三种形态，由 helper 在匹配后直接改写 `task4` 的表（测试夹具，非正式库）：
`FORCE_LINK` 把 B0T6D10001 的作品标为 imdb_linked（tconst tt0006010，startYear 为 `\N`）；
`FORCE_MERGE` 把第二个商品并入第一个商品的作品，制造作品内标题年份 1985/1987、1990/1994 的分歧与
1999/2001 的多 tconst 线索。任务 4 匹配本身对这些商品的行为（page_only / imdb_linked / 合并）不受影响。

## 商品与任务 4 映射（map0001）

ASIN 前缀 `B0T6`。自动关联（imdb_linked，tconst → IMDb 年份）：

| 商品 | 标题 | tconst | 年份 |
|---|---|---|---|
| A10001 + A10002 | Alpha Film (1999)（[VHS] 版） | tt0006001 | 1999 |
| B10001 | Beta Tale (2004) | tt0006002（tvMovie） | 2004 |
| V10001 | Video Works (2001) | tt0006003（video） | 2001 |
| FLF001 | Film F (1990) | tt0006040 | 1990 |
| FLG001 | Film G (1991) | tt0006041 | 1990 |
| FLH001 + FLH002（改造合并） | Film H (1990)/(1994) | tt0006042 | 1990 |
| ERS001 | Early Review Saga (2004) | tt0006050 | 2004 |
| CLS001 | Clash Century (2004) | tt0006051 | 2004 |
| BOT001 | Bound Odyssey (2004) | tt0006049 | 2004 |
| FLJ001 + FLJ002 | Film J (2001)（[VHS] 版） | tt0006060 | 2001 |

- D10001 经 FORCE_LINK 标为 imdb_linked（tt0006010，startYear `\N`）→ 不产生候选，missing（no_candidate）。
- 合集 FLJ003 “Film J / Film K2 (Double Feature) [DVD]” 页面署名两位成员各自的导演（Jay Joiner、Kay
  Keeper），两个成员各自满足 R22 自动关联：成员 1 的 contains 关系指向 FLJ001/002 的作品；成员 2 的新作品
  只经 contains 出现（tconst tt0006061，2002）→ verified。
- 其余商品全部 page_only（标题带“系列说明尾巴”，任务 4 的标题键对不上 IMDb）；FLC001/FLC002、CRM001/CRM002
  标题年份一致自动合并；ONB001/ONB002（年份 2001 对无年份）不合并，product_match_status 都是 review_pending
  （B1 的反例）；FLD001+FLD002、MHA001+MHA002 经 FORCE_MERGE 合并。
- TV0001 是剧集（有页面日期与评论），不参与匹配，`task4.product_movie` 没有它的行。

## 任务 6 的选择状态（`run_stages` 全流程后）

| 案例（AC） | 商品 | 状态 | 说明 |
|---|---|---|---|
| AC02/AC03 | A、B10001、V10001 | verified | 各一条 imdb_t4_link 候选，置信度 0.95 |
| AC03 | D10001 | missing / no_candidate | startYear NULL 不产生候选 |
| AC04 | HNT001 | hint_matched 1975 | 线索 tt0006011（未被占用） |
| AC04 | OWN001 | hint_matched 1999 | 线索 tt0006001 已属于 A 作品：shared_tconst + 质量问题 |
| AC04 | FLJ003#1 | 只记录 | 线索为合集成员：action=collection_member |
| AC04 | MHA001+MHA002（改造合并） | missing / hint_ambiguous | 两个线索 tconst（tt0006012、tt0006013） |
| AC04 | IDX001 | hint_matched 2001 | 线索 tt0006043；另有标题年份 2001 |
| AC04/AC08 | LUK001 | hint_matched 1970 | 线索 tt0006044；共现反查命中 tt0006045（1962）→ tconst_disagreement |
| AC05 | TRS001 | cooccur_matched 1950 | 导演命中 tt0006020，标题实词子集，唯一 |
| AC05 | SOL001 | missing / no_candidate | 一位演员同名：1 分，不足 |
| AC05 | DIS001 | missing / no_candidate | 两位演员同名但标题不吻合（评分记录写明） |
| AC05 | LND001 | missing / no_candidate | 编号排除（商品 8 对 IMDb 无编号） |
| AC05/AC06 | AM1001 | missing / cooccur_ambiguous | 两部同名 Ambi One（1950/1972）都通过，评论无年份提及 |
| AC06 | AM2001 | cooccur_matched 1950 | 两部 Ambi Two 经评论年份（1950 两票）唯一选出：review_year_tiebreak |
| AC05 | SHR001 | cooccur_matched 2004 | 命中的 tt0006002 属于 B10001 的作品：shared_tconst |
| AC06 | RVT001 | missing / no_candidate | 只有评论年份提及（1950 三票），不单独采用 |
| AC07 | FLC001+FLC002 | page_reported 1985 | 两个商品标题年份一致，代表取断言 ID 最小 |
| AC07 | FLD001+FLD002 | missing / title_years_disagree | 1985 对 1987 |
| AC07 | FLE001 | page_reported 1986 | 单一商品 |
| AC08 | FLF001 | verified 1990 | 标题年份与 IMDb 一致 |
| AC08 | FLG001 | verified 1990 | 标题 1991：相差 1 年，只计数 |
| AC08 | FLH001+FLH002 | verified 1990 | 标题 1994：year_disagreement（相差 4 年） |
| AC08 | NEU001 | cooccur_matched 1966 | 只有共现反查与标题年份（标题也 1966） |
| AC10 | FLK001 | page_reported 2002 | Kay Keeper 命中的是 Film K2（标题键 film k2 ≠ film k），只有标题年份 |
| AC11 | ONB001、ONB002 | missing / no_candidate | 各自独立作品（review_pending，B1 反例） |
| AC11 | SPR001 | 无上界 | 只有一条评论：B3 不成立 |
| AC11 | ANC001 | 无上界 | 最早评论 1970-01-01：B5 不成立 |
| AC11 | JAN001 | 上界 2001（year） | 只有 media_release 2001-01-01（jan_first_suspect） |
| AC11 | LST001 | 无上界 | 只有 Date First Available（M2 排除） |
| AC11 | ERS001 | verified 2004 + B4 矛盾 | 最早评论 1999-12-01 早于 2004：bound_before_release_year |
| AC11 | CLS001 | verified 2004 + M4 矛盾 | media_release 1998 早于 2004：bound_before_release_year |
| AC11 | BOT001 | 上界 2004-06-06（media） | 两种证据都成立，取较早的 media_release |
| AC12 | CRM001+CRM002 | 上界（review） | 最早评论 2000-01-01 早于全部 media_release（2005） |
| AC01 | TV0001 | 无 task6 行 | 非参与匹配商品 |

## 评论（`REVIEWS`）与隔离记录

- 时间与正文按 `(日期, 正文)` 给出；未列出的商品默认一条 `("2004-04-04", "Plain review text.")`。
- AM2001 的两条评论含 “this 1950 classic”“made in 1950”；A10002 的一条 2001 年评论含 “(2001)” 与
  “(2003)”——晚于评论年份的 (2003) 不计（R11）。
- `REVIEW_QUARANTINED`（FLK001）与 `NO_REVIEW`（JAN001、LST001、CLS001）的商品只写隔离记录（score=6），
  商品进清单但没有有效评论。
- AC10：FLJ001 两条（2001-03-02、2001-05-10）、FLJ002 一条（2000-12-01）→ J 作品最早 2000-12-01、
  第二早 2001-03-02、评论数 3、有评论商品数 2；合集 FLJ003 的评论（1999-01-01）只在商品层。

## 任务 5 线索（`HINTS`）

`(asin, member_ordinal, tconst, mention_keys, link_decision_ids)` 写成 `task5.t4_upgrade_hint` 行，阶段键
`map0001|t3p1|t5n1|t5r1|<快照>`。人物映射版本两个：`pmap0001`（rule t5r1，阶段键有线索）与
`pmap0002`（rule t5r9，无线索——换人物映射版本后 imdb_t5_hint 候选不再产生，共现反查不受影响）。
