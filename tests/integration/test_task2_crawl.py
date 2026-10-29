"""任务 2 爬虫写回清单：状态判定、换出口重试、尝试日志、快照证据与断点续跑（注入假请求，不访问外网）。"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
from collections import defaultdict
from pathlib import Path

import pytest
from psycopg.rows import dict_row

from etl_task1.loader import load_extraction
from etl_task2.crawl.backfill import backfill_templates
from etl_task2.crawl.crawler import (CrawlConfig, ExitPool, FetchResult, apply_crawl_schema, crawl)
from etl_task2.manifest import build_manifest


def product(asin: str) -> bytes:
    return f'<span id="productTitle">Movie {asin}</span><input name="ASIN" value="{asin}">'.encode()


REDIRECT = {"status": 301, "url": "https://www.amazon.com/dp/AAAAAAAAAA",
            "location": "https://www.amazon.com/dp/AAAAAAAAAA?lv=shuf"}
BLOCK = FetchResult(200, "https://www.amazon.com/checkpoint/continue?token=t", b"<html>nav only</html>")
# 每个 ASIN 依次返回的响应；Exception 表示该次请求抛错。
SCRIPT = {
    "AAAAAAAAAA": [FetchResult(200, "https://www.amazon.com/dp/AAAAAAAAAA?lv=shuf", product("AAAAAAAAAA"),
                               (REDIRECT,))],
    "BBBBBBBBBB": [BLOCK, FetchResult(200, "https://www.amazon.com/dp/BBBBBBBBBB", product("BBBBBBBBBB"))],
    "CCCCCCCCCC": [BLOCK, BLOCK],
    "DDDDDDDDDD": [FetchResult(404, "https://www.amazon.com/dp/DDDDDDDDDD", b"Page Not Found")],
    "EEEEEEEEEE": [TimeoutError("slow"), TimeoutError("slow")],
    "FFFFFFFFFF": [FetchResult(200, "https://www.amazon.com/", b"<html>home</html>")],
}
EXPECTED = {"AAAAAAAAAA": ("success", 1), "BBBBBBBBBB": ("success", 2), "CCCCCCCCCC": ("access_restricted", 2),
            "DDDDDDDDDD": ("not_found", 1), "EEEEEEEEEE": ("timeout", 2), "FFFFFFFFFF": ("parse_failed", 1)}


class FakeFetch:
    def __init__(self, script):
        self.script, self.calls = script, defaultdict(list)

    async def __call__(self, url, proxy, timeout):
        asin = url.rsplit("/", 1)[-1]
        self.calls[asin].append(proxy)
        await asyncio.sleep(0)
        res = self.script[asin][min(len(self.calls[asin]), len(self.script[asin])) - 1]
        if isinstance(res, Exception):
            raise res
        return res


@pytest.fixture
def eid(conn, make_settings, verify):
    assert apply_crawl_schema(conn) in (True, False)
    eid = load_extraction(make_settings(), verify(), conn).extraction_id
    build_manifest(conn, eid)
    return eid


def pool():
    return ExitPool([("p1", "http://p1:1"), ("p2", "http://p2:1")], rate=1000, burst=10,
                    base_cooldown=0.01, max_cooldown=0.05)


def run(conn, eid, tmp_path, fetch, **kw):
    cfg = CrawlConfig(snapshot_dir=tmp_path / "snap", concurrency=3, **kw)
    return asyncio.run(crawl(conn, eid, cfg, pool(), fetch=fetch))


def manifest(conn, eid):
    cur = conn.cursor(row_factory=dict_row)
    return {r["asin"]: r for r in cur.execute(
        "SELECT * FROM task2.product_manifest WHERE extraction_id = %s", (eid,)).fetchall()}


def test_crawl_writes_statuses_attempts_and_evidence(conn, eid, tmp_path):
    fetch = FakeFetch(SCRIPT)
    summary = run(conn, eid, tmp_path, fetch)
    assert summary.status == "succeeded" and summary.asin_done == 6
    rows = manifest(conn, eid)
    assert {a: (r["status"], r["attempt_count"]) for a, r in rows.items()} == EXPECTED
    assert len(fetch.calls["BBBBBBBBBB"]) == 2 and fetch.calls["BBBBBBBBBB"][0] != fetch.calls["BBBBBBBBBB"][1]

    # 成功行证据齐全，快照解压后的哈希与清单一致
    for asin in ("AAAAAAAAAA", "BBBBBBBBBB"):
        r = rows[asin]
        body = gzip.decompress(Path(r["snapshot_path"]).read_bytes())
        assert body == product(asin) and hashlib.sha256(body).hexdigest() == r["content_hash"]
        assert r["http_status"] == 200 and r["fetched_at"] is not None and asin in r["final_url"]
    # HTTP 200 的跳转页不算成功，保留快照作证据；超时没有响应体
    assert rows["CCCCCCCCCC"]["http_status"] == 200 and rows["CCCCCCCCCC"]["status_reason"] == "checkpoint_redirect"
    assert Path(rows["CCCCCCCCCC"]["snapshot_path"]).exists()
    assert rows["EEEEEEEEEE"]["snapshot_path"] is None and "TimeoutError" in rows["EEEEEEEEEE"]["status_reason"]

    attempts = conn.execute("SELECT asin, attempt_no, outcome FROM task2.fetch_attempt WHERE extraction_id = %s "
                            "ORDER BY asin, attempt_no", (eid,)).fetchall()
    assert len(attempts) == 9
    assert [a[2] for a in attempts if a[0] == "BBBBBBBBBB"] == ["access_restricted", "success"]
    # 跳转链与页面模板（R32、R33）：有响应的尝试都记录，超时没有响应则为空
    evidence = {(a, n): (chain, tpl) for a, n, chain, tpl in conn.execute(
        "SELECT asin, attempt_no, redirect_chain, page_template FROM task2.fetch_attempt "
        "WHERE extraction_id = %s", (eid,)).fetchall()}
    assert evidence[("AAAAAAAAAA", 1)] == ([REDIRECT], "dp_product_title")
    assert evidence[("BBBBBBBBBB", 1)] == ([], "checkpoint") and evidence[("BBBBBBBBBB", 2)][1] == "dp_product_title"
    assert evidence[("DDDDDDDDDD", 1)] == ([], "not_found") and evidence[("FFFFFFFFFF", 1)] == ([], "other")
    assert evidence[("EEEEEEEEEE", 1)] == (None, None)
    assert conn.execute("SELECT status, asin_done, outcome_counts->>'success' FROM task2.crawl_run "
                        "WHERE run_id = %s", (summary.run_id,)).fetchone() == ("succeeded", 6, "2")


def test_resume_processes_only_remaining_rows(conn, eid, tmp_path):
    fetch = FakeFetch(SCRIPT)
    first = run(conn, eid, tmp_path, fetch, limit=2)
    assert first.asin_done == 2
    assert sum(r["status"] != "not_collected" for r in manifest(conn, eid).values()) == 2
    second = run(conn, eid, tmp_path, fetch)
    assert second.asin_done == 4
    rows = manifest(conn, eid)
    assert {a: (r["status"], r["attempt_count"]) for a, r in rows.items()} == EXPECTED
    assert conn.execute("SELECT count(*) FROM task2.fetch_attempt WHERE extraction_id = %s", (eid,)).fetchone()[0] == 9
    assert run(conn, eid, tmp_path, fetch).asin_done == 0  # 全部处理完后再跑不产生新请求


def test_retry_status_recrawls_blocked_rows_and_accumulates_attempts(conn, eid, tmp_path):
    run(conn, eid, tmp_path, FakeFetch(SCRIPT))
    later = {**SCRIPT, "CCCCCCCCCC": [FetchResult(200, "https://www.amazon.com/dp/CCCCCCCCCC", product("CCCCCCCCCC"))]}
    summary = run(conn, eid, tmp_path, FakeFetch(later), retry_statuses=("access_restricted",))
    assert summary.asin_done == 1
    r = manifest(conn, eid)["CCCCCCCCCC"]
    assert (r["status"], r["attempt_count"]) == ("success", 3)
    assert conn.execute("SELECT max(attempt_no) FROM task2.fetch_attempt WHERE asin = 'CCCCCCCCCC'").fetchone()[0] == 3


def test_deadline_stops_promptly_and_leaves_queue_untouched(conn, eid, tmp_path):
    class SlowFetch(FakeFetch):
        async def __call__(self, url, proxy, timeout):
            await asyncio.sleep(0.2)
            return await super().__call__(url, proxy, timeout)

    summary = run(conn, eid, tmp_path, SlowFetch(SCRIPT), deadline_minutes=0.3 / 60)  # 0.3 秒
    assert summary.status == "interrupted"
    assert 1 <= summary.asin_done < 6  # 排队中的 ASIN 不再请求
    left = sum(r["status"] == "not_collected" for r in manifest(conn, eid).values())
    assert left == 6 - summary.asin_done


def test_deadline_stops_workers_waiting_for_cooling_exit(conn, eid, tmp_path):
    long_cooldown = ExitPool([("p1", "http://p1:1")], rate=1000, burst=10, base_cooldown=3600, ban_streak=1)
    cfg = CrawlConfig(snapshot_dir=tmp_path / "snap", concurrency=2, deadline_minutes=0.5 / 60)
    always_blocked = FakeFetch({a: [BLOCK] for a in SCRIPT})
    summary = asyncio.run(asyncio.wait_for(crawl(conn, eid, cfg, long_cooldown, fetch=always_blocked), 10))
    assert summary.status == "interrupted" and summary.asin_done < 6


def test_shuffled_order_with_jitter_covers_every_row_once(conn, eid, tmp_path):
    fetch = FakeFetch(SCRIPT)
    assert run(conn, eid, tmp_path, fetch, limit=3, shuffle=True, jitter=0.01).asin_done == 3
    assert run(conn, eid, tmp_path, fetch, shuffle=True, jitter=0.01).asin_done == 3
    rows = manifest(conn, eid)
    assert {a: (r["status"], r["attempt_count"]) for a, r in rows.items()} == EXPECTED


def test_backfill_fills_templates_from_snapshots_only(conn, eid, tmp_path):
    run(conn, eid, tmp_path, FakeFetch(SCRIPT))
    conn.execute("UPDATE task2.fetch_attempt SET page_template = NULL, redirect_chain = NULL WHERE extraction_id = %s",
                 (eid,))
    # 只有每个 ASIN 最后一次、有响应体的尝试保存了快照：A–D、F 各 1 个，E 超时没有快照
    assert backfill_templates(conn, eid) == {"candidates": 5, "filled": 5, "snapshot_missing": 0}
    got = dict(conn.execute("SELECT asin, page_template FROM task2.fetch_attempt WHERE extraction_id = %s "
                            "AND page_template IS NOT NULL", (eid,)).fetchall())
    assert got == {"AAAAAAAAAA": "dp_product_title", "BBBBBBBBBB": "dp_product_title", "CCCCCCCCCC": "checkpoint",
                   "DDDDDDDDDD": "not_found", "FFFFFFFFFF": "other"}
    assert backfill_templates(conn, eid)["candidates"] == 0  # 可重复运行，不重复写
