"""任务 9 只读核对（spec R19、R25、R26、R36–R40；DAG 的 verify 步骤与正式库 check 共用 verify 与 problems）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task9.check                         # 默认抽取的最新血缘版本
    python -m etl_task9.check --version lv0001
输出一段 JSON；全部一致时退出码 0，否则 1。整个核对在只读事务里进行，不写任何表。命令行默认要求版本已 verified；
verify 步骤在标记 verified 之前调用，用 expect_verified=False。

核对项：五个阶段键已登记、依赖链正确且都有完成标记；派生图无悬空边、无缺父节点、无环（R19）；字段选择的值与状态与
目标层逐行一致（R26）；非空值的 trace_status 不是 traced/relocated/manual 的都有 untraced_value 质量问题（R25，缺口
如实列出而不掩盖）；可追溯的非空值都至少有一条影响闭包行；版本计数与数据库重算一致、上游行数与物化时相同；版本与运行状态一致。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg

from .derivations import check_graph
from .rules import STAGES
from .run import RunContext
from .selections import check_values, selection_params
from .versions import KEY_COLUMNS, Keys, keys_for_version, upstream_counts, version_counts

def latest_version(conn: psycopg.Connection, extraction_id: str | None = None) -> str:
    row = conn.execute("SELECT lineage_version_id FROM task9.lineage_version WHERE %s::text IS NULL "
                       "OR extraction_id = %s ORDER BY created_at DESC, lineage_version_id DESC LIMIT 1",
                       (extraction_id, extraction_id)).fetchone()
    if row is None:
        raise RuntimeError("还没有血缘版本")
    return row[0]


def verify_keys(conn: psycopg.Connection, keys: Keys) -> dict[str, Any]:
    """不依赖版本行的核对部分（阶段、图、值、缺口、闭包）。调用方负责只读事务。"""
    out: dict[str, Any] = {"keys": keys.as_json()}
    stages: dict[str, Any] = {}
    parent = None
    for s in STAGES:
        k = getattr(keys, s)
        row = conn.execute("SELECT stage, parent_stage_key, EXISTS (SELECT 1 FROM task9.entity_stage e "
                           "WHERE e.stage_key = k.stage_key AND e.entity_id = '__complete__') FROM task9.stage_key k "
                           "WHERE stage_key = %s", (k,)).fetchone()
        stages[s] = {"stage_key": k, "registered": row is not None, "stage_ok": bool(row and row[0] == s),
                     "parent_ok": bool(row and row[1] == parent), "complete": bool(row and row[2])}
        parent = k
    out["stages"] = stages
    ctx = RunContext("", keys.inputs)
    p = selection_params(conn, ctx, keys.selections, keys.derivations, keys.assertions, keys.sources)
    out["graph"] = check_graph(conn, None, edge_keys=[keys.derivations, keys.selections], ak=keys.assertions,
                               sk=keys.sources)
    out["values"] = check_values(conn, None, p)
    gaps, explained = conn.execute(
        "SELECT count(*), count(q.entity_id) FROM task9.field_selection f "
        "LEFT JOIN task9.quality_issue q ON q.stage_key = f.stage_key AND q.issue_code = 'untraced_value' "
        "AND q.entity_type = 'field_selection' AND q.entity_id = f.selection_id "
        "WHERE f.stage_key = %s AND f.value_text IS NOT NULL "
        "AND f.trace_status NOT IN ('traced', 'relocated', 'manual')", (keys.selections,)).fetchone()
    out["coverage"] = {"gaps": gaps, "gaps_with_issue": explained,
                       "gaps_by_trace_status": dict(conn.execute(
                           "SELECT trace_status, count(*) FROM task9.field_selection WHERE stage_key = %s "
                           "AND value_text IS NOT NULL AND trace_status NOT IN ('traced', 'relocated', 'manual') "
                           "GROUP BY 1 ORDER BY 1", (keys.selections,)).fetchall())}
    out["impact"] = {"rows": conn.execute("SELECT count(*) FROM task9.field_impact WHERE stage_key = %s",
                                          (keys.impact,)).fetchone()[0],
                     "traceable_without_closure": conn.execute(
                         "SELECT count(*) FROM task9.field_selection f WHERE f.stage_key = %s AND f.value_text IS NOT NULL "
                         "AND f.trace_status IN ('traced', 'relocated', 'manual') AND NOT EXISTS (SELECT 1 "
                         "FROM task9.field_impact i WHERE i.stage_key = %s AND i.selection_id = f.selection_id)",
                         (keys.selections, keys.impact)).fetchone()[0]}
    out["issues"] = {s: dict(conn.execute("SELECT issue_code, count(*) FROM task9.quality_issue WHERE stage_key = %s "
                                          "GROUP BY 1 ORDER BY 1", (getattr(keys, s),)).fetchall()) for s in STAGES}
    return out


def verify(conn: psycopg.Connection, lv: str) -> dict[str, Any]:
    """在只读事务里核对一个血缘版本，返回结果（problems 判定是否通过）。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL work_mem = '256MB'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        row = conn.execute(f"SELECT status, run_id, counts, {', '.join(KEY_COLUMNS.values())} "
                           "FROM task9.lineage_version WHERE lineage_version_id = %s", (lv,)).fetchone()
        if row is None:
            raise ValueError(f"血缘版本 {lv} 不存在")
        keys = keys_for_version(conn, lv)
        out = verify_keys(conn, keys)
        out["lineage_version_id"] = lv
        out["version_status"] = row[0]
        stored = dict(row[2])
        recount = version_counts(conn, {s: getattr(keys, s) for s in STAGES})
        out["counts_match"] = {k: stored.get(k) == v for k, v in recount.items()}
        out["upstream_counts"] = {"stored": stored.get("upstream_counts"), "now": upstream_counts(conn, keys.inputs)}
        run = conn.execute("SELECT status, lineage_version_id FROM task9.run WHERE run_id = %s", (row[1],)).fetchone()
        out["run"] = {"run_id": row[1], "status": run[0] if run else None,
                      "lineage_version_id": run[1] if run else None}
        out["table_counts"] = recount
    return out


