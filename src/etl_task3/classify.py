"""任务 3 装载核心（spec R01–R05、R17、R22、R24–R26）：输入分组、读快照与抽取、分类、分批写库、核对、版本差异。

数据流见 design“数据流”。要点：
- 无页面的七组用一条 INSERT … SELECT 写待确认分类；dp_page 组按 ASIN 分批，每批的解析、字段、分类、证据同一事务提交。
- 已提交的行本身就是检查点：重跑只处理本规则版本还没有分类的 ASIN；解析结果按解析版本复用，改规则不重解析。
- 同一抽取、同一规则版本同时只允许一个运行（会话级咨询锁）。

命令行（容器内，工作目录 /opt/etl）：
    python -m etl_task3.classify run [--limit N] [--batch-size N]
    python -m etl_task3.classify diff --from t3r1 --to t3r2
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import psycopg
from psycopg.types.json import Jsonb

from etl_task1.config import load_settings
from etl_task1.db import connect, sql_dir
from etl_task2.crawl.crawler import latest_full_extraction, resolve_snapshot

from . import LABEL_MAP_VERSION, PARSER_VERSION, RULE_VERSION
from .extract import Field, extract_fields
from .rules import CATEGORIES, Result, classify

log = logging.getLogger(__name__)

SCHEMA_VERSION = "201"
GROUPS = ("dp_page", "automation_title", "other_site", "access_restricted", "not_found", "timeout",
          "parse_failed", "not_collected")
DEFAULT_BATCH = 500


class ClassifyBusy(RuntimeError):
    """同一抽取、同一规则版本已有运行持有锁（暂时错误，可重试）。"""


class ClassifyInputError(RuntimeError):
    """输入不合格：没有清单、分组之和不等或出现未知模板（确定性错误）。"""


def apply_task3_schema(conn: psycopg.Connection) -> bool:
    """应用 task3 表结构（须先有任务 2 的 101–103）；已应用时不做任何事。返回是否本次新建。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task3_schema'))")
        have = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
        missing = {"101", "102", "103"} - have
        if missing:
            raise RuntimeError(f"任务 2 表结构 {sorted(missing)} 尚未应用，不能创建 task3 表")
        if SCHEMA_VERSION in have:
            return False
        conn.execute((sql_dir() / "201_task3_schema.sql").read_text(encoding="utf-8"))
    return True


def _group_sql() -> str:
    return (sql_dir() / "task3_input_groups.sql").read_text(encoding="utf-8")


def input_groups(conn: psycopg.Connection, eid: str) -> dict[str, int]:
    """R03：8 组数量；未知模板或之和不等即确定性失败。"""
    rows = conn.execute(f"SELECT input_group, count(*) FROM ({_group_sql()}) g GROUP BY 1",
                        {"eid": eid}).fetchall()
    counts = {g: 0 for g in GROUPS}
    for g, n in rows:
        if g is None:
            raise ClassifyInputError(f"{n} 个 success 行的页面模板未知，无法归组")
        counts[g] = int(n)
    total = conn.execute("SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %s",
                         (eid,)).fetchone()[0]
    if total == 0:
        raise ClassifyInputError(f"抽取 {eid} 没有采集清单")
    if sum(counts.values()) != total:
        raise ClassifyInputError(f"输入分组之和 {sum(counts.values())} ≠ 清单行数 {total}")
    return counts


def _lock_key(eid: str, rule_version: str) -> int:
    return int.from_bytes(hashlib.sha256(f"etl_task3:{eid}:{rule_version}".encode()).digest()[:8], "big",
                          signed=True)


@dataclass
class ClassifySummary:
    run_id: str
    extraction_id: str
    rule_version: str
    status: str
    no_page_classified: int = 0
    dp_classified: int = 0
    parsed: int = 0
    reused_parse: int = 0
    parse_status: dict[str, int] = field(default_factory=dict)
    batches: int = 0
    remaining_dp: int = 0


