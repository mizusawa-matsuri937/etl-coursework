"""任务 8 运行编排（spec R33–R38）。

当前只有分阶段运行 run_stages（kind = 'stage'）：按依赖链执行 review_clean → review_family → review_dedup 中
指定的前若干阶段，用于测试与业务库副本演练；已完成的阶段按阶段键直接跳过，未完成的从第一个未提交的批次单元继续。
完整的去重运行（输入检查、复用、四个阶段、物化版本、核对后才成功）与 DAG 步骤由 T8-04、T8-05 在本模块补充。

命令（项目根目录；演练库见 design“业务库副本演练”）：

    docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler \
        python -m etl_task8.runner stages --through review_dedup
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import psycopg

from .rules import RuleSet, ruleset
from .run import (DEFAULT_BATCH, DEFAULT_PARTITIONS, BeforeCommit, RunInputs, add_counts, apply_task8_schema,
                  check_extraction, close_abandoned, extraction_lock, fail_run, finish_run, start_run)

STAGE_ORDER = ("review_clean", "review_family", "review_dedup")


def run_stage(conn: psycopg.Connection, ctx, stage: str, *, batch_size: int = DEFAULT_BATCH,
              partitions: int = DEFAULT_PARTITIONS, before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """执行一个阶段（调用方已持锁并建好运行）；返回该阶段的计数。"""
    if stage == "review_clean":
        from . import clean
        return clean.clean_reviews(conn, ctx, batch_size=batch_size, before_commit=before_commit)
    if stage == "review_family":
        from . import family
        return family.build_families(conn, ctx, partitions=partitions, before_commit=before_commit)
    if stage == "review_dedup":
        from . import dedup
        return dedup.dedup_reviews(conn, ctx, partitions=partitions, before_commit=before_commit)
    raise ValueError(f"未知的阶段 {stage!r}")


def run_stages(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None = None, *,
               through: str = "review_dedup", batch_size: int = DEFAULT_BATCH, partitions: int = DEFAULT_PARTITIONS,
               before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """按依赖链执行到 through（含）为止；记一条 kind = 'stage' 的运行。失败时运行记为 failed，已提交的批次保留。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if through not in STAGE_ORDER:
        raise ValueError(f"through 只能是 {STAGE_ORDER}")
    rules = rules or ruleset(inputs.rule_version)
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    source = check_extraction(conn, inputs)
    apply_task8_schema(conn)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        ctx = start_run(conn, inputs, rules, "stage",
                        params={"through": through, "batch_size": batch_size, "partitions": partitions})
        counts: dict[str, Any] = {"run_id": ctx.run_id, "source": source}
        try:
            for stage in STAGE_ORDER[:STAGE_ORDER.index(through) + 1]:
                started = time.monotonic()
                counts[stage] = run_stage(conn, ctx, stage, batch_size=batch_size, partitions=partitions,
                                          before_commit=before_commit)
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


def _connect() -> psycopg.Connection:
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    return connect(load_settings(os.environ))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl_task8.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stages", help="按依赖链执行前若干阶段（演练与调试）")
    st.add_argument("--through", default="review_dedup", choices=STAGE_ORDER)
    st.add_argument("--extraction-id", default=RunInputs().extraction_id)
    st.add_argument("--rule-version", default=RunInputs().rule_version)
    st.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    st.add_argument("--partitions", type=int, default=DEFAULT_PARTITIONS)
    args = ap.parse_args(argv)
    inputs = RunInputs(extraction_id=args.extraction_id, rule_version=args.rule_version)
    with _connect() as conn:
        out = run_stages(conn, inputs, through=args.through, batch_size=args.batch_size, partitions=args.partitions)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