def problems(info: dict[str, Any], *, expect_verified: bool = True) -> list[str]:
    found = []
    for s, st in info["stages"].items():
        if not all(st[k] for k in ("registered", "stage_ok", "parent_ok", "complete")):
            found.append(f"阶段 {s}（{st['stage_key']}）登记或完成标记不符：{st}")
    g = info["graph"]
    if g["dangling"] or g["no_parent"] or g["cycle_nodes"]:
        found.append(f"派生图：悬空 {g['dangling']}、缺父节点 {g['no_parent']}、环上节点 {g['cycle_nodes']}")
    if info["values"]["mismatches"]:
        found.append(f"字段选择与目标层不一致 {info['values']['mismatches']} 处：{info['values']['samples']}")
    c = info["coverage"]
    if c["gaps"] != c["gaps_with_issue"]:
        found.append(f"非空值缺口 {c['gaps']} 个中有 {c['gaps'] - c['gaps_with_issue']} 个没有 untraced_value 质量问题")
    if info["impact"]["traceable_without_closure"]:
        found.append(f"可追溯的非空值中 {info['impact']['traceable_without_closure']} 个没有影响闭包行")
    if "counts_match" in info:
        bad = [k for k, ok in info["counts_match"].items() if not ok]
        if bad:
            found.append(f"版本计数与重算不一致：{bad}")
        up = info["upstream_counts"]
        if up["stored"] != up["now"]:
            found.append(f"上游行数与物化时不同：{up}")
        if expect_verified and info["version_status"] != "verified":
            found.append(f"版本 {info['lineage_version_id']} 状态为 {info['version_status']}，不是 verified")
        if expect_verified and info["run"]["status"] != "succeeded":
            found.append(f"运行 {info['run']['run_id']} 状态为 {info['run']['status']}，不是 succeeded")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl_task9.check")
    ap.add_argument("--version", default=None, help="血缘版本（默认：最新版本）")
    args = ap.parse_args(argv)
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    with connect(load_settings(os.environ)) as conn:
        lv = args.version or latest_version(conn)
        info = verify(conn, lv)
    found = problems(info)
    json.dump({"ok": not found, "problems": found, **info}, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if not found else 1


if __name__ == "__main__":
    raise SystemExit(main())
