"""任务 8 的只读取数辅助：任务 1 的原始与暂存层（spec R01：全部带抽取条件）。

review_clean 用 INSERT … SELECT 在库内一次读完一个记录号区间（正文不进 Python 内存）；这里只放需要把原值取进
Python 的小查询：哈希碰撞拆组用的完整原值、近似评论比较用的正文。不在任务 1–7 的表上做任何写操作。
归属阶段（T8-04）读任务 3 分类与任务 7 版本的取数也放在这里。
"""

from __future__ import annotations

from typing import Iterable

import psycopg


def _chunks(items: list, size: int = 2000) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def raw_values(conn: psycopg.Connection, extraction_id: str, source_review_ids: list[str]) -> dict[str, tuple]:
    """source_review_id → 内容键的完整原值 (reviewer_id, UTC 评论日, score, summary_raw, review_text_raw)。"""
    out: dict[str, tuple] = {}
    for chunk in _chunks(sorted(set(source_review_ids))):
        for sid, *raw in conn.execute(
                "SELECT source_review_id, reviewer_id, (review_time_utc AT TIME ZONE 'UTC')::date, score, summary_raw, "
                "review_text_raw FROM public.review_staging WHERE extraction_id = %s AND source_review_id = ANY(%s)",
                (extraction_id, chunk)):
            out[sid] = tuple(raw)
    return out


def cv_movies(conn: psycopg.Connection, conflict_version_id: str) -> set[str]:
    """该任务 7 版本的全部作品：edition_resolved 里单品与合集的 movie_ids 并集（正式库 cv0003 为 8,659 部）。"""
    return {r[0] for r in conn.execute("SELECT DISTINCT unnest(movie_ids) FROM task7.edition_resolved "
                                       "WHERE conflict_version_id = %s", (conflict_version_id,))}


def likely_duplicate_pairs(conn: psycopg.Connection, extraction_id: str, link_stage_key: str
                           ) -> list[tuple[str, str, str]]:
    """任务 7 版本 link_stage_key 下的 likely_duplicate_movie：(较小 movie_id, 较大 movie_id, tconst)，去掉方向重复。"""
    return conn.execute(
        "SELECT least(movie_id COLLATE \"C\", other_movie_id), greatest(movie_id COLLATE \"C\", other_movie_id), "
        "min(tconst) FROM task7.shared_tconst_result "
        "WHERE extraction_id = %s AND stage_key = %s AND link_class = 'likely_duplicate_movie' "
        "AND movie_id <> other_movie_id GROUP BY 1, 2 ORDER BY 1, 2",
        (extraction_id, link_stage_key)).fetchall()


# 商品类别（R26–R28、R42）：t3r2 分类；合集按 cv 是否有作品拆两类，待确认按是否采集拆两类
CATEGORY_SQL = """
CASE WHEN pc.category IS NULL THEN 'unclassified'
     WHEN pc.category = 'collection' AND cardinality(coalesce(e.movie_ids, '{}')) > 0 THEN 'collection_with_movies'
     WHEN pc.category = 'collection' THEN 'collection_without_movies'
     WHEN pc.category = 'pending' AND pc.input_group = 'not_collected' THEN 'not_collected'
     WHEN pc.category = 'pending' THEN 'pending_collected'
     ELSE pc.category END
"""
CATEGORIES = ("movie", "collection_with_movies", "collection_without_movies", "tv_series", "concert_other",
              "non_movie", "pending_collected", "not_collected", "unclassified")


def review_texts(conn: psycopg.Connection, extraction_id: str, source_review_ids: list[str]) -> dict[str, str | None]:
    """source_review_id → 正文原值（近似评论比较用）。"""
    out: dict[str, str | None] = {}
    for chunk in _chunks(sorted(set(source_review_ids))):
        for sid, text in conn.execute(
                "SELECT source_review_id, review_text_raw FROM public.review_staging "
                "WHERE extraction_id = %s AND source_review_id = ANY(%s)", (extraction_id, chunk)):
            out[sid] = text
    return out
