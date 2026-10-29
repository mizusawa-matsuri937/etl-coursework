"""任务 3 统计与导出（spec R18–R23、R28）：summary 各块、CSV 导出、人工抽查表的导出与导入。

口径见 design“统计口径”“人工抽查”。全部查询限定抽取与规则版本；导出不含评论正文与整页 HTML。

命令行（容器内，工作目录 /opt/etl）：
    python -m etl_task3.report summary                    # 打印统计 JSON
    python -m etl_task3.report review-export              # 导出抽查表
    python -m etl_task3.report review-import <csv> --reviewer <姓名或代号>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect, sql_dir
from etl_task2.crawl.crawler import latest_full_extraction

from . import LABEL_MAP_VERSION, PARSER_VERSION, RULE_VERSION
from .classify import GROUPS, apply_task3_schema
from .rules import BASIS_LEVELS, CATEGORIES

STATUSES = ("success", "not_found", "access_restricted", "timeout", "parse_failed", "not_collected")
FIELD_KEYS = ("title", "breadcrumb", "genre", "media_format", "runtime", "director", "actors", "writers",
              "producers", "contributor", "mpaa_rating", "studio", "language", "subtitles", "dubbed",
              "number_of_discs", "aspect_ratio", "product_release_date", "upc", "edition")
FIELD_NOTES = {"product_release_date": "商品发售日期候选，不是电影上映日期（R21）"}
DEV_SAMPLE_SIZE = 1500          # 规则开发样本：dp_page 按 md5(asin) 排序的前 1,500 个（spec H06）
REVIEW_PER_STRATUM = 20         # 人工抽查每类最多 20 个（spec H07）
REVIEW_COLUMNS = ("asin", "sample_stratum", "auto_category", "basis_level", "rule_id", "pending_reason", "title",
                  "product_url", "snapshot_path", "used_signals", "conflict_signals", "human_category", "note")


def output_root(output_dir: Path) -> Path:
    return output_dir / "task3"


def run_dir(output_dir: Path, eid: str, rule_version: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / rule_version / run_key


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 6) if d else None


def _dict(conn, sql: str, params: dict) -> dict:
    return {k: int(v) for k, v in conn.execute(sql, params).fetchall()}


def build_summary(conn: psycopg.Connection, eid: str, rule_version: str = RULE_VERSION) -> dict[str, Any]:
    p = {"eid": eid, "rv": rule_version, "pv": PARSER_VERSION}
    status = {s: 0 for s in STATUSES} | _dict(
        conn, "SELECT status, count(*) FROM task2.product_manifest WHERE extraction_id = %(eid)s GROUP BY 1", p)
    total = sum(status.values())
    cls = "FROM task3.product_classification WHERE extraction_id = %(eid)s AND rule_version = %(rv)s"
    categories = {c: 0 for c in CATEGORIES} | _dict(conn, f"SELECT category, count(*) {cls} GROUP BY 1", p)
    groups = {g: 0 for g in GROUPS} | _dict(conn, f"SELECT input_group, count(*) {cls} GROUP BY 1", p)
    by_group = {g: {c: 0 for c in CATEGORIES} for g in GROUPS}
    for g, c, n in conn.execute(f"SELECT input_group, category, count(*) {cls} GROUP BY 1, 2", p).fetchall():
        by_group[g][c] = int(n)
    by_basis = {c: {b: 0 for b in BASIS_LEVELS} for c in CATEGORIES}
    for c, b, n in conn.execute(f"SELECT category, basis_level, count(*) {cls} GROUP BY 1, 2", p).fetchall():
        by_basis[c][b] = int(n)
    rules = _dict(conn, f"SELECT rule_id, count(*) {cls} GROUP BY 1 ORDER BY 1", p)
    pending = _dict(conn, f"SELECT pending_reason, count(*) {cls} AND category = 'pending' GROUP BY 1", p)
    dp_pages = groups["dp_page"]
    dp_parsed = conn.execute("SELECT count(*) FROM task3.page_parse WHERE extraction_id = %(eid)s "
                             "AND parser_version = %(pv)s AND parse_status = 'parsed'", p).fetchone()[0]
    recognized = sum(n for c, n in categories.items() if c != "pending")
    dp_recognized = sum(n for c, n in by_group["dp_page"].items() if c != "pending")
    movies = categories["movie"]

    have = {}
    for key, movie_n, page_n in conn.execute(
            """SELECT f.field_key, count(*) FILTER (WHERE c.category = 'movie'), count(*)
               FROM (SELECT DISTINCT asin, field_key FROM task3.page_field
                     WHERE extraction_id = %(eid)s AND parser_version = %(pv)s
                       AND field_key IS NOT NULL AND value_raw <> '') f
               JOIN task3.product_classification c
                 ON c.extraction_id = %(eid)s AND c.asin = f.asin AND c.rule_version = %(rv)s
               GROUP BY 1""", p).fetchall():
        have[key] = (int(movie_n), int(page_n))
    coverage = []
    for key in FIELD_KEYS:
        movie_n, page_n = have.get(key, (0, 0))
        coverage.append({
            "field_key": key, "note": FIELD_NOTES.get(key),
            "movie_with_value": movie_n, "movie_missing": movies - movie_n, "movie_ratio": _ratio(movie_n, movies),
            "dp_parsed_with_value": page_n, "dp_parsed_missing": dp_parsed - page_n,
            "dp_parsed_ratio": _ratio(page_n, dp_parsed),
            "all_products_with_value": page_n, "all_products_ratio": _ratio(page_n, total),
            "inferred": 0, "manually_reviewed": 0})

    reviews = {c: {"records_total": 0, "records_valid": 0, "records_quarantined": 0} for c in CATEGORIES}
    for c, t, v, q in conn.execute(
            """SELECT c.category, sum(m.records_total), sum(m.records_valid), sum(m.records_quarantined)
               FROM task3.product_classification c
               JOIN task2.product_manifest m ON m.extraction_id = c.extraction_id AND m.asin = c.asin
               WHERE c.extraction_id = %(eid)s AND c.rule_version = %(rv)s GROUP BY 1""", p).fetchall():
        reviews[c] = {"records_total": int(t), "records_valid": int(v), "records_quarantined": int(q)}
    read, no_asin = conn.execute("SELECT count(*), count(*) FILTER (WHERE product_id_observed IS NULL) "
                                 "FROM public.review_record WHERE extraction_id = %(eid)s", p).fetchone()
    by_cat_total = sum(r["records_total"] for r in reviews.values())

    return {
        "extraction_id": eid, "rule_version": rule_version, "parser_version": PARSER_VERSION,
        "label_map_version": LABEL_MAP_VERSION,
        "collection_status": {"distribution": status, "total_products": total,
                              "success_rate": _ratio(status["success"], total)},
        "source_kind": {"live_crawl": status["success"], "offline_snapshot": 0, "third_party": 0,
                        "note": "任务 2 爬虫取得并保存快照的成功页计入实时采集；未使用外部归档快照或第三方数据（spec H04）"},
        "input_groups": groups,
        "classification": {
            "categories": categories, "by_input_group": by_group, "by_basis_level": by_basis,
            "rules": rules, "pending_reason": pending,
            "movie_structured": by_basis["movie"]["structured"], "movie_inferred": by_basis["movie"]["inferred"],
            "recognized": recognized, "recognition_coverage_all_products": _ratio(recognized, total),
            "dp_page_recognized": dp_recognized, "recognition_coverage_dp_page": _ratio(dp_recognized, dp_pages),
            "dp_page_parsed": int(dp_parsed),
        },
        "field_coverage": coverage,
        "reviews_by_category": {"by_category": reviews, "category_total": by_cat_total,
                                "records_without_asin": int(no_asin), "records_read": int(read),
                                "consistent": by_cat_total + int(no_asin) == int(read)},
        "manual_review": review_summary(conn, eid, rule_version),
    }


def export_files(conn: psycopg.Connection, eid: str, rule_version: str, directory: Path,
                 summary: dict[str, Any]) -> list[dict[str, Any]]:
    """classification.csv（每个 ASIN 一行，含标题与用到的信号）与 field_coverage.csv。"""
    directory.mkdir(parents=True, exist_ok=True)
    sql = (sql_dir() / "task3_classification_export.sql").read_text(encoding="utf-8")
    dest = directory / "classification.csv"
    tmp = dest.with_name(f".{dest.name}.tmp")
    rows = 0
    with open(tmp, "w", newline="", encoding="utf-8") as fh, conn.cursor() as cur:
        w = csv.writer(fh)
        cur.execute(sql, {"eid": eid, "rv": rule_version, "pv": PARSER_VERSION})
        w.writerow([d.name for d in cur.description])
        for r in cur:
            w.writerow(r)
            rows += 1
    tmp.replace(dest)
    buf = io.StringIO()
    keys = list(summary["field_coverage"][0])
    w = csv.DictWriter(buf, fieldnames=keys)
    w.writeheader()
    w.writerows(summary["field_coverage"])
    reporting.write_atomic(directory / "field_coverage.csv", buf.getvalue().encode())
    return [{**reporting.file_digest(dest), "rows": rows},
            {**reporting.file_digest(directory / "field_coverage.csv"), "rows": len(summary["field_coverage"])}]


def render_markdown(s: dict[str, Any]) -> str:
    fmt = lambda n: f"{n:,}" if isinstance(n, int) else ("—" if n is None else str(n))  # noqa: E731
    pct = lambda r: "—" if r is None else f"{r * 100:.2f}%"  # noqa: E731
    lines = ["# 任务 3 电影页面筛选报告", "",
             f"- 最终状态：**{s['final_status']}**（交付完整：{s['delivery_complete']}）",
             f"- DAG / run：`{s['dag_id']}` / `{s['run_key']}`",
             f"- 抽取：`{s['extraction_id']}`；规则 {s.get('rule_version')}、解析 {s.get('parser_version')}、"
             f"标签映射 {s.get('label_map_version')}", "", "## 任务状态", ""]
    lines += [f"- {k}：{v}" for k, v in s["tasks"].items()]
    st = s.get("stats")
    if st:
        cs = st["collection_status"]
        lines += ["", "## 采集状态（分母为全部产品）", "", "| 状态 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in cs["distribution"].items()]
        lines += [f"| 合计 | {fmt(cs['total_products'])} |", "",
                  f"成功采集率 {pct(cs['success_rate'])}（{fmt(cs['distribution']['success'])} / "
                  f"{fmt(cs['total_products'])}）。按来源分别统计：实时采集 {fmt(st['source_kind']['live_crawl'])}、"
                  f"离线快照 {fmt(st['source_kind']['offline_snapshot'])}、第三方补充 "
                  f"{fmt(st['source_kind']['third_party'])}，不合并为一个指标。",
                  "", "## 输入分组", "", "| 输入组 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in st["input_groups"].items()]
        c = st["classification"]
        lines += ["", "## 分类", "", "| 类别 | 数量 | 结构化 | 推断 | 缺失 |", "|---|---|---|---|---|"]
        for cat, n in c["categories"].items():
            b = c["by_basis_level"][cat]
            lines.append(f"| {cat} | {fmt(n)} | {fmt(b['structured'])} | {fmt(b['inferred'])} | {fmt(b['missing'])} |")
        lines += ["", f"- 电影识别数量：{fmt(c['categories']['movie'])}（结构化 {fmt(c['movie_structured'])}、"
                      f"推断 {fmt(c['movie_inferred'])}）",
                  f"- 识别覆盖率（非待确认 / 全部产品）：{pct(c['recognition_coverage_all_products'])}",
                  f"- 识别覆盖率（非待确认 / dp_page）：{pct(c['recognition_coverage_dp_page'])}",
                  "- “结构化”只说明证据来自页面结构化字段，不等于已核实；已核实只指人工抽查一致的产品。",
                  "", "| 待确认原因 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in sorted(c["pending_reason"].items())]
        lines += ["", "| 命中规则 | 数量 |", "|---|---|"] + [f"| {k} | {fmt(v)} |" for k, v in c["rules"].items()]
        lines += ["", "## 字段覆盖率", "",
                  "字段值都直接来自页面，没有推断值。电影类以 ASIN 级电影产品为分母（合并后的作品在任务 4 生成）。", "",
                  "| 字段 | 电影类 | dp_page 已解析 | 全部产品 |", "|---|---|---|---|"]
        for f in st["field_coverage"]:
            note = f"（{f['note']}）" if f["note"] else ""
            lines.append(f"| {f['field_key']}{note} | {fmt(f['movie_with_value'])}（{pct(f['movie_ratio'])}） | "
                         f"{fmt(f['dp_parsed_with_value'])}（{pct(f['dp_parsed_ratio'])}） | "
                         f"{fmt(f['all_products_with_value'])}（{pct(f['all_products_ratio'])}） |")
        r = st["reviews_by_category"]
        lines += ["", "## 评论按分类（不删不改）", "", "| 类别 | 记录数 | 有效 | 隔离 |", "|---|---|---|---|"]
        lines += [f"| {k} | {fmt(v['records_total'])} | {fmt(v['records_valid'])} | {fmt(v['records_quarantined'])} |"
                  for k, v in r["by_category"].items()]
        lines += [f"| 无产品标识 | {fmt(r['records_without_asin'])} | | |", "",
                  f"按类之和 {fmt(r['category_total'])} + 无产品标识 {fmt(r['records_without_asin'])} = "
                  f"{fmt(r['category_total'] + r['records_without_asin'])}，任务 1 读取数 {fmt(r['records_read'])}，"
                  f"一致：{r['consistent']}。"]
        mr = st.get("manual_review")
        if mr and mr["reviewed"]:
            lines += ["", "## 人工抽查（只代表抽查样本）", "",
                      f"标注人：{', '.join(mr['reviewers'])}；已标注 {mr['reviewed']} 个。", "",
                      "| 自动类别 | 样本 | 一致 | 一致率 |", "|---|---|---|---|"]
            lines += [f"| {k} | {v['reviewed']} | {v['agree']} | {pct(v['agree_rate'])} |"
                      for k, v in mr["by_stratum"].items()]
    if s.get("exports"):
        lines += ["", "## 导出文件", ""]
        lines += [f"- `{f['path']}`：{fmt(f['rows'])} 行，{fmt(f['bytes'])} 字节，SHA-256 `{f['sha256']}`"
                  for f in s["exports"]]
    if s["failures"]:
        lines += ["", "## 失败记录", ""]
        lines += [f"- {f.get('task')} 第 {f.get('try_number')} 次：{f.get('error_type')}："
                  f"{str(f.get('message'))[:300]}（暂时错误：{f.get('transient')}）" for f in s["failures"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 人工抽查（R28）

REVIEW_SQL = """
WITH g AS ({groups}),
dev AS (SELECT asin FROM g WHERE input_group = 'dp_page' ORDER BY md5(asin) LIMIT %(dev)s),
c AS (SELECT c.*, row_number() OVER (PARTITION BY c.category ORDER BY md5(c.asin || %(rv)s)) AS rn
      FROM task3.product_classification c
      WHERE c.extraction_id = %(eid)s AND c.rule_version = %(rv)s AND c.input_group = 'dp_page'
        AND c.asin NOT IN (SELECT asin FROM dev))
