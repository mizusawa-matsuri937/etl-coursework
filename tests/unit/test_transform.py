from datetime import datetime, timezone
from decimal import Decimal

import pytest

from etl_task1.records import QUARANTINED, VALID

from _helpers import GOOD, scan_bytes


def one(**over):
    """GOOD 记录并按字段前缀覆盖；值为 None 时删除该行，bytes 时替换。"""
    lines = []
    for line in GOOD:
        prefix = line.split(b":")[0].decode()
        if prefix in over:
            if over[prefix] is None:
                continue
            line = prefix.encode() + b": " + over[prefix]
        lines.append(line)
    (rec,), _ = scan_bytes(b"\n".join(lines))
    return rec


def codes(rec):
    return [i.code for i in rec.issues]


@pytest.mark.parametrize("raw,expected", [(b"1", "1"), (b"5", "5"), (b"4.5", "4.5"), (b"5.0", "5.0")])
def test_valid_scores(raw, expected):
    r = one(**{"review/score": raw})
    assert r.status == VALID and r.normalized.score == Decimal(expected) and not codes(r)


@pytest.mark.parametrize("raw", [b"6", b"0", b"0.99", b"5.01", b"NaN", b"Infinity", b"abc", b"", b"1e0", b"1_0", b" 5"])
def test_invalid_scores_are_null_with_raw_and_issue(raw):
    r = one(**{"review/score": raw})
    assert r.status == VALID and r.normalized.score is None
    assert r.raw["score"] == raw.decode() and "invalid_score" in codes(r)


@pytest.mark.parametrize("raw,h,t,ratio", [
    (b"0/0", 0, 0, None), (b"1/2", 1, 2, Decimal("0.5")), (b"7/7", 7, 7, Decimal("1")),
])
def test_valid_helpfulness(raw, h, t, ratio):
    r = one(**{"review/helpfulness": raw})
    n = r.normalized
    assert (n.helpful_votes, n.total_votes) == (h, t) and n.helpfulness_ratio == ratio and not codes(r)


@pytest.mark.parametrize("raw,reason", [
    (b"2/1", "numerator_exceeds_denominator"), (b"-1/2", "negative"), (b"1/-2", "negative"),
    (b"1.5/2", "format"), (b"a/b", "format"), (b"1", "format"), (b"1/2/3", "format"), (b"", "format"),
    (b"99999999999999999999/99999999999999999999", "out_of_range"),
])
def test_invalid_helpfulness_nulls_votes_and_ratio(raw, reason):
    r = one(**{"review/helpfulness": raw})
    n = r.normalized
    assert (n.helpful_votes, n.total_votes, n.helpfulness_ratio) == (None, None, None)
    issue = next(i for i in r.issues if i.code == "invalid_helpfulness")
    assert issue.details["reason"] == reason and r.raw["helpfulness"] == raw.decode()


def test_unix_zero_is_epoch_utc():
    r = one(**{"review/time": b"0"})
    assert r.normalized.review_time_unix == 0
    assert r.normalized.review_time_utc == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert r.normalized.review_time_utc.isoformat() == "1970-01-01T00:00:00+00:00"


def test_known_timestamp_and_negative():
    assert one(**{"review/time": b"1182729600"}).normalized.review_time_utc == \
        datetime(2007, 6, 25, tzinfo=timezone.utc)
    assert one(**{"review/time": b"-86400"}).normalized.review_time_utc == \
        datetime(1969, 12, 31, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw,reason,unix", [
    (b"bad", "format", None), (b"1.5", "format", None), (b"12 ", "format", None), (b"", "format", None),
    (b"99999999999999999999", "int64_overflow", None), (b"999999999999", "datetime_overflow", 999999999999),
])
def test_invalid_times_keep_raw_and_record_issue(raw, reason, unix):
    r = one(**{"review/time": raw})
    assert r.normalized.review_time_utc is None and r.normalized.review_time_unix == unix
    assert r.raw["review_time"] == raw.decode()
    assert next(i for i in r.issues if i.code == "invalid_review_time").details["reason"] == reason


