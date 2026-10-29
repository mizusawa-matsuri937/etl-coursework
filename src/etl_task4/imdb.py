"""IMDb 固定快照的校验、流式过滤装载与核对（spec R04–R06、R38，AC02）。

数据流见 design“IMDb 参考数据”。要点：
- 装载前逐个计算文件的 SHA-256 与大小，与下载记录 download.json 比对；不一致就确定性失败，库里什么都不写。
- 先读 title.basics 得到保留作品的 tconst 集合（整数，约 150 万个），再依次读 akas、crew、principals 与 name.basics，
  边解压边过滤、分批 COPY，每行保留文件名行号；`\\N` 存为 NULL。
- 全部表在一个事务里写入，失败整体回滚；已装载成功的快照再次运行只返回 reused，不新增、不修改。
- 同一快照同时只允许一个装载（会话级咨询锁）。

命令行（容器内，工作目录 /opt/etl）：
    python -m etl_task4.imdb check --snapshot-id imdb_2026-10-30
    python -m etl_task4.imdb load --snapshot-id imdb_2026-10-30
    python -m etl_task4.imdb verify --snapshot-id imdb_2026-10-30
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect, sql_dir

from . import IMDB_LOADER_VERSION

log = logging.getLogger(__name__)

SCHEMA_VERSION = "301"
DEFAULT_ROOT = "/data/imdb"
DOWNLOAD_RECORD = "download.json"
SNAPSHOT_RE = re.compile(r"^imdb_[0-9A-Za-z._-]+$")

KEEP_TYPES = ("movie", "tvMovie", "video")                                # R05：电影候选
OTHER_TYPES = ("tvSeries", "tvMiniSeries", "tvSpecial", "short", "tvShort")  # R15：只留标题，不作候选
PRINCIPAL_CATEGORIES = ("actor", "actress", "director")

# 文件名 → 表头（装载前核对，列变了就拒绝）
HEADERS: dict[str, tuple[str, ...]] = {
    "title.basics.tsv.gz": ("tconst", "titleType", "primaryTitle", "originalTitle", "isAdult", "startYear",
                            "endYear", "runtimeMinutes", "genres"),
    "title.akas.tsv.gz": ("titleId", "ordering", "title", "region", "language", "types", "attributes",
                          "isOriginalTitle"),
    "title.crew.tsv.gz": ("tconst", "directors", "writers"),
    "title.principals.tsv.gz": ("tconst", "ordering", "nconst", "category", "job", "characters"),
    "name.basics.tsv.gz": ("nconst", "primaryName", "birthYear", "deathYear", "primaryProfession",
                           "knownForTitles"),
}
FILE_NAMES = tuple(HEADERS)
COPY_BATCH_LOG = 1_000_000
DANGLING_TOLERANCE = 0.001   # 引用了 name.basics 里没有的人的行，占比不超过 0.1% 视为源数据的瑕疵


class ImdbInputError(RuntimeError):
    """IMDb 文件缺失、哈希或大小不符、表头或行格式不对（确定性错误）。"""


class ImdbBusy(RuntimeError):
    """同一快照已有装载持有锁（暂时错误，可重试）。"""


@dataclass(frozen=True)
class FileRecord:
    name: str
    url: str
    size_bytes: int
    sha256: str
    last_modified: str | None = None


@dataclass(frozen=True)
class DownloadRecord:
    snapshot_id: str
    base_url: str
    downloaded_at: str
    files: dict[str, FileRecord]


@dataclass
class LoadResult:
    snapshot_id: str
    status: str                                  # loaded | reused
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------- 下载记录与文件校验

def snapshot_dir(snapshot_id: str, root: str | Path | None = None) -> Path:
    """imdb_2026-10-30 → <root>/2026-10-30；root 默认取环境变量 IMDB_DATA_DIR，再默认 /data/imdb。"""
    if not SNAPSHOT_RE.match(snapshot_id):
        raise ImdbInputError(f"快照编号 {snapshot_id!r} 不合法（形如 imdb_2026-10-30）")
    base = Path(root if root is not None else os.environ.get("IMDB_DATA_DIR", DEFAULT_ROOT))
    return base / snapshot_id.removeprefix("imdb_")


def read_download_record(directory: Path) -> DownloadRecord:
    path = Path(directory) / DOWNLOAD_RECORD
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ImdbInputError(f"缺少下载记录 {path}") from None
    except ValueError as exc:
        raise ImdbInputError(f"下载记录 {path} 不是合法 JSON：{exc}") from None
    try:
        files = {f["name"]: FileRecord(f["name"], f["url"], int(f["size_bytes"]), f["sha256"], f.get("last_modified"))
                 for f in raw["files"]}
        record = DownloadRecord(raw["snapshot_id"], raw["base_url"], raw["downloaded_at"], files)
    except (KeyError, TypeError, ValueError) as exc:
        raise ImdbInputError(f"下载记录 {path} 缺少必需字段：{exc!r}") from None
    if not SNAPSHOT_RE.match(record.snapshot_id):
        raise ImdbInputError(f"下载记录里的快照编号 {record.snapshot_id!r} 不合法")
    missing = [n for n in FILE_NAMES if n not in record.files]
    if missing:
        raise ImdbInputError(f"下载记录缺少文件：{missing}")
    return record


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def check_files(directory: Path) -> DownloadRecord:
    """R04：逐个文件核对大小与 SHA-256；任何不符都抛 ImdbInputError，此时还没有写库。"""
    directory = Path(directory)
    record = read_download_record(directory)
    for name in FILE_NAMES:
        want, path = record.files[name], directory / name
        if not path.is_file():
            raise ImdbInputError(f"缺少文件 {path}")
        if path.stat().st_size != want.size_bytes:
            raise ImdbInputError(f"{name} 大小 {path.stat().st_size} ≠ 下载记录 {want.size_bytes}")
        actual = _sha256(path)
        if actual != want.sha256:
            raise ImdbInputError(f"{name} SHA-256 {actual} ≠ 下载记录 {want.sha256}")
    return record


# ---------------------------------------------------------------- 表结构

def apply_imdb_schema(conn: psycopg.Connection) -> bool:
    """应用 imdb 表结构；已应用时不做任何事。返回是否本次新建。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task4_imdb_schema'))")
        have = {r[0] for r in conn.execute("SELECT version FROM public.schema_migration").fetchall()}
        if SCHEMA_VERSION in have:
            return False
        conn.execute((sql_dir() / "301_imdb_schema.sql").read_text(encoding="utf-8"))
    return True


