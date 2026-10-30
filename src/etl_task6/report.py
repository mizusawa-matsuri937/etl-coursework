"""任务 6 统计、导出、实例与版本差异（spec R33、R35，AC13、AC16）。

口径见 design“统计与报告”“实例”。全部查询限定一个上映信息版本；导出不含评论正文与整页 HTML。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from .check import latest_rv, rv_row

SUMMARY_VERSION = "task6-release-summary-1"
ALL_PRODUCTS_TOTAL = 253059


def output_root(output_dir: Path) -> Path:
    return output_dir / "task6"


def run_dir(output_dir: Path, eid: str, rv: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / rv / run_key


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 6) if d else None


def build_summary(conn: psycopg.Connection, rv: str) -> dict[str, Any]:
    """汇总上映信息版本的全貌统计（R33）。"""
    row = rv_row(conn, rv)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]
    pv = row["parser_version"]
    def one(sql: str, *a: Any) -> Any:
        params: tuple | list
        if len(a) == 1 and isinstance(a[0], (tuple, list)):
            params = a[0]
        else:
            params = a
        return conn.execute(sql, params).fetchone()[0]

    # 作品与状态
    edition_movies = one(
        "SELECT count(DISTINCT movie_id) FROM task4.product_movie WHERE mapping_version_id = %s "
        "AND relation_type = 'edition'", t4)
    contains_only_movies = one(
        "SELECT count(*) FROM task4.movie m WHERE m.mapping_version_id = %s AND NOT EXISTS ("
        "SELECT 1 FROM task4.product_movie pm WHERE pm.mapping_version_id = m.mapping_version_id "
        "AND pm.movie_id = m.movie_id AND pm.relation_type = 'edition')", t4)
    total_movies = edition_movies + contains_only_movies

    status_counts = dict(conn.execute(
        "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    missing_reasons = dict(conn.execute(
        "SELECT missing_reason, count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "AND status = 'missing' GROUP BY 1 ORDER BY 1", (rv,)).fetchall())

    # 覆盖率
    works_with_year = sum(v for k, v in status_counts.items() if k != "missing")
    matched_products_total = one(
        "SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s "
        "AND match_scope = 'matched'", eid)
    matched_products_with_year = one(
        "SELECT count(DISTINCT pm.asin) FROM task4.product_movie pm JOIN task6.release_selection s "
        "ON s.release_version_id = %s AND s.movie_id = pm.movie_id WHERE pm.mapping_version_id = %s "
        "AND pm.relation_type = 'edition' AND s.status != 'missing'", rv, t4)

    # 候选
    candidate_counts = dict(conn.execute(
        "SELECT source, count(*) FROM task6.release_candidate WHERE extraction_id = %s "
        "GROUP BY 1 ORDER BY 1", (eid,)).fetchall())
    title_types = dict(conn.execute(
        "SELECT source_ref->>'titleType', count(*) FROM task6.release_candidate "
        "WHERE extraction_id = %s AND source = 'imdb_t4_link' GROUP BY 1 ORDER BY 1", (eid,)).fetchall())

    # 线索处理
    hint_actions = dict(conn.execute(
        "SELECT action, count(*) FROM task6.hint_use WHERE extraction_id = %s "
        "GROUP BY 1 ORDER BY 1", (eid,)).fetchall())
    shared_hints = [
        {"asin": r[0], "tconst": r[1], "owner_movie_id": r[2]}
        for r in conn.execute(
            "SELECT asin, tconst, owner_movie_id FROM task6.hint_use WHERE extraction_id = %s "
            "AND action = 'candidate_shared' ORDER BY asin LIMIT 20", (eid,)).fetchall()
    ]

    # 共现反查
    cooccur_outcomes = dict(conn.execute(
        "SELECT outcome, count(*) FROM task6.cooccur_result WHERE extraction_id = %s "
        "GROUP BY 1 ORDER BY 1", (eid,)).fetchall())
    shared_cooccur_count = one(
        "SELECT count(*) FROM task6.cooccur_result WHERE extraction_id = %s "
        "AND owner_movie_id IS NOT NULL", eid)
    title_guard_excluded = one(
        "SELECT count(DISTINCT movie_id) FROM task6.cooccur_score WHERE extraction_id = %s "
        "AND title_ok IS FALSE", eid)
    number_guard_excluded = one(
        "SELECT count(DISTINCT movie_id) FROM task6.cooccur_score WHERE extraction_id = %s "
        "AND number_guard IS FALSE", eid)

    # 评论提及
    review_mentions_movies = one(
        "SELECT count(DISTINCT movie_id) FROM task6.review_year_mention WHERE extraction_id = %s", eid)

    # 商品发售日期
    dates_by_kind = dict(conn.execute(
        "SELECT kind, count(*) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s GROUP BY 1 ORDER BY 1", (eid, pv)).fetchall())
    jan_first_suspect = one(
        "SELECT count(*) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s AND 'jan_first_suspect' = ANY(flags)", eid, pv)
    after_review_period = one(
        "SELECT count(*) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s AND 'after_review_period' = ANY(flags)", eid, pv)
    products_with_dates = one(
        "SELECT count(DISTINCT asin) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s", eid, pv)

    # 评论时间与上界
    movies_with_review = one(
        "SELECT count(*) FROM task6.movie_review_time WHERE extraction_id = %s "
        "AND t4_mapping_version_id = %s", eid, t4)
    assessments_count = one(
        "SELECT count(*) FROM task6.bound_assessment WHERE release_version_id = %s", rv)
    bounds_by_evidence = dict(conn.execute(
        "SELECT chosen_evidence, count(*) FROM task6.inferred_release_bound WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    bounds_by_confidence = dict(conn.execute(
        "SELECT round(confidence::numeric, 2)::text, count(*) FROM task6.inferred_release_bound "
        "WHERE release_version_id = %s GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    bounds_for_missing = one(
        "SELECT count(*) FROM task6.inferred_release_bound b JOIN task6.release_selection s "
        "ON s.release_version_id = b.release_version_id AND s.movie_id = b.movie_id "
        "WHERE b.release_version_id = %s AND s.status = 'missing'", rv)
    bounds_before_year = dict(conn.execute(
        "SELECT detail->>'evidence', count(*) FROM task6.quality_issue "
        "WHERE issue_code = 'bound_before_release_year' AND extraction_id = %s "
        "GROUP BY 1 ORDER BY 1", (eid,)).fetchall())

    # 证据未通过条件
    failed_checks = dict(conn.execute(
        "SELECT unnest(failed_checks), count(*) FROM task6.bound_evidence WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())

    # R26 预售、合集、跨版本
    collection_reviews = one(
        "SELECT coalesce(sum(review_count), 0) FROM task6.product_review_time WHERE extraction_id = %s "
        "AND product_kind = 'collection'", eid)
    multi_edition_movies = one(
        "SELECT count(*) FROM (SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s "
        "AND relation_type = 'edition' GROUP BY movie_id HAVING count(*) > 1) s", t4)
    earliest_review_before_all_media = one(
        "SELECT count(*) FROM task6.movie_review_time t WHERE t.extraction_id = %s AND t.t4_mapping_version_id = %s "
        "AND t.earliest_review_time IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM task6.product_release_date d JOIN task4.product_movie pm ON pm.mapping_version_id = t.t4_mapping_version_id "
        "AND pm.asin = d.asin AND pm.member_ordinal = 0 AND pm.relation_type = 'edition' "
        "WHERE d.extraction_id = t.extraction_id AND pm.movie_id = t.movie_id AND d.kind = 'media_release' "
        "AND d.value_date <= t.earliest_review_time::date)", eid, t4)

    return {
        "summary_version": SUMMARY_VERSION,
        "release_version_id": rv,
        "extraction_id": eid,
        "t4_mapping_version_id": t4,
        "movies": {
            "total": total_movies,
            "edition_movies": edition_movies,
            "contains_only_movies": contains_only_movies,
            "by_status": status_counts,
            "missing_reasons": missing_reasons,
        },
        "coverage": {
            "works_with_year": works_with_year,
            "works_total": total_movies,
            "works_coverage": _ratio(works_with_year, total_movies),
            "matched_products_with_year": matched_products_with_year,
            "matched_products_total": matched_products_total,
            "matched_products_coverage": _ratio(matched_products_with_year, matched_products_total),
            "all_products_with_year": matched_products_with_year,
            "all_products_total": ALL_PRODUCTS_TOTAL,
            "all_products_coverage": _ratio(matched_products_with_year, ALL_PRODUCTS_TOTAL),
        },
        "candidates": {
            "by_source": candidate_counts,
            "title_types": title_types,
            "year_disagreement": one(
                "SELECT count(*) FROM task6.quality_issue WHERE issue_code = 'year_disagreement' "
                "AND extraction_id = %s", eid),
            "tconst_disagreement": one(
                "SELECT count(*) FROM task6.quality_issue WHERE issue_code = 'tconst_disagreement' "
                "AND extraction_id = %s", eid),
        },
        "hint_use": {
            "actions": hint_actions,
            "candidate_shared_count": len(shared_hints),
            "candidate_shared_top": shared_hints,
        },
        "cooccur": {
            "outcomes": cooccur_outcomes,
            "shared_tconst_count": shared_cooccur_count,
            "title_guard_excluded": title_guard_excluded,
            "number_guard_excluded": number_guard_excluded,
        },
        "review_years": {
            "movies_with_mentions": review_mentions_movies,
        },
        "regional_dates": {
            "coverage": 0,
            "missing_reason": "no_permitted_dated_source",
        },
        "product_dates": {
            "by_kind": dates_by_kind,
            "jan_first_suspect": jan_first_suspect,
            "after_review_period": after_review_period,
            "products_with_dates": products_with_dates,
        },
        "bounds": {
            "movies_with_review": movies_with_review,
            "review_coverage": _ratio(movies_with_review, total_movies),
            "assessments_total": assessments_count,
            "by_evidence": bounds_by_evidence,
            "by_confidence": bounds_by_confidence,
            "bounds_for_missing": bounds_for_missing,
            "failed_checks": failed_checks,
            "bounds_before_year": bounds_before_year,
        },
        "presale_collection_cross_edition": {
            "collection_reviews": collection_reviews,
            "multi_edition_movies": multi_edition_movies,
            "earliest_review_before_all_media": earliest_review_before_all_media,
            "note": "数据中没有预售标记，不宣称已排除预售",
        },
    }


def examples(conn: psycopg.Connection, rv: str) -> dict[str, list[dict[str, Any]]]:
    """生成 R35 要求的各类实例。"""
    row = rv_row(conn, rv)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]

    def _sample_status(st: str, limit: int = 3):
        res = []
        for mid, title, year, reason, cid in conn.execute(
                "SELECT s.movie_id, m.display_title, s.release_year, s.reason, s.selected_candidate_id "
                "FROM task6.release_selection s JOIN task4.movie m ON m.mapping_version_id = %s "
                "AND m.movie_id = s.movie_id WHERE s.release_version_id = %s AND s.status = %s "
                "ORDER BY md5(s.movie_id) LIMIT %s", (t4, rv, st, limit)).fetchall():
            c = conn.execute(
                "SELECT source, source_key, raw_value, source_ref, match_evidence "
                "FROM task6.release_candidate WHERE candidate_id = %s", (cid,)).fetchone() if cid else None
            res.append({
                "movie_id": mid, "title": title, "year": year, "reason": reason,
                "candidate": {
                    "source": c[0], "key": c[1], "raw": c[2], "source_ref": dict(c[3]) if c and c[3] else {},
                    "evidence": dict(c[4]) if c and c[4] else {},
                } if c else None,
            })
        return res

    def _sample_bounds(evidence: str, limit: int = 3):
        res = []
        for mid, title, when, prec, conf, conds, basis in conn.execute(
                "SELECT b.movie_id, m.display_title, b.bound_time, b.bound_precision, b.confidence, "
                "b.conditions, b.basis FROM task6.inferred_release_bound b JOIN task4.movie m "
                "ON m.mapping_version_id = %s AND m.movie_id = b.movie_id WHERE b.release_version_id = %s "
                "AND b.chosen_evidence = %s ORDER BY md5(b.movie_id) LIMIT %s",
                (t4, rv, evidence, limit)).fetchall():
            res.append({
                "movie_id": mid, "title": title, "bound_time": when.isoformat(), "precision": prec,
                "confidence": float(conf), "conditions": dict(conds), "basis": dict(basis),
            })
        return res

    def _sample_failed_evidence(evidence: str, limit: int = 3):
        res = []
        for mid, title, failed, checks in conn.execute(
                "SELECT e.movie_id, m.display_title, e.failed_checks, e.checks "
                "FROM task6.bound_evidence e JOIN task4.movie m ON m.mapping_version_id = %s "
                "AND m.movie_id = e.movie_id WHERE e.release_version_id = %s AND e.evidence = %s "
                "AND e.passed IS FALSE ORDER BY md5(e.movie_id) LIMIT %s",
                (t4, rv, evidence, limit)).fetchall():
            res.append({
                "movie_id": mid, "title": title, "failed_checks": list(failed), "checks": dict(checks),
            })
        return res

    def _sample_coexisting(limit: int = 3):
        res = []
        for mid, title, year, d_asin, d_date, d_kind in conn.execute(
                "SELECT s.movie_id, m.display_title, s.release_year, d.asin, d.value_date, d.kind "
                "FROM task6.release_selection s JOIN task4.movie m ON m.mapping_version_id = %s "
                "AND m.movie_id = s.movie_id JOIN task4.product_movie pm ON pm.mapping_version_id = m.mapping_version_id "
                "AND pm.movie_id = s.movie_id AND pm.member_ordinal = 0 AND pm.relation_type = 'edition' "
                "JOIN task6.product_release_date d ON d.extraction_id = %s AND d.asin = pm.asin "
                "WHERE s.release_version_id = %s AND s.status != 'missing' "
                "ORDER BY md5(s.movie_id) LIMIT %s", (t4, eid, rv, limit)).fetchall():
            res.append({
                "movie_id": mid, "title": title, "release_year": year, "asin": d_asin,
                "product_date": d_date.isoformat(), "kind": d_kind,
            })
        return res

    return {
        "verified": _sample_status("verified"),
        "hint_matched": _sample_status("hint_matched"),
        "cooccur_matched": _sample_status("cooccur_matched"),
        "page_reported": _sample_status("page_reported"),
        "missing": _sample_status("missing"),
        "inferred_review": _sample_bounds("review"),
        "inferred_media": _sample_bounds("media_release"),
        "failed_review": _sample_failed_evidence("review"),
        "failed_media": _sample_failed_evidence("media_release"),
        "coexisting": _sample_coexisting(),
    }


def export_files(conn: psycopg.Connection, rv: str, directory: Path,
                 examples_data: dict[str, Any] | None = None) -> dict[str, str]:
    """导出所有报告文件与数据 CSV，返回 文件名 -> SHA256 哈希。"""
    directory.mkdir(parents=True, exist_ok=True)
    summary_data = build_summary(conn, rv)
    if examples_data is None:
        examples_data = examples(conn, rv)

    files: dict[str, bytes] = {}

    # JSON 报告
    summary_json = json.dumps(summary_data, ensure_ascii=False, indent=2, default=str) + "\n"
    files["summary.json"] = summary_json.encode("utf-8")

    # Markdown 报告
    report_md = render_markdown(summary_data)
    files["report.md"] = report_md.encode("utf-8")

    # Markdown 实例
    examples_md = render_examples_markdown(examples_data)
    files["examples.md"] = examples_md.encode("utf-8")

    # CSV 数据表导出
    def _export_query_csv(sql: str, params: tuple, columns: list[str]) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        for r in conn.execute(sql, params).fetchall():
            writer.writerow([json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x for x in r])
        return buf.getvalue().encode("utf-8")

    files["release_selection.csv"] = _export_query_csv(
        "SELECT movie_id, status, missing_reason, release_year, precision, region, release_type, "
        "regional_release_missing_reason, selected_candidate_id, selection_rule, reason, confidence, disagreement "
        "FROM task6.release_selection WHERE release_version_id = %s ORDER BY movie_id",
        (rv,),
        ["movie_id", "status", "missing_reason", "release_year", "precision", "region", "release_type",
         "regional_release_missing_reason", "selected_candidate_id", "selection_rule", "reason", "confidence", "disagreement"])

    files["release_candidates.csv"] = _export_query_csv(
        "SELECT candidate_id, movie_id, source, source_key, raw_value, value_year, precision, semantic, "
        "flags, shared_with_movie_id, confidence, rule_version FROM task6.release_candidate "
        "WHERE extraction_id = %s ORDER BY candidate_id",
        (summary_data["extraction_id"],),
        ["candidate_id", "movie_id", "source", "source_key", "raw_value", "value_year", "precision", "semantic",
         "flags", "shared_with_movie_id", "confidence", "rule_version"])

    files["cooccur.csv"] = _export_query_csv(
        "SELECT movie_id, outcome, chosen_tconst, passed_tconsts, owner_movie_id "
        "FROM task6.cooccur_result WHERE extraction_id = %s ORDER BY movie_id",
        (summary_data["extraction_id"],),
        ["movie_id", "outcome", "chosen_tconst", "passed_tconsts", "owner_movie_id"])

    files["product_release_dates.csv"] = _export_query_csv(
        "SELECT asin, kind, label_raw, raw_value, value_date, precision, flags, section, page_ordinal "
        "FROM task6.product_release_date WHERE extraction_id = %s ORDER BY asin, assertion_id",
        (summary_data["extraction_id"],),
        ["asin", "kind", "label_raw", "raw_value", "value_date", "precision", "flags", "section", "page_ordinal"])

    files["bounds.csv"] = _export_query_csv(
        "SELECT movie_id, bound_time, bound_precision, bound_kind, chosen_evidence, confidence, status, rule_version "
        "FROM task6.inferred_release_bound WHERE release_version_id = %s ORDER BY movie_id",
        (rv,),
        ["movie_id", "bound_time", "bound_precision", "bound_kind", "chosen_evidence", "confidence", "status", "rule_version"])

    files["hint_use.csv"] = _export_query_csv(
        "SELECT asin, member_ordinal, tconst, movie_id, action, owner_movie_id "
        "FROM task6.hint_use WHERE extraction_id = %s ORDER BY asin, member_ordinal",
        (summary_data["extraction_id"],),
        ["asin", "member_ordinal", "tconst", "movie_id", "action", "owner_movie_id"])

    # 写入文件并记录哈希
    digests: dict[str, str] = {}
    for name, content in files.items():
        reporting.write_atomic(directory / name, content)
        digests[name] = reporting.file_digest(directory / name)

    return digests


def render_markdown(s: dict[str, Any]) -> str:
    m = s["movies"]
    cov = s["coverage"]
    c = s["candidates"]
    b = s["bounds"]
    p = s["presale_collection_cross_edition"]
    return f"""# 任务 6 上映信息报告（{s['release_version_id']}）