def test_empty_and_missing_user_do_not_quarantine():
    empty = one(**{"review/userId": b""})
    assert empty.status == VALID and empty.raw["reviewer_id"] == "" and empty.normalized.reviewer_id is None
    assert "empty_user_id" in codes(empty) and empty.reviewer_id_status == "blank"
    missing = one(**{"review/userId": None})
    assert missing.status == VALID and missing.raw["reviewer_id"] is None and not missing.present["reviewer_id"]
    assert missing.reviewer_id_status == "missing" and "missing_field" in codes(missing)
    blank = one(**{"review/userId": b"   "})
    assert blank.raw["reviewer_id"] == "   " and blank.normalized.reviewer_id is None


def test_unknown_literal_user_is_kept():
    r = one(**{"review/userId": b"unknown"})
    assert r.normalized.reviewer_id == "unknown" and r.observed_reviewer_id == "unknown" and not codes(r)


def test_empty_text_is_distinct_from_missing_text():
    empty = one(**{"review/text": b""})
    assert empty.raw["review_text"] == "" and "empty_review_text" in codes(empty)
    missing = one(**{"review/text": None})
    assert missing.raw["review_text"] is None and "missing_field" in codes(missing)
    assert "empty_review_text" not in codes(missing)


def test_profile_name_missing_or_empty_is_not_an_error():
    assert one(**{"review/profileName": b""}).raw["profile_name"] == ""
    assert one(**{"review/profileName": None}).raw["profile_name"] is None


def test_text_summary_are_never_altered():
    text = b"  <b>HTML</b>   multiple   spaces \xe4\xbd\xa0\xe5\xa5\xbd  "
    r = one(**{"review/text": text, "review/summary": b'"quoted"  '})
    assert r.raw["review_text"] == text.decode() and r.raw["summary"] == '"quoted"  '


def test_product_id_missing_blank_and_nonstandard():
    assert one(**{"product/productId": None}).status == QUARANTINED
    blank = one(**{"product/productId": b"   "})
    assert blank.status == QUARANTINED and "blank_product_id" in blank.quarantine_reasons
    assert one(**{"product/productId": b""}).product_id_status == "blank"
    odd = one(**{"product/productId": b"b00abc"})
    assert odd.status == VALID and odd.normalized.product_id == "b00abc"  # 不改大小写、不补字符
    assert "nonstandard_product_id_format" in codes(odd)


def test_invalid_utf8_quarantines_but_ids_still_observed():
    r = one(**{"review/text": b"bad \xff bytes"})
    assert r.status == QUARANTINED and r.quarantine_reasons == ("invalid_utf8",)
    assert (r.observed_product_id, r.observed_reviewer_id) == ("AAAAAAAAAA", "U1")
    assert r.raw["review_text"] is None  # 不用替换字符冒充原文


def test_undecodable_or_conflicting_ids_are_unidentifiable_not_missing():
    r = one(**{"review/userId": b"U\xff"})
    assert r.reviewer_id_status == "undecodable" and r.observed_reviewer_id is None
    (c,), _ = scan_bytes(b"\n".join(GOOD) + b"\nreview/userId: OTHER")
    assert c.reviewer_id_status == "conflict" and c.observed_reviewer_id is None
    (same,), _ = scan_bytes(b"\n".join(GOOD) + b"\nreview/userId: U1")
    assert same.status == QUARANTINED and same.observed_reviewer_id == "U1"


def test_nul_quarantines():
    r = one(**{"review/text": b"a\x00b"})
    assert r.status == QUARANTINED and "nul_byte" in r.quarantine_reasons


def test_multiple_issues_on_one_record_and_field_positions():
    r = one(**{"review/score": b"9", "review/time": b"x", "review/helpfulness": b"3/1"})
    assert sorted(codes(r)) == ["invalid_helpfulness", "invalid_review_time", "invalid_score"]
    assert r.start_lines["score"] == r.block.start_line + 4
    assert all(i.field for i in r.issues)
