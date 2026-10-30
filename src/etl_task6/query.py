"""任务 6 的两个只读查询（spec R34，AC16）。

    python -m etl_task6.query movie <movie_id>            # 候选、采用值、理由、共现评分、上界评估、商品发售日与最早评论
    python -m etl_task6.query gaps [--status missing|inferred]  # 列出缺失或有推断上界的作品与原因
两者都可加 --rv（默认抽取的最新上映信息版本）与 --extraction-id。输出一段 JSON，不写任何表。
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

from etl_task4.normalize import title_key

from .check import latest_rv, rv_row
from .run import RunInputs


def query_movie(conn: psycopg.Connection, rv: str, movie_id: str) -> dict[str, Any]:
    """查询单部作品的完整上映信息：候选、选择、共现评分、上界与商品层日期。"""
    row = rv_row(conn, rv)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]
    pv = row["parser_version"]
    inputs = RunInputs(
        extraction_id=eid,
        t4_mapping_version_id=t4,
        t5_person_mapping_version_id=row["t5_person_mapping_version_id"],
        imdb_snapshot_id=row["imdb_snapshot_id"],
        parser_version=pv,
        rule_version=row["rule_version"],
    )
    cand_key = inputs.stage_key("candidates")
    cooc_key = inputs.stage_key("cooccur")
    rvy_key = inputs.stage_key("review_years")

    m_row = conn.execute(
        "SELECT display_title, tconst FROM task4.movie WHERE mapping_version_id = %s AND movie_id = %s",
        (t4, movie_id)).fetchone()
    if m_row is None:
        return {"release_version_id": rv, "movie_id": movie_id, "found": False}

    title, tconst = m_row

    # 选择记录
    sel = conn.execute(
        "SELECT status, missing_reason, release_year, precision, selected_candidate_id, selection_rule, "
        "reason, confidence, disagreement FROM task6.release_selection WHERE release_version_id = %s "
        "AND movie_id = %s", (rv, movie_id)).fetchone()

    # 全部候选（按当前版本的 candidates 与 cooccur 阶段键过滤）
    candidates = [
        {"candidate_id": c[0], "source": c[1], "source_key": c[2], "raw_value": c[3], "value_year": c[4],
         "flags": list(c[5] or []), "shared_with_movie_id": c[6], "confidence": float(c[7]) if c[7] else None,
         "source_ref": dict(c[8]) if c[8] else {}, "match_evidence": dict(c[9]) if c[9] else {}}
        for c in conn.execute(
            "SELECT candidate_id, source, source_key, raw_value, value_year, flags, shared_with_movie_id, "
            "confidence, source_ref, match_evidence FROM task6.release_candidate WHERE extraction_id = %s "
            "AND stage_key IN (%s, %s) AND movie_id = %s ORDER BY candidate_id",
            (eid, cand_key, cooc_key, movie_id)).fetchall()
    ]

    # 商品层 edition ASIN 及商品标题键映射
    asins = [r[0] for r in conn.execute(
        "SELECT asin FROM task4.product_movie WHERE mapping_version_id = %s AND movie_id = %s "
        "AND relation_type = 'edition' ORDER BY asin", (t4, movie_id)).fetchall()]
    asin_title_keys: dict[str, str] = {}
    if asins:
        for a_id, norm_v, raw_v in conn.execute(
            "SELECT entity_id, normalized_value, raw_value FROM task4.field_assertion "
            "WHERE extraction_id = %s AND parser_version = %s AND entity_type = 'product_edition' "
            "AND entity_id = ANY(%s) AND field_name = 'title' "
            "ORDER BY entity_id, assertion_id",
            (eid, pv, asins),
        ).fetchall():
            if a_id not in asin_title_keys:
                tk = norm_v or (title_key(raw_v) if raw_v else None)
                if tk:
                    asin_title_keys[a_id] = tk
    fallback_product_key = title_key(title or "") if title else None

    # 共现评分与结果（按当前版本的 cooccur 阶段键过滤，补齐标题键来源、商品标题键与共享标记）
    cooccur_scores = [
        {
            "tconst": cs[0], "score": cs[1], "title_ok": cs[2], "number_guard": cs[3], "passed": cs[4],
            "director_hits": cs[5], "actor_hits": cs[6],
            "matched_title_key": cs[7], "matched_asin": cs[8],
            "key_source": cs[9], "match_key_source": cs[9],
            "title_key_source_line": cs[10], "number_guard_detail": cs[11],
            "product_title_key": asin_title_keys.get(cs[8]) if cs[8] else (fallback_product_key or None),
            "shared_tconst": bool(cs[12]),
        }
        for cs in conn.execute(
            "SELECT cs.tconst, cs.score, cs.title_ok, cs.number_guard, cs.passed, cs.director_hits, cs.actor_hits, "
            "cs.matched_title_key, cs.matched_asin, cs.key_source, cs.title_key_source_line, cs.number_guard_detail, "
            "EXISTS (SELECT 1 FROM task4.movie m WHERE m.mapping_version_id = %s AND m.tconst = cs.tconst "
            "AND m.movie_id != cs.movie_id) AS shared_tconst "
            "FROM task6.cooccur_score cs WHERE cs.extraction_id = %s AND cs.stage_key = %s "
            "AND cs.movie_id = %s ORDER BY cs.score DESC, cs.tconst",
            (t4, eid, cooc_key, movie_id)).fetchall()
    ]
    cooccur_res = conn.execute(
        "SELECT outcome, chosen_tconst, passed_tconsts, owner_movie_id FROM task6.cooccur_result "
        "WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s",
        (eid, cooc_key, movie_id)).fetchone()

    # 评论正文年份提及（按当前版本的 review_years 阶段键过滤）
    mentions = [
        {"year": ym[0], "votes": ym[1], "patterns": dict(ym[2]) if ym[2] else {}}
        for ym in conn.execute(
            "SELECT year, votes, patterns FROM task6.review_year_mention WHERE extraction_id = %s "
            "AND stage_key = %s AND movie_id = %s ORDER BY votes DESC, year",
            (eid, rvy_key, movie_id)).fetchall()
    ]

    # 推断上界与证据评估
    bound = conn.execute(
        "SELECT bound_time, bound_precision, bound_kind, chosen_evidence, confidence, status, rule_version, basis "
        "FROM task6.inferred_release_bound WHERE release_version_id = %s AND movie_id = %s",
        (rv, movie_id)).fetchone()
    assessment = conn.execute(
        "SELECT has_review_evidence, has_media_evidence, passed_evidence, chosen_evidence "
        "FROM task6.bound_assessment WHERE release_version_id = %s AND movie_id = %s",
        (rv, movie_id)).fetchone()
    evidence_rows = [
        {"evidence": ev[0], "evidence_time": ev[1].isoformat() if ev[1] else None, "precision": ev[2],
         "passed": ev[3], "failed_checks": list(ev[4] or []), "confidence": float(ev[5]) if ev[5] else None,
         "checks": dict(ev[6]) if ev[6] else {}}
        for ev in conn.execute(
            "SELECT evidence, evidence_time, evidence_precision, passed, failed_checks, confidence, checks "
            "FROM task6.bound_evidence WHERE release_version_id = %s AND movie_id = %s",
            (rv, movie_id)).fetchall()
    ]

    # 商品层日期与评论
    product_dates = [
        {"asin": pd[0], "kind": pd[1], "label_raw": pd[2], "raw_value": pd[3], "value_date": pd[4].isoformat(),
         "precision": pd[5], "flags": list(pd[6] or [])}
        for pd in conn.execute(
            "SELECT asin, kind, label_raw, raw_value, value_date, precision, flags "
            "FROM task6.product_release_date WHERE extraction_id = %s AND parser_version = %s "
            "AND asin = ANY(%s) ORDER BY asin, assertion_id", (eid, pv, asins)).fetchall()
    ]
    product_review_times = [
        {
            "asin": pr[0], "product_kind": pr[1],
            "earliest_time": pr[2].isoformat() if pr[2] else None,
            "earliest_review_time": pr[2].isoformat() if pr[2] else None,
            "earliest_source_review_id": pr[3],
            "second_time": pr[4].isoformat() if pr[4] else None,
            "second_review_time": pr[4].isoformat() if pr[4] else None,
            "review_count": pr[5],
        }
        for pr in conn.execute(
            "SELECT asin, product_kind, earliest_time, earliest_source_review_id, "
            "second_time, review_count FROM task6.product_review_time "
            "WHERE extraction_id = %s AND asin = ANY(%s) ORDER BY asin",
            (eid, asins)).fetchall()
    ]
    movie_reviews = conn.execute(
        "SELECT earliest_review_time, earliest_precision, earliest_source_review_id, earliest_asin, "
        "second_review_time, review_count FROM task6.movie_review_time WHERE extraction_id = %s "
        "AND t4_mapping_version_id = %s AND movie_id = %s", (eid, t4, movie_id)).fetchone()

    return {
        "release_version_id": rv, "movie_id": movie_id, "display_title": title, "tconst": tconst, "found": True,
        "selection": {
            "status": sel[0], "missing_reason": sel[1], "release_year": sel[2], "precision": sel[3],
            "selected_candidate_id": sel[4], "selection_rule": sel[5], "reason": sel[6],
            "confidence": float(sel[7]) if sel[7] else None, "disagreement": dict(sel[8]) if sel[8] else {},
        } if sel else None,
        "candidates": candidates,
        "cooccur": {
            "result": {"outcome": cooccur_res[0], "chosen_tconst": cooccur_res[1],
                       "passed_tconsts": list(cooccur_res[2] or []), "owner_movie_id": cooccur_res[3]}
            if cooccur_res else None,
            "scores": cooccur_scores,
        },
        "cooccur_score": cooccur_scores,
        "review_year_mentions": mentions,
        "bound": {
            "bound_time": bound[0].isoformat() if bound else None, "precision": bound[1] if bound else None,
            "bound_kind": bound[2] if bound else None, "chosen_evidence": bound[3] if bound else None,
            "confidence": float(bound[4]) if bound and bound[4] else None, "status": bound[5] if bound else None,
            "basis": dict(bound[7]) if bound and bound[7] else {},
        } if bound else None,
        "bound_assessment": {
            "has_review_evidence": assessment[0], "has_media_evidence": assessment[1],
            "passed_evidence": list(assessment[2] or []), "chosen_evidence": assessment[3],
            "evidence_details": evidence_rows,
        } if assessment else None,
        "edition_asins": asins,
        "product_release_dates": product_dates,
        "product_review_time": product_review_times,
        "product_reviews": product_review_times,
        "movie_reviews": {
            "earliest_time": movie_reviews[0].isoformat() if movie_reviews and movie_reviews[0] else None,
            "precision": movie_reviews[1] if movie_reviews else None,
            "earliest_source_review_id": movie_reviews[2] if movie_reviews else None,
            "earliest_asin": movie_reviews[3] if movie_reviews else None,
            "second_time": movie_reviews[4].isoformat() if movie_reviews and movie_reviews[4] else None,
            "review_count": movie_reviews[5] if movie_reviews else 0,
        } if movie_reviews else None,
    }


def query_gaps(conn: psycopg.Connection, rv: str, status_filter: str | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
    """列出缺失或包含推断上界的作品（作业第十部分查询之三）。"""
    row = rv_row(conn, rv)
    t4 = row["t4_mapping_version_id"]
    conditions = []
    params: list[Any] = [t4, rv]

    if status_filter == "missing":
        conditions.append("s.status = 'missing'")
    elif status_filter == "inferred":
        conditions.append("b.bound_time IS NOT NULL")
    else:
        conditions.append("(s.status = 'missing' OR b.bound_time IS NOT NULL)")

    where = " AND ".join(conditions)
    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT s.movie_id, m.display_title, s.status, s.missing_reason, s.release_year,
               b.bound_time, b.bound_precision, b.chosen_evidence, b.confidence
        FROM task6.release_selection s
        JOIN task4.movie m ON m.mapping_version_id = %s AND m.movie_id = s.movie_id
        LEFT JOIN task6.inferred_release_bound b ON b.release_version_id = s.release_version_id
             AND b.movie_id = s.movie_id
        WHERE s.release_version_id = %s AND {where}
        ORDER BY s.status, s.movie_id{limit_clause}
    """
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "movie_id": r[0], "display_title": r[1], "status": r[2], "missing_reason": r[3],
            "release_year": r[4],
            "inferred_bound": {
                "bound_time": r[5].isoformat() if r[5] else None, "precision": r[6],
                "chosen_evidence": r[7], "confidence": float(r[8]) if r[8] else None,
            } if r[5] else None,
        }
        for r in rows
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 6 只读查询")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_m = sub.add_parser("movie", help="查询单个作品的上映信息")
    p_m.add_argument("movie_id", help="movie_id")
    p_m.add_argument("--rv", help="上映信息版本号")
    p_m.add_argument("--extraction-id", help="抽取 ID")

    p_g = sub.add_parser("gaps", help="查询缺失与推断上映信息")
    p_g.add_argument("--status", choices=["missing", "inferred"], help="过滤状态")
    p_g.add_argument("--limit", type=int, default=None, help="最大返回条数")
    p_g.add_argument("--rv", help="上映信息版本号")
    p_g.add_argument("--extraction-id", help="抽取 ID")

    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = getattr(args, "extraction_id", None) or latest_full_extraction(conn)
        rv = getattr(args, "rv", None) or latest_rv(conn, eid)
        if args.cmd == "movie":
            res = query_movie(conn, rv, args.movie_id)
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "gaps":
            res = query_gaps(conn, rv, args.status, getattr(args, "limit", None))
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
