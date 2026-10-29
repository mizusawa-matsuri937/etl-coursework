"""任务 2 爬虫流程步骤：供 DAG amazon_products_task2_crawl 调用的纯 Python 函数（spec R34）。

DAG：check_crawl_input → crawl_pages → verify_crawl → finalize_report → run_gate。
失败语义与清单 DAG 相同（etl_task2.pipeline）：确定性错误不重试，暂时错误（数据库断连、爬虫锁被占用）
交给 Airflow 有上限地重试；爬虫可续跑，重试只处理剩余行。收尾无论成败都运行，门禁判定整体结果。
fetch_mode=fixture 用离线假请求，只允许在 ETL_ENV=test 下使用，测试与测试库端到端不访问外网。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from etl_task1 import reporting
from etl_task1.config import ConfigError, Settings, load_settings
from etl_task1.db import connect
from etl_task2.pipeline import (FAILED, SUCCEEDED, _database_available, _read_failures, failures_dir,
                                output_root)

from .check import problems, run_checks
from .crawler import (RETRYABLE, CrawlConfig, ExitPool, FetchResult, SessionFetcher, apply_crawl_schema, crawl,
                      latest_full_extraction, load_exits)

DAG_ID = "amazon_products_task2_crawl"
STEP_NAMES = ("check_crawl_input", "crawl_pages", "verify_crawl")
FETCH_MODES = ("live", "fixture")
RETRY_CHOICES = RETRYABLE + ("parse_failed",)
SUMMARY_VERSION = "task2-crawl-summary-1"


@dataclass(frozen=True)
class CrawlParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变
    extraction_id: str | None = None
    limit: int | None = 1000
    deadline_minutes: float | None = 60
    concurrency: int = 40
    rate: float = 0.4
    timeout: float = 15
    retry_statuses: tuple[str, ...] = ()
    fetch_mode: str = "live"        # live | fixture（仅 ETL_ENV=test）
    try_number: int = 1
    dag_id: str = DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


class CrawlVerificationError(RuntimeError):
    """本次爬取结果核对不通过（确定性错误）。"""


def settings_for(params: CrawlParams, env: dict[str, str] | None = None) -> Settings:
    s = load_settings(dict(os.environ if env is None else env))
    if params.fetch_mode not in FETCH_MODES:
        raise ConfigError(f"fetch_mode 只能是 {FETCH_MODES}，实际为 {params.fetch_mode!r}")
    if params.fetch_mode == "fixture" and s.env != "test":
        raise ConfigError("离线假请求（fetch_mode=fixture）仅允许在 ETL_ENV=test 下使用")
    bad = set(params.retry_statuses) - set(RETRY_CHOICES)
    if bad:
        raise ConfigError(f"retry_statuses 只能取 {RETRY_CHOICES}，实际含 {sorted(bad)}")
    return s


def fixture_body(asin: str) -> bytes:
    return f'<span id="productTitle">Fixture {asin}</span><input name="ASIN" value="{asin}">'.encode()


async def fixture_fetch(url: str, proxy: str | None, timeout: float) -> FetchResult:
    """确定性的离线响应，按 ASIN 末位区分：D 为 404，C 跳转到 checkpoint，A 带一次 301 跳转，其余为产品页。"""
    asin = url.rstrip("/").rsplit("/", 1)[-1]
    if asin.endswith("D"):
        return FetchResult(404, url, b"<title>Page Not Found</title>")
    if asin.endswith("C"):
        target = f"https://www.amazon.com/checkpoint/continue?asin={asin}"
        return FetchResult(200, target, b"<html>checkpoint</html>",
                           ({"status": 302, "url": url, "location": target},))
    if asin.endswith("A"):
        moved = f"{url}?ref=fixture"
        return FetchResult(200, moved, fixture_body(asin), ({"status": 301, "url": url, "location": moved},))
    return FetchResult(200, url, fixture_body(asin))


def _status_counts(conn, eid: str) -> dict[str, int]:
    return dict(conn.execute("SELECT status, count(*) FROM task2.product_manifest WHERE extraction_id = %s "
                             "GROUP BY status ORDER BY status", (eid,)).fetchall())


def check_crawl_input_step(params: CrawlParams) -> dict[str, Any]:
    """清单存在才继续；应用爬虫迁移；记录运行前的状态分布与待处理行数。"""
    s = settings_for(params)
    with connect(s) as conn:
        apply_crawl_schema(conn)
        eid = params.extraction_id or latest_full_extraction(conn)
        before = _status_counts(conn, eid)
        if not before:
            raise ConfigError(f"抽取 {eid} 没有采集清单，请先运行清单 DAG")
        pending = conn.execute("SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %s "
                               "AND status = ANY(%s) AND original_url IS NOT NULL",
                               (eid, ["not_collected", *params.retry_statuses])).fetchone()[0]
    exits = ["fixture"] if params.fetch_mode == "fixture" else [label for label, _ in load_exits(os.environ)]
    return {"extraction_id": eid, "status_before": before, "pending": pending, "exits": exits}


def crawl_pages_step(params: CrawlParams, checked: dict[str, Any]) -> dict[str, Any]:
    """调用爬虫主循环；到达 limit 或 deadline 正常结束（截止时记 interrupted），已处理行逐个提交。"""
    s = settings_for(params)
    live = params.fetch_mode == "live"
    pool = ExitPool(load_exits(os.environ) if live else [("fixture", None)],
                    rate=params.rate if live else 1000, burst=3 if live else 1000)
    cfg = CrawlConfig(snapshot_dir=output_root(s.output_dir) / "snapshots", concurrency=params.concurrency,
                      max_attempts=1, timeout=params.timeout, limit=params.limit,
                      deadline_minutes=params.deadline_minutes, retry_statuses=params.retry_statuses,
                      shuffle=True, jitter=1.0 if live else 0.0)

    async def run():
        with connect(s) as conn:
            if not live:
                return await crawl(conn, checked["extraction_id"], cfg, pool, fetch=fixture_fetch)
            fetcher = SessionFetcher()
            try:
                return await crawl(conn, checked["extraction_id"], cfg, pool, fetch=fetcher)
            finally:
                await fetcher.aclose()

    started = time.monotonic()
    summary = asyncio.run(run())
    return {**dataclasses.asdict(summary), "elapsed_seconds": round(time.monotonic() - started, 2),
            "exits": [e.label for e in pool.exits]}


def verify_crawl_step(params: CrawlParams, extraction_id: str, crawled: dict[str, Any]) -> dict[str, Any]:
    """R23、R32、R33：全表计数一致，本次运行的快照全部存在且哈希一致，处理数与日志一致。"""
    s = settings_for(params)
    with connect(s) as conn:
        out = run_checks(conn, extraction_id, run_id=crawled["run_id"])
    found = problems(out)
    if sum(out["page_template"].values()) != crawled["asin_done"]:
        found.append(f"本次最后尝试 {sum(out['page_template'].values())} 行 ≠ 处理数 {crawled['asin_done']}")
    if found:
        raise CrawlVerificationError("；".join(found))
    return out


def report_dir(output_dir: Path, extraction_id: str | None, run_key: str) -> Path:
    """outputs/task2/<extraction_id>/crawl/<run_key>/；抽取未知时落在 _failures。"""
    return output_root(output_dir) / extraction_id / "crawl" / run_key if extraction_id \
        else failures_dir(output_dir, run_key)


def finalize_step(params: CrawlParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 summary.json 与 report.md。写出报告本身不代表爬取成功。
    参数不合法时也要留下报告，所以这里只读配置、不校验参数。"""
    s = load_settings(dict(os.environ))
    checked, crawled, verified = (upstream.get(n) for n in STEP_NAMES)
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    failures = _read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "fetch_mode": params.fetch_mode,
        "params": {k: v for k, v in dataclasses.asdict(params).items() if k not in ("run_key", "try_number")},
        "tasks": tasks, "failures": failures,
        "status_before": (checked or {}).get("status_before"), "pending_before": (checked or {}).get("pending"),
        "crawl_run": crawled,
    }
    if verified:
        summary.update(status_after=verified["status"], manifest_rows=verified["manifest_rows"],
                       page_template=verified["page_template"], redirected=verified["redirected"],
                       snapshots_checked=verified["snapshots_checked"])
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    # 检查未通过时抽取未必存在，报告与失败记录一起放在 _failures
    directory = report_dir(s.output_dir, (checked or {}).get("extraction_id"), params.run_key)
    reporting.write_atomic(directory / "summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    reporting.write_atomic(directory / "report.md", render_markdown(summary).encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"],
            "report_dir": str(directory), "extraction_id": eid}


