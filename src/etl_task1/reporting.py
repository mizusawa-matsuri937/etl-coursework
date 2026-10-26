"""运行报告：summary.json、report.md 与导出文件。写入先落临时文件再原子替换。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from .db import sql_dir

REPORT_VERSION = 1
AI_ASSISTANCE = (
    "本项目的计划、代码与文档由 Claude（Claude Code）辅助编写。AI 输出不作为数据事实来源，"
    "正确性依据是固定样本的手写预期、自动化测试、SQL 核对和可回放的原始证据；"
    "本报告不代表用户已逐行人工审阅。"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_atomic(path: Path, data: bytes) -> None:
    """先写同目录临时文件并落盘，再原子替换，避免半份文件被当作结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def file_digest(path: Path) -> dict[str, object]:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return {"path": path.name, "bytes": path.stat().st_size, "sha256": h.hexdigest()}


def run_dir(output_dir: Path, extraction_id: str | None, run_key: str) -> Path:
    """outputs/task1/<extraction_id>/<run_id>/；抽取未知时落在 _failures。"""
    return output_dir / "task1" / (extraction_id or "_failures") / run_key


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return output_dir / "task1" / "_failures" / run_key


def export_csv(conn: psycopg.Connection, sql_file: str, extraction_id: str, dest: Path) -> dict[str, object]:
    """把查询结果流式写成 CSV（含表头），不把全部行读入内存。"""
    sql = (sql_dir() / sql_file).read_text(encoding="utf-8").rstrip().rstrip(";")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    with open(tmp, "wb") as fh, conn.cursor() as cur:
        with cur.copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)", {"eid": extraction_id}) as copy:
            for chunk in copy:
                fh.write(bytes(chunk))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    return file_digest(dest)


def resources_snapshot(conn: psycopg.Connection | None, output_dir: Path) -> dict[str, object]:
    snap: dict[str, object] = {"kind": "measured_at_report_time", "taken_at": utc_now()}
    try:
        usage = shutil.disk_usage(output_dir if output_dir.exists() else output_dir.parent)
        snap["output_disk_free_bytes"] = usage.free
    except OSError:
        snap["output_disk_free_bytes"] = None
    if conn is not None:
        try:
            snap["database_bytes"] = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        except psycopg.Error:
            snap["database_bytes"] = None
    return snap


def _fmt(n: Any) -> str:
    return f"{n:,}" if isinstance(n, int) and not isinstance(n, bool) else str(n)