SELECT c.asin, c.category AS sample_stratum, c.category AS auto_category, c.basis_level, c.rule_id,
       c.pending_reason,
       (SELECT f.value_raw FROM task3.page_field f WHERE f.extraction_id = c.extraction_id AND f.asin = c.asin
          AND f.parser_version = c.parser_version AND f.section = 'title' AND f.ordinal = 1) AS title,
       m.original_url AS product_url, m.snapshot_path,
       (SELECT string_agg(e.signal || '=' || coalesce(e.value_raw, ''), ' | ' ORDER BY e.seq)
          FROM task3.classification_evidence e WHERE e.extraction_id = c.extraction_id AND e.asin = c.asin
          AND e.rule_version = c.rule_version AND e.used) AS used_signals,
       (SELECT string_agg(e.signal || '→' || e.supports || '=' || coalesce(e.value_raw, ''), ' | ' ORDER BY e.seq)
          FROM task3.classification_evidence e WHERE e.extraction_id = c.extraction_id AND e.asin = c.asin
          AND e.rule_version = c.rule_version AND NOT e.used AND e.supports IS NOT NULL
          AND e.supports <> c.category) AS conflict_signals,
       '' AS human_category, '' AS note
FROM c JOIN task2.product_manifest m ON m.extraction_id = c.extraction_id AND m.asin = c.asin
WHERE c.rn <= %(per)s
ORDER BY c.category, c.rn
"""


def _review_rows(conn: psycopg.Connection, eid: str, rule_version: str) -> list[tuple]:
    groups = (sql_dir() / "task3_input_groups.sql").read_text(encoding="utf-8")
    return conn.execute(REVIEW_SQL.format(groups=groups),
                        {"eid": eid, "rv": rule_version, "dev": DEV_SAMPLE_SIZE, "per": REVIEW_PER_STRATUM}).fetchall()


def review_dir(output_dir: Path, eid: str, rule_version: str) -> Path:
    return output_root(output_dir) / eid / rule_version / "review"


def review_export(conn: psycopg.Connection, eid: str, rule_version: str, directory: Path) -> dict[str, Any]:
    rows = _review_rows(conn, eid, rule_version)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(REVIEW_COLUMNS)
    w.writerows(rows)
    dest = directory / "review_sample.csv"
    reporting.write_atomic(dest, buf.getvalue().encode("utf-8"))
    strata: dict[str, int] = {}
    for r in rows:
        strata[r[1]] = strata.get(r[1], 0) + 1
    return {**reporting.file_digest(dest), "file": str(dest), "rows": len(rows), "by_stratum": strata}


class ReviewImportError(ValueError):
    """抽查表与导出时不一致或取值非法。"""


def review_import(conn: psycopg.Connection, eid: str, rule_version: str, path: Path, reviewer: str) -> dict[str, Any]:
    """写入已标注的行；ASIN 集合必须与当前抽样一致，空白标注跳过；同一 ASIN 以后导入的为准。"""
    if not reviewer.strip():
        raise ReviewImportError("必须提供标注人 --reviewer")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    expected = {r[0]: r for r in _review_rows(conn, eid, rule_version)}
    got = {r["asin"] for r in rows}
    if got != set(expected):
        raise ReviewImportError(f"ASIN 集合与抽样不一致：多出 {sorted(got - set(expected))[:5]}，"
                                f"缺少 {sorted(set(expected) - got)[:5]}")
    labeled, skipped = [], 0
    for r in rows:
        label = (r.get("human_category") or "").strip()
        if not label:
            skipped += 1
            continue
        if label not in CATEGORIES:
            raise ReviewImportError(f"{r['asin']} 的 human_category={label!r} 不是 {CATEGORIES} 之一")
        e = expected[r["asin"]]
        labeled.append((eid, r["asin"], rule_version, e[1], e[2], label, (r.get("note") or "").strip() or None,
                        reviewer.strip(), digest))
    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO task3.manual_review (extraction_id, asin, rule_version, sample_stratum, auto_category,
                   human_category, note, reviewer, import_file_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (extraction_id, asin, rule_version) DO UPDATE SET
                   human_category = EXCLUDED.human_category, note = EXCLUDED.note, reviewer = EXCLUDED.reviewer,
                   reviewed_at = clock_timestamp(), import_file_hash = EXCLUDED.import_file_hash
               WHERE manual_review.import_file_hash <> EXCLUDED.import_file_hash""", labeled)
    return {"file_sha256": digest, "rows": len(rows), "labeled": len(labeled), "unlabeled": skipped}


