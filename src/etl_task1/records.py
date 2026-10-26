"""记录数据结构：原始块、字段定义与解析结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .quality import QualityIssue

VALID = "valid"
QUARANTINED = "quarantined"

# 字段键 -> 完整行首前缀（含冒号）
FIELD_PREFIXES: dict[str, bytes] = {
    "product_id": b"product/productId:",
    "reviewer_id": b"review/userId:",
    "profile_name": b"review/profileName:",
    "helpfulness": b"review/helpfulness:",
    "score": b"review/score:",
    "review_time": b"review/time:",
    "summary": b"review/summary:",
    "review_text": b"review/text:",
}
# 冒号之前的部分 -> 字段键；用 partition 查表避免对每行逐个比较前缀
HEAD_TO_FIELD: dict[bytes, str] = {p[:-1]: k for k, p in FIELD_PREFIXES.items()}


@dataclass(frozen=True)
class RawBlock:
    """一个非空块及其原始定位。字节区间 [start_offset, end_offset) 为解压流位置。"""

    record_no: int
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    size: int
    sha256: str
    data: bytes | None  # 超限时为 None，完整字节在 spill_path
    spill_path: Path | None = None

    @property
    def oversize(self) -> bool:
        return self.spill_path is not None


@dataclass(frozen=True)
class Normalized:
    product_id: str | None = None
    reviewer_id: str | None = None
    helpful_votes: int | None = None
    total_votes: int | None = None
    helpfulness_ratio: Decimal | None = None
    score: Decimal | None = None
    review_time_unix: int | None = None
    review_time_utc: datetime | None = None


@dataclass(frozen=True)
class ParsedRecord:
    block: RawBlock
    source_review_id: str
    status: str
    quarantine_reasons: tuple[str, ...]
    # 原值：缺失为 None，存在但为空为 ""；无法严格解码的字段为 None
    raw: dict[str, str | None]
    present: dict[str, bool]
    start_lines: dict[str, int]
    normalized: Normalized
    issues: tuple[QualityIssue, ...]
    # R22：可明确识别的原始 ID 及状态（ok/missing/blank/conflict/undecodable/unreadable）
    observed_product_id: str | None
    product_id_status: str
    observed_reviewer_id: str | None
    reviewer_id_status: str
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.status == VALID