def render_markdown(s: dict[str, Any]) -> str:
    L: list[str] = []
    a = L.append
    a(f"# 任务 1 运行报告")
    a("")
    a(f"- 最终状态：**{s['final_status']}**；交付完整：**{'是' if s['delivery_complete'] else '否'}**")
    a(f"- 范围：`{s['scope']}`；DAG：`{s['dag_id']}`；运行：`{s['run_key']}`；抽取：`{s.get('extraction_id')}`")
    a(f"- 生成时间：{s['generated_at']}")
    if s["scope"] == "sample":
        a("- **这是样本运行，数量不得与全量官方基准直接比较。**")
    a("")
    a("## 任务状态")
    a("")
    a("| 任务 | 状态 |")
    a("|---|---|")
    for name, st in s["tasks"].items():
        a(f"| {name} | {st} |")
    if s["failures"]:
        a("")
        a("## 失败记录")
        a("")
        a("| 任务 | 第几次 | 类别 | 可重试 | 错误 |")
        a("|---|---|---|---|---|")
        for f in s["failures"]:
            msg = str(f.get("message", "")).replace("|", "\\|").replace("\n", " ")[:300]
            a(f"| {f.get('task')} | {f.get('try_number')} | {f.get('error_type')} | "
              f"{'是' if f.get('transient') else '否'} | {msg} |")
    ex = s.get("extraction")
    if ex:
        a("")
        a("## 抽取状态")
        a("")
        a(f"- 状态：{ex['status']}；scope_complete={ex['scope_complete']}；"
          f"source_eof_reached={ex['source_eof_reached']}；gzip_verified={ex['gzip_verified']}")
        a(f"- 已提交批次：{ex['batches_committed']}；最后已提交记录序号：{ex['last_record_no']}")
        if ex.get("failure_reason"):
            a(f"- 失败原因：{ex['failure_reason']}")
    elif s.get("database_available") is False:
        a("")
        a("## 抽取状态")
        a("")
        a("- 数据库不可用，无法读取抽取状态；见失败记录与 Airflow 日志。")
    src = s.get("source")
    if src:
        a("")
        a("## 输入文件")
        a("")
        a(f"- 来源：{src['source_url']}；下载完成时间：{src['download_completed_at'] or '未知'}；本次校验：{src['verified_at']}")
        a(f"- 大小：记录 {_fmt(src['recorded_size_bytes'])} / 实测 {_fmt(src['measured_size_bytes'])}")
        a(f"- SHA-256：`{src['measured_sha256']}`（本地实测，非官方发布值：{not src['sha256_is_official']}）")
    c = s.get("counts")
    if c:
        a("")
        a("## 数量")
        a("")
        a("| 指标 | 数值 |")
        a("|---|---|")
        for k, v in c.items():
            a(f"| {k} | {_fmt(v)} |")
        a("")
        a("读取 = 有效 + 隔离；问题行数与有问题的记录数是不同口径。observed 含隔离记录中可明确识别的 ID，valid 只含有效记录。")
    if s.get("invariants"):
        a("")
        a("## 不变量")
        a("")
        a("| 检查 | 结果 | 说明 |")
        a("|---|---|---|")
        for i in s["invariants"]:
            a(f"| {i['name']} | {'通过' if i['ok'] else '失败'} | {i['detail']} |")
    b = s.get("baseline")
    if b:
        a("")
        a("## 与官方基准对照")
        a("")
        a(f"基准：{b.get('source')}。状态：**{b['status']}**。{b.get('note', '')}")
        if b["rows"]:
            a("")
            a("| 指标 | 官方 | 实测 | 差值 |")
            a("|---|---|---|---|")
            for r in b["rows"]:
                a(f"| {r['metric']} | {_fmt(r['official']) if r['official'] is not None else '—'} | {_fmt(r['measured'])} | {r['difference']:+,} |")
        if b.get("explanation"):
            a("")
            a(f"差异说明：{b['explanation']}")
    q = s.get("quality")
    if q:
        a("")
        a("## 质量问题分布")
        a("")
        a("| 问题码 | 严重程度 | 问题行数 | 涉及记录数 |")
        a("|---|---|---|---|")
        for r in q:
            a(f"| {r['code']} | {r['severity']} | {_fmt(r['issue_rows'])} | {_fmt(r['records'])} |")
    if s.get("exports"):
        a("")
        a("## 导出文件")
        a("")
        for e in s["exports"]:
            a(f"- `{e['path']}`：{_fmt(e['bytes'])} 字节，SHA-256 `{e['sha256']}`")
    if s.get("resources"):
        a("")
        a("## 资源")
        a("")
        a("下列为本次运行记录的实测值；资源预算与全量估算见 design 与 AC12 证据。")
        a("")
        a("```json")
        a(json.dumps(s["resources"], ensure_ascii=False, indent=2, default=str))
        a("```")
    if s.get("program"):
        p = s["program"]
        a("")
        a("## 程序版本")
        a("")
        a(f"- 包 {p['package_version']}；解析 {p['parser_version']}；规则 {p['rules_version']}；"
          f"源码树 `{p['source_tree_sha256']}`；Git 提交 {p['git_commit'] or '未提供'}")
    a("")
    a("## AI 辅助说明")
    a("")
    a(s["ai_assistance"])
    a("")
    return "\n".join(L)


def write_report(directory: Path, summary: dict[str, Any]) -> list[dict[str, object]]:
    """写 summary.json 与 report.md（原子替换）；返回两个文件的摘要。"""
    write_atomic(directory / "report.md", render_markdown(summary).encode())
    write_atomic(directory / "summary.json",
                 (json.dumps(summary, ensure_ascii=False, indent=2, default=str, sort_keys=True) + "\n").encode())
    return [file_digest(directory / "summary.json"), file_digest(directory / "report.md")]
