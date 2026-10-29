"""爬虫结果核对（只读，人工复核与 DAG 的 verify_crawl 共用）：状态之和、成功证据、尝试次数与日志一致、
快照哈希，并统计页面模板与跳转（R32、R33）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task2.crawl.check                 # 核对全部快照
    python -m etl_task2.crawl.check --sample 500    # 随机抽 500 个快照核对哈希
    python -m etl_task2.crawl.check --run-id <ID>   # 只核对某次运行写入的快照
输出一段 JSON；全部一致时退出码 0，否则 1。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from typing import Any

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction, resolve_snapshot

COUNTS_SQL = """
SELECT count(*),
       count(*) FILTER (WHERE status = 'success' AND (final_url IS NULL OR http_status IS NULL
                        OR fetched_at IS NULL OR snapshot_path IS NULL OR content_hash IS NULL)),
       count(*) FILTER (WHERE status <> 'not_collected' AND attempt_count = 0),
       count(*) FILTER (WHERE status = 'not_collected' AND attempt_count > 0),
       count(*) FILTER (WHERE attempt_count <> (SELECT count(*) FROM task2.fetch_attempt a
                                               WHERE a.extraction_id = m.extraction_id AND a.asin = m.asin)),
       count(*) FILTER (WHERE status = 'success' AND final_url NOT LIKE '%%' || asin || '%%')
FROM task2.product_manifest m WHERE extraction_id = %s
"""
# 每个 ASIN 最后一次尝试的模板与跳转；run_id 为空时统计全部
EVIDENCE_SQL = """
SELECT coalesce(page_template, 'unknown'), count(*),
       count(*) FILTER (WHERE jsonb_array_length(redirect_chain) > 0),
       count(*) FILTER (WHERE redirect_chain IS NULL)
FROM task2.fetch_attempt a
WHERE extraction_id = %(eid)s AND (%(run)s::text IS NULL OR run_id = %(run)s)
  AND attempt_no = (SELECT attempt_count FROM task2.product_manifest m
                    WHERE m.extraction_id = a.extraction_id AND m.asin = a.asin)
GROUP BY 1 ORDER BY 1
"""
PROBLEM_KEYS = ("success_missing_evidence", "processed_without_attempt", "not_collected_with_attempt",
                "attempt_count_mismatch", "snapshot_missing", "snapshot_hash_mismatch")


def run_checks(conn: psycopg.Connection, eid: str, *, sample: int = 0, run_id: str | None = None) -> dict[str, Any]:
    """只读核对，返回计数字典；problems(out) 为空表示通过。run_id 只限定快照与模板统计的范围。"""
    status = dict(conn.execute("SELECT status, count(*) FROM task2.product_manifest WHERE extraction_id = %s "
                               "GROUP BY status ORDER BY status", (eid,)).fetchall())
    total, success_missing, done_no_attempt, pending_with_attempt, attempt_mismatch, success_url_other = (
        conn.execute(COUNTS_SQL, (eid,)).fetchone())
    where = "m.extraction_id = %s AND m.snapshot_path IS NOT NULL"
    params: list[Any] = [eid]
    if run_id:  # 本次运行最后写入清单的快照
        where += (" AND EXISTS (SELECT 1 FROM task2.fetch_attempt a WHERE a.extraction_id = m.extraction_id "
                  "AND a.asin = m.asin AND a.run_id = %s AND a.attempt_no = m.attempt_count)")
        params.append(run_id)
    order = ""
    if sample:
        order = " ORDER BY random() LIMIT %s"
        params.append(sample)
    snaps = conn.execute("SELECT m.snapshot_path, m.content_hash FROM task2.product_manifest m WHERE " + where
                         + order, params).fetchall()
    attempts = conn.execute("SELECT count(*) FROM task2.fetch_attempt WHERE extraction_id = %s",
                            (eid,)).fetchone()[0]
    evidence = conn.execute(EVIDENCE_SQL, {"eid": eid, "run": run_id}).fetchall()

    missing = bad_hash = 0
    for path, digest in snaps:
        f = resolve_snapshot(path)
        if not f.exists():
            missing += 1
        elif hashlib.sha256(gzip.decompress(f.read_bytes())).hexdigest() != digest:
            bad_hash += 1

    return {
        "extraction_id": eid,
        "run_id": run_id,
        "manifest_rows": total,
        "status": status,
        "status_sum_equals_rows": sum(status.values()) == total,
        "fetch_attempts": attempts,
        "success_missing_evidence": success_missing,
        "processed_without_attempt": done_no_attempt,
        "not_collected_with_attempt": pending_with_attempt,
        "attempt_count_mismatch": attempt_mismatch,
        "success_final_url_without_asin": success_url_other,
        "snapshots_checked": len(snaps),
        "snapshot_missing": missing,
        "snapshot_hash_mismatch": bad_hash,
        "page_template": {t: n for t, n, _, _ in evidence},
        "redirected": sum(r for _, _, r, _ in evidence),
        "redirect_not_recorded": sum(u for _, _, _, u in evidence),
    }


def problems(out: dict[str, Any]) -> list[str]:
    found = [] if out["status_sum_equals_rows"] else ["状态之和不等于清单行数"]
    return found + [f"{k}={out[k]}" for k in PROBLEM_KEYS if out[k]]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="任务 2 爬虫结果核对（只读）")
    p.add_argument("--extraction-id")
    p.add_argument("--sample", type=int, default=0, help="随机抽多少个快照核对哈希；0 表示全部")
    p.add_argument("--run-id", help="只核对该次运行写入的快照与模板")
    args = p.parse_args(argv)

    with connect(load_settings()) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        out = run_checks(conn, eid, sample=args.sample, run_id=args.run_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if problems(out) else 0


if __name__ == "__main__":
    sys.exit(main())
