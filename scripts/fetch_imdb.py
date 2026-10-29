#!/usr/bin/env python3
"""下载 IMDb Non-Commercial Datasets 的五个文件并写下载记录 download.json（任务 4，spec R04）。

在宿主机运行（容器内没有外网），**联网下载约 1–1.5 GB，运行前必须取得用户许可**，所以需要显式加 --confirm。
文件放仓库外：默认 ../imdb/<下载日期>/（与 ../movies.txt.gz 同级），不要提交到仓库。IMDb 数据限个人、非商业使用，不得再分发：
https://developer.imdb.com/non-commercial-datasets/

用法（项目根目录；本脚本只用标准库）：
    python3 scripts/fetch_imdb.py --confirm
    python3 scripts/fetch_imdb.py --confirm --date 2026-10-30 --dest /绝对路径/imdb

下载完成后把 .env 里的 IMDB_HOST_DIR 设为 imdb 目录的父级（即包含 <下载日期>/ 的目录），快照编号为 imdb_<下载日期>。
下载记录对每个文件写地址、开始与结束时间、HTTP Last-Modified、字节数与 SHA-256；装载前 etl_task4.imdb 会逐个重算核对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://datasets.imdbws.com/"
FILES = ("title.basics.tsv.gz", "title.akas.tsv.gz", "title.crew.tsv.gz", "title.principals.tsv.gz",
         "name.basics.tsv.gz")
USER_AGENT = "etl-coursework-task4/1.0 (personal non-commercial use)"
CHUNK = 1 << 20


def now() -> datetime:
    return datetime.now(timezone.utc)


def fetch(url: str, dest: Path) -> dict:
    """流式下载到临时文件，写完再改名；返回该文件的下载记录。"""
    started = now()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest, size = hashlib.sha256(), 0
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        last_modified = resp.headers.get("Last-Modified")
        while chunk := resp.read(CHUNK):
            out.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            print(f"\r{dest.name}: {size / 1048576:,.0f} MiB", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    tmp.replace(dest)
    return {"name": dest.name, "url": url, "started_at": started.isoformat(), "finished_at": now().isoformat(),
            "last_modified": last_modified, "size_bytes": size, "sha256": digest.hexdigest()}


def main(argv: list[str] | None = None) -> int:
    default_dest = Path(__file__).resolve().parents[2] / "imdb"
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--confirm", action="store_true", help="确认已取得用户许可，开始联网下载")
    ap.add_argument("--date", default=now().strftime("%Y-%m-%d"), help="下载日期，决定目录名与快照编号")
    ap.add_argument("--dest", type=Path, default=default_dest, help=f"imdb 父目录，默认 {default_dest}")
    args = ap.parse_args(argv)
    if not args.confirm:
        print("需要用户许可才能联网下载约 1–1.5 GB 数据；确认后加 --confirm 重新运行。", file=sys.stderr)
        return 2
    directory = args.dest / args.date
    if (directory / "download.json").exists():
        print(f"{directory}/download.json 已存在；换一个 --date，或先自行确认后删除旧目录。", file=sys.stderr)
        return 2
    directory.mkdir(parents=True, exist_ok=True)
    files = [fetch(BASE_URL + name, directory / name) for name in FILES]
    record = {"dataset": "IMDb Non-Commercial Datasets", "snapshot_id": f"imdb_{args.date}", "base_url": BASE_URL,
              "downloaded_at": now().isoformat(), "files": files}
    (directory / "download.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完成：{directory}（快照编号 {record['snapshot_id']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