# ---------------------------------------------------------------- 流式读取

def iter_rows(path: Path, name: str) -> Iterator[tuple[int, list[str | None]]]:
    """逐行读取 .tsv.gz，产出 (行号, 各列)；行号从 1 起，表头是第 1 行，数据从第 2 行起；`\\N` → None。"""
    header = HEADERS[name]
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as fh:
        first = fh.readline().rstrip("\n").split("\t")
        if tuple(first) != header:
            raise ImdbInputError(f"{name} 表头 {first} ≠ 预期 {list(header)}")
        for line_no, line in enumerate(fh, start=2):
            cols = line.rstrip("\n").split("\t")
            if len(cols) != len(header):
                raise ImdbInputError(f"{name} 第 {line_no} 行有 {len(cols)} 列，应为 {len(header)} 列")
            yield line_no, [None if c == "\\N" else c for c in cols]


def _num(token: str, prefix: str, name: str, line_no: int) -> int:
    """tt0000001 → 1；用整数放进内存集合。"""
    if not token.startswith(prefix) or not token[len(prefix):].isdigit():
        raise ImdbInputError(f"{name} 第 {line_no} 行的标识 {token!r} 不是 {prefix}+数字")
    return int(token[len(prefix):])


def _int(value: str | None, name: str, line_no: int, column: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise ImdbInputError(f"{name} 第 {line_no} 行 {column}={value!r} 不是整数") from None


def _bool(value: str | None, name: str, line_no: int, column: str) -> bool | None:
    if value is None:
        return None
    if value not in ("0", "1"):
        raise ImdbInputError(f"{name} 第 {line_no} 行 {column}={value!r} 不是 0/1")
    return value == "1"


@dataclass
class _Stat:
    read: int = 0
    kept: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1


@contextmanager
def _copy(conn: psycopg.Connection, table: str, columns: tuple[str, ...]):
    """一次只开一个 COPY（同一连接上不能交错）。"""
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        yield cp


def _load_basics(conn, directory: Path, sid: str, titles: set[int]) -> _Stat:
    """第一遍保留电影候选并统计读取与过滤；第二遍把 R15 的其他类型标题写入 title_other（不重复计数）。"""
    name, st = "title.basics.tsv.gz", _Stat()
    tcols = ("snapshot_id", "tconst", "title_type", "primary_title", "original_title", "is_adult", "start_year",
             "end_year", "runtime_minutes", "genres", "line_no")
    with _copy(conn, "imdb.title", tcols) as cp:
        for line_no, c in iter_rows(directory / name, name):
            st.read += 1
            tconst, ttype = c[0], c[1]
            num = _num(tconst, "tt", name, line_no)
            if ttype in KEEP_TYPES:
                genres = c[8].split(",") if c[8] else None
                cp.write_row((sid, tconst, ttype, c[2], c[3], _bool(c[4], name, line_no, "isAdult"),
                              _int(c[5], name, line_no, "startYear"), _int(c[6], name, line_no, "endYear"),
                              _int(c[7], name, line_no, "runtimeMinutes"), genres, line_no))
                titles.add(num)
                st.kept += 1
            elif ttype in OTHER_TYPES:
                st.skip("to_title_other")
            else:
                st.skip(f"title_type:{ttype}")
    ocols = ("snapshot_id", "tconst", "title_type", "primary_title", "original_title", "start_year", "line_no")
    with _copy(conn, "imdb.title_other", ocols) as cp:
        for line_no, c in iter_rows(directory / name, name):
            if c[1] in OTHER_TYPES:
                cp.write_row((sid, c[0], c[1], c[2], c[3], _int(c[5], name, line_no, "startYear"), line_no))
    return st


def _load_akas(conn, directory: Path, sid: str, titles: set[int]) -> _Stat:
    name, st = "title.akas.tsv.gz", _Stat()
    cols = ("snapshot_id", "tconst", "ordering", "title", "region", "language", "types", "attributes",
            "is_original", "line_no")
    with _copy(conn, "imdb.title_aka", cols) as cp:
        for line_no, c in iter_rows(directory / name, name):
            st.read += 1
            if _num(c[0], "tt", name, line_no) not in titles:
                st.skip("not_in_titles")
                continue
            cp.write_row((sid, c[0], _int(c[1], name, line_no, "ordering"), c[2], c[3], c[4], c[5], c[6],
                          _bool(c[7], name, line_no, "isOriginalTitle"), line_no))
            st.kept += 1
    return st


def _load_crew(conn, directory: Path, sid: str, titles: set[int], names: set[int]) -> _Stat:
    name, st = "title.crew.tsv.gz", _Stat()
    with _copy(conn, "imdb.title_director", ("snapshot_id", "tconst", "ordinal", "nconst", "line_no")) as cp:
        for line_no, c in iter_rows(directory / name, name):
            st.read += 1
            if _num(c[0], "tt", name, line_no) not in titles:
                st.skip("not_in_titles")
                continue
            if c[1] is None:
                st.skip("no_directors")
                continue
            for ordinal, nconst in enumerate(c[1].split(","), start=1):
                names.add(_num(nconst, "nm", name, line_no))
                cp.write_row((sid, c[0], ordinal, nconst, line_no))
                st.kept += 1
    return st


def _load_principals(conn, directory: Path, sid: str, titles: set[int], names: set[int]) -> _Stat:
    name, st = "title.principals.tsv.gz", _Stat()
    cols = ("snapshot_id", "tconst", "ordering", "nconst", "category", "job", "characters", "line_no")
    with _copy(conn, "imdb.title_principal", cols) as cp:
        for line_no, c in iter_rows(directory / name, name):
            st.read += 1
            if _num(c[0], "tt", name, line_no) not in titles:
                st.skip("not_in_titles")
                continue
            if c[3] not in PRINCIPAL_CATEGORIES:
                st.skip(f"category:{c[3]}")
                continue
            names.add(_num(c[2], "nm", name, line_no))
            cp.write_row((sid, c[0], _int(c[1], name, line_no, "ordering"), c[2], c[3], c[4], c[5], line_no))
            st.kept += 1
    return st


def _load_names(conn, directory: Path, sid: str, names: set[int]) -> _Stat:
    name, st = "name.basics.tsv.gz", _Stat()
    cols = ("snapshot_id", "nconst", "primary_name", "birth_year", "death_year", "primary_profession", "line_no")
    with _copy(conn, "imdb.name", cols) as cp:
        for line_no, c in iter_rows(directory / name, name):
            st.read += 1
            if _num(c[0], "nm", name, line_no) not in names:
                st.skip("not_referenced")
                continue
            cp.write_row((sid, c[0], c[1], _int(c[2], name, line_no, "birthYear"),
                          _int(c[3], name, line_no, "deathYear"), c[4], line_no))
            st.kept += 1
    return st


# ---------------------------------------------------------------- 装载

def _lock_key(snapshot_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"etl_task4_imdb:{snapshot_id}".encode()).digest()[:8], "big", signed=True)


