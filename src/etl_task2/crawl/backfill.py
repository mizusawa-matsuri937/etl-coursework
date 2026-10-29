"""从已保存的快照回填 103 之前尝试的页面模板（R33）。只写 fetch_attempt.page_template 为空且有快照的行，可重复运行。

跳转链无法回填：103 之前没有记录，保持 NULL。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task2.crawl.backfill
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import apply_crawl_schema, detect_template, latest_full_extraction, resolve_snapshot

BATCH = 1000


def backfill_templates(conn: psycopg.Connection, eid: str) -> dict[str, int]:
    rows = conn.execute("SELECT asin, attempt_no, final_url, http_status, snapshot_path FROM task2.fetch_attempt "
                        "WHERE extraction_id = %s AND page_template IS NULL AND snapshot_path IS NOT NULL "
                        "ORDER BY asin, attempt_no", (eid,)).fetchall()
    filled = missing = 0
    for i in range(0, len(rows), BATCH):
        updates = []
        for asin, attempt_no, final_url, http_status, path in rows[i:i + BATCH]:
            f = resolve_snapshot(path)
            if not f.exists():
                missing += 1
                continue
            updates.append((detect_template(final_url or "", http_status or 0, gzip.decompress(f.read_bytes())),
                            eid, asin, attempt_no))
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany("UPDATE task2.fetch_attempt SET page_template = %s "
                            "WHERE extraction_id = %s AND asin = %s AND attempt_no = %s", updates)
        filled += len(updates)
    return {"candidates": len(rows), "filled": filled, "snapshot_missing": missing}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="从快照回填页面模板")
    p.add_argument("--extraction-id")
    args = p.parse_args(argv)
    with connect(load_settings()) as conn:
        apply_crawl_schema(conn)
        eid = args.extraction_id or latest_full_extraction(conn)
        out = {"extraction_id": eid, **backfill_templates(conn, eid)}
    print(json.dumps(out, ensure_ascii=False))
    return 1 if out["snapshot_missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
