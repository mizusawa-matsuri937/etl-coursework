"""任务 7 统计、导出、实例与版本差异（spec R50、R58、R60，AC14、AC17）。

全部查询限定一个冲突消解版本及其登记的四个阶段键，所以多个版本、多次运行共存时不会串读。导出不含评论正文与整页 HTML。
覆盖率分母：作品 8,659、参与匹配商品 9,590、全部商品 253,059（版本层字段另给两个分母）。
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

from .check import EDITION_FIELDS, MOVIE_MULTI, MOVIE_SCALAR, cv_row, latest_cv
from .versions import diff_versions  # noqa: F401  report diff 的实现，随版本物化放在 versions.py

SUMMARY_VERSION = "task7-conflict-summary-1"
ALL_PRODUCTS_TOTAL = 253059
CAST_COVERAGE_NOTE = ("cast 的 IMDb 一侧是 title_principal 里 category 为 actor、actress 的主要演职员，"
                      "不是完整演员表；cast_coverage 只说明页面与主要演员各自覆盖了哪一侧")


def output_root(output_dir: Path) -> Path:
    return output_dir / "task7"


def run_dir(output_dir: Path, eid: str, cv: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / cv / run_key


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return output_root(output_dir) / "_failures" / run_key


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 6) if d else None


def resolve_run(conn: psycopg.Connection, cv: str, run_id: str | None = None) -> tuple[str, dict[str, Any]]:
    """版本对应的真实运行（run_id, 阶段计数）。显式传入不存在或不属于该版本的 run_id 时报错拒绝；
    传入 reused 运行时读它复用的那次 succeeded 运行的计数；未传时取该版本最新的 succeeded 运行（跳过 reused）。"""
    if run_id is not None:
        r = conn.execute("SELECT run_id, status, conflict_version_id, counts FROM task7.run WHERE run_id = %s",
                         (run_id,)).fetchone()
        if r is None:
            raise ValueError(f"运行 {run_id!r} 在 task7.run 中不存在")
        if r[2] != cv:
            raise ValueError(f"运行 {run_id!r} 属于冲突消解版本 {r[2]!r}，而非 {cv!r}")
        if r[1] == "reused":
            orig = conn.execute("SELECT counts FROM task7.run WHERE run_id = %s AND status = 'succeeded'",
                                ((r[3] or {}).get("reused_run"),)).fetchone()
            return r[0], dict(orig[0]) if orig else dict(r[3] or {})
        return r[0], dict(r[3] or {})
    r = conn.execute("SELECT run_id, counts FROM task7.run WHERE conflict_version_id = %s AND kind = 'resolve' "
                     "AND status = 'succeeded' ORDER BY started_at DESC LIMIT 1", (cv,)).fetchone()
    if r is not None:
        return r[0], dict(r[1] or {})
    return cv_row(conn, cv)["run_id"], {}


def build_summary(conn: psycopg.Connection, cv: str, *, run_id: str | None = None) -> dict[str, Any]:
    """版本全貌统计（R58）。"""
    row = cv_row(conn, cv)
    actual_run, run_counts = resolve_run(conn, cv, run_id)
    eid = row["extraction_id"]
    ekey, lkey, skey, mkey = (row["edition_stage_key"], row["link_stage_key"], row["scalar_stage_key"],
                              row["multivalue_stage_key"])

    def one(sql: str, *a: Any) -> Any:
        return conn.execute(sql, a).fetchone()[0]

    def tally(sql: str, *a: Any) -> dict[str, int]:
        return {str(k): n for k, n in conn.execute(sql, a)}

    movies = one("SELECT count(*) FROM task7.movie_resolved WHERE conflict_version_id = %s", cv)
    editions = one("SELECT count(*) FROM task7.edition_resolved WHERE conflict_version_id = %s", cv)

    def coverage(table: str, entity_total: int, fields: tuple[str, ...], with_all: bool) -> dict[str, Any]:
        counts: dict[str, dict[str, int]] = {f: {"resolved": 0, "conflict_unresolved": 0, "missing": 0} for f in fields}
        for f, st, n in conn.execute(f"SELECT field_name, status, count(*) FROM task7.{table} WHERE conflict_version_id = %s "
                                     "GROUP BY 1, 2", (cv,)):
            counts[f][st] = n
        out = {}
        for f in fields:
            c = counts[f]
            e = {**c, "denominator": entity_total,
                 "resolved_ratio": _ratio(c["resolved"], entity_total),
                 "conflict_unresolved_ratio": _ratio(c["conflict_unresolved"], entity_total),
                 "missing_ratio": _ratio(c["missing"], entity_total)}
            if with_all:
                e["all_products_denominator"] = ALL_PRODUCTS_TOTAL
                e["resolved_ratio_all_products"] = _ratio(c["resolved"], ALL_PRODUCTS_TOTAL)
            out[f] = e
        return out

    mcov = coverage("movie_field_selection", movies, MOVIE_SCALAR + MOVIE_MULTI, False)
    # inferred：release_year 缺失但有任务 6 推断上界的作品（上界只在参考列，不是 release_year 候选）
    for f in mcov:
        mcov[f]["inferred"] = 0
    mcov["release_year"]["inferred"] = one(
        "SELECT count(*) FROM task7.movie_resolved r JOIN task7.movie_field_selection s ON s.conflict_version_id = "
        "r.conflict_version_id AND s.movie_id = r.movie_id AND s.field_name = 'release_year' WHERE "
        "r.conflict_version_id = %s AND s.status = 'missing' AND r.inferred_upper_bound IS NOT NULL", cv)
    ecov = coverage("edition_field_selection", editions, EDITION_FIELDS, True)

    def conflict_types(table: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for f, ct, n in conn.execute(f"SELECT field_name, conflict_type, count(*) FROM task7.{table} "
                                     "WHERE conflict_version_id = %s GROUP BY 1, 2 ORDER BY 1, 2", (cv,)):
            out.setdefault(f, {})[ct] = n
        return out

    candidates = {et: {f"{f}:{st}": n for f, st, n in conn.execute(
        "SELECT field_name, status, count(*) FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s "
        "AND entity_type = %s GROUP BY 1, 2 ORDER BY 1, 2", (eid, key, et))}
        for et, key in (("movie", skey), ("edition", ekey))}

    halted_reasons = tally("SELECT halt_reason, count(*) FROM task7.link_assessment WHERE extraction_id = %s "
                           "AND stage_key = %s AND cross_source_mismatch_halt GROUP BY 1 ORDER BY 1", eid, lkey)
    signals = {name: one(f"SELECT count(*) FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s "
                         f"AND {col} = 'bad'", eid, lkey)
               for name, col in (("Y", "sig_year"), ("RT", "sig_runtime"), ("D", "sig_director"), ("T", "sig_title"))}
    link = {
        "assessed": one("SELECT count(*) FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s", eid, lkey),
        "by_source": tally("SELECT tconst_source, count(*) FROM task7.link_assessment WHERE extraction_id = %s "
                           "AND stage_key = %s GROUP BY 1 ORDER BY 1", eid, lkey),
        "weak": one("SELECT count(*) FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s AND is_weak",
                    eid, lkey),
        "halted": one("SELECT count(*) FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s "
                      "AND cross_source_mismatch_halt", eid, lkey),
        "halt_reasons": halted_reasons, "signals_lit": signals,
        "weak_single_signal": one("SELECT count(*) FROM task7.quality_issue WHERE extraction_id = %s AND stage_key = %s "
                                  "AND issue_code = 'weak_link_single_signal'", eid, lkey),
    }
    alignment = tally("SELECT role || ':' || outcome || ':' || coalesce(level, '-'), count(*) FROM task7.credit_alignment "
                      "WHERE extraction_id = %s AND stage_key = %s GROUP BY 1 ORDER BY 1", eid, mkey)
    genre_terms = {f"{sec}:{disp}": {"terms": n, "asin_occurrences": occ} for sec, disp, n, occ in conn.execute(
        "SELECT section, disposition, count(*), coalesce(sum(cardinality(asins)), 0) FROM task7.movie_genre_term "
        "WHERE extraction_id = %s AND stage_key = %s GROUP BY 1, 2 ORDER BY 1, 2", (eid, mkey))}
    return {
        "summary_version": SUMMARY_VERSION, "conflict_version_id": cv, "parent_version_id": row["parent_version_id"],
        "version_status": row["status"], "run_id": actual_run, "stage_counts": run_counts, "extraction_id": eid,
        "t4_mapping_version_id": row["t4_mapping_version_id"],
        "t5_person_mapping_version_id": row["t5_person_mapping_version_id"],
        "t6_release_version_id": row["t6_release_version_id"], "parser_version": row["parser_version"],
        "norm_version": row["norm_version"], "rule_version": row["rule_version"],
        "imdb_snapshot_id": row["imdb_snapshot_id"],
        "stage_keys": {"edition": ekey, "link": lkey, "scalar": skey, "multivalue": mkey},
        "totals": {"movies": movies, "editions": editions, "all_products": ALL_PRODUCTS_TOTAL,
                   "movies_with_imdb_link": link["assessed"]},
        "coverage": {"movie_fields": mcov, "edition_fields": ecov},
        "conflict_types": {"movie": conflict_types("movie_field_selection"),
                           "edition": conflict_types("edition_field_selection")},
        "candidate_status": candidates,
        "runtime_diff": {"by_class": tally("SELECT diff_class, count(*) FROM task7.runtime_diff WHERE extraction_id = %s "
                                           "AND stage_key = %s GROUP BY 1 ORDER BY 1", eid, skey),
                         "pal_formula_match": one("SELECT count(*) FROM task7.runtime_diff WHERE extraction_id = %s "
                                                  "AND stage_key = %s AND pal_formula_match", eid, skey),
                         "pairs": one("SELECT count(*) FROM task7.runtime_diff WHERE extraction_id = %s AND stage_key = %s",
                                      eid, skey)},
        "link_assessment": link,
        "credit_alignment": {"by_role_outcome_level": alignment,
                             "aligned_rows": sum(v for k, v in alignment.items() if ":aligned:" in k)},
        "people": {"by_role_coverage_status": tally(
            "SELECT role || ':' || source_coverage || ':' || status, count(*) FROM task7.movie_person_resolved "
            "WHERE extraction_id = %s AND stage_key = %s GROUP BY 1 ORDER BY 1", eid, mkey),
                   "imdb_person_ext": one("SELECT count(*) FROM task7.imdb_person_ext WHERE extraction_id = %s "
                                          "AND imdb_snapshot_id = %s", eid, row["imdb_snapshot_id"])},
        "cast_coverage": {"by_value": tally("SELECT cast_coverage, count(*) FROM task7.movie_resolved "
                                            "WHERE conflict_version_id = %s GROUP BY 1 ORDER BY 1", cv),
                          "note": CAST_COVERAGE_NOTE},
        "genres": {"term_dispositions": genre_terms,
                   "members_by_coverage": tally("SELECT source_coverage, count(*) FROM task7.movie_genre "
                                                "WHERE extraction_id = %s AND stage_key = %s GROUP BY 1 ORDER BY 1",
                                                eid, mkey)},
        "edition_conflicts": tally("SELECT conflict_type, count(*) FROM task7.edition_field_selection "
                                   "WHERE conflict_version_id = %s AND conflict_type <> 'none' GROUP BY 1 ORDER BY 1", cv),
        "shared_tconst": {"by_class": tally("SELECT link_class, count(*) FROM task7.shared_tconst_conflict "
                                            "WHERE conflict_version_id = %s GROUP BY 1 ORDER BY 1", cv),
                          "pairs": one("SELECT count(*) FROM task7.shared_tconst_conflict WHERE conflict_version_id = %s",
                                       cv),
                          "source_issues": one("SELECT coalesce(sum(cardinality(source_issue_ids)), 0) FROM "
                                               "task7.shared_tconst_conflict WHERE conflict_version_id = %s", cv)},
        "quality_issues": tally("SELECT issue_code, count(*) FROM task7.quality_issue WHERE extraction_id = %s "
                                "AND stage_key IN (%s, %s, %s, %s) GROUP BY 1 ORDER BY 1", eid, ekey, lkey, skey, mkey),
    }


# ---------------------------------------------------------------- 实例（R60）

def _cands(conn: psycopg.Connection, eid: str, key: str, entity: str, field: str) -> list[dict[str, Any]]:
    return [{"candidate_id": cid, "source": src, "source_key": sk, "raw_value": raw, "normalized_value": nv,
             "status": st, "reason": why, "source_ref": dict(ref)}
            for cid, src, sk, raw, nv, st, why, ref in conn.execute(
                "SELECT candidate_id, source, source_key, raw_value, normalized_value, status, status_reason, source_ref "
                "FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s AND entity_id = %s "
                "AND field_name = %s ORDER BY candidate_id", (eid, key, entity, field))]


def examples(conn: psycopg.Connection, cv: str, per_category: int = 3) -> dict[str, list[dict[str, Any]]]:
    """R60 的各类实例，每类最多 per_category 例，附可定位的证据（候选 ID 与来源、ASIN、tconst、人物 ID）。"""
    row = cv_row(conn, cv)
    eid = row["extraction_id"]
    ekey, lkey, skey, mkey = (row["edition_stage_key"], row["link_stage_key"], row["scalar_stage_key"],
                              row["multivalue_stage_key"])
    n = per_category

    def selection_examples(field: str, types: tuple[str, ...], with_link: bool = False) -> list[dict[str, Any]]:
        out = []
        for mid, title, st, val, ctype, rule, reason, detail in conn.execute(
                "SELECT s.movie_id, r.title, s.status, s.winning_value, s.conflict_type, s.selection_rule, s.reason, "
                "s.disagreement_detail FROM task7.movie_field_selection s JOIN task7.movie_resolved r "
                "ON r.conflict_version_id = s.conflict_version_id AND r.movie_id = s.movie_id WHERE s.conflict_version_id = %s "
                "AND s.field_name = %s AND s.conflict_type = ANY(%s) ORDER BY md5(s.movie_id) LIMIT %s",
                (cv, field, list(types), n)).fetchall():
            e = {"movie_id": mid, "title": title, "field": field, "status": st, "winning_value": val,
                 "conflict_type": ctype, "rule": rule, "reason": reason, "detail": dict(detail)}
            if field in MOVIE_SCALAR:
                e["candidates"] = _cands(conn, eid, skey, mid, field)
            if with_link:
                lk = conn.execute("SELECT effective_tconst, tconst_source, sig_year, sig_runtime, sig_director, sig_title, "
                                  "cross_source_mismatch_halt, halt_reason FROM task7.link_assessment WHERE extraction_id = %s "
                                  "AND stage_key = %s AND movie_id = %s", (eid, lkey, mid)).fetchone()
                if lk:
                    e["link_assessment"] = dict(zip(("tconst", "source", "Y", "RT", "D", "T", "halt", "halt_reason"), lk))
            out.append(e)
        return out

    def edition_examples(field: str, types: tuple[str, ...]) -> list[dict[str, Any]]:
        out = []
        for asin, st, val, ctype, rule, reason, detail, flags in conn.execute(
                "SELECT s.asin, s.status, s.winning_value, s.conflict_type, s.selection_rule, s.reason, "
                "s.disagreement_detail, r.date_flags FROM task7.edition_field_selection s JOIN task7.edition_resolved r "
                "ON r.conflict_version_id = s.conflict_version_id AND r.asin = s.asin WHERE s.conflict_version_id = %s "
                "AND s.field_name = %s AND s.conflict_type = ANY(%s) ORDER BY md5(s.asin) LIMIT %s",
                (cv, field, list(types), n)).fetchall():
            out.append({"asin": asin, "field": field, "status": st, "winning_value": val, "conflict_type": ctype,
                        "rule": rule, "reason": reason, "date_flags": list(flags), "detail": dict(detail),
                        "candidates": _cands(conn, eid, ekey, asin, field)})
        return out

    def people_examples(where: str, params: tuple, limit: int = n) -> list[dict[str, Any]]:
        out = []
        for mid, title in conn.execute(
                "SELECT DISTINCT p.movie_id, r.title FROM task7.movie_person_resolved p JOIN task7.movie_resolved r "
                "ON r.conflict_version_id = %s AND r.movie_id = p.movie_id WHERE p.extraction_id = %s AND p.stage_key = %s "
                f"AND {where} ORDER BY p.movie_id LIMIT %s", (cv, eid, mkey, *params, limit)).fetchall():
            members = [{"person_id": pid, "role": role, "source_coverage": cov, "status": st, "credit_order": order,
                        "imdb_ordering": io_, "nconst": nc, "raw_names": raw, "note": dict(note)}
                       for pid, role, cov, st, order, io_, nc, raw, note in conn.execute(
                           "SELECT person_id, role, source_coverage, status, credit_order, imdb_ordering, nconst, raw_names, "
                           "resolution_note FROM task7.movie_person_resolved WHERE extraction_id = %s AND stage_key = %s "
                           "AND movie_id = %s ORDER BY role, credit_order NULLS LAST, person_id", (eid, mkey, mid))]
            out.append({"movie_id": mid, "title": title, "members": members})
        return out

    def alignment_examples(outcomes: tuple[str, ...], multi: bool) -> list[dict[str, Any]]:
        out = []
        having = "max(segment) > 0" if multi else "max(segment) = 0"
        for mid, ppid in conn.execute(
                "SELECT movie_id, page_person_id FROM task7.credit_alignment WHERE extraction_id = %s AND stage_key = %s "
                f"GROUP BY movie_id, page_person_id HAVING {having} AND bool_or(outcome = ANY(%s)) "
                "ORDER BY md5(movie_id || page_person_id) LIMIT %s", (eid, mkey, list(outcomes), n)).fetchall():
            rows = [{"role": role, "raw_name": raw, "segment": seg, "segment_name": name, "resolved_person_id": rid,
                     "nconst": nc, "level": lv, "outcome": oc}
                    for role, raw, seg, name, rid, nc, lv, oc in conn.execute(
                        "SELECT role, raw_name, segment, segment_name, resolved_person_id, nconst, level, outcome "
                        "FROM task7.credit_alignment WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s "
                        "AND page_person_id = %s ORDER BY role, segment", (eid, mkey, mid, ppid))]
            title = conn.execute("SELECT title FROM task7.movie_resolved WHERE conflict_version_id = %s AND movie_id = %s",
                                 (cv, mid)).fetchone()[0]
            out.append({"movie_id": mid, "title": title, "page_person_id": ppid, "alignment": rows})
        return out

    runtime = {}
    for cls in ("exact", "tolerance_1_2", "pal_evidence", "variance_3_5", "cut_or_multidisc", "unexplained_6_15",
                "unexplained_gt15"):
        runtime[f"runtime_{cls}"] = [
            {"movie_id": mid, "title": title, "asin": asin, "page_runtime": pg, "imdb_runtime": im, "delta": d,
             "pal_formula_match": pal, "has_pal_evidence": evid, "cut_type": list(cut), "discs": discs}
            for mid, title, asin, pg, im, d, pal, evid, cut, discs in conn.execute(
                "SELECT d.movie_id, r.title, d.asin, d.page_runtime, d.imdb_runtime, d.delta, d.pal_formula_match, "
                "d.has_pal_evidence, d.cut_type, d.discs FROM task7.runtime_diff d JOIN task7.movie_resolved r "
                "ON r.conflict_version_id = %s AND r.movie_id = d.movie_id WHERE d.extraction_id = %s AND d.stage_key = %s "
                "AND d.diff_class = %s ORDER BY md5(d.movie_id || d.asin) LIMIT %s", (cv, eid, skey, cls, n))]

    genre_ex = []
    for mid, title in conn.execute(
            "SELECT t.movie_id, r.title FROM task7.movie_genre_term t JOIN task7.movie_resolved r ON r.conflict_version_id = %s "
            "AND r.movie_id = t.movie_id WHERE t.extraction_id = %s AND t.stage_key = %s GROUP BY 1, 2 "
            "HAVING bool_or(t.disposition = 'mapped') AND bool_or(t.disposition <> 'mapped') ORDER BY md5(t.movie_id) LIMIT %s",
            (cv, eid, mkey, n)).fetchall():
        genre_ex.append({
            "movie_id": mid, "title": title,
            "terms": [{"section": sec, "term": term, "disposition": disp, "mapped_genres": mapped, "asins": asins}
                      for sec, term, disp, mapped, asins in conn.execute(
                          "SELECT section, term, disposition, mapped_genres, asins FROM task7.movie_genre_term "
                          "WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s ORDER BY section, term",
                          (eid, mkey, mid))],
            "genres": [{"genre": g, "source_coverage": c, "raw_terms": raw, "source_ref": dict(ref)}
                       for g, c, raw, ref in conn.execute(
                           "SELECT genre, source_coverage, raw_terms, source_ref FROM task7.movie_genre "
                           "WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s ORDER BY genre", (eid, mkey, mid))]})

    shared = [{"tconst": tc, "movie_id": mid, "other_movie_id": other, "link_class": cls, "basis": dict(basis),
               "source_issue_ids": list(issues)}
              for tc, mid, other, cls, basis, issues in conn.execute(
                  "SELECT tconst, movie_id, other_movie_id, link_class, basis, source_issue_ids FROM "
                  "task7.shared_tconst_conflict WHERE conflict_version_id = %s ORDER BY link_class, md5(tconst || movie_id) "
                  "LIMIT %s", (cv, n * 3))]

    return {
        "title_conflict": selection_examples("title", ("formatting_or_alias", "cross_source_override",
                                                       "intra_source_tie_unresolved", "cross_source_mismatch_halt")),
        "year_disagreement": selection_examples("release_year", ("minor_tolerance", "cross_source_override",
                                                                 "severe_mismatch_unresolved",
                                                                 "intra_source_tie_unresolved"), True),
        "mismatch_halt": selection_examples("release_year", ("cross_source_mismatch_halt",), True),
        **runtime,
        "credits_union": people_examples("p.source_coverage = 'imdb_only' AND p.role = 'cast'", ()),
        "credit_alignment": alignment_examples(("aligned", "tie_not_aligned"), False),
        "multi_person": alignment_examples(("aligned", "unsplit_multi_person"), True),
        "billing_order": people_examples("p.resolution_note ? 'billing_conflict'", ()),
        "genre_mapping": genre_ex,
        "rating": selection_examples("mpaa_rating", ("unrated_vs_rated", "intra_source_tie_unresolved")),
        "edition_publisher": edition_examples("publisher", ("formatting_or_alias", "intra_asin_publisher_conflict")),
        "edition_release_date": edition_examples("product_release_date", ("semantic_distinction",)),
        "shared_tconst": shared,
    }


def render_examples_markdown(ex: dict[str, list[dict[str, Any]]]) -> str:
    lines = ["# 任务 7 实例报告", "", "每类最多 3 例，附可定位的证据（候选 ID、ASIN、tconst、人物 ID）；没有实例的类别写明“无”。", ""]
    for cat, items in ex.items():
        lines.append(f"## {cat}（{len(items)} 例）")
        if not items:
            lines += ["无。", ""]
            continue
        for item in items:
            label = item.get("title") or item.get("asin") or item.get("tconst") or ""
            ident = item.get("movie_id") or item.get("asin") or ""
            lines.append(f"- **{label}** (`{ident}`)")
            lines.append("  ```json\n  " + json.dumps(item, ensure_ascii=False, indent=2, default=str).replace("\n", "\n  ")
                         + "\n  ```")
        lines.append("")
    return "\n".join(lines)


def render_markdown(s: dict[str, Any]) -> str:
    t = s["totals"]
    mc, ec = s["coverage"]["movie_fields"], s["coverage"]["edition_fields"]

    def table(cov: dict[str, Any], with_all: bool) -> str:
        head = "| 字段 | resolved | conflict_unresolved | missing | inferred |" + (" resolved/全部商品 |" if with_all else "")
        sep = "|---|---|---|---|---|" + ("---|" if with_all else "")
        rows = []
        for f, c in cov.items():
            cells = [f, f"{c['resolved']}（{c['resolved_ratio']:.2%}）", f"{c['conflict_unresolved']}", f"{c['missing']}",
                     f"{c.get('inferred', 0)}"]
            if with_all:
                cells.append(f"{c['resolved_ratio_all_products']:.2%}")
            rows.append("| " + " | ".join(cells) + " |")
        return "\n".join([head, sep, *rows])

    lk = s["link_assessment"]
    return f"""# 任务 7 冲突消解报告（{s['conflict_version_id']}）

