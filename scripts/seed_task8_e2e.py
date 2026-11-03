#!/usr/bin/env python3
"""为任务 8 的真实 scheduler 端到端验证准备 etl_test：用任务 1 的装载器装入合成评论（_task8_helpers.small_world），
再按正式库同形写入任务 2 清单、任务 3 分类（t3r2）与任务 7 版本 cv0003，并应用迁移 701。之后任务 8 的 DAG 只读这些结果。

只在测试容器里运行，且只允许 ETL_ENV=test、库名以 _test 结尾；会清空 etl_test（与集成测试的会话开始一致）。
用法（项目根目录；脚本经标准输入传入，因为 scripts/ 没有挂载进测试容器）：

    docker compose --profile test run --rm -T etl-test python - < scripts/seed_task8_e2e.py

输出一行 JSON：extraction_id、records、products。随后用 compose.e2e.yaml 起 Airflow 服务（见 doc/task8/runbook.md 第 2 节）。
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

import _task8_helpers as h8  # noqa: E402
from etl_task1.config import load_settings  # noqa: E402
from etl_task1.db import apply_schema, connect  # noqa: E402
from etl_task2.manifest import apply_task2_schema  # noqa: E402


def main() -> int:
    base = load_settings(os.environ)
    if base.env != "test" or not base.db_name.endswith("_test"):
        print("只允许在 ETL_ENV=test 且库名以 _test 结尾的数据库上运行", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="t8e2e_"))

    def make_settings(**over):
        return dataclasses.replace(base, **{"output_dir": tmp / "out", "batch_rows": 3, **over})

    with connect(base) as conn:
        for schema in ("task8", "task7", "task6", "task5", "task4", "imdb", "task3", "task2"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        apply_schema(conn)
        apply_task2_schema(conn)
        eid = h8.seed_small_world(conn, make_settings, tmp)
        records = conn.execute("SELECT count(*) FROM public.review_record WHERE extraction_id = %s", (eid,)).fetchone()[0]
        products = conn.execute("SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %s", (eid,)).fetchone()[0]
    print(json.dumps({"extraction_id": eid, "records": records, "products": products}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
