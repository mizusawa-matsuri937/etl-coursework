"""任务 3 只读核对（人工复核用，与 DAG 的 verify_classification 共用 classify.verify）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task3.check                       # 默认抽取与当前规则版本
    python -m etl_task3.check --rule-version t3r1
输出一段 JSON；全部一致时退出码 0，否则 1。
"""

from __future__ import annotations

import argparse
import json
import sys

from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from . import RULE_VERSION
from .classify import problems, verify


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 3 分类结果只读核对")
    ap.add_argument("--extraction-id")
    ap.add_argument("--rule-version", default=RULE_VERSION)
    args = ap.parse_args(argv)
    with connect(load_settings()) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        out = verify(conn, eid, args.rule_version)
    out["problems"] = problems(out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
