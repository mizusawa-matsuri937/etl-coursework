"""测试辅助：构造内存流并扫描。"""

from __future__ import annotations

import io
from pathlib import Path

from etl_task1.parser import ScanStatus, scan_records
from etl_task1.records import ParsedRecord

FILE_ID = "a" * 64


def scan_bytes(data: bytes, **kwargs) -> tuple[list[ParsedRecord], ScanStatus]:
    status = ScanStatus()
    records = list(scan_records(io.BytesIO(data), kwargs.pop("file_id", FILE_ID), status, **kwargs))
    return records, status


def record_bytes(*lines: bytes, eol: bytes = b"\n") -> bytes:
    return eol.join(lines)


GOOD = [
    b"product/productId: AAAAAAAAAA", b"review/userId: U1", b"review/profileName: N",
    b"review/helpfulness: 1/1", b"review/score: 5.0", b"review/time: 0",
    b"review/summary: S", b"review/text: T",
]