def _stored_hashes(conn: psycopg.Connection, sid: str) -> dict[str, str]:
    return dict(conn.execute("SELECT file_name, sha256 FROM imdb.source_file WHERE snapshot_id = %s",
                             (sid,)).fetchall())


def load_snapshot(conn: psycopg.Connection, directory: Path, *, snapshot_id: str | None = None,
                  before_commit: Callable[[], None] | None = None) -> LoadResult:
    """装载一个快照。已装载成功且文件哈希一致时返回 reused；哈希不符、表头或行格式不对时抛 ImdbInputError。
    before_commit 只给测试注入故障：在事务提交前调用，抛异常即整体回滚。"""
    if not conn.autocommit:
        raise ValueError("装载连接必须是自动提交模式（使用 etl_task1.db.connect）")
    directory = Path(directory)
    started = time.monotonic()
    record = check_files(directory)                 # R04：先核对，不符则什么都没写
    sid = snapshot_id or record.snapshot_id
    if sid != record.snapshot_id:
        raise ImdbInputError(f"快照编号 {sid} 与下载记录里的 {record.snapshot_id} 不一致")
    key = _lock_key(sid)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        raise ImdbBusy(f"IMDb 快照 {sid} 正由另一个运行装载")
    try:
        row = conn.execute("SELECT status FROM imdb.snapshot WHERE snapshot_id = %s", (sid,)).fetchone()
        if row and row[0] == "loaded":              # R38：同一快照重复装载只复用
            stored = _stored_hashes(conn, sid)
            wanted = {n: record.files[n].sha256 for n in FILE_NAMES}
            if stored != wanted:
                raise ImdbInputError(f"快照 {sid} 已装载，但文件哈希与库中记录不同；换内容必须用新的快照编号")
            return LoadResult(sid, "reused", _file_stats(conn, sid), round(time.monotonic() - started, 2))
        with conn.transaction():                    # 登记“装载中”；上次失败遗留的快照行在这里清掉（数据已随事务回滚）
            conn.execute("DELETE FROM imdb.snapshot WHERE snapshot_id = %s", (sid,))
            conn.execute("INSERT INTO imdb.snapshot (snapshot_id, base_url, downloaded_at, loader_version) "
                         "VALUES (%s, %s, %s, %s)",
                         (sid, record.base_url, datetime.fromisoformat(record.downloaded_at), IMDB_LOADER_VERSION))
        try:
            with conn.transaction():
                stats = _load_all(conn, directory, sid)
                for name in FILE_NAMES:
                    f, st = record.files[name], stats[name]
                    conn.execute(
                        "INSERT INTO imdb.source_file (snapshot_id, file_name, url, size_bytes, sha256, last_modified, "
                        "rows_read, rows_kept, filter_counts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (sid, name, f.url, f.size_bytes, f.sha256, f.last_modified, st.read, st.kept,
                         json.dumps(st.counts, sort_keys=True)))
                conn.execute("UPDATE imdb.snapshot SET status = 'loaded', loaded_at = clock_timestamp() "
                             "WHERE snapshot_id = %s", (sid,))
                if before_commit:
                    before_commit()
        except BaseException as exc:
            conn.execute("UPDATE imdb.snapshot SET status = 'failed', failure_reason = %s WHERE snapshot_id = %s",
                         (f"{type(exc).__name__}: {exc}"[:2000], sid))
            raise
        return LoadResult(sid, "loaded", _file_stats(conn, sid), round(time.monotonic() - started, 2))
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except psycopg.Error:
            pass


