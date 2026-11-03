"""任务 9 报告命令（spec R34、R44、R45）。

    python -m etl_task9.report diff lv0001 lv0002     # 两个血缘版本的选择级差异（R34，只读）

导出（report export，R44、R45）在 T9-05 加入。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .versions import diff_versions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl_task9.report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diff", help="两个血缘版本按实体、字段与成员的差异（R34）")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--limit", type=int, default=50)
    args = ap.parse_args(argv)
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    with connect(load_settings(os.environ)) as conn:
        out = diff_versions(conn, args.old, args.new, limit=args.limit)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