@dataclass
class _Page:
    asin: str
    snapshot_path: str
    content_hash: str
    final_url: str
    fetched_at: Any
    attempt_no: int


def read_page(page: _Page) -> tuple[str, bytes | None, dict[str, Any]]:
    """读快照并核对哈希；返回 (parse_status, 原始字节或 None, 细节)。"""
    path = resolve_snapshot(page.snapshot_path)
    try:
        raw = gzip.decompress(path.read_bytes())
    except FileNotFoundError:
        return "snapshot_missing", None, {"path": str(path)}
    except (OSError, EOFError) as exc:  # gzip 损坏按哈希不一致处理：内容不能信
        return "snapshot_hash_mismatch", None, {"path": str(path), "error": str(exc)[:200]}
    actual = hashlib.sha256(raw).hexdigest()
    if actual != page.content_hash:
        return "snapshot_hash_mismatch", None, {"path": str(path), "expected": page.content_hash, "actual": actual}
    return "parsed", raw, {}


def _close_abandoned(conn: psycopg.Connection, eid: str, rule_version: str) -> None:
    conn.execute("UPDATE task3.classify_run SET status = 'interrupted', finished_at = clock_timestamp(), "
                 "failure_reason = '上一次运行未正常结束（进程中止）' "
                 "WHERE extraction_id = %s AND rule_version = %s AND status = 'running'", (eid, rule_version))


def _classify_no_page(conn: psycopg.Connection, eid: str, rule_version: str, run_id: str) -> int:
    """R04：七组无页面的 ASIN 一律待确认，依据为清单状态与最后一次尝试。"""
    src = _group_sql()
    with conn.transaction():
        n = conn.execute(
            f"""INSERT INTO task3.product_classification
                    (extraction_id, asin, rule_version, input_group, category, basis_level, rule_id,
                     pending_reason, run_id)
                SELECT %(eid)s, g.asin, %(rv)s, g.input_group, 'pending', 'missing', 'NP1', g.input_group, %(run)s
                FROM ({src}) g WHERE g.input_group <> 'dp_page'
                ON CONFLICT DO NOTHING""",
            {"eid": eid, "rv": rule_version, "run": run_id}).rowcount
        conn.execute(
            f"""INSERT INTO task3.classification_evidence
                    (extraction_id, asin, rule_version, seq, signal, supports, used, source_ref)
                SELECT %(eid)s, g.asin, %(rv)s, 1, 'manifest_status', NULL, true,
                       jsonb_build_object('status', g.status, 'status_reason', g.status_reason,
                                          'attempt_no', nullif(g.attempt_count, 0), 'page_template', g.page_template,
                                          'final_url', g.final_url, 'content_hash', g.content_hash)
                FROM ({src}) g
                JOIN task3.product_classification c
                  ON c.extraction_id = %(eid)s AND c.asin = g.asin AND c.rule_version = %(rv)s AND c.run_id = %(run)s
                WHERE g.input_group <> 'dp_page'
                ON CONFLICT DO NOTHING""",
            {"eid": eid, "rv": rule_version, "run": run_id})
    return n


def _pending_dp(conn: psycopg.Connection, eid: str, rule_version: str, after: str, n: int) -> list[_Page]:
    rows = conn.execute(
        f"""SELECT g.asin, g.snapshot_path, g.content_hash, g.final_url, g.fetched_at, g.attempt_count
            FROM ({_group_sql()}) g
            WHERE g.input_group = 'dp_page' AND g.asin COLLATE "C" > %(after)s
              AND NOT EXISTS (SELECT 1 FROM task3.product_classification c
                              WHERE c.extraction_id = %(eid)s AND c.asin = g.asin AND c.rule_version = %(rv)s)
            ORDER BY g.asin COLLATE "C" LIMIT %(n)s""",
        {"eid": eid, "rv": rule_version, "after": after, "n": n}).fetchall()
    return [_Page(*r) for r in rows]


