"""任务 8 清洗阶段 review_clean（spec AC02；AC14 的 SQL 与 Python 一致性；AC01 的抽取限定部分）。

上游表由任务 1 的装载器从合成 gzip 生成（tests/fixtures/task8/README.md）。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_clean.py
"""

from __future__ import annotations

import pytest

import _task8_helpers as h
from etl_task8.rules import bucket, content_hash, helpfulness_status
from task8.world import Rec, unix

pytestmark = pytest.mark.integration

A, B = "B000000001", "B000000002"


def _clean_rows(conn, eid: str) -> dict[int, dict]:
    key = h.stage_key(conn, eid, "review_clean")
    cur = conn.execute("SELECT * FROM task8.review_clean WHERE clean_stage_key = %s ORDER BY record_no", (key,))
    cols = [d.name for d in cur.description]
    return {r[1]: dict(zip(cols, r)) for r in cur.fetchall()}


def _issues(conn, eid: str) -> list[tuple]:
    key = h.stage_key(conn, eid, "review_clean")
    return conn.execute("SELECT record_no, code, evidence, detail FROM task8.review_issue WHERE stage_key = %s "
                        "ORDER BY record_no, code", (key,)).fetchall()


def test_ac02_helpfulness_status_and_incomplete(conn, make_settings, tmp_path):
    recs = [Rec(A, "U1", helpfulness="3/5"), Rec(A, "U2", helpfulness="0/0"), Rec(A, "U3", helpfulness="8/1"),
            Rec(A, "U4", helpfulness="-1/2"), Rec(A, "U5", helpfulness="abc"), Rec(A, "U6", helpfulness=None),
            Rec(B, "U7", helpfulness="2/3", score=None, time=None)]
    eid = h.setup(conn, make_settings, tmp_path, recs)
    out = h.run(conn, eid, "review_clean")
    rows = _clean_rows(conn, eid)
    got = [(r["helpfulness_raw"], r["helpfulness_status"], r["helpful_votes"], r["total_votes"], r["key_complete"])
           for r in rows.values()]
    assert got == [("3/5", "ok", 3, 5, True), ("0/0", "zero_zero", 0, 0, True),
                   ("8/1", "numerator_exceeds_denominator", None, None, True), ("-1/2", "negative", None, None, True),
                   ("abc", "unparseable", None, None, True), (None, "missing", None, None, True),
                   ("2/3", "ok", 2, 3, False)]
    # SQL 判定与 Python 判定逐条一致；票数与任务 1 的规范值一致
    for r in rows.values():
        assert helpfulness_status(r["helpfulness_raw"]) == (r["helpfulness_status"], r["helpful_votes"], r["total_votes"])
    staged = dict(conn.execute("SELECT r.record_no, s.helpfulness_ratio FROM public.review_record r JOIN "
                               "public.review_staging s USING (extraction_id, source_review_id) WHERE r.extraction_id = %s",
                               (eid,)).fetchall())
    assert staged[2] is None                                    # 0/0：比例为 NULL（R07）
    issues = _issues(conn, eid)
    assert [(n, c, e) for n, c, e, _ in issues] == [
        (3, "numerator_exceeds_denominator", "8/1"), (4, "negative_votes", "-1/2"),
        (5, "unparseable_helpfulness", "abc"), (6, "missing_helpfulness", None), (7, "incomplete_review", None)]
    assert issues[-1][3] == {"missing": ["review_time", "score"]}
    assert rows[7]["content_hash"] is None and rows[7]["review_day"] is None
    assert out["review_clean"]["helpfulness_status"] == {"ok": 2, "zero_zero": 1, "numerator_exceeds_denominator": 1,
                                                         "negative": 1, "unparseable": 1, "missing": 1}
    assert (out["review_clean"]["rows"], out["review_clean"]["issues"], out["review_clean"]["incomplete"]) == (7, 5, 1)


