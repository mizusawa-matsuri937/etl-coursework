"""质量问题：问题码、严重程度与确定性问题键。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .identity import RULES_VERSION

EVIDENCE_LIMIT = 256  # 问题证据的字符上限；完整原值保存在暂存/隔离表中

ERROR, WARNING, INFO = "error", "warning", "info"

# 隔离原因
OVERSIZE_RECORD = "oversize_record"
NUL_BYTE = "nul_byte"
INVALID_UTF8 = "invalid_utf8"
DUPLICATE_PREFIX = "duplicate_prefix"
UNKNOWN_LINE = "unknown_line"
MISSING_PRODUCT_ID = "missing_product_id"
BLANK_PRODUCT_ID = "blank_product_id"

# 有效记录上的字段问题
MISSING_FIELD = "missing_field"
EMPTY_USER_ID = "empty_user_id"
EMPTY_REVIEW_TEXT = "empty_review_text"
INVALID_HELPFULNESS = "invalid_helpfulness"
INVALID_SCORE = "invalid_score"
INVALID_REVIEW_TIME = "invalid_review_time"
NONSTANDARD_PRODUCT_ID = "nonstandard_product_id_format"

QUARANTINE_CODES = frozenset(
    {OVERSIZE_RECORD, NUL_BYTE, INVALID_UTF8, DUPLICATE_PREFIX, UNKNOWN_LINE,
     MISSING_PRODUCT_ID, BLANK_PRODUCT_ID}
)


@dataclass(frozen=True)
class QualityIssue:
    field: str | None
    code: str
    severity: str
    evidence: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    rule_version: str = RULES_VERSION

    def key(self, source_review_id: str, ordinal: int = 0) -> str:
        """确定性问题键：同一记录同一规则下重试得到相同的键。"""
        raw = f"{source_review_id}|{self.field}|{self.code}|{ordinal}|{self.rule_version}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clip(text: str | None, limit: int = EVIDENCE_LIMIT) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit]


def byte_evidence(data: bytes, position: int, radius: int = 16) -> str:
    """以十六进制给出出错位置附近的字节，避免替换字符丢失原文。"""
    lo = max(0, position - radius)
    return data[lo : position + radius].hex()