def _count_pending_dp(conn: psycopg.Connection, eid: str, rule_version: str) -> int:
    return conn.execute(
        f"""SELECT count(*) FROM ({_group_sql()}) g WHERE g.input_group = 'dp_page'
            AND NOT EXISTS (SELECT 1 FROM task3.product_classification c
                            WHERE c.extraction_id = %(eid)s AND c.asin = g.asin AND c.rule_version = %(rv)s)""",
        {"eid": eid, "rv": rule_version}).fetchone()[0]


def _existing_parses(conn: psycopg.Connection, eid: str, asins: list[str]) -> dict[str, tuple[str, list[Field]]]:
    out: dict[str, tuple[str, list[Field]]] = {}
    for asin, status in conn.execute(
            "SELECT asin, parse_status FROM task3.page_parse WHERE extraction_id = %s AND parser_version = %s "
            "AND asin = ANY(%s)", (eid, PARSER_VERSION, asins)).fetchall():
        out[asin] = (status, [])
    for asin, section, ordinal, label, value, key in conn.execute(
            "SELECT asin, section, ordinal, label_raw, value_raw, field_key FROM task3.page_field "
            "WHERE extraction_id = %s AND parser_version = %s AND asin = ANY(%s) ORDER BY asin, section, ordinal",
            (eid, PARSER_VERSION, asins)).fetchall():
        out[asin][1].append(Field(section, ordinal, label, value, key))
    return out


_PENDING_BY_PARSE = {"snapshot_missing": "SN1", "snapshot_hash_mismatch": "SN2", "parse_error": "SN3"}


def _write_batch(conn: psycopg.Connection, eid: str, rule_version: str, run_id: str, pages: list[_Page],
                 classifier: Callable[[Iterable[Field]], Result], summary: ClassifySummary,
                 before_commit: Callable[[int], None] | None) -> None:
    existing = _existing_parses(conn, eid, [p.asin for p in pages])
    parse_rows, field_rows, issue_rows, class_rows, ev_rows = [], [], [], [], []
    for page in pages:
        if page.asin in existing:
            status, fields = existing[page.asin]
            summary.reused_parse += 1
        else:
            status, raw, detail = read_page(page)
            fields, present = [], None
            if raw is not None:
                try:
                    fields, present = extract_fields(raw)
                except Exception as exc:  # noqa: BLE001  单页解析异常不中断整批
                    status, detail = "parse_error", {"error": f"{type(exc).__name__}: {exc}"[:300]}
                    fields, present = [], None
            parse_rows.append((eid, page.asin, PARSER_VERSION, page.content_hash, page.snapshot_path,
                               page.final_url, page.fetched_at, page.attempt_no, status,
                               Jsonb(present) if present is not None else None, len(fields), run_id))
            field_rows += [(eid, page.asin, PARSER_VERSION, f.section, f.ordinal, f.label_raw, f.value_raw,
                            f.field_key) for f in fields]
            if status != "parsed":
                issue_rows.append((eid, page.asin, PARSER_VERSION, status, Jsonb(detail)))
            elif present is not None and not any(present.values()):
                issue_rows.append((eid, page.asin, PARSER_VERSION, "no_evidence_sections", Jsonb({})))
            summary.parsed += 1
        summary.parse_status[status] = summary.parse_status.get(status, 0) + 1
        if status == "parsed":
            result = classifier(fields)
            class_rows.append((eid, page.asin, rule_version, "dp_page", result.category, result.basis_level,
                               result.rule_id, result.pending_reason, PARSER_VERSION, run_id))
            for seq, ev in enumerate(result.evidence, start=1):
                s = ev.signal
                ev_rows.append((eid, page.asin, rule_version, seq, s.name, s.supports, ev.used, PARSER_VERSION,
                                s.section, s.ordinal, s.value_raw, s.value_num, None))
            if not result.evidence:  # 页面上一个信号都没有：依据就是“抽到了什么”
                ev_rows.append((eid, page.asin, rule_version, 1, "no_signals", None, True, PARSER_VERSION, None,
                                None, None, None, Jsonb({"field_count": len(fields),
                                                         "field_keys": sorted({f.field_key for f in fields
                                                                               if f.field_key})})))
        else:  # R05：不使用该页内容，证据是快照检查结果
            class_rows.append((eid, page.asin, rule_version, "dp_page", "pending", "missing",
                               _PENDING_BY_PARSE[status], status, PARSER_VERSION, run_id))
            ev_rows.append((eid, page.asin, rule_version, 1, "snapshot_check", None, True, PARSER_VERSION, None,
                            None, None, None, Jsonb({"parse_status": status, "snapshot_path": page.snapshot_path,
                                                     "content_hash": page.content_hash})))
    with conn.transaction():
        with conn.cursor() as cur:
            if parse_rows:
                cur.executemany(
                    "INSERT INTO task3.page_parse (extraction_id, asin, parser_version, content_hash, snapshot_path, "
                    "final_url, fetched_at, attempt_no, parse_status, sections_present, field_count, run_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", parse_rows)
            _copy(cur, "task3.page_field", ("extraction_id", "asin", "parser_version", "section", "ordinal",
                                            "label_raw", "value_raw", "field_key"), field_rows)
            if issue_rows:
                cur.executemany("INSERT INTO task3.quality_issue (extraction_id, asin, parser_version, issue_code, "
                                "detail) VALUES (%s, %s, %s, %s, %s)", issue_rows)
            cur.executemany(
                "INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, category, "
                "basis_level, rule_id, pending_reason, parser_version, run_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", class_rows)
            _copy(cur, "task3.classification_evidence",
                  ("extraction_id", "asin", "rule_version", "seq", "signal", "supports", "used", "parser_version",
                   "section", "ordinal", "value_raw", "value_num", "source_ref"), ev_rows)
        if before_commit:
            before_commit(summary.batches + 1)
    summary.batches += 1
    summary.dp_classified += len(class_rows)


