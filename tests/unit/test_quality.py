from etl_task1 import quality as q


def test_issue_key_deterministic_and_distinct():
    a = q.QualityIssue("score", q.INVALID_SCORE, q.WARNING)
    assert a.key("sid") == a.key("sid")
    assert a.key("sid") != a.key("other")
    assert a.key("sid", 0) != a.key("sid", 1)
    b = q.QualityIssue("score", q.INVALID_SCORE, q.WARNING, rule_version="v2")
    assert a.key("sid") != b.key("sid")


def test_clip_and_byte_evidence():
    assert q.clip("x" * 1000) == "x" * q.EVIDENCE_LIMIT and q.clip(None) is None and q.clip("ab") == "ab"
    assert q.byte_evidence(b"abc\xffdef", 3, radius=2) == b"bc\xffd".hex()
    assert q.byte_evidence(b"ab", 0) == b"ab".hex()
