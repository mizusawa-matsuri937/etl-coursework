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


def review_texts(conn: psycopg.Connection, extraction_id: str, source_review_ids: list[str]) -> dict[str, str | None]:
    """source_review_id → 正文原值（近似评论比较用）。"""
    out: dict[str, str | None] = {}
    for chunk in _chunks(sorted(set(source_review_ids))):
        for sid, text in conn.execute(
                "SELECT source_review_id, review_text_raw FROM public.review_staging "
                "WHERE extraction_id = %s AND source_review_id = ANY(%s)", (extraction_id, chunk)):
            out[sid] = text
    return out
