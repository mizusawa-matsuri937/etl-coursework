"""块 → 解析结果：字段识别、严格解码、分类与基础规范化（R05、R07、R13–R22）。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import quality as q
from .identity import source_review_id
from .quality import QualityIssue, clip
from .records import (
    FIELD_PREFIXES,
    HEAD_TO_FIELD,
    QUARANTINED,
    VALID,
    Normalized,
    ParsedRecord,
    RawBlock,
)

_INT64_MAX = 2**63 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_HELPFULNESS = re.compile(r"(-?\d+)/(-?\d+)", re.ASCII)
_SCORE = re.compile(r"[+-]?\d+(\.\d+)?", re.ASCII)
_UNIX_SECONDS = re.compile(r"-?\d+", re.ASCII)
_ASIN_LIKE = re.compile(r"[A-Z0-9]{10}", re.ASCII)
_SCORE_MIN, _SCORE_MAX = Decimal(1), Decimal(5)
_ID_HEAD_LIMIT = 4096  # 旁路记录中只为识别 ID 而读取的行首字节数
_RATIO_QUANT = Decimal("0.0000000001")

# 每个字段的出现记录：(绝对行号, 值字节)
_Occurrences = dict[str, list[tuple[int, bytes]]]


def _split_lines(data: bytes) -> list[tuple[bytes, bool]]:
    """按 LF 切行并返回 (去掉行结束符的内容, 是否已终止)。CRLF 的 CR 视为行结束符的一部分。"""
    out: list[tuple[bytes, bool]] = []
    pos, n = 0, len(data)
    while pos < n:
        i = data.find(b"\n", pos)
        if i < 0:
            out.append((data[pos:], False))
            break
        end = i - 1 if i > pos and data[i - 1 : i] == b"\r" else i
        out.append((data[pos:end], True))
        pos = i + 1
    return out


def _collect(lines: list[tuple[bytes, bool]], first_line: int) -> tuple[_Occurrences, list[tuple[int, bytes]]]:
    occ: _Occurrences = {}
    unknown: list[tuple[int, bytes]] = []
    for idx, (content, _terminated) in enumerate(lines):
        head, sep, tail = content.partition(b":")
        field = HEAD_TO_FIELD.get(head) if sep else None
        line_no = first_line + idx
        if field is None:
            unknown.append((line_no, content[:64]))
            continue
        if tail.startswith(b" "):  # 前缀后最多移除一个分隔空格
            tail = tail[1:]
        occ.setdefault(field, []).append((line_no, tail))
    return occ, unknown


def _id_status(values: list[tuple[int, bytes]] | None) -> tuple[str | None, str]:
    """R22：根据某 ID 字段的全部出现判断可识别的原始 ID 及状态。"""
    if not values:
        return None, "missing"
    decoded: set[str] = set()
    for _line, raw in values:
        if b"\x00" in raw:
            return None, "undecodable"
        try:
            decoded.add(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return None, "undecodable"
    if len(decoded) > 1:
        return None, "conflict"
    (value,) = decoded
    if not value.strip():
        return None, "blank"
    return value, "ok"


def _parse_helpfulness(raw: str) -> tuple[int | None, int | None, Decimal | None, str | None]:
    m = _HELPFULNESS.fullmatch(raw)
    if not m:
        return None, None, None, "format"
    helpful, total = int(m.group(1)), int(m.group(2))
    if helpful < 0 or total < 0:
        return None, None, None, "negative"
    if helpful > _INT64_MAX or total > _INT64_MAX:
        return None, None, None, "out_of_range"
    if helpful > total:
        return None, None, None, "numerator_exceeds_denominator"
    ratio = (Decimal(helpful) / Decimal(total)).quantize(_RATIO_QUANT) if total else None
    return helpful, total, ratio, None


def _parse_score(raw: str) -> tuple[Decimal | None, str | None]:
    if not _SCORE.fullmatch(raw):
        return None, "format"
    value = Decimal(raw)
    if not _SCORE_MIN <= value <= _SCORE_MAX:
        return None, "out_of_range"
    return value, None


def _parse_time(raw: str) -> tuple[int | None, datetime | None, str | None]:
    if not _UNIX_SECONDS.fullmatch(raw):
        return None, None, "format"
    seconds = int(raw)
    if abs(seconds) > _INT64_MAX:
        return None, None, "int64_overflow"
    try:
        return seconds, _EPOCH + timedelta(seconds=seconds), None
    except OverflowError:
        return seconds, None, "datetime_overflow"


def _decode(value: bytes) -> str | None:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _quarantine_issues(
    block: RawBlock,
    data: bytes | None,
    occ: _Occurrences,
    unknown: list[tuple[int, bytes]],
    product_status: str,
    undecodable: list[tuple[str, int, int, bytes]],
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    if block.oversize:
        issues.append(QualityIssue(None, q.OVERSIZE_RECORD, q.ERROR,
                                   details={"size": block.size, "spill_path": str(block.spill_path)}))
    if data is not None and b"\x00" in data:
        issues.append(QualityIssue(None, q.NUL_BYTE, q.ERROR,
                                   details={"offset": data.index(b"\x00")}))
    for field, line_no, pos, value in undecodable:
        issues.append(QualityIssue(field, q.INVALID_UTF8, q.ERROR,
                                   evidence=q.byte_evidence(value, pos),
                                   details={"line": line_no, "byte_position": pos}))
    for field, items in occ.items():
        if len(items) > 1:
            issues.append(QualityIssue(
                field, q.DUPLICATE_PREFIX, q.ERROR,
                details={"lines": [n for n, _ in items],
                         "values": [clip(_decode(v) or v.hex()) for _, v in items]}))
    for line_no, content in unknown:
        issues.append(QualityIssue(None, q.UNKNOWN_LINE, q.ERROR,
                                   evidence=q.clip(_decode(content) or content.hex(), 64),
                                   details={"line": line_no}))
    if product_status == "missing":
        issues.append(QualityIssue("product_id", q.MISSING_PRODUCT_ID, q.ERROR))
    elif product_status == "blank":
        issues.append(QualityIssue("product_id", q.BLANK_PRODUCT_ID, q.ERROR))
    return issues


def _scan_oversize(path: Path) -> tuple[_Occurrences, int]:
    """旁路大记录：只读取各行行首以识别 ID，不把整条记录读入内存。"""
    from .parser import iter_pieces

    occ: _Occurrences = {}
    line_no = 0
    head = bytearray()
    open_line = False
    truncated = False

    def close_line() -> None:
        nonlocal head, open_line, truncated
        content = bytes(head)
        if content.endswith(b"\n"):
            content = content[:-1]
        if content.endswith(b"\r"):
            content = content[:-1]
        key, sep, tail = content.partition(b":")
        field = HEAD_TO_FIELD.get(key) if sep else None
        if field in ("product_id", "reviewer_id"):
            if truncated:  # 行首之外还有未读内容：视为无法可靠识别
                tail = b"\xff"
            elif tail.startswith(b" "):
                tail = tail[1:]
            occ.setdefault(field, []).append((line_no, tail))
        head, open_line, truncated = bytearray(), False, False

    with open(path, "rb") as fh:
        for piece, final in iter_pieces(fh):
            open_line = True
            room = _ID_HEAD_LIMIT - len(head)
            if len(piece) > room:
                truncated = True
            head += piece[: max(room, 0)]
            if final:
                line_no += 1
                close_line()
    if open_line:
        line_no += 1
        close_line()
    return occ, line_no


def parse_record(block: RawBlock, source_file_id: str) -> ParsedRecord:
    sid = source_review_id(source_file_id, block.record_no)
    issues: list[QualityIssue] = []
    raw: dict[str, str | None] = {f: None for f in FIELD_PREFIXES}
    present: dict[str, bool] = {f: False for f in FIELD_PREFIXES}
    start_lines: dict[str, int] = {}

    if block.oversize:
        assert block.spill_path is not None
        occ, _ = _scan_oversize(block.spill_path)
        unknown: list[tuple[int, bytes]] = []
        data = None
    else:
        assert block.data is not None
        data = block.data
        occ, unknown = _collect(_split_lines(data), block.start_line)

    product_id, product_status = _id_status(occ.get("product_id"))
    reviewer_id, reviewer_status = _id_status(occ.get("reviewer_id"))
    for f, items in occ.items():
        present[f] = True
        start_lines[f] = items[0][0]

    undecodable: list[tuple[str, int, int, bytes]] = []
    if data is not None:
        for f, items in occ.items():
            for line_no, value in items:
                try:
                    value.decode("utf-8")
                except UnicodeDecodeError as exc:
                    undecodable.append((f, line_no, exc.start, value))
                    break

    q_issues = _quarantine_issues(block, data, occ, unknown, product_status, undecodable)
    if block.oversize:
        # 旁路记录只有 oversize_record 一个确定原因；其余内容未在内存中检查。
        q_issues = [i for i in q_issues if i.code == q.OVERSIZE_RECORD]
    reasons = list(dict.fromkeys(i.code for i in q_issues))

    if reasons:
        for f, items in occ.items():
            if len(items) == 1 and not block.oversize:
                raw[f] = _decode(items[0][1])
        return ParsedRecord(
            block=block, source_review_id=sid, status=QUARANTINED,
            quarantine_reasons=tuple(reasons), raw=raw, present=present, start_lines=start_lines,
            normalized=Normalized(), issues=tuple(q_issues),
            observed_product_id=product_id, product_id_status=product_status,
            observed_reviewer_id=reviewer_id, reviewer_id_status=reviewer_status,
        )

    for f, items in occ.items():
        raw[f] = items[0][1].decode("utf-8")  # 已确认可严格解码且仅出现一次

    for f in FIELD_PREFIXES:
        if f != "product_id" and not present[f]:
            issues.append(QualityIssue(f, q.MISSING_FIELD, q.WARNING))

    norm: dict[str, object] = {"product_id": raw["product_id"]}
    if raw["product_id"] is not None and not _ASIN_LIKE.fullmatch(raw["product_id"]):
        issues.append(QualityIssue("product_id", q.NONSTANDARD_PRODUCT_ID, q.INFO,
                                   evidence=clip(raw["product_id"])))

    user = raw["reviewer_id"]
    if present["reviewer_id"]:
        if user is not None and user.strip():
            norm["reviewer_id"] = user
        else:
            issues.append(QualityIssue("reviewer_id", q.EMPTY_USER_ID, q.WARNING, evidence=clip(user)))

    if present["review_text"] and raw["review_text"] == "":
        issues.append(QualityIssue("review_text", q.EMPTY_REVIEW_TEXT, q.WARNING))

    if raw["helpfulness"] is not None:
        helpful, total, ratio, bad = _parse_helpfulness(raw["helpfulness"])
        if bad:
            issues.append(QualityIssue("helpfulness", q.INVALID_HELPFULNESS, q.WARNING,
                                       evidence=clip(raw["helpfulness"]), details={"reason": bad}))
        else:
            norm.update(helpful_votes=helpful, total_votes=total, helpfulness_ratio=ratio)

    if raw["score"] is not None:
        score, bad = _parse_score(raw["score"])
        if bad:
            issues.append(QualityIssue("score", q.INVALID_SCORE, q.WARNING,
                                       evidence=clip(raw["score"]), details={"reason": bad}))
        else:
            norm["score"] = score

    if raw["review_time"] is not None:
        seconds, utc, bad = _parse_time(raw["review_time"])
        norm["review_time_unix"] = seconds
        norm["review_time_utc"] = utc
        if bad:
            issues.append(QualityIssue("review_time", q.INVALID_REVIEW_TIME, q.WARNING,
                                       evidence=clip(raw["review_time"]), details={"reason": bad}))

    return ParsedRecord(
        block=block, source_review_id=sid, status=VALID, quarantine_reasons=(), raw=raw,
        present=present, start_lines=start_lines, normalized=Normalized(**norm),  # type: ignore[arg-type]
        issues=tuple(issues), observed_product_id=product_id, product_id_status=product_status,
        observed_reviewer_id=reviewer_id, reviewer_id_status=reviewer_status,
    )