- **抽取 ID**：`{s['extraction_id']}`；任务 4 映射 `{s['t4_mapping_version_id']}`、人物映射 `{s['t5_person_mapping_version_id']}`、上映版本 `{s['t6_release_version_id']}`、IMDb 快照 `{s['imdb_snapshot_id']}`、规则 `{s['rule_version']}`
- **版本状态**：{s['version_status']}；运行 `{s['run_id']}`
- **作品** {t['movies']} 部，**参与匹配商品** {t['editions']} 个（全部商品 {t['all_products']}）

## 1. 作品层字段覆盖（分母 {t['movies']}）

{table(mc, False)}

## 2. 版本层字段覆盖（分母 {t['editions']}，另给全部商品 {t['all_products']}）

{table(ec, True)}

## 3. 关联评估与错配熔断

- 有效 IMDb 关联 {lk['assessed']} 部（弱匹配 {lk['weak']}），熔断 {lk['halted']} 部；信号亮起：{json.dumps(lk['signals_lit'], ensure_ascii=False)}；弱匹配单信号 {lk['weak_single_signal']} 部
- 熔断原因：{json.dumps(lk['halt_reasons'], ensure_ascii=False)}

## 4. 冲突类型

- 作品层：{json.dumps(s['conflict_types']['movie'], ensure_ascii=False)}
- 版本层：{json.dumps(s['conflict_types']['edition'], ensure_ascii=False)}

