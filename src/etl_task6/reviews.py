"""评论时间（spec R20）：商品层的最早、第二早有效评论与评论数；作品层只汇总 edition 关系的电影类商品。

合集商品的评论只在商品层保存，不进成员作品（与任务 4、5 的处理一致）。作品层的第二早评论一定在各商品的
前两条之中，所以商品层只存两条即可（design“评论时间与推断上界”）。
"""

from __future__ import annotations

from typing import Any

import psycopg

from .run import DEFAULT_BATCH, BeforeCommit, RunContext, copy_rows, mark_batch, mark_entities, next_batch_no
from . import sources

PRODUCT_COLUMNS = ("extraction_id", "asin", "product_kind", "earliest_time", "earliest_source_review_id",
                   "second_time", "review_count", "run_id")
MOVIE_COLUMNS = ("extraction_id", "t4_mapping_version_id", "movie_id", "earliest_review_time", "earliest_precision",
                 "earliest_source_review_id", "earliest_asin", "second_review_time", "review_count",
                 "reviewed_asin_count", "edition_asin_count", "run_id")


def product_review_times(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                         before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """按商品分批：每个参与匹配商品（电影类与合集）的有效评论取前两条与计数。"""
    i = ctx.inputs
    world = sources_world(conn, ctx)
    key, stage = i.stage_key("product_reviews"), "product_reviews"
    unmarked = conn.execute(
        "SELECT e.asin FROM task4.product_edition e LEFT JOIN task6.entity_stage es ON es.extraction_id = e.extraction_id "
        "AND es.stage = 'product_reviews' AND es.stage_key = %s AND es.entity_id = e.asin "
        "WHERE e.extraction_id = %s AND e.t3_rule_version = %s AND e.match_scope = 'matched' "
        "AND es.entity_id IS NULL ORDER BY e.asin COLLATE \"C\"",
        (key, i.extraction_id, world.t3)).fetchall()
    todo = [r[0] for r in unmarked]
    counts: dict[str, Any] = {"products": 0, "with_reviews": 0}
    batch_no = next_batch_no(conn, ctx, stage)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        with conn.transaction():
            rows: list[tuple] = []
            counts_batch: dict[str, int] = {a: 0 for a in batch}
            first: dict[str, tuple] = {}
            second: dict[str, tuple] = {}
            for k in range(0, len(batch), 500):
                for asin, rid, when in conn.execute(
                        "SELECT product_id, source_review_id, review_time_utc FROM review_staging "
                        "WHERE extraction_id = %s AND product_id = ANY(%s) ORDER BY product_id, review_time_utc, "
                        "source_review_id", (i.extraction_id, batch[k:k + 500])):
                    counts_batch[asin] += 1
                    if asin not in first:
                        first[asin] = (rid, when)
                    elif asin not in second:
                        second[asin] = (rid, when)
            for asin in batch:
                if asin in first:
                    p = world.products[asin]
                    rows.append((i.extraction_id, asin, p["product_kind"], first[asin][1], first[asin][0],
                                 second[asin][1] if asin in second else None, counts_batch[asin], ctx.run_id))
                    counts["with_reviews"] += 1
            # 换映射版本重跑时阶段键变化会重新遍历：先删本批旧行再写（表按商品、与映射无关）
            conn.execute("DELETE FROM task6.product_review_time WHERE extraction_id = %s AND asin = ANY(%s)",
                         (i.extraction_id, batch))
            copy_rows(conn, "task6.product_review_time", PRODUCT_COLUMNS, rows)
            mark_entities(conn, ctx, stage, batch_no, [(a, 1 if a in first else 0) for a in batch])
            mark_batch(conn, ctx, stage, batch_no, batch[0], batch[-1], len(rows))
            if before_commit is not None:
                before_commit(stage, batch_no)
        counts["products"] += len(batch)
        batch_no += 1
    return counts


def movie_review_times(conn: psycopg.Connection, ctx: RunContext) -> dict[str, int]:
    """作品层汇总（全局步骤，一个事务）：edition 商品的前两早评论汇总成作品的最早、第二早评论。"""
    i = ctx.inputs
    world = sources_world(conn, ctx)
    per_movie: dict[str, list[tuple]] = {}
    for asin, kind, earliest, earliest_rid, second, n in conn.execute(
            "SELECT asin, product_kind, earliest_time, earliest_source_review_id, second_time, review_count "
            "FROM task6.product_review_time WHERE extraction_id = %s", (i.extraction_id,)):
        p = world.products.get(asin, {})
        if p.get("product_kind") != "movie":        # 合集商品不进成员作品（R20）
            continue
        movie = world.asin_movie.get((asin, 0))
        if movie:
            per_movie.setdefault(movie, []).append((asin, earliest, earliest_rid, second, n))
    rows: list[tuple] = []
    with conn.transaction():
        # 全局步骤重跑（换人物映射版本等）时先删本输入键下的旧行再汇总（确定性重算，旧映射版本的行保留）
        conn.execute("DELETE FROM task6.movie_review_time WHERE extraction_id = %s AND t4_mapping_version_id = %s",
                     (i.extraction_id, i.t4_mapping_version_id))
        for movie, products in sorted(per_movie.items()):
            ordered = sorted(products, key=lambda x: (x[1], x[0]))
            all_times = []
            for x in ordered:
                all_times.append((x[1], x[2], x[0]))
                if x[3] is not None:
                    all_times.append((x[3], None, x[0]))
            all_times.sort(key=lambda t: (t[0], t[2]))
            earliest_asin = all_times[0][2]
            second = all_times[1][0] if len(all_times) > 1 else None
            reviewed = sum(1 for x in ordered if x[4])
            count = sum(x[4] for x in ordered)
            rows.append((i.extraction_id, i.t4_mapping_version_id, movie, all_times[0][0], "day", all_times[0][1],
                         earliest_asin, second, count, reviewed, len(world.edition_asins.get(movie, ())), ctx.run_id))
        copy_rows(conn, "task6.movie_review_time", MOVIE_COLUMNS, rows)
    return {"movies": len(rows)}


def sources_world(conn: psycopg.Connection, ctx: RunContext):
    from .candidates import World
    return World(conn, ctx)
