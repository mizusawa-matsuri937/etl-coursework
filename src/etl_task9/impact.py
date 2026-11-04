"""影响闭包 build_impact、规则影响、来源核验与上游重跑映射（spec R32–R35、R38）。

- build_impact：从每条字段选择沿派生边（derivations 与 selections 两个阶段键的边）向上走到源记录，写
  field_impact(源记录, 选择, 最短路径边数)。整个闭包在本阶段一个事务里写入（R38：没有半写入的闭包）；中断后重跑
  从头重算（阶段没有完成标记）。按选择 ID 的桶分若干条语句执行，控制单条递归查询的中间结果。
- rule_impact（R33）：按 rule_id 或规则版本找边，沿子边向下走到字段选择，列出受影响的字段。
- verify_sources（R32）：重算被引用的页面快照正文、IMDb 文件与评论文件的 SHA-256；不一致的源记录经 field_impact
  得到受影响字段，再按 RERUN 给出需重跑的上游步骤与 DAG（R35）。文件不可读（容器未挂载）记为 unavailable，
  不当作不一致。
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

import psycopg

from .derivations import BUCKETS
from .rules import resolve_snapshot, snapshot_status
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, Unit, process_units, register_stage_key,
                  require_stage_complete)

STAGE = "impact"
GROUP = 256               # 每条闭包语句处理的桶数（4,096 桶 → 16 条语句）
MAX_DEPTH = 40            # 派生图无环（R19），深度上限只防御异常数据

# R35：变化的源 → 受影响的上游任务、阶段与 DAG（按重跑顺序）。任务 9 自身以新输入键生成新的血缘版本。
STEPS = {
    "imdb": ("imdb", "IMDb 快照装载", "imdb_reference_task4_load"),
    "task1": ("task1", "评论解析装载", "amazon_reviews_task1"),
    "task2": ("task2", "采集清单与候选链接", "amazon_products_task2_manifest"),
    "task3": ("task3", "页面解析与分类（t3r2/t3p1）", "amazon_products_task3_classify"),
    "task4": ("task4", "页面断言与同一电影匹配（map）", "amazon_movies_task4_match"),
    "task5": ("task5", "人物提及与归并（pmap）", "amazon_movies_task5_person"),
    "task6": ("task6", "上映候选、评论时间与推断上界（rv）", "amazon_movies_task6_release"),
    "task7": ("task7", "冲突消解（cv 的四个阶段键）", "amazon_movies_task7_conflict"),
    "task8": ("task8", "评论去重与归属（rd）", "amazon_reviews_task8_dedup"),
    "task9": ("task9", "字段级血缘（新血缘版本）", "amazon_movies_task9_lineage"),
}
RERUN = {
    "page_snapshot": ("task3", "task4", "task5", "task6", "task7", "task8", "task9"),
    "imdb_row": ("imdb", "task4", "task5", "task6", "task7", "task8", "task9"),
    "review_record": ("task1", "task2", "task6", "task7", "task8", "task9"),
    "manual_revision": ("task4", "task5", "task6", "task7", "task8", "task9"),
}


def rerun_plan(kinds: Sequence[str]) -> list[dict[str, Any]]:
    """若干种源记录变化时的重跑步骤（去重，保持上游在前的顺序）。"""
    order = list(STEPS)
    need = sorted({t for k in kinds for t in RERUN[k]}, key=order.index)
    return [{"order": n, "task": STEPS[t][0], "step": STEPS[t][1], "dag_id": STEPS[t][2]}
            for n, t in enumerate(need, 1)]


# ---------------------------------------------------------------- 影响闭包（R32、R38）

CLOSURE_SQL = f"""
WITH RECURSIVE up(sel, kind, id, depth) AS (
    SELECT d.child_id, d.parent_kind, d.parent_id, 1
    FROM task9.derivation d
    WHERE d.stage_key = ANY(%(keys)s) AND d.child_kind = 'selection' AND substr(d.child_id, 4, 3) = ANY(%(batch)s)
  UNION
    SELECT u.sel, d.parent_kind, d.parent_id, u.depth + 1
    FROM up u
    JOIN task9.derivation d ON d.stage_key = ANY(%(keys)s) AND d.child_kind = 'assertion' AND d.child_id = u.id
    WHERE u.kind = 'assertion' AND u.depth < {MAX_DEPTH})