def test_ac14_sql_hash_and_buckets_match_python(conn, make_settings, tmp_path):
    texts = ["Plain text.", "plain text.", "Plain text. ", "Ünïcode — “quotes” and 日本語",
             'See <a href="http://www.amazon.com/dp/B000000001">this</a> &quot;great&quot;']
    recs = [Rec(A, "U1", text=t) for t in texts]
    recs += [Rec(B, "U1", summary=None), Rec(B, "U1", summary=""), Rec(B, "U2", score="4.0", time=unix(3))]
    eid = h.setup(conn, make_settings, tmp_path, recs)
    h.run(conn, eid, "review_clean")
    rows = _clean_rows(conn, eid)
    raw = {n: r for n, *r in conn.execute(
        "SELECT r.record_no, s.reviewer_id, (s.review_time_utc AT TIME ZONE 'UTC')::date, s.score, s.summary_raw, "
        "s.review_text_raw, s.product_id FROM public.review_record r JOIN public.review_staging s "
        "USING (extraction_id, source_review_id) WHERE r.extraction_id = %s", (eid,))}
    assert len(rows) == len(recs)
    for n, r in rows.items():
        reviewer, day, score, summary, text, asin = raw[n]
        assert bytes(r["content_hash"]) == content_hash(reviewer, day, score, summary, text), n
        assert (r["asin_bucket"], r["reviewer_bucket"]) == (bucket(asin), bucket(reviewer))
        assert r["review_day"] == day
    hashes = [bytes(rows[n]["content_hash"]) for n in sorted(rows)]
    assert len(set(hashes)) == len(hashes)                     # 大小写、末尾空格、摘要缺失与空摘要都不相同
    assert raw[6][3] is None and raw[7][3] == ""


def test_ac01_clean_reads_only_requested_extraction(conn, make_settings, tmp_path):
    recs = [Rec(A, "U1"), Rec(A, "U2", raw_text=b"bad \xff bytes"), Rec(B, "U3", extra=(b"garbage line",)),
            Rec(B, "U4"), Rec(None, "U5", extra=(b"5b4",))]   # 末条与正式库的 12 条同形：无法识别的行且无产品 ID
    eid = h.setup(conn, make_settings, tmp_path, recs)
    sample = h.load(conn, make_settings, tmp_path, recs, name="sample", sample_max=2)
    assert sample != eid
    before = h.public_digest(conn)
    h.run(conn, eid, "review_clean")
    assert h.public_digest(conn) == before
    reasons = dict(conn.execute("SELECT r.record_no, q.reasons FROM public.quarantine_record q JOIN public.review_record r "
                                "USING (extraction_id, source_review_id) WHERE q.extraction_id = %s", (eid,)).fetchall())
    assert reasons == {2: ["invalid_utf8"], 3: ["unknown_line"], 5: ["unknown_line", "missing_product_id"]}
    rows = _clean_rows(conn, eid)
    assert sorted(rows) == [1, 4]                               # 隔离记录不进 review_clean（R05）
    srids = h.record_nos(conn, eid)
    assert all(rows[n]["source_review_id"] == srids[n] for n in rows)
    assert conn.execute("SELECT count(*) FROM task8.stage_key WHERE extraction_id = %s", (sample,)).fetchone()[0] == 0


def test_clean_batches_resume_after_fault(conn, make_settings, tmp_path):
    recs = [Rec(A if i % 2 else B, f"U{i}", helpfulness=f"{i}/9") for i in range(1, 8)]
    eid = h.setup(conn, make_settings, tmp_path, recs)

    def fail_second(stage, batch_no):
        if stage == "review_clean" and batch_no == 2:
            raise RuntimeError("注入故障")

    with pytest.raises(RuntimeError, match="注入故障"):
        h.run(conn, eid, "review_clean", batch_size=2, before_commit=fail_second)
    assert sorted(_clean_rows(conn, eid)) == [1, 2]              # 只有第 1 批落库
    assert conn.execute("SELECT status FROM task8.run").fetchone()[0] == "failed"
    out = h.run(conn, eid, "review_clean", batch_size=3)
    assert out["review_clean"]["batches"] == 2                  # 从第 3 条继续：3–5、6–7
    resumed = {n: {k: v for k, v in r.items() if k != "batch_no"} for n, r in _clean_rows(conn, eid).items()}
    again = h.run(conn, eid, "review_clean")
    assert again["review_clean"].get("skipped") == "complete"
    h.reset(conn)
    h.run(conn, eid, "review_clean")
    once = {n: {k: v for k, v in r.items() if k != "batch_no"} for n, r in _clean_rows(conn, eid).items()}
    assert resumed == once and len(once) == 7
