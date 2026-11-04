#!/usr/bin/env python3
"""为任务 9 的真实 scheduler 端到端验证准备 etl_test：用任务 9 集成测试的夹具（_task9_helpers.seed，任务 1–8 上游经各任务
自己的写入函数生成，再补成正式库形状），快照文件写到 outputs/e2e/t9_seed/（Airflow 容器与测试容器都挂载了 outputs/），
并应用迁移 801。之后任务 9 的 DAG 只读这些上游结果。

只在测试容器里运行，且只允许 ETL_ENV=test、库名以 _test 结尾；会清空 etl_test（与集成测试的会话开始一致）。
用法（项目根目录；脚本经标准输入传入，因为 scripts/ 没有挂载进测试容器）：

    docker compose --profile test run --rm -T etl-test python - < scripts/seed_task9_e2e.py

输出一行 JSON：extraction_id、conflict_version_id、snapshot_root。随后用 compose.e2e.yaml 起 Airflow 服务
（见 doc/task9/runbook.md 第 2 节）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

for sub in ("fixtures", "unit", "integration"):
    sys.path.insert(0, f"/opt/etl/tests/{sub}")

import _task9_helpers as h  # noqa: E402
from etl_task1.config import load_settings  # noqa: E402
from etl_task1.db import apply_schema, connect  # noqa: E402
from etl_task2.manifest import apply_task2_schema  # noqa: E402

ROOT = Path("/opt/etl/outputs/e2e/t9_seed")


def main() -> int:
    base = load_settings(os.environ)
    if base.env != "test" or not base.db_name.endswith("_test"):
        print("只允许在 ETL_ENV=test 且库名以 _test 结尾的数据库上运行", file=sys.stderr)
        return 2
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)

    def make_settings(**over):
        return dataclasses.replace(base, **{"output_dir": ROOT / "out", "batch_rows": 3, **over})

    with connect(base) as conn:
        for schema in ("task9", "task8", "task7", "task6", "task5", "task4", "imdb", "task3", "task2"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        apply_schema(conn)
        apply_task2_schema(conn)
        t = h.seed(conn, make_settings, ROOT)
        h.add_products(conn, t)
    print(json.dumps({"extraction_id": t.eid, "conflict_version_id": t.cv, "review_dedup_version_id": t.rd,
                      "snapshot_root": str(t.snapshot_root)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