- **抽取 ID**：`{s['extraction_id']}`
- **任务 4 映射版本**：`{s['t4_mapping_version_id']}`
- **作品总数**：{m['total']}（经 edition：{m['edition_movies']}，只经 contains：{m['contains_only_movies']}）

## 1. 覆盖率与状态分布

| 状态 | 作品数 | 占比 |
|---|---|---|
| verified | {m['by_status'].get('verified', 0)} | {m['by_status'].get('verified', 0) / m['total']:.2%} |
| hint_matched | {m['by_status'].get('hint_matched', 0)} | {m['by_status'].get('hint_matched', 0) / m['total']:.2%} |
| cooccur_matched | {m['by_status'].get('cooccur_matched', 0)} | {m['by_status'].get('cooccur_matched', 0) / m['total']:.2%} |
| page_reported | {m['by_status'].get('page_reported', 0)} | {m['by_status'].get('page_reported', 0) / m['total']:.2%} |
| missing | {m['by_status'].get('missing', 0)} | {m['by_status'].get('missing', 0) / m['total']:.2%} |

### 缺失原因
{json.dumps(m['missing_reasons'], ensure_ascii=False, indent=2)}

### 覆盖率口径
- 作品覆盖率：{cov['works_with_year']} / {cov['works_total']} ({cov['works_coverage']:.2%})
- 参与匹配商品覆盖率：{cov['matched_products_with_year']} / {cov['matched_products_total']} ({cov['matched_products_coverage']:.2%})
- 全部商品覆盖率：{cov['all_products_with_year']} / {cov['all_products_total']} ({cov['all_products_coverage']:.2%})

