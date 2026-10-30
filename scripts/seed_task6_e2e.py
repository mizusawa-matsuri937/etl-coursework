#!/usr/bin/env python3
"""为任务 6 的真实 scheduler 端到端验证准备 etl_test：合成的任务 3/4/5 数据与 IMDb 快照，
并迁移应用 501。之后任务 6 的 DAG 只读这些结果。

只在测试容器里运行，且只允许 ETL_ENV=test、库名以 _test 结尾；会清空 etl_test（与集成测试的会话开始一致）。
合成数据是测试夹具里手写的（tests/fixtures/task6/world.py 与 tests/integration/_task6_helpers.py）。
用法（项目根目录；脚本经标准输入传入，因为 scripts/ 没有挂载进测试容器）：

    docker compose --profile test run --rm -T etl-test python - < scripts/seed_task6_e2e.py

输出一行 JSON：extraction_id、imdb_snapshot_id、t4_mapping_version_id、t5_person_mapping_version_id。
随后用 compose.e2e.yaml 起 Airflow 服务（见 doc/task6/runbook.md 第 2 节）。
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

import _task6_helpers as h6  # noqa: E402
from etl_task1.config import load_settings  # noqa: E402
from etl_task1.db import apply_schema, connect  # noqa: E402
from etl_task2.manifest import apply_task2_schema  # noqa: E402


def main() -> int:
    base = load_settings(os.environ)
    if base.env != "test" or not base.db_name.endswith("_test"):
        print("只允许在 ETL_ENV=test 且库名以 _test 结尾的数据库上运行", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="t6seed_"))

    def make_settings(**over):
        return dataclasses.replace(base, **over)

    with connect(base) as conn:
        for schema in ("task6", "task5", "task4", "imdb", "task3", "task2"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        apply_schema(conn)
        apply_task2_schema(conn)
        world = h6.make_world(conn, make_settings, tmp)
        movie_count = conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s",
                                   (world.mapping,)).fetchone()[0]
    print(json.dumps({
        "extraction_id": world.extraction_id,
        "imdb_snapshot_id": world.sid,
        "t4_mapping_version_id": world.mapping,
        "t5_person_mapping_version_id": world.pmap_with_hints,
        "movies": movie_count,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
