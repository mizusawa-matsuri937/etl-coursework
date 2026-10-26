"""生成任务 1 固定样本：10 条人工构造记录的 gzip、manifest 与预期值。

用法（项目根目录）：python tests/fixtures/build_fixtures.py
输出写入 tests/fixtures/data/；gzip 头时间固定为 0，结果可重复。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
GZIP_NAME = "task1_sample.txt.gz"
MANIFEST_NAME = "task1_sample.manifest.json"
SOURCE_URL = "https://example.invalid/task1_sample.txt.gz"  # 人工样本，非真实来源


def record(product=b"AAAAAAAAAA", user=b"USER_A", profile=b"Profile A", helpfulness=b"1/1",
           score=b"5", time=b"0", summary=b"Summary", text=b"Plain review text"):
    """返回一条完整记录的行列表（无行尾）。传 None 表示省略该行。"""
    fields = [
        (b"product/productId:", product), (b"review/userId:", user),
        (b"review/profileName:", profile), (b"review/helpfulness:", helpfulness),
        (b"review/score:", score), (b"review/time:", time),
        (b"review/summary:", summary), (b"review/text:", text),
    ]
    lines = []
    for prefix, value in fields:
        if value is None:
            continue
        lines.append(prefix + (b" " + value if value != b"" else b""))
    return lines


# 第 1 条正文：冒号、逗号、制表符、反斜杠与行内前缀字样
TEXT_1 = b"Text: a, b,\ttab \\ backslash and inline review/score: 1 plus product/productId: X"

RECORDS: list[list[bytes]] = [
    record(text=TEXT_1),
    record(text=TEXT_1),
    record(user=b"", profile=b"", summary=b"", helpfulness=b"0/0"),
    record(product=b"BBBBBBBBBB", user=b"USER_B", score=b"6", helpfulness=b"2/1", time=b"bad"),
    record(product=b"BBBBBBBBBB", user=b"USER_C", text=b"bad bytes \xff\xfe here"),
    record(product=b"CCCCCCCCCC", user=b"USER_D") + [b"review/score: 4"],
    record(product=None, user=b"USER_E"),
    record(product=b"DDDDDDDDDD", user=b"USER_F", text=b"nul \x00 byte"),
    record(product=b"EEEEEEEEEE", user=b"unknown"),
    record(product=b"FFFFFFFFFF", user=b"USER_G", text=b""),
]


def build_bytes(records: list[list[bytes]] = RECORDS) -> bytes:
    """记录间用一个空行分隔；末条无结尾换行或分隔空行。"""
    return b"\n\n".join(b"\n".join(lines) for lines in records)


def gzip_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=6) as fh:
        fh.write(data)
    return buf.getvalue()


def write_manifest(path: Path, gz: bytes) -> None:
    path.write_text(json.dumps({
        "source_url": SOURCE_URL,
        "download_completed_at": None,
        "size_bytes": len(gz),
        "sha256": hashlib.sha256(gz).hexdigest(),
        "gzip_integrity": None,
    }, indent=2) + "\n", encoding="utf-8")


BAD_HASH_MANIFEST_NAME = "task1_sample.bad_hash.manifest.json"
TRUNCATED_GZIP_NAME = "task1_truncated.txt.gz"
TRUNCATED_MANIFEST_NAME = "task1_truncated.manifest.json"


def build(out_dir: Path = DATA_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    gz = gzip_bytes(build_bytes())
    gz_path, manifest_path = out_dir / GZIP_NAME, out_dir / MANIFEST_NAME
    gz_path.write_bytes(gz)
    write_manifest(manifest_path, gz)
    build_failure_cases(out_dir, gz)
    return gz_path, manifest_path


def build_failure_cases(out_dir: Path, gz: bytes) -> None:
    """失败演示输入：manifest 哈希错误；以及去掉 CRC/长度尾部的截断 gzip（manifest 与其自身一致）。"""
    bad = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    bad["sha256"] = "0" * 64
    (out_dir / BAD_HASH_MANIFEST_NAME).write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    truncated = gz[:-8]
    (out_dir / TRUNCATED_GZIP_NAME).write_bytes(truncated)
    write_manifest(out_dir / TRUNCATED_MANIFEST_NAME, truncated)


if __name__ == "__main__":
    for p in build():
        print(p)
