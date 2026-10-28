"""任务 2 清单流程步骤：与 Airflow 解耦的纯 Python 函数，DAG 只负责调用与编排（spec R13、R14）。

步骤之间只传小型 JSON（抽取 ID、构建 ID、计数、文件摘要），不传清单行或评论正文。
沿用任务 1 的失败语义：确定性错误不重试，暂时错误交给 Airflow 有上限地重试，收尾无论成败都运行，门禁判定整体结果。
"""

from __future__ import annotations

import csv
import dataclasses
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg

from etl_task1 import reporting
from etl_task1.config import ConfigError, Settings, load_settings
from etl_task1.db import connect, sql_dir
from etl_task1.pipeline import FAILED, SUCCEEDED, StepError, gate_decision  # noqa: F401  门禁与任务 1 同义
from etl_task1.pipeline import is_transient as _task1_transient

from . import RULE_VERSION
from .manifest import (ManifestBusy, apply_task2_schema, build_id_for, build_manifest,
                       check_extraction)
from .urls import URL_TEMPLATE, URL_TEMPLATE_VERSION

log = logging.getLogger(__name__)

DAG_ID = "amazon_products_task2_manifest"
STEP_NAMES = ("check_extraction", "build_manifest", "verify_manifest", "export_manifest")
STATUSES = ("success", "not_found", "access_restricted", "timeout", "parse_failed", "not_collected")
SUMMARY_VERSION = "task2-manifest-summary-1"
EXPORT_NAME = "product_manifest.csv"


@dataclass(frozen=True)
class RunParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变
    extraction_id: str | None = None
    allow_sample: bool = False
    try_number: int = 1
    dag_id: str = DAG_ID
    fault_injection: str | None = None   # before_commit | transient_once:<标记文件>；仅 ETL_ENV=test

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


class InjectedFault(RuntimeError):
    """测试用确定性故障。"""


class ManifestVerificationError(RuntimeError):
    """清单与来源数量不符（确定性错误）。"""

    def __init__(self, failures: list[str]):
        super().__init__("；".join(failures))
        self.failures = failures


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, ManifestBusy) or _task1_transient(exc)


def settings_for(params: RunParams, env: dict[str, str] | None = None) -> Settings:
    s = load_settings(dict(os.environ if env is None else env))
    if params.fault_injection and s.env != "test":
        raise ConfigError("故障注入仅允许在 ETL_ENV=test 下启用")
    return s


def output_root(output_dir: Path) -> Path:
    return output_dir / "task2"


def run_dir(output_dir: Path, extraction_id: str | None, build_id: str | None, run_key: str) -> Path:
    """outputs/task2/<extraction_id>/<build_id>/<run_key>/；抽取或构建未知时落在 _failures。"""
    if extraction_id and build_id:
        return output_root(output_dir) / extraction_id / build_id / run_key
    return failures_dir(output_dir, run_key)


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return output_root(output_dir) / "_failures" / run_key


