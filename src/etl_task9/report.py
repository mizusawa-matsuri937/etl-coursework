"""任务 9 报告：覆盖率与计数导出、实例与版本差异（spec R34、R44、R45）。

    python -m etl_task9.report export [--version lv0001]     # 导出到 outputs/task9/<抽取>/<血缘版本>/<run_key>/
    python -m etl_task9.report diff lv0001 lv0002             # 两个血缘版本的选择级差异（R34，只读）

导出（R44）：summary.json（覆盖率按实体、字段与 trace_status 分列，源记录与断言按类型计数，派生边按操作计数，重放补定位，
质量问题，评论层视图核对数）、quality_issues.csv、examples.md（R45：四个演示查询各 3 例，四类作业血缘示例各 1 例）、
report.md 与 checksums.json（各文件 SHA-256）。导出不含评论正文全文与整页 HTML：评论只给定位与记录哈希。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting

from .query import query_field, query_person, query_product, query_review, query_source
from .versions import Keys, diff_versions, keys_for_version, version_counts

SUMMARY_VERSION = "task9-lineage-report-1"


def output_root(output_dir: Path) -> Path:
    return Path(output_dir) / "task9"


def run_dir(output_dir: Path, eid: str, lv: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / lv / run_key


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return output_root(output_dir) / "_failures" / run_key


# ---------------------------------------------------------------- 汇总（R44）

def build_summary(conn: psycopg.Connection, lv: str) -> dict[str, Any]:
    keys = keys_for_version(conn, lv)
    i = keys.inputs
    row = conn.execute("SELECT status, run_id, created_at, verified_at, counts FROM task9.lineage_version "
                       "WHERE lineage_version_id = %s", (lv,)).fetchone()
    stage = {s: getattr(keys, s) for s in ("sources", "assertions", "derivations", "selections", "impact")}
    cov = conn.execute(
        "SELECT entity_type, field_name, trace_status, count(*), count(*) FILTER (WHERE value_text IS NOT NULL) "
        "FROM task9.field_selection WHERE stage_key = %s GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", (keys.selections,)).fetchall()
    by_field: dict[str, dict[str, Any]] = {}
    for et, f, ts, n, nn in cov:
        d = by_field.setdefault(f"{et}.{f}", {"selections": 0, "non_null": 0, "non_null_traceable": 0, "trace_status": {}})
        d["selections"] += n
        d["non_null"] += nn
        d["trace_status"][ts] = n
        if ts in ("traced", "relocated", "manual"):
            d["non_null_traceable"] += nn
    for d in by_field.values():
        d["coverage"] = round(d["non_null_traceable"] / d["non_null"], 4) if d["non_null"] else None
    by_entity: dict[str, dict[str, int]] = {}
    for k, d in by_field.items():
        e = by_entity.setdefault(k.split(".")[0], {"non_null": 0, "non_null_traceable": 0})
        e["non_null"] += d["non_null"]
        e["non_null_traceable"] += d["non_null_traceable"]
    for e in by_entity.values():
        e["coverage"] = round(e["non_null_traceable"] / e["non_null"], 4) if e["non_null"] else None
    issues = conn.execute("SELECT q.issue_code, count(*) FROM task9.quality_issue q WHERE q.stage_key = ANY(%s) "
                          "GROUP BY 1 ORDER BY 1", (list(stage.values()),)).fetchall()
    review_view = dict(conn.execute(
        "SELECT coalesce(m.mapping_status, 'unmapped'), count(*) FROM public.review_record rr "
        "LEFT JOIN task8.review_mapping m ON m.dedup_stage_key = %s AND m.record_no = rr.record_no "
        "WHERE rr.extraction_id = %s GROUP BY 1 ORDER BY 1",
        (i.rd_stage_keys[2], i.extraction_id)).fetchall())
    return {
        "summary_version": SUMMARY_VERSION, "lineage_version_id": lv, "status": row[0], "run_id": row[1],
        "created_at": row[2], "verified_at": row[3], "inputs": i.as_json(), "stage_keys": stage,
        "counts": version_counts(conn, stage),
        "upstream_counts": dict(row[4]).get("upstream_counts"),
        "assertions_by_upstream": dict(conn.execute(
            "SELECT upstream_table, count(*) FROM task9.assertion WHERE stage_key = %s GROUP BY 1 ORDER BY 1",
            (keys.assertions,)).fetchall()),
        "coverage_by_entity": by_entity, "coverage_by_field": by_field,
        "quality_issues": dict(issues),
        "review_view_mapping_status": review_view,
    }


# ---------------------------------------------------------------- 实例（R45）

def _pick(conn: psycopg.Connection, sql: str, params: tuple, n: int = 3) -> list[tuple]:
    return conn.execute(sql + f" LIMIT {n}", params).fetchall()


def _ev(evidence: list[dict[str, Any]], n: int = 3) -> list[str]:
    out = []
    for e in evidence[:n]:
        loc = e.get("locator") or {}
        where = (f"{loc.get('file')} 第 {loc.get('line_no')} 行" if e.get("source_kind") == "imdb_row"
                 else f"ASIN {loc.get('asin')} 快照 {loc.get('snapshot_path')}" if e.get("source_kind") == "page_snapshot"
                 else f"评论记录 {loc.get('record_no')}（行 {loc.get('start_line')}）")
        out.append(f"{e.get('source_kind')} {where}，哈希 `{(e.get('content_hash') or '')[:12]}…`，"
                   f"获取 {e.get('acquired_at')}，路径长度 {e.get('depth')}（{' → '.join(e.get('operations') or [])}）")
    return out


def examples(conn: psycopg.Connection, lv: str) -> dict[str, list[dict[str, Any]]]:
    """四个演示查询各 3 例、四类作业血缘示例各 1 例（按 ID 排序确定性选取，只读）。"""
    keys = keys_for_version(conn, lv)
    lk, ak, i = keys.selections, keys.assertions, keys.inputs
    ex: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        "field", "product", "review", "source", "person",
        "edition_date_not_movie_year", "third_party_adopted", "alias_merged", "year_missing_bound_only")}

    def field_ex(mid: str, f: str) -> dict[str, Any]:
        q = query_field(conn, keys, mid, f, max_parents=3, member_limit=3)
        s = q["selections"][0]
        return {"movie_id": mid, "field": f, "value": s["value_text"], "status": s["status"],
                "trace_status": s["trace_status"], "decision_rule": s["decision_rule"], "reason": s["reason"],
                "members": q["members"], "evidence": _ev(s["evidence"]),
                "not_selected": [{"value": c["candidate"]["normalized_value"], "status": c["candidate"]["upstream_status"],
                                  "reason": c["candidate"]["upstream_reason"], "evidence": _ev(c["evidence"], 1)}
                                 for c in s.get("not_selected", [])[:3]]}

    # 采用 IMDb（第三方记录）、另有被拒候选的年份；有 IMDb 与页面两方证据的导演；标题
    picks = _pick(conn, "SELECT entity_id FROM task9.field_selection WHERE stage_key = %s AND entity_type = 'movie' "
                        "AND field_name = 'release_year' AND status = 'resolved' "
                        "AND EXISTS (SELECT 1 FROM unnest(considered_assertion_ids) c JOIN task9.assertion a "
                        "  ON a.stage_key = %s AND a.assertion_id = c WHERE a.upstream_status = 'rejected') "
                        "AND EXISTS (SELECT 1 FROM unnest(selected_assertion_ids) c JOIN task9.assertion a "
                        "  ON a.stage_key = %s AND a.assertion_id = c WHERE a.rule_id LIKE 'imdb%%') "
                        "ORDER BY md5(selection_id)", (lk, ak, ak), 1)
    picks += _pick(conn, "SELECT entity_id FROM task9.field_selection WHERE stage_key = %s AND entity_type = 'movie' "
                         "AND field_name = 'director' AND decision_rule = 'credit_both' ORDER BY md5(selection_id)", (lk,), 1)
    picks += _pick(conn, "SELECT entity_id FROM task9.field_selection WHERE stage_key = %s AND entity_type = 'movie' "
                         "AND field_name = 'title' AND status = 'resolved' ORDER BY md5(selection_id)", (lk,), 1)
    for (mid,), f in zip(picks, ("release_year", "director", "title")):
        ex["field"].append(field_ex(mid, f))
    if ex["field"] and ex["field"][0]["field"] == "release_year":
        ex["third_party_adopted"].append(ex["field"][0])
    asins = _pick(conn, "SELECT asin FROM task4.product_movie WHERE mapping_version_id = %s AND relation_type = 'edition' "
                        "ORDER BY md5(asin)", (i.t4_mapping_version_id,), 1)
    asins += _pick(conn, "SELECT asin FROM task4.product_movie WHERE mapping_version_id = %s AND relation_type = 'contains' "
                         "ORDER BY md5(asin)", (i.t4_mapping_version_id,), 1)
    asins += _pick(conn, "SELECT asin FROM task2.product_manifest WHERE extraction_id = %s AND snapshot_path IS NULL "
                         "ORDER BY md5(asin)", (i.extraction_id,), 1)
    for (a,) in asins:
        q = query_product(conn, keys, a)
        ex["product"].append({"asin": a, "collection": q["collection"], "collection_status": q.get("collection_status"),
                              "category": (q.get("category") or {}).get("category"), "product_kind": q.get("product_kind"),
                              "match_status": q.get("match_status"), "movies": q.get("movies"),
                              "edition_title": (q.get("edition") or {}).get("edition_title")})
    reviews = _pick(conn, "SELECT a.entity_id FROM task9.derivation d JOIN task9.assertion a ON a.stage_key = %s "
                          "AND a.assertion_id = d.parent_id WHERE d.stage_key = %s AND d.role = 'direct_dependency' "
                          "AND a.upstream_table = 'public.review_staging' ORDER BY md5(d.parent_id)",
                    (ak, keys.derivations), 1)
    reviews += _pick(conn, "SELECT rr.source_review_id FROM task8.review_mapping m JOIN public.review_record rr "
                           "ON rr.extraction_id = %s AND rr.record_no = m.record_no WHERE m.dedup_stage_key = %s "
                           "AND m.mapping_status = 'quarantined' ORDER BY m.record_no", (i.extraction_id, i.rd_stage_keys[2]), 1)
    reviews += _pick(conn, "SELECT rr.source_review_id FROM task8.review_mapping m JOIN public.review_record rr "
                           "ON rr.extraction_id = %s AND rr.record_no = m.record_no WHERE m.dedup_stage_key = %s "
                           "AND m.match_level <> 'representative' ORDER BY m.record_no", (i.extraction_id, i.rd_stage_keys[2]), 1)
    for (sid,) in reviews:
        q = query_review(conn, keys, sid)
        ex["review"].append({"source_review_id": sid, "record_no": q["record"]["record_no"],
                             "start_line": q["record"]["start_line"], "raw_sha256": q["record"]["raw_sha256"],
                             **{k: q["mapping"][k] for k in ("mapping_status", "match_level", "logical_review_id",
                                                             "family_id")},
                             "movies": [m["movie_id"] for m in q["movies"]],
                             "bound_dependencies": [(d["movie_id"], d["role"]) for d in q["bound_dependencies"]]})
    refs = []
    if asins:
        refs.append({"asin": asins[0][0]})
    t = _pick(conn, "SELECT locator->>'tconst' FROM task9.source_record WHERE stage_key = %s AND source_kind = 'imdb_row' "
                    "AND upstream_table = 'imdb.title' ORDER BY source_record_id", (keys.sources,), 1)
    if t:
        refs.append({"tconst": t[0][0]})
    if reviews:
        refs.append({"source_review_id": reviews[0][0]})
    for ref in refs:
        q = query_source(conn, keys, limit=5, **ref)
        ex["source"].append({"input": ref, "source_records": len(q["source_records"]),
                             "affected_selections": q["affected_selections"], "affected_entities": q["affected_entities"],
                             "by_field": q["by_field"], "rerun": [r["dag_id"] for r in q["rerun_if_changed"]]})
    persons = _pick(conn, "SELECT entity_id FROM task9.field_selection WHERE stage_key = %s AND entity_type = 'person' "
                          "AND field_name = 'alias' GROUP BY entity_id ORDER BY count(*) DESC, md5(entity_id)", (lk,), 3)
    for (pid,) in persons:
        q = query_person(conn, keys, pid, limit=10)
        item = {"person_id": pid, "canonical_name": (q["canonical_name"] or {}).get("value_text"),
                "canonical_source": (q["canonical_name"] or {}).get("reason"),
                "aliases": [a["member_key"] for a in q["aliases"]], "mentions": q["mention_count"],
                "credits": [(c["mention_key"], c["raw_value"], c["basis"], c["round"]) for c in q["credits"][:5]],
                "external_ids": [s["value_text"] for s in q["external_ids"]],
                "works": [(w["movie_id"], w["role"]) for w in q["works"][:6]]}
        ex["person"].append(item)
    if ex["person"]:
        ex["alias_merged"].append(ex["person"][0])
    # 商品发售日期不进作品年份：版本发售年份与作品年份不同的作品
    d = _pick(conn, "SELECT m.entity_id, m.value_text, e.entity_id, e.value_text FROM task9.field_selection m "
                    "JOIN task4.product_movie pm ON pm.mapping_version_id = %s AND pm.movie_id = m.entity_id "
                    "AND pm.relation_type = 'edition' JOIN task9.field_selection e ON e.stage_key = m.stage_key "
                    "AND e.entity_type = 'edition' AND e.entity_id = pm.asin AND e.field_name = 'product_release_date' "
                    "AND e.value_text IS NOT NULL WHERE m.stage_key = %s AND m.entity_type = 'movie' "
                    "AND m.field_name = 'release_year' AND m.value_text IS NOT NULL "
                    "AND left(e.value_text, 4) <> m.value_text ORDER BY md5(m.selection_id)",
              (i.t4_mapping_version_id, lk), 1)
    for mid, year, asin, date in d:
        f = field_ex(mid, "release_year")
        ex["edition_date_not_movie_year"].append({"movie_id": mid, "release_year": year, "edition_asin": asin,
                                                  "product_release_date": date, "release_year_evidence": f["evidence"],
                                                  "note": "版本发售日期只在版本字段 product_release_date，不在作品年份的采用路径上"})
    b = _pick(conn, "SELECT y.entity_id FROM task9.field_selection y JOIN task9.field_selection b ON b.stage_key = y.stage_key "
                    "AND b.entity_type = 'movie' AND b.entity_id = y.entity_id AND b.field_name = 'inferred_upper_bound' "
                    "AND b.value_text IS NOT NULL WHERE y.stage_key = %s AND y.entity_type = 'movie' "
                    "AND y.field_name = 'release_year' AND y.value_text IS NULL ORDER BY md5(y.selection_id)", (lk,), 1)
    for (mid,) in b:
        q = query_field(conn, keys, mid, "release_year", max_parents=3)
        bound = q.get("related_inferred_upper_bound") or {}
        ex["year_missing_bound_only"].append({"movie_id": mid, "release_year_status": q["selections"][0]["status"],
                                              "missing_reason": q["selections"][0]["missing_reason"],
                                              "bound": bound.get("value_text"), "bound_status": bound.get("status"),
                                              "bound_rule": bound.get("decision_rule"), "bound_kind": bound.get("reason"),
                                              "bound_evidence": _ev(bound.get("evidence") or [])})
    return ex


TITLES = {"field": "查询 field：作品字段的最终值、候选与原始证据", "product": "查询 product：商品血缘",
          "review": "查询 review：原始评论到逻辑评论与作品", "source": "查询 source：源记录影响的最终字段",
          "person": "查询 person：人物署名、别名归并与参与作品",
          "edition_date_not_movie_year": "作业示例：商品发售日期不进作品年份",
          "third_party_adopted": "作业示例：第三方记录（IMDb）被采用", "alias_merged": "作业示例：署名别名归并到同一 person_id",
          "year_missing_bound_only": "作业示例：上映年份缺失，只有条件性上界"}


def render_examples_markdown(ex: dict[str, list[dict[str, Any]]]) -> str:
    lines = ["# 任务 9 血缘实例", "", "由 `python -m etl_task9.report export` 从数据库只读生成；每例的完整输出可用 "
             "`python -m etl_task9.query` 对应子命令复现。评论只给定位与记录哈希，不含正文。", ""]
    for k, title in TITLES.items():
        lines += [f"## {title}", ""]
        if not ex.get(k):
            lines += ["（本版本没有符合条件的实例）", ""]
        for n, item in enumerate(ex.get(k, []), 1):
            lines.append(f"### 例 {n}")
            for key, val in item.items():
                if isinstance(val, list) and val and isinstance(val[0], str):
                    lines.append(f"- {key}：")
                    lines += [f"  - {v}" for v in val]
                else:
                    lines.append(f"- {key}：{json.dumps(val, ensure_ascii=False, default=str)}")
            lines.append("")
    return "\n".join(lines) + "\n"


def render_markdown(s: dict[str, Any]) -> str:
    c = s["counts"]
    lines = [f"# 任务 9 血缘版本 {s['lineage_version_id']}", "",
             f"- 状态：{s['status']}；运行 {s['run_id']}；规则版本 {s['inputs']['rule_version']}",
             f"- 非空最终值 {c['non_null']}，可追溯 {c['non_null_traceable']}，覆盖率 {c['coverage']}",
             f"- 源记录 {c['source_records']}", f"- 断言 {c['assertions']}", f"- 派生边 {c['derivations']}",
             f"- 重放补定位 {c['relocations']}", f"- 影响闭包 {c['field_impact']}", f"- 质量问题 {s['quality_issues']}",
             f"- 评论层视图（映射状态） {s['review_view_mapping_status']}", "", "## 覆盖率（按实体）", "",
             "| 实体 | 非空 | 可追溯 | 覆盖率 |", "|---|---|---|---|"]
    lines += [f"| {k} | {v['non_null']} | {v['non_null_traceable']} | {v['coverage']} |"
              for k, v in s["coverage_by_entity"].items()]
    lines += ["", "## 覆盖率（按字段与 trace_status）", "", "| 字段 | 选择 | 非空 | 覆盖率 | trace_status |",
              "|---|---|---|---|---|"]
    lines += [f"| {k} | {v['selections']} | {v['non_null']} | {v['coverage']} | "
              f"{json.dumps(v['trace_status'], ensure_ascii=False)} |" for k, v in s["coverage_by_field"].items()]
    return "\n".join(lines) + "\n"


def export_files(conn: psycopg.Connection, lv: str, directory: Path) -> dict[str, Any]:
    """写导出文件与 checksums.json，返回 文件名 → 摘要。"""
    directory.mkdir(parents=True, exist_ok=True)
    summary = build_summary(conn, lv)
    ex = examples(conn, lv)
    keys = keys_for_version(conn, lv)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["stage_key", "entity_type", "entity_id", "issue_code", "detail"])
    for r in conn.execute("SELECT stage_key, entity_type, entity_id, issue_code, detail FROM task9.quality_issue "
                          "WHERE stage_key = ANY(%s) ORDER BY issue_id",
                          ([keys.sources, keys.assertions, keys.derivations, keys.selections, keys.impact],)):
        w.writerow([*r[:4], json.dumps(r[4], ensure_ascii=False)])
    files = {"summary.json": (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode(),
             "report.md": render_markdown(summary).encode(),
             "examples.md": render_examples_markdown(ex).encode(),
             "quality_issues.csv": buf.getvalue().encode()}
    digests: dict[str, Any] = {}
    for name, content in files.items():
        reporting.write_atomic(directory / name, content)
        digests[name] = reporting.file_digest(directory / name)
    reporting.write_atomic(directory / "checksums.json", (json.dumps(digests, ensure_ascii=False, indent=2) + "\n").encode())
    return digests


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl_task9.report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diff", help="两个血缘版本按实体、字段与成员的差异（R34）")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--limit", type=int, default=50)
    e = sub.add_parser("export", help="导出覆盖率、计数、质量问题与实例（R44、R45）")
    e.add_argument("--version", default=None)
    e.add_argument("--run-key", default="manual")
    args = ap.parse_args(argv)
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    s = load_settings(os.environ)
    with connect(s) as conn:
        if args.cmd == "diff":
            out: Any = diff_versions(conn, args.old, args.new, limit=args.limit)
        else:
            from .check import latest_version
            lv = args.version or latest_version(conn)
            eid = keys_for_version(conn, lv).inputs.extraction_id
            directory = run_dir(s.output_dir, eid, lv, args.run_key)
            out = {"directory": str(directory), "files": export_files(conn, lv, directory)}
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
