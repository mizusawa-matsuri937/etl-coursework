"""任务 9 运行编排（spec R36–R40）。

T9-01 只提供 run_stages：输入检查 → 抽取锁 → 按依赖链执行前若干阶段（kind = 'stage'，不物化血缘版本），用于测试与
业务库副本演练。阶段结果按阶段键保存：已完成的阶段直接跳过（完成标记），未完成的从第一个未提交的实体继续（R38）。
完整运行（物化 lv 版本、verify、reused）与 DAG 步骤在 T9-05 加入，沿用任务 8 runner 的写法。

命令（项目根目录；演练库见 design“业务库副本演练”）：

    docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler \
        python -m etl_task9.runner stages --through assertions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, RunInputs, add_counts, apply_task9_schema, check_inputs,
                  close_abandoned, extraction_lock, fail_run, finish_run, stage_complete, start_run)
from .rules import STAGES

IMPLEMENTED = ("sources", "assertions")      # T9-01；derivations、selections、impact 由后续阶段实现


def exec_stage(conn: psycopg.Connection, ctx: RunContext, stage: str, *, batch_size: int = DEFAULT_BATCH,
               snapshot_root: str | Path | None = None, snapshot_workers: int = 8,
               before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """执行一个阶段（调用方已持锁并建好运行）；该阶段键已完成时直接返回 skipped。"""
    if stage_complete(conn, ctx.key(stage)):
        return {"stage_key": ctx.key(stage), "skipped": True}
    if stage == "sources":
        from . import source_records
        return source_records.build_sources(conn, ctx, batch_size=batch_size, snapshot_root=snapshot_root,
                                            snapshot_workers=snapshot_workers, before_commit=before_commit)
    if stage == "assertions":
        from . import assertions
        return assertions.build_assertions(conn, ctx, batch_size=batch_size, before_commit=before_commit)
    raise NotImplementedError(f"阶段 {stage} 尚未实现（T9-01 只实现 {IMPLEMENTED}）")


def run_stages(conn: psycopg.Connection, inputs: RunInputs, *, through: str = "assertions",
               batch_size: int = DEFAULT_BATCH, snapshot_root: str | Path | None = None, snapshot_workers: int = 8,
               before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """按依赖链执行到 through（含）为止；记一条 kind = 'stage' 的运行。失败时运行记为 failed，已提交的批次保留。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if through not in STAGES:
        raise ValueError(f"through 只能是 {STAGES}")
    inputs = check_inputs(conn, inputs)
    apply_task9_schema(conn)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        ctx = start_run(conn, inputs, "stage", params={"through": through, "batch_size": batch_size,
                                                       "snapshot_root": str(snapshot_root) if snapshot_root else None,
                                                       "snapshot_workers": snapshot_workers})
        counts: dict[str, Any] = {"run_id": ctx.run_id}
        try:
            for stage in STAGES[:STAGES.index(through) + 1]:
                started = time.monotonic()
                counts[stage] = exec_stage(conn, ctx, stage, batch_size=batch_size, snapshot_root=snapshot_root,
                                           snapshot_workers=snapshot_workers, before_commit=before_commit)
                counts[stage]["elapsed_seconds"] = round(time.monotonic() - started, 2)
                add_counts(conn, ctx.run_id, stage, counts[stage])
        except BaseException as exc:
            try:
                fail_run(conn, ctx.run_id, f"{type(exc).__name__}: {exc}")
            except psycopg.Error:
                pass
            raise
        finish_run(conn, ctx.run_id, "succeeded", {k: v for k, v in counts.items() if k != "run_id"})
        return counts


# ---------------------------------------------------------------- 命令行

def _connect() -> psycopg.Connection:
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    return connect(load_settings(os.environ))


def main(argv: list[str] | None = None) -> int:
    d = RunInputs()
    ap = argparse.ArgumentParser(prog="python -m etl_task9.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stages", help="按依赖链执行前若干阶段（演练与调试，不物化血缘版本）")
    st.add_argument("--through", default="assertions", choices=IMPLEMENTED)
    st.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    st.add_argument("--snapshot-root", default=None, help="快照相对路径的根目录（默认项目根目录 /opt/etl）")
    st.add_argument("--snapshot-workers", type=int, default=8)
    for name in ("extraction_id", "t3_rule_version", "parser_version", "t4_mapping_version_id", "norm_version",
                 "t5_person_mapping_version_id", "t6_release_version_id", "conflict_version_id",
                 "review_dedup_version_id", "imdb_snapshot_id", "rule_version"):
        st.add_argument("--" + name.replace("_", "-"), default=getattr(d, name))
    args = ap.parse_args(argv)
    inputs = RunInputs(**{k: getattr(args, k) for k in (
        "extraction_id", "t3_rule_version", "parser_version", "t4_mapping_version_id", "norm_version",
        "t5_person_mapping_version_id", "t6_release_version_id", "conflict_version_id", "review_dedup_version_id",
        "imdb_snapshot_id", "rule_version")})
    with _connect() as conn:
        out = run_stages(conn, inputs, through=args.through, batch_size=args.batch_size,
                         snapshot_root=args.snapshot_root, snapshot_workers=args.snapshot_workers)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