## 5. 片长差异

- 按对分布：{json.dumps(s['runtime_diff']['by_class'], ensure_ascii=False)}（共 {s['runtime_diff']['pairs']} 对，`pal_formula_match` {s['runtime_diff']['pal_formula_match']} 对）

## 6. 演职员

- 署名对齐：{json.dumps(s['credit_alignment']['by_role_outcome_level'], ensure_ascii=False)}
- 成员：{json.dumps(s['people']['by_role_coverage_status'], ensure_ascii=False)}；`imdb_person_ext` {s['people']['imdb_person_ext']} 人
- `cast_coverage`：{json.dumps(s['cast_coverage']['by_value'], ensure_ascii=False)}。{s['cast_coverage']['note']}。

## 7. 类型词面

- {json.dumps(s['genres']['term_dispositions'], ensure_ascii=False)}
- 成员来源：{json.dumps(s['genres']['members_by_coverage'], ensure_ascii=False)}

## 8. 共享 tconst

- {s['shared_tconst']['pairs']} 对（来自 {s['shared_tconst']['source_issues']} 条任务 6 质量问题）：{json.dumps(s['shared_tconst']['by_class'], ensure_ascii=False)}

## 9. 质量问题

- {json.dumps(s['quality_issues'], ensure_ascii=False)}
"""


def export_files(conn: psycopg.Connection, cv: str, directory: Path, examples_data: dict[str, Any] | None = None, *,
                 run_id: str | None = None, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """导出报告与数据 CSV，返回 文件名 -> SHA-256。不含评论正文与整页 HTML。"""
    directory.mkdir(parents=True, exist_ok=True)
    row = cv_row(conn, cv)
    eid = row["extraction_id"]
    skey, lkey, mkey = row["scalar_stage_key"], row["link_stage_key"], row["multivalue_stage_key"]
    summary = summary or build_summary(conn, cv, run_id=run_id)
    examples_data = examples_data if examples_data is not None else examples(conn, cv)
    files: dict[str, bytes] = {
        "summary.json": (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode(),
        "report.md": render_markdown(summary).encode(),
        "examples.md": render_examples_markdown(examples_data).encode(),
    }

    def csv_of(sql: str, params: tuple, columns: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(columns)
        for r in conn.execute(sql, params):
            w.writerow([json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x for x in r])
        return buf.getvalue().encode()

    sel_cols = ["status", "winning_value", "selected_candidate_ids", "considered_candidate_ids", "conflict_type",
                "selection_rule", "reason", "confidence"]
    files["movie_field_selection.csv"] = csv_of(
        f"SELECT movie_id, field_name, {', '.join(sel_cols)} FROM task7.movie_field_selection "
        "WHERE conflict_version_id = %s ORDER BY movie_id, field_name", (cv,), ["movie_id", "field_name", *sel_cols])
    files["edition_field_selection.csv"] = csv_of(
        f"SELECT asin, field_name, {', '.join(sel_cols)} FROM task7.edition_field_selection "
        "WHERE conflict_version_id = %s ORDER BY asin, field_name", (cv,), ["asin", "field_name", *sel_cols])
    files["link_assessment.csv"] = csv_of(
        "SELECT movie_id, effective_tconst, tconst_source, is_weak, sig_year, sig_runtime, sig_director, sig_title, "
        "cross_source_mismatch_halt, halt_reason FROM task7.link_assessment WHERE extraction_id = %s AND stage_key = %s "
        "ORDER BY movie_id", (eid, lkey),
        ["movie_id", "effective_tconst", "tconst_source", "is_weak", "sig_year", "sig_runtime", "sig_director", "sig_title",
         "cross_source_mismatch_halt", "halt_reason"])
    files["runtime_diff.csv"] = csv_of(
        "SELECT movie_id, asin, page_runtime, imdb_runtime, delta, diff_class, pal_formula_match, has_pal_evidence "
        "FROM task7.runtime_diff WHERE extraction_id = %s AND stage_key = %s ORDER BY movie_id, asin", (eid, skey),
        ["movie_id", "asin", "page_runtime", "imdb_runtime", "delta", "diff_class", "pal_formula_match", "has_pal_evidence"])
    files["credit_alignment.csv"] = csv_of(
        "SELECT movie_id, role, page_person_id, raw_name, segment, segment_name, resolved_person_id, nconst, level, outcome "
        "FROM task7.credit_alignment WHERE extraction_id = %s AND stage_key = %s ORDER BY movie_id, role, page_person_id, segment",
        (eid, mkey), ["movie_id", "role", "page_person_id", "raw_name", "segment", "segment_name", "resolved_person_id",
                      "nconst", "level", "outcome"])
    files["movie_genre_term.csv"] = csv_of(
        "SELECT movie_id, section, term, disposition, mapped_genres, asins FROM task7.movie_genre_term "
        "WHERE extraction_id = %s AND stage_key = %s ORDER BY movie_id, section, term", (eid, mkey),
        ["movie_id", "section", "term", "disposition", "mapped_genres", "asins"])
    files["shared_tconst.csv"] = csv_of(
        "SELECT tconst, movie_id, other_movie_id, link_class, source_issue_ids FROM task7.shared_tconst_conflict "
        "WHERE conflict_version_id = %s ORDER BY tconst, movie_id", (cv,),
        ["tconst", "movie_id", "other_movie_id", "link_class", "source_issue_ids"])
    digests: dict[str, Any] = {}
    for name, content in files.items():
        reporting.write_atomic(directory / name, content)
        digests[name] = reporting.file_digest(directory / name)
    return digests


def build_and_write_report(conn: psycopg.Connection, cv: str | None = None, output_dir: Path = Path("outputs"), *,
                           extraction_id: str | None = None, run_id: str | None = None, run_key: str | None = None
                           ) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    eid = extraction_id or (cv_row(conn, cv)["extraction_id"] if cv else latest_full_extraction(conn))
    resolved = cv or latest_cv(conn, eid)
    summary = build_summary(conn, resolved, run_id=run_id)
    directory = run_dir(output_dir, eid, resolved, run_key or summary["run_id"])
    digests = export_files(conn, resolved, directory, run_id=run_id, summary=summary)
    return summary, directory, digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 7 报告与导出")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (("summary", "打印统计 JSON"), ("examples", "打印实例"), ("export", "导出文件")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--cv", help="冲突消解版本号（默认最近一次）")
        p.add_argument("--extraction-id", help="抽取 ID")
        if name != "examples":
            p.add_argument("--run-id", help="运行 ID（默认取该版本最新 succeeded 运行；不存在的会被拒绝）")
        if name == "export":
            p.add_argument("--run-key", default=None, help="输出目录 run_key（默认取真实 run_id）")
            p.add_argument("--output-dir", default="outputs", help="输出根目录")
    p_diff = sub.add_parser("diff", help="两个版本的差异")
    p_diff.add_argument("old_cv")
    p_diff.add_argument("new_cv")
    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        if args.cmd == "diff":
            for v in (args.old_cv, args.new_cv):
                cv_row(conn, v)
            d = diff_versions(conn, args.old_cv, args.new_cv)
            print(f"版本差异 {args.old_cv} -> {args.new_cv}：共 {len(d)} 处变化")
            for layer, ent, field, before, after, why in d:
                print(f"  [{layer}] {ent} {field}: {before} -> {after}（{why}）")
            return 0
        eid = args.extraction_id or latest_full_extraction(conn)
        cv = args.cv or latest_cv(conn, eid)
        if args.cmd == "summary":
            print(json.dumps(build_summary(conn, cv, run_id=args.run_id), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "examples":
            print(json.dumps(examples(conn, cv), ensure_ascii=False, indent=2, default=str))
        else:
            _, directory, hashes = build_and_write_report(conn, cv, Path(args.output_dir), extraction_id=eid,
                                                          run_id=args.run_id, run_key=args.run_key)
            print(f"已导出到 {directory}")
            for k, v in hashes.items():
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