INSERT INTO task9.field_impact (stage_key, source_record_id, selection_id, depth)
SELECT %(ik)s, id, sel, min(depth) FROM up WHERE kind = 'source_record' GROUP BY id, sel
ON CONFLICT DO NOTHING"""


def build_impact(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                 before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """写影响闭包（调用方已持锁并建好运行；selections 阶段须已完成）。单元 closure 只有一个实体，整个闭包与完成标记
    在同一个事务提交（R38）。"""
    require_stage_complete(conn, ctx, "sources")
    dk = require_stage_complete(conn, ctx, "derivations")
    lk = require_stage_complete(conn, ctx, "selections")
    ik = register_stage_key(conn, ctx, STAGE)

    def write(_batch: Sequence[str], _: Any) -> int:
        conn.execute("SET LOCAL work_mem = '256MB'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        n = 0
        for k in range(0, len(BUCKETS), GROUP):
            n += conn.execute(CLOSURE_SQL, {"keys": [dk, lk], "batch": BUCKETS[k:k + GROUP], "ik": ik}).rowcount
        return n

    counts: dict[str, Any] = {"stage_key": ik, "units": process_units(
        conn, ctx, STAGE, ik, [Unit("closure", ["all"], write)], batch_size=batch_size, before_commit=before_commit)}
    counts.update(summary(conn, ik, lk))
    return counts


def summary(conn: psycopg.Connection, ik: str, lk: str) -> dict[str, Any]:
    rows, sources, sels, max_depth = conn.execute(
        "SELECT count(*), count(DISTINCT source_record_id), count(DISTINCT selection_id), max(depth) "
        "FROM task9.field_impact WHERE stage_key = %s", (ik,)).fetchone()
    non_null = conn.execute("SELECT count(*) FROM task9.field_selection WHERE stage_key = %s AND value_text IS NOT NULL",
                            (lk,)).fetchone()[0]
    return {"field_impact": rows, "source_records": sources, "selections": sels, "max_depth": max_depth,
            "non_null_selections": non_null}


# ---------------------------------------------------------------- 规则影响（R33）

def rule_impact(conn: psycopg.Connection, keys, *, rule_id: str | None = None, rule_version: str | None = None,
                limit: int = 50) -> dict[str, Any]:
    """依赖某条规则（rule_id）或规则版本（rule_version）的边 → 沿子边向下 → 受影响的字段选择。两者同时给出时取交。"""
    if not rule_id and not rule_version:
        raise ValueError("需要 rule_id 或 rule_version")
    p = {"keys": [keys.derivations, keys.selections], "rid": rule_id, "rv": rule_version, "lk": keys.selections}
    edges = conn.execute(
        "SELECT operation, upstream_task, count(*) FROM task9.derivation WHERE stage_key = ANY(%(keys)s) "
        "AND (%(rid)s::text IS NULL OR rule_id = %(rid)s) AND (%(rv)s::text IS NULL OR rule_version = %(rv)s) "
        "GROUP BY 1, 2 ORDER BY 1, 2", p).fetchall()
    rows = conn.execute(
        f"""WITH RECURSIVE down(kind, id) AS (
                SELECT DISTINCT child_kind, child_id FROM task9.derivation
                WHERE stage_key = ANY(%(keys)s) AND (%(rid)s::text IS NULL OR rule_id = %(rid)s)
                  AND (%(rv)s::text IS NULL OR rule_version = %(rv)s)
              UNION
                SELECT d.child_kind, d.child_id FROM down
                JOIN task9.derivation d ON d.stage_key = ANY(%(keys)s) AND d.parent_kind = 'assertion'
                     AND d.parent_id = down.id
                WHERE down.kind = 'assertion')
            SELECT s.selection_id, s.entity_type, s.entity_id, s.field_name, s.member_key, s.value_text, s.status
            FROM (SELECT DISTINCT id FROM down WHERE kind = 'selection') d
            JOIN task9.field_selection s ON s.stage_key = %(lk)s AND s.selection_id = d.id
            ORDER BY s.entity_type, s.field_name, s.entity_id, s.member_key""", p).fetchall()
    by_field: dict[str, int] = {}
    for r in rows:
        by_field[f"{r[1]}.{r[3]}"] = by_field.get(f"{r[1]}.{r[3]}", 0) + 1
    return {"rule_id": rule_id, "rule_version": rule_version, "keys": keys.as_json(),
            "edges": [{"operation": o, "upstream_task": t, "edges": n} for o, t, n in edges],
            "affected_selections": len(rows), "by_field": by_field,
            "selections": [dict(zip(("selection_id", "entity_type", "entity_id", "field_name", "member_key",
                                     "value", "status"), r)) for r in rows[:limit]]}


# ---------------------------------------------------------------- 来源核验（R32、R35）

def file_sha256(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _affected(conn: psycopg.Connection, keys, source_ids: list[str], limit: int) -> dict[str, Any]:
    if not source_ids:
        return {"selections": 0, "by_field": {}, "sample": []}
    rows = conn.execute(
        "SELECT s.entity_type, s.field_name, count(DISTINCT s.selection_id) FROM task9.field_impact i "
        "JOIN task9.field_selection s ON s.stage_key = %s AND s.selection_id = i.selection_id "
        "WHERE i.stage_key = %s AND i.source_record_id = ANY(%s) GROUP BY 1, 2 ORDER BY 1, 2",
        (keys.selections, keys.impact, source_ids)).fetchall()
    sample = conn.execute(
        "SELECT DISTINCT ON (s.selection_id) s.selection_id, s.entity_type, s.entity_id, s.field_name, s.member_key, "
        "s.value_text, i.depth FROM task9.field_impact i "
        "JOIN task9.field_selection s ON s.stage_key = %s AND s.selection_id = i.selection_id "
        "WHERE i.stage_key = %s AND i.source_record_id = ANY(%s) ORDER BY s.selection_id, i.depth LIMIT %s",
        (keys.selections, keys.impact, source_ids, limit)).fetchall()
    total = conn.execute("SELECT count(DISTINCT selection_id) FROM task9.field_impact WHERE stage_key = %s "
                         "AND source_record_id = ANY(%s)", (keys.impact, source_ids)).fetchone()[0]
    return {"selections": total, "by_field": {f"{a}.{b}": n for a, b, n in rows},
            "sample": [dict(zip(("selection_id", "entity_type", "entity_id", "field_name", "member_key", "value",
                                 "depth"), r)) for r in sample]}


def verify_sources(conn: psycopg.Connection, keys, *, snapshot_root: str | Path | None = None,
                   imdb_root: str | Path | None = None, kinds: Sequence[str] = ("page_snapshot", "imdb_row",
                                                                                "review_record"),
                   workers: int = 8, limit: int = 20) -> dict[str, Any]:
    """重算被引用来源的哈希并与登记值比对（只读）。返回各类核验结果、不一致的源记录、受影响字段与重跑步骤。"""
    from .source_records import default_snapshot_root
    from etl_task4.imdb import snapshot_dir

    sk = keys.sources
    out: dict[str, Any] = {"keys": keys.as_json(), "checked": {}, "mismatched": [], "unavailable": []}
    bad_ids: dict[str, list[str]] = {}
    if "page_snapshot" in kinds:
        root = Path(snapshot_root) if snapshot_root else default_snapshot_root()
        rows = conn.execute("SELECT source_record_id, locator->>'snapshot_path', content_hash, locator->>'asin' "
                            "FROM task9.source_record WHERE stage_key = %s AND source_kind = 'page_snapshot' "
                            "ORDER BY source_record_id", (sk,)).fetchall()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(lambda r: snapshot_status(resolve_snapshot(r[1], root), r[2]), rows))
        n_ok = 0
        for (sid, path, expected, asin), (status, detail) in zip(rows, results):
            if status == "ok":
                n_ok += 1
                continue
            bad_ids.setdefault("page_snapshot", []).append(sid)
            out["mismatched"].append({"source_record_id": sid, "source_kind": "page_snapshot", "asin": asin,
                                      "status": status, "snapshot_path": path, **detail})
        out["checked"]["page_snapshot"] = {"records": len(rows), "ok": n_ok, "root": str(root)}
    if "imdb_row" in kinds:
        files = conn.execute("SELECT locator->>'snapshot', locator->>'file', content_hash, count(*) "
                             "FROM task9.source_record WHERE stage_key = %s AND source_kind = 'imdb_row' "
                             "GROUP BY 1, 2, 3 ORDER BY 1, 2", (sk,)).fetchall()
        checked = []
        for snap, name, expected, n in files:
            path = snapshot_dir(snap, imdb_root) / name
            if not path.exists():
                out["unavailable"].append({"source_kind": "imdb_row", "file": str(path), "records": n})
                checked.append({"file": name, "records": n, "status": "unavailable"})
                continue
            actual = file_sha256(path)
            status = "ok" if actual == expected else "file_hash_mismatch"
            checked.append({"file": name, "records": n, "status": status})
            if status != "ok":
                ids = [r[0] for r in conn.execute(
                    "SELECT source_record_id FROM task9.source_record WHERE stage_key = %s AND source_kind = 'imdb_row' "
                    "AND locator->>'snapshot' = %s AND locator->>'file' = %s", (sk, snap, name))]
                bad_ids.setdefault("imdb_row", []).extend(ids)
                out["mismatched"].append({"source_kind": "imdb_row", "file": str(path), "status": status,
                                          "expected": expected, "actual": actual, "records": len(ids)})
        out["checked"]["imdb_row"] = checked
    if "review_record" in kinds:
        files = conn.execute(
            "SELECT f.path, f.recorded_sha256, count(*) FROM task9.source_record r "
            "JOIN public.source_file f ON f.source_file_id = r.locator->>'source_file_sha256' "
            "WHERE r.stage_key = %s AND r.source_kind = 'review_record' GROUP BY 1, 2", (sk,)).fetchall()
        checked = []
        for path, expected, n in files:
            p = Path(path)
            if not p.exists():
                out["unavailable"].append({"source_kind": "review_record", "file": path, "records": n})
                checked.append({"file": path, "records": n, "status": "unavailable"})
                continue
            actual = file_sha256(p)
            status = "ok" if actual == expected else "file_hash_mismatch"
            checked.append({"file": path, "records": n, "status": status})
            if status != "ok":
                ids = [r[0] for r in conn.execute(
                    "SELECT source_record_id FROM task9.source_record WHERE stage_key = %s "
                    "AND source_kind = 'review_record' AND locator->>'source_file_sha256' = %s", (sk, expected))]
                bad_ids.setdefault("review_record", []).extend(ids)
                out["mismatched"].append({"source_kind": "review_record", "file": path, "status": status,
                                          "expected": expected, "actual": actual, "records": len(ids)})
        out["checked"]["review_record"] = checked
    all_bad = [i for ids in bad_ids.values() for i in ids]
    out["affected"] = _affected(conn, keys, all_bad, limit)
    out["rerun"] = rerun_plan(list(bad_ids))
    out["ok"] = not all_bad
    return out


# ---------------------------------------------------------------- 命令行

def main(argv: list[str] | None = None) -> int:
    """python -m etl_task9.impact verify-sources [--kinds page_snapshot,imdb_row,review_record]（只读）。
    有不一致时退出码 1，便于脚本判断；文件不可读（unavailable）不算不一致。"""
    import argparse
    import json
    import sys

    from etl_task1.config import load_settings
    from etl_task1.db import connect

    from .run import RunInputs
    from .versions import resolve

    ap = argparse.ArgumentParser(prog="python -m etl_task9.impact")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify-sources", help="重算被引用快照、IMDb 文件与评论文件的哈希（R32）")
    v.add_argument("--lineage-version", default=None)
    v.add_argument("--kinds", default="page_snapshot,imdb_row,review_record")
    v.add_argument("--snapshot-root", default=None)
    v.add_argument("--imdb-root", default=None)
    v.add_argument("--workers", type=int, default=8)
    v.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    import os
    with connect(load_settings(os.environ)) as conn:
        keys = resolve(conn, args.lineage_version, RunInputs(), need="impact")
        out = verify_sources(conn, keys, snapshot_root=args.snapshot_root, imdb_root=args.imdb_root,
                             kinds=[k for k in args.kinds.split(",") if k], workers=args.workers, limit=args.limit)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
