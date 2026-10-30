"""推断上界（spec R21–R26）：对每个有评论或有商品发售日期的作品评估两种证据，成立的取较早者另存。

评论证据条件 B1–B5、发售日期证据条件 M1–M4 的判定与置信度分档见 rules.py 与 design。
B4 / M4 不成立时写质量问题 bound_before_release_year（只记录，不改任务 4）。推断上界不写进选择记录、
不改变 release_year 与状态（R24）。R26 的预售、合集、跨版本计数在报告里计算（report.py）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import sources
from .candidates import World
from .rules import (MediaEvidence, ReviewEvidence, RuleSet, check_media_evidence, check_review_evidence,
                    evidence_confidence, merge_bounds)
from .run import RunContext, add_quality_issue, copy_rows

EVIDENCE_COLUMNS = ("release_version_id", "movie_id", "evidence", "evidence_time", "evidence_precision", "basis",
                    "checks", "passed", "failed_checks", "confidence")
ASSESS_COLUMNS = ("release_version_id", "movie_id", "has_review_evidence", "has_media_evidence", "passed_evidence",
                  "chosen_evidence")
BOUND_COLUMNS = ("release_version_id", "movie_id", "bound_time", "bound_precision", "bound_kind", "chosen_evidence",
                 "basis", "conditions", "confidence", "status", "rule_version")


def build_bounds(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *, before_commit=None) -> dict[str, Any]:
    """全局步骤，一个事务：评估、合并、另存与质量问题。"""
    i = ctx.inputs
    rv = conn.execute("SELECT release_version_id FROM task6.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0]
    world = World(conn, ctx)
    if rv is None:
        raise RuntimeError("择优步骤尚未生成上映信息版本，不能评估推断上界")
    review_min, review_max = sources.review_time_range(conn, i.extraction_id)
    review_pending = sources.review_pending_products(conn, i.t4_mapping_version_id)

    adopted: dict[str, int | None] = {}
    for movie, status, year in conn.execute(
            "SELECT movie_id, status, release_year FROM task6.release_selection WHERE release_version_id = %s "
            "AND status IN ('verified', 'hint_matched', 'cooccur_matched')", (rv,)):
        adopted[movie] = year        # B4 / M4：已采用的 IMDb 来源年份（page_reported 的标题年份不参与比较）
    reviews: dict[str, tuple] = {}
    for movie, earliest, rid, asin, second, count, reviewed, editions in conn.execute(
            "SELECT movie_id, earliest_review_time, earliest_source_review_id, earliest_asin, second_review_time, "
            "review_count, reviewed_asin_count, edition_asin_count FROM task6.movie_review_time "
            "WHERE extraction_id = %s AND t4_mapping_version_id = %s", (i.extraction_id, i.t4_mapping_version_id)):
        reviews[movie] = (earliest, rid, asin, second, count, reviewed, editions)
    media: dict[str, tuple] = {}
    all_media_movies: set[str] = set()
    for movie, asin, value, precision_, flags, assertion_id, kind in conn.execute(
            "SELECT pm.movie_id, d.asin, d.value_date, d.precision, d.flags, d.assertion_id, d.kind "
            "FROM task6.product_release_date d JOIN task4.product_movie pm ON pm.mapping_version_id = %s "
            "AND pm.asin = d.asin AND pm.member_ordinal = 0 AND pm.relation_type = 'edition' "
            "WHERE d.extraction_id = %s AND d.parser_version = %s "
            "ORDER BY d.value_date, d.assertion_id", (i.t4_mapping_version_id, i.extraction_id, i.parser_version)):
        all_media_movies.add(movie)
        if kind == "media_release":
            media.setdefault(movie, []).append((asin, value, precision_, list(flags or []), assertion_id))
    # 最早评论后 365 天内的评论数（评论证据的置信度分档）
    dense: dict[str, int] = {}
    for movie, n in conn.execute(
            "SELECT pm.movie_id, count(*) FROM review_staging rs JOIN task4.product_movie pm ON pm.mapping_version_id = %s "
            "AND pm.asin = rs.product_id AND pm.member_ordinal = 0 AND pm.relation_type = 'edition' "
            "JOIN task6.movie_review_time t ON t.extraction_id = rs.extraction_id "
            "AND t.t4_mapping_version_id = pm.mapping_version_id AND t.movie_id = pm.movie_id "
            "WHERE rs.extraction_id = %s AND rs.review_time_utc BETWEEN t.earliest_review_time "
            "AND t.earliest_review_time + make_interval(days => %s) GROUP BY 1",
            (i.t4_mapping_version_id, i.extraction_id, rules.b3_days)):
        dense[movie] = n

    evidence_rows: list[tuple] = []
    assessment_rows: list[tuple] = []
    bound_rows: list[tuple] = []
    counts: dict[str, Any] = {"assessments": 0, "review_passed": 0, "media_passed": 0, "bounds": 0,
                              "by_evidence": {}, "bound_before_release_year": 0, "failed_first": {}}
    with conn.transaction():
        # 同一上映信息版本重跑（Airflow 步骤重试）时先删后写（确定性重算）
        conn.execute("DELETE FROM task6.bound_evidence WHERE release_version_id = %s", (rv,))
        conn.execute("DELETE FROM task6.bound_assessment WHERE release_version_id = %s", (rv,))
        conn.execute("DELETE FROM task6.inferred_release_bound WHERE release_version_id = %s", (rv,))
        for movie in sorted(set(reviews) | all_media_movies):
            passed: list[tuple[str, str, str]] = []
            has_review = movie in reviews
            has_media = movie in media and bool(media[movie])
            if has_review:
                earliest, rid, asin, second, _count, _rev, editions = reviews[movie]
                pending = bool(world.edition_asins.get(movie, set()) & review_pending)
                checks = check_review_evidence(
                    ReviewEvidence(earliest.date().isoformat(), earliest.year,
                                   second.date().isoformat() if second else None, _count,
                                   pending, editions > 0, adopted.get(movie), review_min, review_max), rules)
                failed = [k for k, ok in checks.items() if not ok]
                conf = evidence_confidence("review", checks, rules, review_dense=dense.get(movie, 0) >= 3)
                if failed:
                    counts["failed_first"][failed[0]] = counts["failed_first"].get(failed[0], 0) + 1
                    if "B4" in failed:
                        counts["bound_before_release_year"] += 1
                        add_quality_issue(conn, ctx, movie, "bound_before_release_year",
                                          {"evidence": "review", "earliest": earliest.date().isoformat(),
                                           "adopted_year": adopted.get(movie)})
                else:
                    passed.append(("review", earliest.date().isoformat(), "day"))
                    counts["review_passed"] += 1
                evidence_rows.append((rv, movie, "review", earliest.date(), "day",
                                      Jsonb({"source_review_id": rid, "asin": asin}), Jsonb(checks), not failed,
                                      failed, conf))
            if has_media:
                asin, value, precision_, flags, assertion = sorted(media[movie], key=lambda x: (x[1], x[4]))[0]
                jan = "jan_first_suspect" in flags
                checks = check_media_evidence(
                    MediaEvidence(value.isoformat(), value.year, jan,
                                  bool(world.edition_asins.get(movie, set()) & review_pending),
                                  adopted.get(movie)))
                failed = [k for k, ok in checks.items() if not ok]
                conf = evidence_confidence("media_release", checks, rules, jan_first_suspect=jan)
                if failed:
                    counts["failed_first"][failed[0]] = counts["failed_first"].get(failed[0], 0) + 1
                    if "M4" in failed:
                        counts["bound_before_release_year"] += 1
                        add_quality_issue(conn, ctx, movie, "bound_before_release_year",
                                          {"evidence": "media_release", "date": value.isoformat(),
                                           "adopted_year": adopted.get(movie)})
                else:
                    passed.append(("media_release", value.isoformat(), "year" if jan else "day"))
                    counts["media_passed"] += 1
                evidence_rows.append((rv, movie, "media_release", value, "year" if jan else "day",
                                      Jsonb({"assertion_id": assertion, "asin": asin}), Jsonb(checks), not failed,
                                      failed, conf))
            chosen = merge_bounds(passed)
            assessment_rows.append((rv, movie, has_review, has_media, [p[0] for p in passed],
                                    chosen[0] if chosen else None))
            counts["assessments"] += 1
            if chosen:
                evidence, when, precision = chosen
                basis = ("最早评论" if evidence == "review" else "最早的 media_release 商品发售日期")
                row = next(r for r in evidence_rows if r[2] == evidence and r[1] == movie)
                bound_rows.append((rv, movie, date.fromisoformat(when), precision, "not_later_than", evidence,
                                   Jsonb({"note": basis, "evidence_time": when}), row[6], row[9],
                                   "inferred", rules.version))
                counts["by_evidence"][evidence] = counts["by_evidence"].get(evidence, 0) + 1
                counts["bounds"] += 1
        copy_rows(conn, "task6.bound_evidence", EVIDENCE_COLUMNS, evidence_rows)
        copy_rows(conn, "task6.bound_assessment", ASSESS_COLUMNS, assessment_rows)
        copy_rows(conn, "task6.inferred_release_bound", BOUND_COLUMNS, bound_rows)
        conn.execute("UPDATE task6.release_version SET counts = counts || jsonb_build_object('bounds', %s::jsonb) "
                     "WHERE release_version_id = %s", (Jsonb(counts), rv))
    return counts