def record_failure(output_dir: Path, params: RunParams, step: str, exc: BaseException, transient: bool) -> None:
    """失败写成文件，数据库不可用时也能留下原因。写不进去只记日志，不掩盖原异常。"""
    try:
        entry = {"task": step, "try_number": params.try_number, "error_type": type(exc).__name__,
                 "message": str(exc)[:4000], "transient": transient, "at": reporting.utc_now(),
                 "traceback": "".join(traceback.format_exception(exc))[-6000:]}
        path = failures_dir(output_dir, params.run_key) / f"{step}-try{params.try_number}.json"
        reporting.write_atomic(path, (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode())
    except Exception:  # noqa: BLE001
        log.exception("无法写入失败记录")


def run_step(step: str, params: RunParams, fn: Callable[[], dict[str, Any]],
             output_dir: Path | None = None) -> dict[str, Any]:
    """执行步骤；失败时记录并转成 StepError，由 DAG 决定是否重试。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        transient = is_transient(exc)
        record_failure(output_dir or Path(os.environ.get("ETL_OUTPUT_DIR", "outputs")), params, step, exc, transient)
        raise StepError(step, exc, transient) from exc


def check_extraction_step(params: RunParams) -> dict[str, Any]:
    """R01：输入抽取合格才继续；同时确保 task2 表结构已应用。"""
    if not params.extraction_id:
        raise ConfigError("必须提供 extraction_id 参数")
    s = settings_for(params)
    with connect(s) as conn:
        apply_task2_schema(conn)
        scope = check_extraction(conn, params.extraction_id, allow_sample=params.allow_sample)
    return {"extraction_id": params.extraction_id, "scope": scope,
            "build_id": build_id_for(params.extraction_id), "url_template": URL_TEMPLATE,
            "url_template_version": URL_TEMPLATE_VERSION, "rule_version": RULE_VERSION}


def _fault_hook(params: RunParams) -> Callable[[], None] | None:
    fault = params.fault_injection
    if not fault:
        return None
    if fault.startswith("transient_once:"):
        marker = Path(fault.split(":", 1)[1])
        if not marker.exists():  # 模拟数据库暂时断连：第一次失败，之后成功
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("injected\n")
            raise psycopg.OperationalError("injected transient connection failure")
        return None
    if fault == "before_commit":
        def boom() -> None:
            raise InjectedFault("injected failure before manifest commit")
        return boom
    raise ConfigError(f"未知的故障注入：{fault!r}")


def build_manifest_step(params: RunParams, checked: dict[str, Any]) -> dict[str, Any]:
    s = settings_for(params)
    hook = _fault_hook(params)
    started = time.monotonic()
    with connect(s) as conn:
        r = build_manifest(conn, checked["extraction_id"], allow_sample=params.allow_sample,
                           dag_id=params.dag_id, airflow_run_id=params.run_key, before_commit=hook)
    return {**dataclasses.asdict(r), "elapsed_seconds": round(time.monotonic() - started, 2)}


def verify_manifest_step(params: RunParams, extraction_id: str) -> dict[str, Any]:
    """R02、R03、R08：清单行数、in_valid 分布、状态分布与任务 1 现算的产品数核对。"""
    s = settings_for(params)
    started = time.monotonic()
    with connect(s) as conn:
        for table in ("review_record", "task2.product_manifest"):
            conn.execute(f"ANALYZE {table}")
        cur = conn.execute((sql_dir() / "task2_manifest_checks.sql").read_text(encoding="utf-8"),
                           {"eid": extraction_id, "tv": URL_TEMPLATE_VERSION})
        counts = dict(zip((d.name for d in cur.description), (int(v) for v in cur.fetchone())))
        dist = dict(conn.execute("SELECT status, count(*) FROM task2.product_manifest WHERE extraction_id = %s "
                                 "GROUP BY status", (extraction_id,)).fetchall())
    status_distribution = {st: int(dist.get(st, 0)) for st in STATUSES}
    failures = []
    if counts["manifest_rows"] != counts["products_observed"]:
        failures.append(f"清单行数 {counts['manifest_rows']} ≠ observed 产品数 {counts['products_observed']}")
    if counts["in_valid_true"] != counts["products_valid"]:
        failures.append(f"in_valid_records 为真 {counts['in_valid_true']} ≠ valid 产品数 {counts['products_valid']}")
    if counts["in_valid_true"] + counts["in_valid_false"] != counts["manifest_rows"]:
        failures.append("in_valid_records 分布之和不等于清单行数")
    if set(dist) - set(STATUSES) or sum(status_distribution.values()) != counts["manifest_rows"]:
        failures.append(f"状态分布之和不等于清单行数：{dist}")
    if counts["other_template_rows"]:
        failures.append(f"{counts['other_template_rows']} 行的链接模板版本不是 {URL_TEMPLATE_VERSION}")
    if failures:
        raise ManifestVerificationError(failures)
    return {"counts": counts, "status_distribution": status_distribution,
            "elapsed_seconds": round(time.monotonic() - started, 2)}


def export_manifest_step(params: RunParams, extraction_id: str, build_id: str,
                         verified: dict[str, Any]) -> dict[str, Any]:
    """R14：导出清单 CSV 并核对行数与库一致。summary.json 由收尾步骤写出。"""
    s = settings_for(params)
    directory = run_dir(s.output_dir, extraction_id, build_id, params.run_key)
    with connect(s) as conn:
        digest = reporting.export_csv(conn, "task2_manifest_export.sql", extraction_id, directory / EXPORT_NAME)
    with open(directory / EXPORT_NAME, newline="", encoding="utf-8") as fh:
        data_rows = sum(1 for _ in csv.reader(fh)) - 1
    if data_rows != verified["counts"]["manifest_rows"]:
        raise ManifestVerificationError([f"导出 {data_rows} 行 ≠ 清单 {verified['counts']['manifest_rows']} 行"])
    return {"directory": str(directory), "files": [{**digest, "rows": data_rows}]}


def _read_failures(output_dir: Path, run_key: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(failures_dir(output_dir, run_key).glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out.append({"task": p.stem, "error_type": "unreadable_failure_file", "message": str(p)})
    return out


def finalize_step(params: RunParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：汇总现状写 summary.json 与 report.md。写出报告本身不代表数据成功。"""
    s = settings_for(params)
    checked, built, verified, exported = (upstream.get(n) for n in STEP_NAMES)
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    build_id = (checked or {}).get("build_id")
    failures = _read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "build_id": build_id,
        "scope": (checked or {}).get("scope"), "allow_sample": params.allow_sample,
        "url_template": URL_TEMPLATE, "url_template_version": URL_TEMPLATE_VERSION, "rule_version": RULE_VERSION,
        "tasks": tasks, "failures": failures,
        "build": {k: built[k] for k in ("run_id", "status", "inserted_rows", "total_rows", "elapsed_seconds")}
        if built else None,
    }
    if verified:
        c = verified["counts"]
        summary.update(
            manifest_rows=c["manifest_rows"], products_observed=c["products_observed"],
            products_valid=c["products_valid"], url_issue_rows=c["url_issue_rows"],
            in_valid_distribution={"true": c["in_valid_true"], "false": c["in_valid_false"]},
            status_distribution=verified["status_distribution"],
            verify_elapsed_seconds=verified["elapsed_seconds"])
    summary["exports"] = (exported or {}).get("files", [])
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    directory = run_dir(s.output_dir, eid if build_id else None, build_id, params.run_key)
    reporting.write_atomic(directory / "summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    reporting.write_atomic(directory / "report.md", render_markdown(summary).encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"],
            "report_dir": str(directory), "extraction_id": eid}


