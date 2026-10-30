"""任务 6 的只读取数辅助：任务 3/4/5、IMDb 与任务 1 有效评论的统一入口（spec R01：全部带输入键）。

各阶段模块从这里取数，不在任务 1–5 的表上做任何写操作。-person 断言与标题键沿用任务 4 的标准化版本
（t4n1；姓名的严格键就是任务 4 断言的 normalized_value，与任务 5 页面规范化一致）。
"""

from __future__ import annotations

from typing import Any, Iterable

import psycopg

from etl_task1.db import sql_dir  # noqa: F401  保持与其他模块一致的可用性
from etl_task4.normalize import title_key  # noqa: F401  供需要重新折叠标题的调用方使用

from . import T4_NORM_VERSION

STRONG_QUALIFIERS = ("byline", "reversed", "detail")     # 严格键可信的页面署名（与任务 4 STRONG_QUALIFIERS 一致）


def t3_rule_of(conn: psycopg.Connection, t4: str) -> str:
    return conn.execute("SELECT t3_rule_version FROM task4.mapping_version WHERE mapping_version_id = %s",
                        (t4,)).fetchone()[0]


def movies(conn: psycopg.Connection, t4: str) -> list[tuple[str, str | None]]:
    """映射版本的全部作品（含只经 contains 出现的），按 movie_id 排序。"""
    return conn.execute("SELECT movie_id, tconst FROM task4.movie WHERE mapping_version_id = %s "
                        "ORDER BY movie_id COLLATE \"C\"", (t4,)).fetchall()


def relation_rows(conn: psycopg.Connection, t4: str) -> dict[str, list[tuple[int, str, str]]]:
    """asin → [(member_ordinal, movie_id, relation_type)]，用于把断言归到作品与 B2 的 edition 判定。"""
    out: dict[str, list[tuple[int, str, str]]] = {}
    for asin, ordinal, movie, rel in conn.execute(
            "SELECT asin, member_ordinal, movie_id, relation_type FROM task4.product_movie "
            "WHERE mapping_version_id = %s ORDER BY asin, member_ordinal", (t4,)):
        out.setdefault(asin, []).append((ordinal, movie, rel))
    return out


def relation_basis(conn: psycopg.Connection, t4: str) -> dict[tuple[str, int, str], tuple[str, int | None]]:
    """(asin, member_ordinal, movie_id) → (basis, decision_id)，任务 4 关联依据（R06 证据）。"""
    return {(a, mo, m): (b, d) for a, mo, m, b, d in conn.execute(
        "SELECT asin, member_ordinal, movie_id, basis, decision_id FROM task4.product_movie "
        "WHERE mapping_version_id = %s", (t4,))}


def product_rows(conn: psycopg.Connection, extraction_id: str, t3: str) -> dict[str, dict[str, Any]]:
    """参与匹配商品（电影类与合集，match_scope='matched'）→ 页面来源（快照哈希、地址、采集时间）。"""
    return {a: {"product_kind": k, "content_hash": ch, "final_url": u, "fetched_at": f}
            for a, k, ch, u, f in conn.execute(
                "SELECT asin, product_kind, content_hash, final_url, fetched_at FROM task4.product_edition "
                "WHERE extraction_id = %s AND t3_rule_version = %s AND match_scope = 'matched' ORDER BY asin",
                (extraction_id, t3))}


def source_record_brief(conn: psycopg.Connection, assertion_ids: list[int]) -> dict[int, dict[str, Any]]:
    """断言 ID → 页面来源记录（快照路径与地址）。"""
    if not assertion_ids:
        return {}
    rows = conn.execute("SELECT a.assertion_id, jsonb_build_object('source_record_id', r.source_record_id, "
                        "'locator', r.locator, 'content_hash', r.content_hash, 'acquired_at', r.acquired_at) "
                        "FROM task4.field_assertion a JOIN task4.source_record r USING (source_record_id) "
                        "WHERE a.assertion_id = ANY(%s)", (assertion_ids,)).fetchall()
    return {a: b for a, b in rows}


def assertions_by_entity(conn: psycopg.Connection, extraction_id: str, parser_version: str, field: str,
                         entities: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """某字段名的断言（entity_type、entity_id、ordinal、原值、规范值、qualifier、定位、source_record_id）。"""
    sql = ("SELECT assertion_id, entity_type, entity_id, ordinal, raw_value, normalized_value, qualifier, "
           "source_record_id, source_locator FROM task4.field_assertion WHERE extraction_id = %s AND "
           f"parser_version = %s AND norm_version = '{T4_NORM_VERSION}' AND field_name = %s")
    args: list[Any] = [extraction_id, parser_version, field]
    if entities is not None:
        sql += " AND entity_id = ANY(%s)"
        args.append(list(entities))
    sql += " ORDER BY entity_id, ordinal, assertion_id"
    keys = ("assertion_id", "entity_type", "entity_id", "ordinal", "raw_value", "normalized_value", "qualifier",
            "source_record_id", "source_locator")
    return [dict(zip(keys, r)) for r in conn.execute(sql, args)]


def imdb_titles(conn: psycopg.Connection, snapshot_id: str, tconsts: list[str]) -> dict[str, dict[str, Any]]:
    """tconst → 快照里的作品（年份、类型、行号）。"""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(tconsts), 500):
        chunk = tconsts[i:i + 500]
        for tc, year, ttype, line in conn.execute(
                "SELECT tconst, start_year, title_type, line_no FROM imdb.title WHERE snapshot_id = %s "
                "AND tconst = ANY(%s)", (snapshot_id, chunk)):
            out[tc] = {"start_year": year, "title_type": ttype, "line_no": int(line)}
    return out


def imdb_basics_file(conn: psycopg.Connection, snapshot_id: str) -> str | None:
    row = conn.execute("SELECT file_name FROM imdb.source_file WHERE snapshot_id = %s AND file_name LIKE "
                       "'title.basics%%'", (snapshot_id,)).fetchone()
    return row[0] if row else None


def snapshot_downloaded_at(conn: psycopg.Connection, snapshot_id: str):
    row = conn.execute("SELECT downloaded_at FROM imdb.snapshot WHERE snapshot_id = %s",
                       (snapshot_id,)).fetchone()
    return row[0] if row else None


def hints(conn: psycopg.Connection, extraction_id: str, stage_key: str) -> list[tuple]:
    """任务 5 升级线索（只读，R07）。"""
    return conn.execute("SELECT asin, member_ordinal, tconst, mention_keys, link_decision_ids, mention_count "
                        "FROM task5.t4_upgrade_hint WHERE extraction_id = %s AND stage_key = %s "
                        "ORDER BY asin, member_ordinal, tconst", (extraction_id, stage_key)).fetchall()


def review_pending_products(conn: psycopg.Connection, t4: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT asin FROM task4.product_match_status WHERE mapping_version_id = %s "
                                       "AND status = 'review_pending'", (t4,))}


def review_time_range(conn: psycopg.Connection, extraction_id: str) -> tuple[str, str]:
    """该抽取有效评论的时间范围（B5 的边界）。"""
    row = conn.execute("SELECT min(review_time_utc), max(review_time_utc) FROM review_staging "
                       "WHERE extraction_id = %s", (extraction_id,)).fetchone()
    return row[0].date().isoformat(), row[1].date().isoformat()
