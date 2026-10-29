"""爬虫结果核对（只读，人工复核用）：状态之和、成功证据、尝试次数与日志一致、快照哈希。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task2.crawl.check                 # 核对全部快照
    python -m etl_task2.crawl.check --sample 500    # 随机抽 500 个快照核对哈希
输出一段 JSON；全部一致时退出码 0，否则 1。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="任务 2 爬虫结果核对（只读）")
    p.add_argument("--extraction-id")
    p.add_argument("--sample", type=int, default=0, help="随机抽多少个快照核对哈希；0 表示全部")
    args = p.parse_args(argv)

    with connect(load_settings()) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        status = dict(conn.execute("SELECT status, count(*) FROM task2.product_manifest WHERE extraction_id = %s "
                                   "GROUP BY status ORDER BY status", (eid,)).fetchall())
        total, success_missing, done_no_attempt, pending_with_attempt, attempt_mismatch, success_url_other = (
            conn.execute(COUNTS_SQL, (eid,)).fetchone())
        order = "ORDER BY random() LIMIT %s" if args.sample else ""
        params = (eid, args.sample) if args.sample else (eid,)
        snaps = conn.execute("SELECT snapshot_path, content_hash FROM task2.product_manifest "
                             "WHERE extraction_id = %s AND snapshot_path IS NOT NULL " + order, params).fetchall()
        attempts = conn.execute("SELECT count(*) FROM task2.fetch_attempt WHERE extraction_id = %s",
                                (eid,)).fetchone()[0]

    missing = bad_hash = 0
    for path, digest in snaps:
        f = Path(path)
        if not f.exists():
            missing += 1
        elif hashlib.sha256(gzip.decompress(f.read_bytes())).hexdigest() != digest:
            bad_hash += 1

    out = {
        "extraction_id": eid,
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
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    problems = (not out["status_sum_equals_rows"], success_missing, done_no_attempt, pending_with_attempt,
                attempt_mismatch, missing, bad_hash)
    return 1 if any(problems) else 0


if __name__ == "__main__":
    sys.exit(main())
