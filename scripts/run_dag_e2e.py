#!/usr/bin/env python3
"""通过真实 Airflow scheduler 触发 DAG（默认任务 1，可用 --dag-id 指定）并等待结束，打印各任务状态（仅用标准库）。

用法（工作目录：项目根目录，需先 `docker compose up -d`）：
    python3 scripts/run_dag_e2e.py --run-id e2e_sample_1 --conf '{"scope":"sample","sample_max_records":100,
        "source_gzip_path":"/data/fixtures/task1_sample.txt.gz",
        "source_manifest_path":"/data/fixtures/task1_sample.manifest.json"}'
任务 2 清单：
    python3 scripts/run_dag_e2e.py --dag-id amazon_products_task2_manifest --run-id e2e_t2 \
        --conf '{"extraction_id":"<64 位抽取 ID>","allow_sample":true}'
退出码：DAG run 成功为 0，失败为 1，超时为 2。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

DAG_ID = "amazon_reviews_task1"


def airflow(*args: str) -> str:
    cmd = ["docker", "compose", "exec", "-T", "airflow-scheduler", "airflow", *args]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} 失败：{out.stderr.strip()[-500:]}")
    return out.stdout


def json_of(text: str):
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    return json.loads(text[start:]) if start >= 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dag-id", default=DAG_ID)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--conf", default="{}")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    airflow("dags", "trigger", a.dag_id, "--run-id", a.run_id, "--conf", a.conf)
    deadline = time.time() + a.timeout
    state = None
    while time.time() < deadline:
        runs = json_of(airflow("dags", "list-runs", a.dag_id, "-o", "json")) or []
        state = next((r["state"] for r in runs if r["run_id"] == a.run_id), None)
        if state in ("success", "failed"):
            break
        time.sleep(3)
    tasks = json_of(airflow("tasks", "states-for-dag-run", a.dag_id, a.run_id, "-o", "json")) or []
    for t in tasks:
        print(f"{t['task_id']:<18} {t['state']:<16} {t.get('start_date', '')[:19]}")
    print(f"DAG run {a.run_id}: {state}")
    return {"success": 0, "failed": 1}.get(state, 2)


if __name__ == "__main__":
    sys.exit(main())
