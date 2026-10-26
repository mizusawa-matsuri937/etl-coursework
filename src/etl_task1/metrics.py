"""运行资源记录与磁盘保护。

每个已提交批次后采样一次：进程 RSS、累计记录/字节、速率、数据库大小和磁盘剩余。样本逐行追加到
`resources.jsonl`，汇总（峰值与平均速率）写入 `summary()`，供报告引用。磁盘剩余低于保留阈值时，
在已提交批次之后抛出 `LowDiskSpace`，检查点保留，空间恢复后可从该批之后续跑。
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import time
from pathlib import Path

import psycopg

from .reporting import utc_now

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
DB_SIZE_EVERY = 10  # pg_database_size 每隔若干批取一次，减少额外查询


class LowDiskSpace(RuntimeError):
    """磁盘剩余低于保留阈值：已提交批次保留，装载安全停止。"""


def current_rss_bytes() -> int | None:
    try:
        with open("/proc/self/statm", "rb") as fh:
            return int(fh.read().split()[1]) * PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return None


def peak_rss_bytes() -> int:
    """进程峰值 RSS：Linux 的 ru_maxrss（KiB）与当前 RSS 的较大者，二者统计时点略有差异。"""
    return max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024, current_rss_bytes() or 0)


def cgroup_peak_bytes() -> int | None:
    """容器 cgroup 的内存峰值（含同容器内其他进程），不可用时为 None。"""
    for name in ("memory.peak", "memory/memory.max_usage_in_bytes"):
        try:
            return int(Path("/sys/fs/cgroup", name).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def disk_free(paths: tuple[Path, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in paths:
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            out[str(p)] = shutil.disk_usage(probe).free
        except OSError:
            continue
    return out


def check_disk(paths: tuple[Path, ...], min_free_bytes: int) -> dict[str, int]:
    free = disk_free(paths)
    low = {p: f for p, f in free.items() if f < min_free_bytes}
    if low:
        detail = "，".join(f"{p} 剩余 {f:,} 字节" for p, f in low.items())
        raise LowDiskSpace(f"磁盘剩余低于保留阈值 {min_free_bytes:,} 字节：{detail}；已提交批次保留，空间恢复后可续跑")
    return free


class ResourceMonitor:
    def __init__(self, path: Path | None, disk_paths: tuple[Path, ...], min_free_bytes: int,
                 *, records_before: int = 0):
        self.path = path
        self.disk_paths = disk_paths
        self.min_free_bytes = min_free_bytes
        self.records_before = records_before  # 续跑时检查点已有的记录数
        self.started = time.monotonic()
        self.started_at = utc_now()
        self.batches = 0
        self.records = 0
        self.bytes = 0
        self.rss_peak_sampled = 0
        self.db_bytes_first: int | None = None
        self.db_bytes_last: int | None = None
        self.disk_free_first: dict[str, int] | None = None
        self.disk_free_last: dict[str, int] = {}
        self.disk_free_min: dict[str, int] = {}
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def start(self, conn: psycopg.Connection | None = None) -> None:
        """装载前先取基线并检查磁盘，空间不足时一条批次也不写。"""
        free = check_disk(self.disk_paths, self.min_free_bytes)
        self.disk_free_first = dict(free)
        self.disk_free_min = dict(free)
        self.db_bytes_first = self._db_bytes(conn)

    @staticmethod
    def _db_bytes(conn: psycopg.Connection | None) -> int | None:
        if conn is None:
            return None
        try:
            return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        except psycopg.Error:
            return None

    def on_batch(self, conn: psycopg.Connection | None, *, batch_no: int, last_record_no: int,
                 batch_records: int, batch_bytes: int) -> None:
        """批次已提交后调用。先落样本再检查磁盘，保证停止前的状态已记录。"""
        self.batches += 1
        self.records += batch_records
        self.bytes += batch_bytes
        elapsed = time.monotonic() - self.started
        rss = current_rss_bytes()
        self.rss_peak_sampled = max(self.rss_peak_sampled, rss or 0)
        if self.batches % DB_SIZE_EVERY == 1 or self.db_bytes_last is None:
            self.db_bytes_last = self._db_bytes(conn) or self.db_bytes_last
        free = disk_free(self.disk_paths)
        self.disk_free_last = free
        for p, f in free.items():
            self.disk_free_min[p] = min(f, self.disk_free_min.get(p, f))
        sample = {"at": utc_now(), "elapsed_s": round(elapsed, 2), "batch_no": batch_no,
                  "last_record_no": last_record_no, "records_this_run": self.records,
                  "bytes_this_run": self.bytes,
                  "records_per_s": round(self.records / elapsed, 1) if elapsed > 0 else None,
                  "rss_bytes": rss, "peak_rss_bytes": peak_rss_bytes(),
                  "db_bytes": self.db_bytes_last, "disk_free_bytes": free}
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
        low = {p: f for p, f in free.items() if f < self.min_free_bytes}
        if low:
            check_disk(self.disk_paths, self.min_free_bytes)

    def finish(self, conn: psycopg.Connection | None = None) -> None:
        self.db_bytes_last = self._db_bytes(conn) or self.db_bytes_last

    def summary(self) -> dict[str, object]:
        elapsed = time.monotonic() - self.started
        growth = (self.db_bytes_last - self.db_bytes_first
                  if self.db_bytes_first is not None and self.db_bytes_last is not None else None)
        return {
            "kind": "measured", "started_at": self.started_at, "elapsed_seconds": round(elapsed, 2),
            "batches": self.batches, "records_this_run": self.records, "bytes_this_run": self.bytes,
            "records_before_run": self.records_before,
            "records_per_second": round(self.records / elapsed, 1) if elapsed > 0 else None,
            "raw_bytes_per_second": round(self.bytes / elapsed) if elapsed > 0 else None,
            "peak_rss_bytes": peak_rss_bytes(), "peak_rss_sampled_bytes": self.rss_peak_sampled,
            "container_memory_peak_bytes": cgroup_peak_bytes(),
            "db_bytes_start": self.db_bytes_first, "db_bytes_end": self.db_bytes_last,
            "db_growth_bytes": growth,
            "db_bytes_per_record": round(growth / self.records, 1) if growth and self.records else None,
            "disk_free_start": self.disk_free_first, "disk_free_end": self.disk_free_last,
            "disk_free_min": self.disk_free_min, "min_free_bytes_threshold": self.min_free_bytes,
            "samples_file": str(self.path) if self.path else None,
        }


def estimate_full(summary: dict[str, object], total_records: int) -> dict[str, object] | None:
    """按设计公式由试跑实测外推全量。前缀样本不是随机样本，只是估算。"""
    n = summary.get("records_this_run") or 0
    sec = summary.get("elapsed_seconds") or 0
    growth = summary.get("db_growth_bytes")
    if not n or not sec:
        return None
    est: dict[str, object] = {
        "kind": "estimate", "basis_records": n, "target_records": total_records,
        "estimated_load_seconds": round(sec / n * total_records),
    }
    if growth:
        est["estimated_db_bytes"] = round(growth / n * total_records)
    return est
