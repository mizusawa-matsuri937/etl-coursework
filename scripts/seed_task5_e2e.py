#!/usr/bin/env python3
"""为任务 5 的真实 scheduler 端到端验证准备 etl_test：合成的任务 3 结构数据（抽取、清单、页面字段、分类）、一个合成 IMDb 快照（直接装载进库），
并真正运行任务 4 的匹配得到映射版本（map0001）。之后任务 5 的 DAG 只读这些结果。

只在测试容器里运行，且只允许 ETL_ENV=test、库名以 _test 结尾；会清空 etl_test（与集成测试的会话开始一致）。
合成数据是测试夹具里手写的（tests/fixtures/task5 与 tests/integration/_task5_helpers.combined_inputs），不是真实商品、人物或 IMDb 文件。
用法（项目根目录；脚本经标准输入传入，因为 scripts/ 没有挂载进测试容器）：

    docker compose --profile test run --rm -T etl-test python - < scripts/seed_task5_e2e.py

输出一行 JSON：extraction_id、imdb_snapshot_id、t4_mapping_version_id。随后用 compose.e2e.yaml 起 Airflow 服务（见 doc/task5/runbook.md 第 2 节）；
任务 5 的 DAG 不读 IMDb 文件，所以不需要改 IMDB_HOST_DIR。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

for sub in ("fixtures", "unit", "integration"):
    sys.path.insert(0, f"/opt/etl/tests/{sub}")

import build_fixtures as bf  # noqa: E402
import _task4_helpers as t4h  # noqa: E402
from _task4_helpers import P  # noqa: E402
from _task5_helpers import combined_inputs  # noqa: E402

from etl_task1.config import load_settings  # noqa: E402
from etl_task1.db import apply_schema, connect  # noqa: E402
from etl_task2.manifest import apply_task2_schema  # noqa: E402
from etl_task4.runner import execute_match  # noqa: E402


def main() -> int:
    base = load_settings(os.environ)
    if base.env != "test" or not base.db_name.endswith("_test"):
        print("只允许在 ETL_ENV=test 且库名以 _test 结尾的数据库上运行", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="t5seed_"))
    gz, manifest = bf.build(tmp / "fixture")
    settings = dataclasses.replace(base, source_gzip_path=gz, source_manifest_path=manifest, output_dir=tmp / "out",
                                   batch_rows=3)
    products, imdb = combined_inputs(extra_films=True)
    products += [P("B0T5Z00T01", "tv_series", "Some Series Season 1", reviews=6), P("B0T5Z00N01", "nopage", group="not_found", reviews=3)]
    with connect(base) as conn:
        for schema in ("task5", "task4", "imdb", "task3", "task2"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        apply_schema(conn)
        apply_task2_schema(conn)
        fx = t4h.build(conn, lambda **over: dataclasses.replace(settings, **over), tmp, products)
        t4h.reset_task4(conn)
        sid = t4h.load_imdb(conn, tmp, imdb)
        t4 = execute_match(conn, t4h.inputs_for(fx, sid))
    print(json.dumps({"extraction_id": fx.eid, "imdb_snapshot_id": sid, "t4_mapping_version_id": t4.mapping_version_id,
                      "products": len(products)}))
    return 0


sys.exit(main())