def review_summary(conn: psycopg.Connection, eid: str, rule_version: str) -> dict[str, Any]:
    rows = conn.execute("SELECT asin, sample_stratum, auto_category, human_category, note, reviewer "
                        "FROM task3.manual_review WHERE extraction_id = %s AND rule_version = %s "
                        "ORDER BY sample_stratum, asin", (eid, rule_version)).fetchall()
    by: dict[str, dict[str, Any]] = {}
    mismatches = []
    for asin, stratum, auto, human, note, _ in rows:
        b = by.setdefault(stratum, {"reviewed": 0, "agree": 0})
        b["reviewed"] += 1
        if auto == human:
            b["agree"] += 1
        else:
            mismatches.append({"asin": asin, "auto_category": auto, "human_category": human, "note": note})
    for b in by.values():
        b["agree_rate"] = _ratio(b["agree"], b["reviewed"])
    return {"reviewed": len(rows), "agree": sum(b["agree"] for b in by.values()),
            "reviewers": sorted({r[5] for r in rows}), "by_stratum": by, "mismatches": mismatches,
            "scope_note": "只代表抽查样本（dp_page 中按自动类别分层、排除规则开发样本），不能代表全量准确率"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 3 统计与人工抽查")
    ap.add_argument("cmd", choices=("summary", "review-export", "review-import"))
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--extraction-id")
    ap.add_argument("--rule-version", default=RULE_VERSION)
    ap.add_argument("--reviewer", default="")
    args = ap.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        apply_task3_schema(conn)
        eid = args.extraction_id or latest_full_extraction(conn)
        if args.cmd == "summary":
            out: dict[str, Any] = build_summary(conn, eid, args.rule_version)
        elif args.cmd == "review-export":
            out = review_export(conn, eid, args.rule_version, review_dir(s.output_dir, eid, args.rule_version))
        else:
            if not args.csv:
                ap.error("review-import 需要 csv 路径")
            out = review_import(conn, eid, args.rule_version, Path(args.csv), args.reviewer)
            out["summary"] = review_summary(conn, eid, args.rule_version)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
