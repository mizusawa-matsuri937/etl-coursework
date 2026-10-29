"""人工纠正（spec R30，AC08）：导入“必须合并”“不得合并”“移出”三种人工决策，再重算生成新的映射版本。

CSV 列：kind（must_link、cannot_link、detach）、left（ASIN）、right（ASIN；detach 留空）、note、annotator。
导入整体一个事务：引用不存在的商品、类别不对、重复写法有误时整份失败，不写部分行。人工决策不删除自动判定，
映射叠加在自动聚类之后；与硬冲突矛盾的人工合并仍执行，但记录 human_vs_veto（见 cluster.build_mapping）。

命令行（容器内，工作目录 /opt/etl）：
    python -m etl_task4.review import decisions.csv --extraction-id <抽取> --imdb-snapshot imdb_2026-10-30 --annotator <姓名>
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect

from .match import DEFAULT_RULES, RuleSet
from .run import RunInputs, extraction_lock, finish_run, human_set_hash, start_run
from .runner import RunResult, execute_match

KINDS = ("must_link", "cannot_link", "detach")


class ReviewInputError(RuntimeError):
    """人工决策文件不合格（确定性错误）；整份导入失败。"""


@dataclass(frozen=True)
class HumanRow:
    kind: str
    left: str
    right: str | None
    note: str | None = None
    annotator: str | None = None


@dataclass
class ImportResult:
    inserted: int
    duplicates: int
    set_hash: str
    previous_hash: str
    file_sha256: str


def read_csv(path: Path) -> tuple[list[HumanRow], str]:
    raw = Path(path).read_bytes()
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    missing = {"kind", "left"} - set(reader.fieldnames or ())
    if missing:
        raise ReviewInputError(f"人工决策文件缺少列 {sorted(missing)}")
    rows = [HumanRow((r.get("kind") or "").strip(), (r.get("left") or "").strip(), (r.get("right") or "").strip() or None,
                     (r.get("note") or "").strip() or None, (r.get("annotator") or "").strip() or None)
            for r in reader]
    return rows, hashlib.sha256(raw).hexdigest()


def _validate(conn: psycopg.Connection, inputs: RunInputs, rows: Iterable[HumanRow]) -> None:
    movies = {r[0] for r in conn.execute(
        "SELECT asin FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s "
        "AND product_kind = 'movie' AND match_scope = 'matched'", (inputs.extraction_id, inputs.t3_rule_version)).fetchall()}
    problems = []
    for n, r in enumerate(rows, start=1):
        if r.kind not in KINDS:
            problems.append(f"第 {n} 行：类别 {r.kind!r} 不是 {KINDS}")
            continue
        if (r.kind == "detach") != (r.right is None):
            problems.append(f"第 {n} 行：detach 不填 right，其余两类必须填 right")
        if r.left not in movies or (r.right is not None and r.right not in movies):
            problems.append(f"第 {n} 行：{r.left}/{r.right} 不是本抽取里参与匹配的电影类商品")
        if r.right is not None and r.right == r.left:
            problems.append(f"第 {n} 行：left 与 right 相同")
    if problems:
        raise ReviewInputError("；".join(problems))


def import_decisions(conn: psycopg.Connection, inputs: RunInputs, rows: list[HumanRow], *, annotator: str,
                     file_sha256: str) -> ImportResult:
    """写入人工决策（累积的决策集），并登记一条 human_import 运行。重复的决策被忽略。"""
    if not rows:
        raise ReviewInputError("人工决策文件没有任何决策")
    with extraction_lock(conn, inputs.extraction_id):
        _validate(conn, inputs, rows)
        previous = human_set_hash(conn, inputs.extraction_id)
        with conn.transaction():
            inserted = 0
            for r in rows:
                inserted += conn.execute(
                    "INSERT INTO task4.human_decision (extraction_id, set_hash, kind, left_entity, right_entity, note, "
                    "annotator, import_file_sha256) VALUES (%s, 'pending', %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (extraction_id, kind, left_entity, (coalesce(right_entity, ''))) DO NOTHING",
                    (inputs.extraction_id, r.kind, r.left, r.right, r.note, r.annotator or annotator,
                     file_sha256)).rowcount
            new_hash = human_set_hash(conn, inputs.extraction_id)
            conn.execute("UPDATE task4.human_decision SET set_hash = %s WHERE extraction_id = %s AND set_hash = 'pending'",
                         (new_hash, inputs.extraction_id))
            ctx = start_run(conn, dataclasses.replace(inputs, human_set_hash=new_hash), "human_import",
                            params={"file_sha256": file_sha256, "rows": len(rows)})
            conn.execute("UPDATE task4.human_decision SET run_id = %s WHERE extraction_id = %s AND run_id IS NULL",
                         (ctx.run_id, inputs.extraction_id))
            finish_run(conn, ctx.run_id, "succeeded", {"inserted": inserted, "duplicates": len(rows) - inserted})
    return ImportResult(inserted, len(rows) - inserted, new_hash, previous, file_sha256)


def import_and_remap(conn: psycopg.Connection, inputs: RunInputs, rows: list[HumanRow], *, annotator: str,
                     file_sha256: str, rules: RuleSet = DEFAULT_RULES, batch_size: int = 500,
                     ) -> tuple[ImportResult, RunResult]:
    """导入之后立即重算：决策集变了，输入键就是新的，生成新的映射版本；标准化与判定阶段不重做。"""
    imported = import_decisions(conn, inputs, rows, annotator=annotator, file_sha256=file_sha256)
    return imported, execute_match(conn, inputs, rules, batch_size=batch_size)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 4 人工纠正")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="导入人工决策 CSV 并重算映射")
    imp.add_argument("csv", type=Path)
    imp.add_argument("--extraction-id", required=True)
    imp.add_argument("--imdb-snapshot", required=True)
    imp.add_argument("--annotator", required=True)
    args = ap.parse_args(argv)
    rows, sha = read_csv(args.csv)
    inputs = RunInputs(extraction_id=args.extraction_id, imdb_snapshot_id=args.imdb_snapshot)
    with connect(load_settings()) as conn:
        imported, result = import_and_remap(conn, inputs, rows, annotator=args.annotator, file_sha256=sha)
        print(json.dumps({"import": dataclasses.asdict(imported), "run": result.run_id, "status": result.status,
                          "mapping_version_id": result.mapping_version_id}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