def _load_all(conn: psycopg.Connection, directory: Path, sid: str) -> dict[str, _Stat]:
    titles: set[int] = set()
    names: set[int] = set()
    stats: dict[str, _Stat] = {}
    stats["title.basics.tsv.gz"] = _load_basics(conn, directory, sid, titles)
    log.info("title.basics：保留 %d 部作品", len(titles))
    stats["title.akas.tsv.gz"] = _load_akas(conn, directory, sid, titles)
    stats["title.crew.tsv.gz"] = _load_crew(conn, directory, sid, titles, names)
    stats["title.principals.tsv.gz"] = _load_principals(conn, directory, sid, titles, names)
    log.info("导演与主要演职员引用 %d 个人", len(names))
    stats["name.basics.tsv.gz"] = _load_names(conn, directory, sid, names)
    return stats


def _file_stats(conn: psycopg.Connection, sid: str) -> dict[str, dict[str, Any]]:
    return {r[0]: {"rows_read": r[1], "rows_kept": r[2], "filter_counts": r[3], "sha256": r[4]}
            for r in conn.execute("SELECT file_name, rows_read, rows_kept, filter_counts, sha256 FROM imdb.source_file "
                                  "WHERE snapshot_id = %s ORDER BY file_name", (sid,)).fetchall()}