## 2. 候选与质量问题
- 候选来源分布：{json.dumps(c['by_source'], ensure_ascii=False)}
- 年份分歧（year_disagreement）：{c['year_disagreement']}
- tconst 分歧（tconst_disagreement）：{c['tconst_disagreement']}

## 3. 推断上界与评论
- 评估作品数：{b['assessments_total']}
- 推断上界总数：{sum(b['by_evidence'].values())}（按证据：{json.dumps(b['by_evidence'], ensure_ascii=False)}）
- 无年份作品中获得推断上界数：{b['bounds_for_missing']}
- 上界早于上映年份（bound_before_release_year）：{json.dumps(b['bounds_before_year'], ensure_ascii=False)}

## 4. 预售、合集与跨版本分析（R26）
- 合集商品评论数（保持在商品层）：{p['collection_reviews']}
- 多版本作品数：{p['multi_edition_movies']}
- 最早评论早于全部 media_release 日期的作品数：{p['earliest_review_before_all_media']}
- 说明：{p['note']}
"""


def render_examples_markdown(ex: dict[str, list[dict[str, Any]]]) -> str:
    lines = ["# 任务 6 实例报告", ""]
    for cat, items in ex.items():
        lines.append(f"## {cat} ({len(items)} 例)")
        for item in items:
            lines.append(f"- **{item.get('title', 'N/A')}** (`{item.get('movie_id', 'N/A')}`):")
            lines.append(f"  ```json\n  {json.dumps(item, ensure_ascii=False, indent=2)}\n  ```")
        lines.append("")
    return "\n".join(lines)


def diff_versions(conn: psycopg.Connection, old_rv: str, new_rv: str) -> list[tuple[str, str, str, str]]:
    """列出两个版本之间选择记录或推断上界变化的作品与原因（R27）。"""
    diffs = []
    for mid, o_st, o_yr, n_st, n_yr, o_r, n_r in conn.execute(
            "SELECT o.movie_id, o.status, o.release_year, n.status, n.release_year, o.reason, n.reason "
            "FROM task6.release_selection o JOIN task6.release_selection n ON n.movie_id = o.movie_id "
            "WHERE o.release_version_id = %s AND n.release_version_id = %s AND "
            "(o.status != n.status OR o.release_year IS DISTINCT FROM n.release_year)", (old_rv, new_rv)).fetchall():
        diffs.append((mid, f"{o_st}({o_yr})", f"{n_st}({n_yr})", f"选择变化：{o_r} -> {n_r}"))
    for mid, o_t, o_e, n_t, n_e in conn.execute(
            "SELECT o.movie_id, o.bound_time, o.chosen_evidence, n.bound_time, n.chosen_evidence "
            "FROM task6.inferred_release_bound o JOIN task6.inferred_release_bound n ON n.movie_id = o.movie_id "
            "WHERE o.release_version_id = %s AND n.release_version_id = %s AND "
            "(o.bound_time != n.bound_time OR o.chosen_evidence != n.chosen_evidence)", (old_rv, new_rv)).fetchall():
        diffs.append((mid, f"{o_e}({o_t})", f"{n_e}({n_t})", "推断上界变化"))
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 6 报告与导出")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sum = sub.add_parser("summary", help="打印统计 JSON")
    p_sum.add_argument("--rv", help="上映信息版本号")
    p_sum.add_argument("--extraction-id", help="抽取 ID")

    p_ex = sub.add_parser("examples", help="打印实例")
    p_ex.add_argument("--rv", help="上映信息版本号")

    p_exp = sub.add_parser("export", help="导出文件")
    p_exp.add_argument("--rv", help="上映信息版本号")
    p_exp.add_argument("--run-key", default="manual", help="run_key")
    p_exp.add_argument("--output-dir", default="outputs", help="输出根目录")

    p_diff = sub.add_parser("diff", help="版本差异")
    p_diff.add_argument("old_rv", help="旧上映信息版本号")
    p_diff.add_argument("new_rv", help="新上映信息版本号")

    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = getattr(args, "extraction_id", None) or latest_full_extraction(conn)
        rv = getattr(args, "rv", None) or latest_rv(conn, eid)
        if args.cmd == "summary":
            print(json.dumps(build_summary(conn, rv), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "examples":
            print(json.dumps(examples(conn, rv), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "export":
            directory = run_dir(Path(args.output_dir), eid, rv, args.run_key)
            hashes = export_files(conn, rv, directory)
            print(f"已导出到 {directory}")
            for k, v in hashes.items():
                print(f"  {k}: {v}")
        elif args.cmd == "diff":
            d = diff_versions(conn, args.old_rv, args.new_rv)
            print(f"版本差异 {args.old_rv} -> {args.new_rv}：共 {len(d)} 处变化")
            for mid, before, after, reason in d:
                print(f"  {mid}: {before} -> {after} ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
