"""任务 7 的三个只读查询（spec R59，AC14）。

    python -m etl_task7.query movie <movie_id>                       # 字段值、状态、规则理由、全部候选与来源、版本摘要、关联评估
    python -m etl_task7.query edition <asin>                         # 版本属性、内部冲突、所属作品、评论统计
    python -m etl_task7.query conflicts [--type T] [--field F] [--layer movie|edition] [--limit N]   # 按冲突类型或字段列出冲突
三者都可加 --cv（默认抽取的最新冲突消解版本）与 --extraction-id。输出一段 JSON，不写任何表。查询限定版本登记的阶段键，不串版本。
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

from .check import cv_row, latest_cv
from .report import _cands

SEL_COLS = ("field_name", "status", "winning_value", "selected_candidate_ids", "considered_candidate_ids",
            "conflict_type", "selection_rule", "reason", "confidence", "disagreement_detail")


def _selection(r: tuple) -> dict[str, Any]:
    d = dict(zip(SEL_COLS, r))
    d["confidence"] = float(d["confidence"])
    d["selected_candidate_ids"] = list(d["selected_candidate_ids"])
    d["considered_candidate_ids"] = list(d["considered_candidate_ids"])
    d["disagreement_detail"] = dict(d["disagreement_detail"])
    return d


def query_movie(conn: psycopg.Connection, cv: str, movie_id: str) -> dict[str, Any]:
    row = cv_row(conn, cv)
    eid = row["extraction_id"]
    ekey, lkey, skey, mkey = (row["edition_stage_key"], row["link_stage_key"], row["scalar_stage_key"],
                              row["multivalue_stage_key"])
    res = conn.execute(
        "SELECT title, original_title, release_year, release_precision, runtime_minutes, mpaa_rating, effective_tconst, "
        "tconst_source, cross_source_mismatch_halt, cast_coverage, inferred_upper_bound, inferred_bound_precision, "
        "inferred_bound_evidence, genre_count, director_count, starring_count, cast_count FROM task7.movie_resolved "
        "WHERE conflict_version_id = %s AND movie_id = %s", (cv, movie_id)).fetchone()
    if res is None:
        return {"conflict_version_id": cv, "movie_id": movie_id, "found": False}
    cols = ("title", "original_title", "release_year", "release_precision", "runtime_minutes", "mpaa_rating",
            "effective_tconst", "tconst_source", "cross_source_mismatch_halt", "cast_coverage", "inferred_upper_bound",
            "inferred_bound_precision", "inferred_bound_evidence", "genre_count", "director_count", "starring_count",
            "cast_count")
    resolved = dict(zip(cols, res))
    if resolved["inferred_upper_bound"]:
        resolved["inferred_upper_bound"] = resolved["inferred_upper_bound"].isoformat()
    fields: dict[str, Any] = {}
    for r in conn.execute(f"SELECT {', '.join(SEL_COLS)} FROM task7.movie_field_selection WHERE conflict_version_id = %s "
                          "AND movie_id = %s ORDER BY field_name", (cv, movie_id)):
        sel = _selection(r)
        name = sel["field_name"]
        if name in ("title", "original_title", "release_year", "runtime_minutes", "mpaa_rating"):
            sel["candidates"] = _cands(conn, eid, skey, movie_id, name)      # 全部候选与来源，含未采用的
        fields[name] = sel
    link = conn.execute(
        "SELECT effective_tconst, tconst_source, base_reliability, is_weak, sig_year, sig_runtime, sig_director, "
        "sig_title, cross_source_mismatch_halt, halt_reason, signal_detail FROM task7.link_assessment "
        "WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s", (eid, lkey, movie_id)).fetchone()
    link_d = None
    if link:
        link_d = dict(zip(("tconst", "source", "base_reliability", "is_weak", "Y", "RT", "D", "T", "halt", "halt_reason",
                           "signal_detail"), link))
        link_d["base_reliability"] = float(link_d["base_reliability"])
    return {
        "conflict_version_id": cv, "movie_id": movie_id, "found": True, "movie": resolved, "fields": fields,
        "link_assessment": link_d,
        "genres": [{"genre": g, "source_coverage": c, "raw_terms": raw, "source_ref": dict(ref)}
                   for g, c, raw, ref in conn.execute(
                       "SELECT genre, source_coverage, raw_terms, source_ref FROM task7.movie_genre WHERE extraction_id = %s "
                       "AND stage_key = %s AND movie_id = %s ORDER BY genre", (eid, mkey, movie_id))],
        "people": [{"person_id": pid, "role": role, "source_coverage": cov, "status": st, "credit_order": order,
                    "imdb_ordering": io_, "nconst": nc, "raw_names": raw, "resolution_note": dict(note)}
                   for pid, role, cov, st, order, io_, nc, raw, note in conn.execute(
                       "SELECT person_id, role, source_coverage, status, credit_order, imdb_ordering, nconst, raw_names, "
                       "resolution_note FROM task7.movie_person_resolved WHERE extraction_id = %s AND stage_key = %s "
                       "AND movie_id = %s ORDER BY role, credit_order NULLS LAST, person_id", (eid, mkey, movie_id))],
        "credit_alignment": [dict(zip(("role", "page_person_id", "raw_name", "segment", "resolved_person_id", "level",
                                       "outcome"), r)) for r in conn.execute(
            "SELECT role, page_person_id, raw_name, segment, resolved_person_id, level, outcome FROM "
            "task7.credit_alignment WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s "
            "ORDER BY role, page_person_id, segment", (eid, mkey, movie_id))],
        "runtime_diff": [dict(zip(("asin", "page_runtime", "imdb_runtime", "delta", "diff_class", "pal_formula_match",
                                   "has_pal_evidence"), r)) for r in conn.execute(
            "SELECT asin, page_runtime, imdb_runtime, delta, diff_class, pal_formula_match, has_pal_evidence "
            "FROM task7.runtime_diff WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s ORDER BY asin",
            (eid, skey, movie_id))],
        "editions": [dict(zip(("asin", "product_kind", "edition_title", "runtime_minutes", "mpaa_rating", "publisher",
                               "product_release_date", "discs", "media_format", "language", "subtitles", "aspect_ratio",
                               "region", "cut_type", "video_system"), r)) | {"relation_type": rel}
                     for *r, rel in conn.execute(
                         "SELECT e.asin, e.product_kind, e.edition_title, e.runtime_minutes, e.mpaa_rating, e.publisher, "
                         "e.product_release_date::text, e.discs, e.media_format, e.language, e.subtitles, e.aspect_ratio, "
                         "e.region, e.cut_type, e.video_system, pm.relation_type FROM task7.edition_resolved e "
                         "JOIN task4.product_movie pm ON pm.mapping_version_id = %s AND pm.asin = e.asin AND pm.movie_id = %s "
                         "WHERE e.conflict_version_id = %s ORDER BY e.asin, pm.member_ordinal",
                         (row["t4_mapping_version_id"], movie_id, cv))],
        "aliases": {k: n for k, n in conn.execute(
            "SELECT alias_kind, count(*) FROM task7.movie_title_alias WHERE extraction_id = %s AND stage_key = %s "
            "AND movie_id = %s GROUP BY 1 ORDER BY 1", (eid, skey, movie_id))},
        "shared_tconst": [dict(zip(("tconst", "other_movie_id", "link_class"), r)) for r in conn.execute(
            "SELECT tconst, other_movie_id, link_class FROM task7.shared_tconst_conflict WHERE conflict_version_id = %s "
            "AND (movie_id = %s OR other_movie_id = %s) ORDER BY tconst", (cv, movie_id, movie_id))],
        "quality_issues": [{"issue_code": c, "stage_key": k, "detail": dict(d)} for c, k, d in conn.execute(
            "SELECT issue_code, stage_key, detail FROM task7.quality_issue WHERE extraction_id = %s AND entity_id = %s "
            "AND stage_key IN (%s, %s, %s) ORDER BY issue_id", (eid, movie_id, lkey, skey, mkey))],
    }


def query_edition(conn: psycopg.Connection, cv: str, asin: str) -> dict[str, Any]:
    row = cv_row(conn, cv)
    eid, ekey = row["extraction_id"], row["edition_stage_key"]
    cols = ("asin", "product_kind", "movie_ids", "edition_title", "edition_clean_title", "runtime_minutes", "mpaa_rating",
            "publisher", "product_release_date", "media_release_date", "listing_first_available_date", "date_flags", "discs",
            "media_format", "language", "subtitles", "aspect_ratio", "region", "cut_type", "video_system",
            "attributes_detail")
    r = conn.execute(f"SELECT {', '.join(cols)} FROM task7.edition_resolved WHERE conflict_version_id = %s AND asin = %s",
                     (cv, asin)).fetchone()
    if r is None:
        return {"conflict_version_id": cv, "asin": asin, "found": False}
    attrs = dict(zip(cols, r))
    for k in ("product_release_date", "media_release_date", "listing_first_available_date"):
        attrs[k] = attrs[k].isoformat() if attrs[k] else None
    fields = {}
    for sr in conn.execute(f"SELECT {', '.join(SEL_COLS)} FROM task7.edition_field_selection WHERE conflict_version_id = %s "
                           "AND asin = %s ORDER BY field_name", (cv, asin)):
        sel = _selection(sr)
        sel["candidates"] = _cands(conn, eid, ekey, asin, sel["field_name"])
        fields[sel["field_name"]] = sel
    return {
        "conflict_version_id": cv, "asin": asin, "found": True, "edition": attrs, "fields": fields,
        "internal_conflicts": {f: {"conflict_type": s["conflict_type"], "reason": s["reason"],
                                   "detail": s["disagreement_detail"]}
                               for f, s in fields.items() if s["conflict_type"] != "none"},
        "movies": [{"movie_id": mid, "title": title, "relation_type": rel, "member_ordinal": ordinal}
                   for mid, title, rel, ordinal in conn.execute(
                       "SELECT pm.movie_id, r.title, pm.relation_type, pm.member_ordinal FROM task4.product_movie pm "
                       "LEFT JOIN task7.movie_resolved r ON r.conflict_version_id = %s AND r.movie_id = pm.movie_id "
                       "WHERE pm.mapping_version_id = %s AND pm.asin = %s ORDER BY pm.member_ordinal, pm.movie_id",
                       (cv, row["t4_mapping_version_id"], asin))],
        "review_stats": {"by_classification": {k: n for k, n in conn.execute(
            "SELECT classification, count(*) FROM public.review_record WHERE extraction_id = %s AND product_id_observed = %s "
            "GROUP BY 1 ORDER BY 1", (eid, asin))}},
    }


def query_conflicts(conn: psycopg.Connection, cv: str, *, conflict_type: str | None = None, field: str | None = None,
                    layer: str | None = None, limit: int = 50) -> dict[str, Any]:
    cv_row(conn, cv)
    items: list[dict[str, Any]] = []
    for lyr, table, entity in (("movie", "movie_field_selection", "movie_id"), ("edition", "edition_field_selection", "asin")):
        if layer and layer != lyr:
            continue
        sql = (f"SELECT {entity}, field_name, status, winning_value, conflict_type, selection_rule, reason, confidence, "
               f"disagreement_detail, considered_candidate_ids FROM task7.{table} WHERE conflict_version_id = %s "
               "AND conflict_type <> 'none'")
        params: list[Any] = [cv]
        if conflict_type:
            sql += " AND conflict_type = %s"
            params.append(conflict_type)
        if field:
            sql += " AND field_name = %s"
            params.append(field)
        sql += f" ORDER BY conflict_type, {entity}, field_name LIMIT %s"
        params.append(limit)
        for ent, f, st, val, ct, rule, reason, conf, detail, cids in conn.execute(sql, params):
            items.append({"layer": lyr, "entity_id": ent, "field": f, "status": st, "winning_value": val,
                          "conflict_type": ct, "rule": rule, "reason": reason, "confidence": float(conf),
                          "detail": dict(detail), "considered_candidate_ids": list(cids)})
    counts = {f"{lyr}:{ct}": n for lyr, table in (("movie", "movie_field_selection"), ("edition", "edition_field_selection"))
              if not layer or layer == lyr for ct, n in conn.execute(
                  f"SELECT conflict_type, count(*) FROM task7.{table} WHERE conflict_version_id = %s "
                  "AND conflict_type <> 'none' GROUP BY 1 ORDER BY 1", (cv,))}
    items = items[:limit]
    return {"conflict_version_id": cv, "filters": {"type": conflict_type, "field": field, "layer": layer},
            "counts_by_type": counts, "returned": len(items), "conflicts": items}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 7 只读查询")
    parser.add_argument("--cv", help="冲突消解版本号（默认最近一次）")
    parser.add_argument("--extraction-id", help="抽取 ID")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("movie").add_argument("movie_id")
    sub.add_parser("edition").add_argument("asin")
    p = sub.add_parser("conflicts")
    p.add_argument("--type", dest="conflict_type")
    p.add_argument("--field")
    p.add_argument("--layer", choices=("movie", "edition"))
    p.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        conn.execute("SET default_transaction_read_only = on")
        eid = args.extraction_id or latest_full_extraction(conn)
        cv = args.cv or latest_cv(conn, eid)
        if args.cmd == "movie":
            out = query_movie(conn, cv, args.movie_id)
        elif args.cmd == "edition":
            out = query_edition(conn, cv, args.asin)
        else:
            out = query_conflicts(conn, cv, conflict_type=args.conflict_type, field=args.field, layer=args.layer,
                                  limit=args.limit)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
