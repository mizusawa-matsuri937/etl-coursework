"""任务 8 报告、实例与四个只读查询（spec AC12）。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_report.py
"""

from __future__ import annotations

import hashlib
import json

import pytest

import _task8_helpers as h
from etl_task8 import query, report

pytestmark = pytest.mark.integration

CATEGORIES = ("D1_exact_same_asin", "D2_same_asin_variant", "D3_family_copy", "weak_support_family",
              "C1_cross_family_same_content", "C2_near_duplicate_family", "C2_near_duplicate_stat_group",
              "helpfulness_anomaly", "incomplete_record", "collection_review", "cross_movie_shared", "stat_group")


@pytest.fixture
def world(conn, make_settings, tmp_path):
    eid = h.seed_small_world(conn, make_settings, tmp_path)
    r = h.execute(conn, eid, partitions=2)
    assert r.status == "succeeded"
    return eid, r.review_dedup_version_id, r.run_id


def test_ac12_summary_matches_database(conn, world):
    eid, rd, run_id = world
    s = report.build_summary(conn, rd)
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]     # noqa: E731
    assert s["run_id"] == run_id and s["version_status"] == "verified"
    assert s["records"]["read"] == one("SELECT count(*) FROM public.review_record WHERE extraction_id = %s", eid)
    assert s["records"]["quarantined"] == one("SELECT count(*) FROM public.review_record WHERE extraction_id = %s "
                                              "AND classification = 'quarantined'", eid) == 1
    assert s["records"]["incomplete"] == 1
    v = h.version(conn, rd)
    assert s["logical_reviews"] == one("SELECT count(*) FROM task8.logical_review WHERE dedup_stage_key = %s",
                                       v["dedup_stage_key"])
    assert s["duplicates"]["total"] == s["records"]["key_complete"] - s["logical_reviews"]
    assert set(s["duplicates"]["by_level"]) == {"representative", "D1_exact_same_asin", "D2_same_asin_variant",
                                                "D3_family_copy"}
    assert s["helpfulness"]["numerator_exceeds_denominator"] == 1 and s["helpfulness"]["missing"] == 1
    assert s["families"]["weak_support_multi_asin"] >= 1 and s["families"]["weak_support_multi_asin_D3_records"] >= 1
    assert s["candidates"]["by_type_scope"] == {"cross_family_same_content:cross_family": 1,
                                                "near_duplicate:family": 1, "near_duplicate:stat_group": 1}
    m = s["movies"]
    assert sum(m["difference_sources"].values()) == m["raw_records_on_singles"] - m["logical_distinct"]
    assert m["difference_sources"]["cross_movie_sharing"] == 1 and m["logical_only_on_collections"] == 1
    assert s["stat_groups"]["near_dup_pairs"] == 1
    assert s["stat_groups"]["sensitivity_rated"] == s["stat_groups"]["logical_reviews"] - 1
    cats = s["categories"]
    assert {"movie", "collection_with_movies", "tv_series", "not_collected", "concert_other",
            "pending_collected"} <= set(cats)
    assert sum(c["raw_valid"] for c in cats.values()) == s["records"]["valid"]


def test_ac12_export_examples_cover_all_categories_without_full_text(conn, world, tmp_path):
    eid, rd, run_id = world
    summary, directory, digests = report.build_and_write_report(conn, rd, tmp_path / "out", run_key="k1")
    assert directory == tmp_path / "out" / "task8" / eid / rd / "k1"
    files = {p.name for p in directory.iterdir()}
    assert {"summary.json", "report.md", "examples.md", "movie_review_stats.csv", "stat_group_review_stats.csv",
            "product_review_stats.csv", "dup_candidate.csv", "checksums.json"} <= files
    sums = json.loads((directory / "checksums.json").read_text())
    for name, d in sums.items():                                    # 记录的 SHA-256 与文件一致
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == d["sha256"] == digests[name]["sha256"]
    on_disk = json.loads((directory / "summary.json").read_text())
    assert on_disk["logical_reviews"] == summary["logical_reviews"]
    ex = report.examples(conn, rd)
    assert all(ex[c] for c in CATEGORIES), [c for c in CATEGORIES if not ex[c]]
    md = (directory / "examples.md").read_text()
    assert all(f"## {c}（" in md for c in CATEGORIES) and "source_review_id" in md
    full = conn.execute("SELECT review_text_raw FROM public.review_staging WHERE extraction_id = %s "
                        "AND length(review_text_raw) > 80", (eid,)).fetchall()
    assert full                                                     # 夹具里有超过 80 字符的正文
    blob = "".join((directory / f).read_text() for f in files)
    assert all(t not in blob for (t,) in full)                      # 导出不含正文全文


def test_ac12_four_queries(conn, world):
    eid, rd, _ = world
    rows = dict(conn.execute("SELECT r.record_no, r.classification FROM public.review_record r WHERE r.extraction_id = %s",
                             (eid,)).fetchall())
    statuses = {}
    for rn in rows:
        out = query.query_review(conn, rd, str(rn))
        statuses[out["mapping"]["mapping_status"]] = out
    assert set(statuses) == {"mapped", "quarantined", "incomplete"}     # 有效、隔离、不完整记录都返回状态
    mapped = statuses["mapped"]
    by_srid = query.query_review(conn, rd, mapped["source_review_id"])
    assert by_srid["record_no"] == mapped["record_no"] and by_srid["family"]["family_id"].startswith("rf_")
    lid = mapped["mapping"]["logical_review_id"]
    lg = query.query_logical(conn, rd, lid)
    assert lg["found"] and any(r["record_no"] == mapped["record_no"] for r in lg["records"])
    assert lg["text_prefix"] is None or len(lg["text_prefix"]) <= 80
    for by in ("product", "movie", "group"):
        st = query.query_stats(conn, rd, by, limit=5)
        assert st["rows"] and st["notes"] == list(report.NOTES)
        assert any("不含合集" in n for n in st["notes"])
    grp = query.query_stats(conn, rd, "group", limit=50)
    assert any(len(g["movies"]) == 2 for g in grp["rows"])
    rv = query.query_reviewer(conn, rd, "EU15")
    assert rv["found"] and len(rv["logical_reviews"]) == 2 and rv["candidates"][0]["type"] == "near_duplicate"
    assert query.query_review(conn, rd, "999999")["found"] is False