# ---------------------------------------------------------------- 核对

TABLE_FOR_FILE = {"title.basics.tsv.gz": "imdb.title", "title.akas.tsv.gz": "imdb.title_aka",
                  "title.crew.tsv.gz": "imdb.title_director", "title.principals.tsv.gz": "imdb.title_principal",
                  "name.basics.tsv.gz": "imdb.name"}


def verify_snapshot(conn: psycopg.Connection, sid: str) -> dict[str, Any]:
    """只读核对：每个文件的保留行数等于表中行数；别名、导演、演员都指向已装载的作品，导演与演员都有人名。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute("SELECT status FROM imdb.snapshot WHERE snapshot_id = %s", (sid,)).fetchone()
        out: dict[str, Any] = {"snapshot_id": sid, "status": row[0] if row else None}
        files = _file_stats(conn, sid)
        out["files"] = {}
        for name, table in TABLE_FOR_FILE.items():
            actual = conn.execute(f"SELECT count(*) FROM {table} WHERE snapshot_id = %s", (sid,)).fetchone()[0]
            kept = files.get(name, {}).get("rows_kept")
            out["files"][name] = {"rows_read": files.get(name, {}).get("rows_read"), "rows_kept": kept,
                                  "table_rows": actual}
        out["title_other_rows"] = conn.execute("SELECT count(*) FROM imdb.title_other WHERE snapshot_id = %s",
                                               (sid,)).fetchone()[0]
        out["title_other_expected"] = files.get("title.basics.tsv.gz", {}).get("filter_counts", {}).get(
            "to_title_other", 0)
        out["title_overlap_other"] = conn.execute(
            "SELECT count(*) FROM imdb.title t JOIN imdb.title_other o USING (snapshot_id, tconst) "
            "WHERE t.snapshot_id = %s", (sid,)).fetchone()[0]
        out["dangling_director_names"] = conn.execute(
            "SELECT count(*) FROM imdb.title_director d WHERE d.snapshot_id = %s AND NOT EXISTS "
            "(SELECT 1 FROM imdb.name n WHERE n.snapshot_id = d.snapshot_id AND n.nconst = d.nconst)",
            (sid,)).fetchone()[0]
        out["dangling_principal_names"] = conn.execute(
            "SELECT count(*) FROM imdb.title_principal p WHERE p.snapshot_id = %s AND NOT EXISTS "
            "(SELECT 1 FROM imdb.name n WHERE n.snapshot_id = p.snapshot_id AND n.nconst = p.nconst)",
            (sid,)).fetchone()[0]
        out["types"] = dict(conn.execute("SELECT title_type, count(*) FROM imdb.title WHERE snapshot_id = %s "
                                         "GROUP BY 1 ORDER BY 1", (sid,)).fetchall())
    return out


def problems(out: dict[str, Any]) -> list[str]:
    found = []
    if out["status"] != "loaded":
        found.append(f"快照状态为 {out['status']}，不是 loaded")
    for name, f in out["files"].items():
        if f["rows_kept"] is None:
            found.append(f"{name} 没有装载记录")
        elif f["rows_kept"] != f["table_rows"]:
            found.append(f"{name} 保留 {f['rows_kept']} 行，表中 {f['table_rows']} 行")
    if out["title_other_rows"] != out["title_other_expected"]:
        found.append(f"title_other 有 {out['title_other_rows']} 行，应为 {out['title_other_expected']} 行")
    if out["title_overlap_other"]:
        found.append(f"title_overlap_other = {out['title_overlap_other']}")
    # 真实文件里有少量引用了 name.basics 没有的 nconst（2026-10-29 快照：导演 86 行、主要演职员 618 行）：
    # 不删行，按占比判断；超过 DANGLING_TOLERANCE 才算问题，否则记为警告（warnings）。
    for k, name in (("dangling_director_names", "title.crew.tsv.gz"), ("dangling_principal_names", "title.principals.tsv.gz")):
        rows = out["files"].get(name, {}).get("table_rows") or 0
        if out[k] and (rows == 0 or out[k] / rows > DANGLING_TOLERANCE):
            found.append(f"{k} = {out[k]}（占 {out[k] / rows:.4%}，超过容忍的 {DANGLING_TOLERANCE:.2%}）" if rows else f"{k} = {out[k]}")
    return found


def warnings(out: dict[str, Any]) -> list[str]:
    return [f"{k} = {out[k]}：引用了 name.basics 里没有的人，不删行，比较时跳过（占比低于 {DANGLING_TOLERANCE:.2%}）"
            for k in ("dangling_director_names", "dangling_principal_names") if out[k]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 4 IMDb 快照校验、装载与核对")
    ap.add_argument("cmd", choices=("check", "load", "verify"))
    ap.add_argument("--snapshot-id", required=True)
    ap.add_argument("--root", help="快照所在的父目录；默认取 IMDB_DATA_DIR 或 /data/imdb")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = snapshot_dir(args.snapshot_id, args.root)
    if args.cmd == "check":
        record = check_files(directory)
        print(json.dumps({"snapshot_id": record.snapshot_id, "files": {n: f.sha256 for n, f in record.files.items()}},
                         indent=2))
        return 0
    with connect(load_settings()) as conn:
        apply_imdb_schema(conn)
        if args.cmd == "load":
            r = load_snapshot(conn, directory, snapshot_id=args.snapshot_id)
            print(json.dumps(r.__dict__, ensure_ascii=False, indent=2, default=str))
            return 0
        out = verify_snapshot(conn, args.snapshot_id)
        out["problems"] = problems(out)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 1 if out["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