def _copy(cur, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def classify_extraction(conn: psycopg.Connection, eid: str, *, rule_version: str = RULE_VERSION,
                        classifier: Callable[[Iterable[Field]], Result] = classify, batch_size: int = DEFAULT_BATCH,
                        limit: int | None = None, dag_id: str | None = None, airflow_run_id: str | None = None,
                        before_commit: Callable[[int], None] | None = None) -> ClassifySummary:
    """一次运行：无页面组 → dp_page 分批。limit 限制本次处理的 dp_page 数（试跑用）。"""
    if not conn.autocommit:
        raise ValueError("装载连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if batch_size < 1 or (limit is not None and limit < 1):
        raise ValueError("batch_size 与 limit 必须为正整数")
    input_groups(conn, eid)
    key = _lock_key(eid, rule_version)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise ClassifyBusy(f"抽取 {eid} 的规则版本 {rule_version} 正由另一个运行处理")
    run_id = uuid.uuid4().hex
    summary = ClassifySummary(run_id, eid, rule_version, "running")
    try:
        _close_abandoned(conn, eid, rule_version)
        conn.execute("INSERT INTO task3.classify_run (run_id, extraction_id, parser_version, label_map_version, "
                     "rule_version, dag_id, airflow_run_id, params) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     (run_id, eid, PARSER_VERSION, LABEL_MAP_VERSION, rule_version, dag_id, airflow_run_id,
                      Jsonb({"batch_size": batch_size, "limit": limit})))
        try:
            summary.no_page_classified = _classify_no_page(conn, eid, rule_version, run_id)
            after, started = "", time.monotonic()
            while limit is None or summary.dp_classified < limit:
                n = batch_size if limit is None else min(batch_size, limit - summary.dp_classified)
                pages = _pending_dp(conn, eid, rule_version, after, n)
                if not pages:
                    break
                _write_batch(conn, eid, rule_version, run_id, pages, classifier, summary, before_commit)
                after = pages[-1].asin
                if summary.batches % 10 == 0:
                    log.info("已分类 %d 个 dp_page（%.0f s）", summary.dp_classified, time.monotonic() - started)
            summary.remaining_dp = _count_pending_dp(conn, eid, rule_version)
            new_rows = summary.no_page_classified + summary.dp_classified
            summary.status = "succeeded" if new_rows else "reused"
            conn.execute("UPDATE task3.classify_run SET status = %s, parsed = %s, classified = %s, "
                         "finished_at = clock_timestamp() WHERE run_id = %s",
                         (summary.status, summary.parsed, new_rows, run_id))
        except BaseException as exc:
            summary.status = "failed"
            try:
                conn.execute("UPDATE task3.classify_run SET status = 'failed', parsed = %s, classified = %s, "
                             "finished_at = clock_timestamp(), failure_reason = %s WHERE run_id = %s",
                             (summary.parsed, summary.no_page_classified + summary.dp_classified,
                              f"{type(exc).__name__}: {exc}"[:2000], run_id))
            except psycopg.Error:
                log.exception("无法登记失败审计")
            raise
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except psycopg.Error:
            pass
    return summary


# ---------------------------------------------------------------- 核对与版本差异

VERIFY_SQL = """
WITH c AS (SELECT * FROM task3.product_classification WHERE extraction_id = %(eid)s AND rule_version = %(rv)s)
SELECT
  (SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %(eid)s) AS manifest_rows,
  (SELECT count(*) FROM c) AS classification_rows,
  (SELECT count(*) FROM task2.product_manifest m WHERE m.extraction_id = %(eid)s
     AND NOT EXISTS (SELECT 1 FROM c WHERE c.asin = m.asin)) AS missing_classification,
  (SELECT count(*) FROM c WHERE c.category <> 'pending' AND NOT EXISTS (
     SELECT 1 FROM task3.classification_evidence e WHERE e.extraction_id = c.extraction_id AND e.asin = c.asin
       AND e.rule_version = c.rule_version AND e.used)) AS classified_without_evidence,
  (SELECT count(*) FROM c WHERE NOT EXISTS (
     SELECT 1 FROM task3.classification_evidence e WHERE e.extraction_id = c.extraction_id AND e.asin = c.asin
       AND e.rule_version = c.rule_version)) AS rows_without_any_evidence,
  (SELECT count(*) FROM task3.page_parse p WHERE p.extraction_id = %(eid)s AND p.parser_version = %(pv)s
     AND p.parse_status = 'parsed' AND p.field_count <> (SELECT count(*) FROM task3.page_field f
       WHERE f.extraction_id = p.extraction_id AND f.asin = p.asin AND f.parser_version = p.parser_version))
     AS field_count_mismatch,
  (SELECT count(*) FROM task3.page_parse WHERE extraction_id = %(eid)s AND parser_version = %(pv)s
     AND parse_status = 'snapshot_hash_mismatch') AS snapshot_hash_mismatch,
  (SELECT count(*) FROM task3.page_parse WHERE extraction_id = %(eid)s AND parser_version = %(pv)s
     AND parse_status = 'snapshot_missing') AS snapshot_missing,
  (SELECT count(*) FROM task3.page_parse WHERE extraction_id = %(eid)s AND parser_version = %(pv)s
     AND parse_status = 'parse_error') AS parse_error,
  (SELECT count(*) FROM public.review_record WHERE extraction_id = %(eid)s) AS records_read,
  (SELECT count(*) FROM public.review_record WHERE extraction_id = %(eid)s
     AND product_id_observed IS NULL) AS reviews_without_asin,
  (SELECT coalesce(sum(m.records_total), 0) FROM task2.product_manifest m JOIN c ON c.asin = m.asin
     WHERE m.extraction_id = %(eid)s) AS reviews_by_category_total
"""


def verify(conn: psycopg.Connection, eid: str, rule_version: str = RULE_VERSION) -> dict[str, Any]:
    """R02、R17、R22：只读核对，返回计数；problems(out) 为空表示通过。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        cur = conn.execute(VERIFY_SQL, {"eid": eid, "rv": rule_version, "pv": PARSER_VERSION})
        out: dict[str, Any] = dict(zip((d.name for d in cur.description), (int(v) for v in cur.fetchone())))
        out["categories"] = {c: 0 for c in CATEGORIES} | dict(conn.execute(
            "SELECT category, count(*) FROM task3.product_classification WHERE extraction_id = %s "
            "AND rule_version = %s GROUP BY 1", (eid, rule_version)).fetchall())
        out["input_groups"] = {g: 0 for g in GROUPS} | dict(conn.execute(
            "SELECT input_group, count(*) FROM task3.product_classification WHERE extraction_id = %s "
            "AND rule_version = %s GROUP BY 1", (eid, rule_version)).fetchall())
        out["expected_groups"] = input_groups(conn, eid)
    out.update(extraction_id=eid, rule_version=rule_version, parser_version=PARSER_VERSION,
               category_sum=sum(out["categories"].values()))
    return out


def problems(out: dict[str, Any]) -> list[str]:
    found = []
    if out["classification_rows"] != out["manifest_rows"] or out["missing_classification"]:
        found.append(f"分类 {out['classification_rows']} 行、缺 {out['missing_classification']} 个，"
                     f"清单 {out['manifest_rows']} 行")
    if out["category_sum"] != out["classification_rows"]:
        found.append("六类之和不等于分类行数")
    if out["input_groups"] != out["expected_groups"]:
        found.append(f"分类的输入组分布 {out['input_groups']} ≠ 清单分组 {out['expected_groups']}")
    for k in ("classified_without_evidence", "rows_without_any_evidence", "field_count_mismatch"):
        if out[k]:
            found.append(f"{k} = {out[k]}")
    if out["reviews_by_category_total"] + out["reviews_without_asin"] != out["records_read"]:
        found.append(f"按类评论数 {out['reviews_by_category_total']} + 无产品记录 {out['reviews_without_asin']} "
                     f"≠ 读取数 {out['records_read']}")
    return found


def diff_versions(conn: psycopg.Connection, eid: str, old: str, new: str) -> list[dict[str, Any]]:
    """R25：两个规则版本间类别或命中规则发生变化的 ASIN。"""
    rows = conn.execute(
        """SELECT a.asin, a.category, b.category, a.rule_id, b.rule_id
           FROM task3.product_classification a
           JOIN task3.product_classification b ON b.extraction_id = a.extraction_id AND b.asin = a.asin
           WHERE a.extraction_id = %s AND a.rule_version = %s AND b.rule_version = %s
             AND (a.category, a.rule_id) IS DISTINCT FROM (b.category, b.rule_id)
           ORDER BY a.asin COLLATE "C" """, (eid, old, new)).fetchall()
    return [{"asin": r[0], "old_category": r[1], "new_category": r[2], "old_rule": r[3], "new_rule": r[4]}
            for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 3 电影页面筛选")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="解析并分类（重跑只处理未完成的 ASIN）")
    run.add_argument("--extraction-id")
    run.add_argument("--limit", type=int)
    run.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    d = sub.add_parser("diff", help="比较两个规则版本")
    d.add_argument("--extraction-id")
    d.add_argument("--from", dest="old", required=True)
    d.add_argument("--to", dest="new", required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with connect(load_settings()) as conn:
        apply_task3_schema(conn)
        eid = args.extraction_id or latest_full_extraction(conn)
        if args.cmd == "run":
            s = classify_extraction(conn, eid, batch_size=args.batch_size, limit=args.limit)
            print(json.dumps(s.__dict__, ensure_ascii=False, indent=2))
            return 0
        changes = diff_versions(conn, eid, args.old, args.new)
        print(json.dumps({"changed": len(changes), "rows": changes}, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(main())
