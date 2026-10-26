"""流程测试辅助：用环境变量把输出与输入指向临时位置，按 DAG 顺序执行各步骤。"""

from __future__ import annotations

import json
from pathlib import Path

from etl_task1 import pipeline
from etl_task1.pipeline import RunParams, StepError


def params(fixture_files, run_key="manual__test", **over) -> RunParams:
    gz, manifest = fixture_files
    base = dict(run_key=run_key, scope="sample", sample_max_records=100, batch_rows=3,
                source_gzip_path=str(gz), source_manifest_path=str(manifest))
    base.update(over)
    return RunParams(**base)


def run_all(p: RunParams) -> dict:
    """顺序执行，等价于 DAG：上游失败时记录 None，收尾与门禁照常运行。"""
    out: dict = {}
    failed: dict[str, StepError] = {}

    def attempt(name, fn):
        if fn is None:  # 上游失败：Airflow 中该任务为 upstream_failed，不会运行也不会留下失败记录
            out[name] = None
            return
        try:
            out[name] = pipeline.run_step(name, p, fn)
        except StepError as exc:
            out[name] = None
            failed[name] = exc

    attempt("verify_source", lambda: pipeline.verify_source_step(p))
    ok = out["verify_source"] is not None
    attempt("extract_load", (lambda: pipeline.extract_load_step(p, out["verify_source"])) if ok else None)
    ok = ok and out["extract_load"] is not None
    eid = lambda: out["extract_load"]["extraction_id"]
    attempt("validate_counts", (lambda: pipeline.validate_counts_step(p, eid())) if ok else None)
    ok = ok and out["validate_counts"] is not None
    attempt("export_results", (lambda: pipeline.export_results_step(p, eid(), out["validate_counts"])) if ok else None)
    upstream = {n: out.get(n) for n in pipeline.STEP_NAMES}
    final = pipeline.run_step("finalize_report", p, lambda: pipeline.finalize_report_step(p, upstream))
    gate_ok, reason = pipeline.gate_decision(final)
    return {"steps": out, "failed": failed, "final": final, "gate_ok": gate_ok, "gate_reason": reason}


def read_summary(final: dict) -> dict:
    return json.loads((Path(final["report_dir"]) / "summary.json").read_text(encoding="utf-8"))
