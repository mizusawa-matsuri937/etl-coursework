#!/usr/bin/env python3
"""为任务 7 的真实 scheduler 端到端验证准备 etl_test：合成的任务 3–6 数据与 IMDb 快照（几部作品，带类型、导演、演员与署名），
并应用迁移 601。之后任务 7 的 DAG 只读这些结果。

只在测试容器里运行，且只允许 ETL_ENV=test、库名以 _test 结尾；会清空 etl_test（与集成测试的会话开始一致）。
合成数据来自测试夹具（tests/fixtures/task7/world.py、tests/integration/_task7_helpers.py 与 _task7_worlds.py），不运行上游流水线。
用法（项目根目录；脚本经标准输入传入，因为 scripts/ 没有挂载进测试容器）：

    docker compose --profile test run --rm -T etl-test python - < scripts/seed_task7_e2e.py

输出一行 JSON：extraction_id、imdb_snapshot_id、t4_mapping_version_id、t5_person_mapping_version_id、t6_release_version_id、movies。
随后用 compose.e2e.yaml 起 Airflow 服务（见 doc/task7/runbook.md 第 2 节）。
"""

from __future__ import annotations

import json
import os
import sys

for sub in ("fixtures", "unit", "integration"):
    sys.path.insert(0, f"/opt/etl/tests/{sub}")

import _task7_helpers as h7  # noqa: E402
from _task7_worlds import small_world  # noqa: E402
from etl_task1.config import load_settings  # noqa: E402
from etl_task1.db import apply_schema, connect  # noqa: E402
from etl_task2.manifest import apply_task2_schema  # noqa: E402


def main() -> int:
    base = load_settings(os.environ)
    if base.env != "test" or not base.db_name.endswith("_test"):
        print("只允许在 ETL_ENV=test 且库名以 _test 结尾的数据库上运行", file=sys.stderr)
        return 2
    with connect(base) as conn:
        for schema in ("task7", "task6", "task5", "task4", "imdb", "task3", "task2"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        apply_schema(conn)
        apply_task2_schema(conn)
        seeded = h7.seed(conn, small_world(5))
        movies = conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", (h7.T4,)).fetchone()[0]
    print(json.dumps({"extraction_id": seeded.eid, "imdb_snapshot_id": h7.SNAPSHOT, "t4_mapping_version_id": h7.T4,
                      "t5_person_mapping_version_id": h7.PMAP, "t6_release_version_id": h7.RV, "movies": movies}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