def _database_available(s: Settings) -> bool:
    try:
        with connect(s, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


def render_markdown(s: dict[str, Any]) -> str:
    fmt = lambda n: f"{n:,}" if isinstance(n, int) else str(n)  # noqa: E731
    lines = ["# 任务 2 采集清单构建报告", "",
             f"- 最终状态：**{s['final_status']}**（交付完整：{s['delivery_complete']}）",
             f"- DAG / run：`{s['dag_id']}` / `{s['run_key']}`",
             f"- 抽取：`{s['extraction_id']}`（scope={s['scope']}）",
             f"- 构建：`{s['build_id']}`，链接模板 `{s['url_template']}`（{s['url_template_version']}），"
             f"规则 {s['rule_version']}", "", "## 任务状态", ""]
    lines += [f"- {k}：{v}" for k, v in s["tasks"].items()]
    if "manifest_rows" in s:
        lines += ["", "## 数量核对", "",
                  "| 指标 | 数值 |", "|---|---|",
                  f"| 清单行数 | {fmt(s['manifest_rows'])} |",
                  f"| observed 产品数（任务 1 现算） | {fmt(s['products_observed'])} |",
                  f"| valid 产品数（任务 1 现算） | {fmt(s['products_valid'])} |",
                  f"| in_valid_records 真 / 假 | {fmt(s['in_valid_distribution']['true'])} / "
                  f"{fmt(s['in_valid_distribution']['false'])} |",
                  f"| 无法生成链接（url_issue） | {fmt(s['url_issue_rows'])} |",
                  "", "## 状态分布", "", "| 状态 | 行数 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in s["status_distribution"].items()]
    if s["exports"]:
        lines += ["", "## 导出文件", ""]
        lines += [f"- `{f['path']}`：{fmt(f['rows'])} 行，{fmt(f['bytes'])} 字节，SHA-256 `{f['sha256']}`"
                  for f in s["exports"]]
    if s["failures"]:
        lines += ["", "## 失败记录", ""]
        lines += [f"- {f.get('task')} 第 {f.get('try_number')} 次：{f.get('error_type')}："
                  f"{str(f.get('message'))[:300]}（暂时错误：{f.get('transient')}）" for f in s["failures"]]
    return "\n".join(lines) + "\n"
