"""任务 8 的四个只读查询（spec R43，AC12）。

    python -m etl_task8.query review <source_review_id 或 record_no>   # 原始记录 → 逻辑评论、层次、家族、商品、作品、统计组、匹配状态
    python -m etl_task8.query logical <logical_review_id>               # 逻辑评论 → 全部原始记录与差异、作品、候选
    python -m etl_task8.query stats --by product|movie|group [--id X] [--limit N]   # 评论数与评分，附口径说明
    python -m etl_task8.query reviewer <reviewer_id> [--limit N]        # 用户 → 显示名变体与评论
都可加 --version（默认抽取的最新去重版本）与 --extraction-id。输出一段 JSON，不写任何表；查询限定版本登记的阶段键。
正文只显示前 80 个字符。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from . import sources
from .check import latest_version, version_row
from .report import NOTES, TEXT_PREFIX, _f


def _keys(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(row[f"{s}_stage_key"] for s in ("clean", "family", "dedup", "attribution"))  # type: ignore[return-value]


def _prefix(conn: psycopg.Connection, eid: str, srid: str) -> str | None:
    t = sources.review_texts(conn, eid, [srid]).get(srid)
    return None if t is None else t[:TEXT_PREFIX]


def _movies_and_group(conn: psycopg.Connection, rd: str, ak: str, lid: str) -> list[dict[str, Any]]:
    return [{"movie_id": m, "via_asins": list(v), "also_on_collection": c, "stat_group_id": g}
            for m, v, c, g in conn.execute(
                "SELECT w.movie_id, w.via_asins, w.also_on_collection, s.stat_group_id FROM task8.logical_review_work w "
                "LEFT JOIN task8.stat_group_member s ON s.attribution_stage_key = w.attribution_stage_key "
                "AND s.movie_id = w.movie_id WHERE w.attribution_stage_key = %s AND w.logical_review_id = %s "
                "ORDER BY w.movie_id", (ak, lid))]


def _candidates(conn: psycopg.Connection, dk: str, ak: str, lid: str) -> list[dict[str, Any]]:
    return [{"candidate_id": cid, "type": t, "logical_review_ids": list(ids), "similarity": _f(sim), "basis": dict(b)}
            for cid, t, ids, sim, b in conn.execute(
                "SELECT candidate_id, candidate_type, logical_review_ids, similarity, basis FROM task8.dup_candidate "
                "WHERE stage_key IN (%s, %s) AND %s = ANY(logical_review_ids) ORDER BY candidate_id", (dk, ak, lid))]


def query_review(conn: psycopg.Connection, rd: str, ref: str) -> dict[str, Any]:
    row = version_row(conn, rd)
    eid = row["extraction_id"]
    ck, fk, dk, ak = _keys(row)
    where, arg = ("record_no = %s", int(ref)) if ref.isdigit() else ("source_review_id = %s", ref)
    rec = conn.execute(f"SELECT record_no, source_review_id, classification, product_id_observed FROM public.review_record "
                       f"WHERE extraction_id = %s AND {where}", (eid, arg)).fetchone()
    if rec is None:
        return {"review_dedup_version_id": rd, "ref": ref, "found": False}
    rn, srid, cls, product = rec
    out: dict[str, Any] = {"review_dedup_version_id": rd, "found": True, "record_no": rn, "source_review_id": srid,
                           "classification": cls, "product_id_observed": product}
    m = conn.execute("SELECT asin, logical_review_id, mapping_status, match_level, diff_fields FROM task8.review_mapping "
                     "WHERE dedup_stage_key = %s AND record_no = %s", (dk, rn)).fetchone()
    out["mapping"] = None if m is None else {"asin": m[0], "logical_review_id": m[1], "mapping_status": m[2],
                                             "match_level": m[3], "diff_fields": list(m[4])}
    out["issues"] = [{"code": c, "evidence": e} for c, e in conn.execute(
        "SELECT code, evidence FROM task8.review_issue WHERE stage_key IN (%s, %s) AND record_no = %s ORDER BY code",
        (ck, dk, rn))]
    asin = m[0] if m else product
    if asin:
        p = conn.execute("SELECT product_category, has_movie, raw_valid, logical_reviews FROM task8.product_review_stats "
                         "WHERE review_dedup_version_id = %s AND asin = %s", (rd, asin)).fetchone()
        fam = conn.execute("SELECT f.family_id, r.asin_count, r.weak_support FROM task8.family_member f "
                           "JOIN task8.review_family r USING (family_stage_key, family_id) "
                           "WHERE f.family_stage_key = %s AND f.asin = %s", (fk, asin)).fetchone()
        out["product"] = None if p is None else {"asin": asin, "category": p[0], "has_movie": p[1], "raw_valid": p[2],
                                                 "logical_reviews": p[3]}
        out["family"] = None if fam is None else {"family_id": fam[0], "asin_count": fam[1], "weak_support": fam[2]}
    if m and m[1]:
        out["movies"] = _movies_and_group(conn, rd, ak, m[1])
        out["text_prefix"] = _prefix(conn, eid, srid)
    return out


def query_logical(conn: psycopg.Connection, rd: str, lid: str) -> dict[str, Any]:
    row = version_row(conn, rd)
    eid = row["extraction_id"]
    ck, _, dk, ak = _keys(row)
    lr = conn.execute("SELECT reviewer_id, review_day, score, family_id, collision_variant, representative_record_no, "
                      "copy_count, asin_count, helpful_votes, total_votes, helpfulness_record_no FROM task8.logical_review "
                      "WHERE dedup_stage_key = %s AND logical_review_id = %s", (dk, lid)).fetchone()
    if lr is None:
        return {"review_dedup_version_id": rd, "logical_review_id": lid, "found": False}
    cols = ("reviewer_id", "review_day", "score", "family_id", "collision_variant", "representative_record_no",
            "copy_count", "asin_count", "helpful_votes", "total_votes", "helpfulness_record_no")
    d = dict(zip(cols, lr))
    d["review_day"], d["score"] = str(d["review_day"]), _f(d["score"])
    records = [{"record_no": r[0], "source_review_id": r[1], "asin": r[2], "match_level": r[3], "diff_fields": list(r[4]),
                "helpfulness_raw": r[5], "profile_name_raw": r[6]} for r in conn.execute(
        "SELECT m.record_no, m.source_review_id, m.asin, m.match_level, m.diff_fields, c.helpfulness_raw, "
        "c.profile_name_raw FROM task8.review_mapping m JOIN task8.review_clean c ON c.clean_stage_key = %s "
        "AND c.record_no = m.record_no WHERE m.dedup_stage_key = %s AND m.logical_review_id = %s ORDER BY m.record_no",
        (ck, dk, lid))]
    return {"review_dedup_version_id": rd, "logical_review_id": lid, "found": True, **d,
            "text_prefix": _prefix(conn, eid, records[0]["source_review_id"]) if records else None,
            "records": records, "movies": _movies_and_group(conn, rd, ak, lid), "candidates": _candidates(conn, dk, ak, lid)}


STATS = {
    "product": ("product_review_stats", "asin",
                ("asin", "product_category", "has_movie", "raw_valid", "raw_quarantined", "raw_rated", "raw_avg_score",
                 "logical_reviews", "logical_rated", "logical_avg_score")),
    "movie": ("movie_review_stats", "movie_id",
              ("movie_id", "stat_group_id", "raw_records", "raw_rated", "raw_avg_score", "logical_reviews", "logical_rated",
               "logical_avg_score", "shared_with_group", "near_dup_candidates")),
    "group": ("stat_group_review_stats", "stat_group_id",
              ("stat_group_id", "movie_count", "raw_records", "raw_rated", "raw_avg_score", "logical_reviews",
               "logical_rated", "logical_avg_score", "near_dup_pairs", "sensitivity_rated", "sensitivity_avg_score")),
}


def query_stats(conn: psycopg.Connection, rd: str, by: str, entity: str | None = None, limit: int = 20) -> dict[str, Any]:
    """第十部分第 5 个查询：按商品、作品或统计组的评论数与评分（去重前后），输出附口径说明。"""
    version_row(conn, rd)
    table, key, cols = STATS[by]
    sql = f"SELECT {', '.join(cols)} FROM task8.{table} WHERE review_dedup_version_id = %s"
    args: tuple = (rd,)
    if entity:
        sql += f" AND {key} = %s"
        args += (entity,)
    sql += f" ORDER BY logical_reviews DESC, {key} LIMIT %s"
    rows = []
    for r in conn.execute(sql, (*args, limit)):
        d = dict(zip(cols, r))
        for k, v in d.items():
            if k.endswith("avg_score"):
                d[k] = _f(v)
        rows.append(d)
    if by == "group" and rows:
        for d in rows:
            d["movies"] = [m for (m,) in conn.execute(
                "SELECT movie_id FROM task8.movie_review_stats WHERE review_dedup_version_id = %s AND stat_group_id = %s "
                "ORDER BY movie_id", (rd, d["stat_group_id"]))]
    return {"review_dedup_version_id": rd, "by": by, "rows": rows, "notes": list(NOTES)}


def query_reviewer(conn: psycopg.Connection, rd: str, reviewer: str, limit: int = 20) -> dict[str, Any]:
    row = version_row(conn, rd)
    _, _, dk, ak = _keys(row)
    r = conn.execute("SELECT profile_names, profile_variant_count, record_count, logical_count, first_day, last_day "
                     "FROM task8.reviewer WHERE dedup_stage_key = %s AND reviewer_id = %s", (dk, reviewer)).fetchone()
    if r is None:
        return {"review_dedup_version_id": rd, "reviewer_id": reviewer, "found": False}
    reviews = [{"logical_review_id": lid, "review_day": str(day), "score": _f(score), "family_id": fam,
                "copy_count": n, "asin_count": na} for lid, day, score, fam, n, na in conn.execute(
        "SELECT logical_review_id, review_day, score, family_id, copy_count, asin_count FROM task8.logical_review "
        "WHERE dedup_stage_key = %s AND reviewer_id = %s ORDER BY review_day, logical_review_id LIMIT %s",
        (dk, reviewer, limit))]
    cands = [{"candidate_id": cid, "type": t, "logical_review_ids": list(ids), "similarity": _f(sim)}
             for cid, t, ids, sim in conn.execute(
                 "SELECT candidate_id, candidate_type, logical_review_ids, similarity FROM task8.dup_candidate "
                 "WHERE stage_key IN (%s, %s) AND reviewer_id = %s ORDER BY candidate_id LIMIT %s", (dk, ak, reviewer, limit))]
    return {"review_dedup_version_id": rd, "reviewer_id": reviewer, "found": True, "profile_names": dict(r[0]),
            "profile_variant_count": r[1], "record_count": r[2], "logical_count": r[3], "first_day": str(r[4]),
            "last_day": str(r[5]), "logical_reviews": reviews, "candidates": cands}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 8 只读查询")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_review = sub.add_parser("review", help="原始记录 → 逻辑评论与匹配状态")
    p_review.add_argument("ref", help="source_review_id 或 record_no")
    p_logical = sub.add_parser("logical", help="逻辑评论 → 全部原始记录与差异")
    p_logical.add_argument("logical_review_id")
    p_stats = sub.add_parser("stats", help="按商品、作品或统计组的评论数与评分")
    p_stats.add_argument("--by", choices=tuple(STATS), required=True)
    p_stats.add_argument("--id", dest="entity", help="只看一个 ASIN、movie_id 或 stat_group_id")
    p_reviewer = sub.add_parser("reviewer", help="用户 → 显示名变体与评论")
    p_reviewer.add_argument("reviewer_id")
    for p in (p_review, p_logical, p_stats, p_reviewer):
        p.add_argument("--version", help="去重版本号（默认最近一次）")
        p.add_argument("--extraction-id", help="抽取 ID")
    for p in (p_stats, p_reviewer):
        p.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        rd = args.version or latest_version(conn, eid)
        if args.cmd == "review":
            out = query_review(conn, rd, args.ref)
        elif args.cmd == "logical":
            out = query_logical(conn, rd, args.logical_review_id)
        elif args.cmd == "stats":
            out = query_stats(conn, rd, args.by, args.entity, args.limit)
        else:
            out = query_reviewer(conn, rd, args.reviewer_id, args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