def render_markdown(s: dict[str, Any]) -> str:
    fmt = lambda n: f"{n:,}" if isinstance(n, int) else str(n)  # noqa: E731
    lines = ["# 任务 2 产品页面爬取报告", "",
             f"- 最终状态：**{s['final_status']}**（交付完整：{s['delivery_complete']}）",
             f"- DAG / run：`{s['dag_id']}` / `{s['run_key']}`",
             f"- 抽取：`{s['extraction_id']}`，请求方式 {s['fetch_mode']}",
             f"- 参数：{json.dumps(s['params'], ensure_ascii=False)}", "", "## 任务状态", ""]
    lines += [f"- {k}：{v}" for k, v in s["tasks"].items()]
    run = s.get("crawl_run")
    if run:
        lines += ["", "## 本次运行", "",
                  f"- crawl_run：`{run['run_id']}`，状态 {run['status']}，处理 {fmt(run['asin_done'])} 个 ASIN，"
                  f"用时 {run['elapsed_seconds']} 秒", f"- 出口：{', '.join(run['exits'])}",
                  "", "| 判定 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in sorted(run["outcomes"].items())]
    if "status_after" in s:
        lines += ["", "## 页面模板与跳转（本次）", "", "| 模板 | 数量 |", "|---|---|"]
        lines += [f"| {k} | {fmt(v)} |" for k, v in s["page_template"].items()]
        lines += ["", f"- 发生跳转：{fmt(s['redirected'])} 个；核对快照 {fmt(s['snapshots_checked'])} 个",
                  "", "## 清单状态分布", "", "| 状态 | 运行前 | 运行后 |", "|---|---|---|"]
        before = s.get("status_before") or {}
        for k in sorted(set(before) | set(s["status_after"])):
            lines.append(f"| {k} | {fmt(before.get(k, 0))} | {fmt(s['status_after'].get(k, 0))} |")
        lines.append(f"| 合计 | {fmt(sum(before.values()))} | {fmt(s['manifest_rows'])} |")
    if s["failures"]:
        lines += ["", "## 失败记录", ""]
        lines += [f"- {f.get('task')} 第 {f.get('try_number')} 次：{f.get('error_type')}："
                  f"{str(f.get('message'))[:300]}（暂时错误：{f.get('transient')}）" for f in s["failures"]]
    return "\n".join(lines) + "\n"
