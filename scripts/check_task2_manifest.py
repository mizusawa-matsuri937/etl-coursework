#!/usr/bin/env python3
"""逐行比较任务 2 清单导出与任务 1 的产品汇总（AC09 人工复核用，仅标准库）。

用法（工作目录：项目根目录）：
    python3 scripts/check_task2_manifest.py outputs/task2/<抽取ID>/<构建ID>/<run_key>/product_manifest.csv \
        outputs/task1/<抽取ID>/full_run_2_rerun/distinct_products.csv
输出一段 JSON；全部一致时退出码 0，否则 1。
"""

from __future__ import annotations

import csv
import json
import sys

COLS = ["records_total", "records_valid", "records_quarantined", "first_record_no", "last_record_no"]
CRAWLER = ["final_url", "http_status", "fetched_at", "snapshot_path", "content_hash", "status_reason"]


def main(manifest_path: str, reference_path: str) -> int:
    with open(manifest_path, newline="", encoding="utf-8") as fh:
        man = list(csv.DictReader(fh))
    with open(reference_path, newline="", encoding="utf-8") as fh:
        ref = list(csv.DictReader(fh))
    out = {
        "manifest_rows": len(man),
        "reference_rows": len(ref),
        "asin_sequence_equal": [r["asin"] for r in man] == [r["product_id"] for r in ref],
        "column_mismatches": sum(1 for m, r in zip(man, ref) for c in COLS if m[c] != r[c]),
        "in_valid_true": sum(1 for m in man if m["in_valid_records"] == "t"),
        "in_valid_false": sum(1 for m in man if m["in_valid_records"] == "f"),
        "status": {s: sum(1 for m in man if m["status"] == s) for s in sorted({m["status"] for m in man})},
        "url_issue_rows": sum(1 for m in man if m["url_issue"]),
        "url_mismatches": sum(1 for m in man if m["original_url"] != f"https://www.amazon.com/dp/{m['asin']}"
                              and not m["url_issue"]),
        "crawler_fields_nonempty": sum(1 for m in man for c in CRAWLER if m[c]),
        "attempt_nonzero": sum(1 for m in man if m["attempt_count"] != "0"),
        "review_time_null": sum(1 for m in man if not m["earliest_review_time"]),
        "review_time_null_but_in_valid": sum(1 for m in man if not m["earliest_review_time"]
                                             and m["in_valid_records"] == "t"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    ok = (out["manifest_rows"] == out["reference_rows"] and out["asin_sequence_equal"]
          and out["column_mismatches"] == 0 and out["url_mismatches"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
