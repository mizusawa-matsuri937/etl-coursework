#!/usr/bin/env python3
"""为任务 4 的真实 scheduler 端到端验证准备 etl_test：合成的任务 3 结构数据（抽取、清单、页面字段、分类）和一个合成 IMDb 快照。

只在测试容器里运行，且只允许 ETL_ENV=test、库名以 _test 结尾；会清空 etl_test（与集成测试的会话开始一致）。
合成数据是测试夹具里手写的，不是真实商品或 IMDb 文件。用法（项目根目录；脚本经标准输入传入，因为 scripts/ 没有挂载进测试容器）：

    docker compose --profile test run --rm -T etl-test python - --imdb-out /opt/etl/outputs/e2e/imdb_fixture < scripts/seed_task4_e2e.py

输出一行 JSON：extraction_id、snapshot_id、imdb_root。随后用 IMDB_HOST_DIR=$PWD/outputs/e2e/imdb_fixture 重建 Airflow 服务
（见 doc/task4/runbook.md 第 3 节）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

for sub in ("fixtures", "unit", "integration"):
    sys.path.insert(0, f"/opt/etl/tests/{sub}")

import build_fixtures as bf  # noqa: E402
from _task4_helpers import P, build, match_products  # noqa: E402
from task4 import imdb_files as ifx  # noqa: E402

from etl_task1.config import load_settings  # noqa: E402
from etl_task1.db import apply_schema, connect  # noqa: E402
from etl_task2.manifest import apply_task2_schema  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--imdb-out", required=True, help="合成 IMDb 快照的父目录（容器内路径）")
    args = ap.parse_args()
    base = load_settings(os.environ)
    if base.env != "test" or not base.db_name.endswith("_test"):
        print("只允许在 ETL_ENV=test 且库名以 _test 结尾的数据库上运行", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="t4seed_"))
    gz, manifest = bf.build(tmp / "fixture")
    settings = dataclasses.replace(base, source_gzip_path=gz, source_manifest_path=manifest, output_dir=tmp / "out",
                                   batch_rows=3)
    with connect(base) as conn:
        for schema in ("task4", "imdb", "task3", "task2"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        apply_schema(conn)
        apply_task2_schema(conn)
        products = match_products() + [P("B0T4M00T01", "tv_series", "Some Series Season 1", reviews=6),
                                       P("B0T4M00N01", "nopage", group="not_found", reviews=3)]
        fx = build(conn, lambda **over: dataclasses.replace(settings, **over), tmp, products)
    out = Path(args.imdb_out)
    ifx.build_snapshot(out / ifx.DATE, **ifx.MATCH)
    print(json.dumps({"extraction_id": fx.eid, "snapshot_id": ifx.SNAPSHOT_ID, "imdb_root": str(out),
                      "products": len(products)}))
    return 0


sys.exit(main())
