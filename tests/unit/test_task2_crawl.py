"""任务 2 爬虫的纯逻辑：令牌桶、出口池冷却与轮换、状态判定、出口配置，以及一次本地真实 Scrapling 请求。"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from etl_task2.crawl.crawler import (ExitPool, SessionFetcher, TokenBucket, classify, load_exits,
                                     scrapling_fetch)

ASIN = "B000TEST01"
PRODUCT = b'<html><span id="productTitle"> A Movie </span><input name="ASIN" value="B000TEST01"></html>'


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_token_bucket_limits_burst_and_refills():
    clock = FakeClock()
    b = TokenBucket(rate=2, burst=2, clock=clock)
    b.take(), b.take()
    assert b.wait_time() == pytest.approx(0.5)
    clock.now += 0.5
    assert b.wait_time() == 0
    clock.now += 100  # 不超过 burst
    b.take(), b.take()
    assert b.wait_time() > 0


def make_pool(clock, n=2, **kw):
    kw = {"window": 10, "max_block_ratio": 0.5, "ban_streak": 4, "base_cooldown": 60, "max_cooldown": 900, **kw}
    return ExitPool([(f"p{i}", f"http://p{i}:1") for i in range(n)], rate=1, burst=1,
                    clock=clock, sleep=clock.sleep, **kw)


def feed(pool, e, pattern: str) -> None:
    """按字符逐次上报：b=被拦截，s=成功。"""
    for ch in pattern:
        pool.report(e, "access_restricted" if ch == "b" else "success")


def test_scattered_blocks_below_ratio_are_not_penalised():
    clock = FakeClock()
    pool = make_pool(clock, n=1)
    e = pool.exits[0]
    feed(pool, e, "bsbsssbsss")  # 30%：低于 50% 的 80%，不罚并提速（已在上限则保持）
    assert e.bucket.rate == 1 and e.cooldown_until <= clock.now and not e.window


def test_window_over_ratio_backs_off_and_doubles_cooldown():
    clock = FakeClock()
    pool = make_pool(clock, n=1)
    e = pool.exits[0]
    feed(pool, e, "bbsbbsbbsb")  # 70% > 50%
    assert e.bucket.rate == 0.5 and e.cooldown_until == clock.now + 60
    clock.now = e.cooldown_until
    feed(pool, e, "bsbbsbbsbb")
    assert e.bucket.rate == 0.25 and e.cooldown_until == clock.now + 120
    clock.now = e.cooldown_until
    feed(pool, e, "ssssssssss")  # 恢复：速率乘 1.5（至少加 max_rate 的 10%），超标计数清零
    assert e.bucket.rate == pytest.approx(0.375) and e.bad_windows == 0


def test_slow_exit_is_evaluated_by_time_not_only_by_window_size():
    clock = FakeClock()
    pool = make_pool(clock, n=1, window=50, min_samples=10, eval_interval=60)
    e = pool.exits[0]
    e.bucket.set_rate(0.1)
    feed(pool, e, "s" * 10)
    assert e.bucket.rate == 0.1  # 未满 60 秒且未满 50 次：不评估
    clock.now += 61
    feed(pool, e, "s")
    assert e.bucket.rate == pytest.approx(0.2) and not e.window  # 过了 60 秒、已有 11 次：评估并提速


def test_ban_streak_trips_before_window_fills_and_rotates_exit():
    clock = FakeClock()
    pool = make_pool(clock)
    first = asyncio.run(pool.acquire())
    feed(pool, first, "bbbb")
    assert first.cooldown_until == clock.now + 60 and first.bucket.rate == 0.5
    assert asyncio.run(pool.acquire()) is not first
    feed(pool, first, "b")  # 冷却中再次拦截：只计数不加罚
    assert first.bucket.rate == 0.5 and first.blocked == 5


def test_pool_waits_when_all_exits_cooling():
    clock = FakeClock()
    pool = make_pool(clock, n=1)
    e = asyncio.run(pool.acquire())
    feed(pool, e, "bbbb")
    t0 = clock.now
    assert asyncio.run(pool.acquire()) is e
    assert clock.now - t0 >= 60  # 等到冷却结束才放行，而不是消耗 ASIN


def test_acquire_stops_on_event():
    from etl_task2.crawl.crawler import CrawlStopped
    clock = FakeClock()
    pool = make_pool(clock, n=1)
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(CrawlStopped):
        asyncio.run(pool.acquire(stop))


@pytest.mark.parametrize("status,url,body,expected", [
    (200, f"https://www.amazon.com/dp/{ASIN}?lv=shuf", PRODUCT, ("success", "product_page")),
    (200, "https://www.amazon.com/checkpoint/continue?token=x", b"<html>nav</html>",
     ("access_restricted", "checkpoint_redirect")),
    (200, f"https://www.amazon.com/dp/{ASIN}", b'<form action="/errors/validateCaptcha">',
     ("access_restricted", "captcha")),
    (503, f"https://www.amazon.com/dp/{ASIN}", b"Something went wrong", ("access_restricted", "http_503")),
    (429, f"https://www.amazon.com/dp/{ASIN}", b"", ("access_restricted", "http_429")),
    (404, f"https://www.amazon.com/dp/{ASIN}", b"<title>Page Not Found</title>", ("not_found", "http_404")),
    (200, f"https://www.amazon.com/dp/{ASIN}", b"<html>home</html>", ("parse_failed", "no_product_title")),
    (200, "https://www.amazon.com/dp/B000OTHER9",
     b'<span id="productTitle">x</span><input name="ASIN" value="B000OTHER9">', ("parse_failed", "asin_mismatch")),
])
def test_classify(status, url, body, expected):
    assert classify(ASIN, status, url, body) == expected


def test_load_exits_hides_credentials_and_falls_back():
    exits = load_exits({"CRAWL_PROXY_URLS": "http://u:pw@gw.example:8000, http://u:pw@gw.example:8000,http://1.2.3.4:80"})
    assert [label for label, _ in exits] == ["gw.example:8000", "gw.example:8000#2", "1.2.3.4:80"]
    assert exits[0][1] == "http://u:pw@gw.example:8000"
    assert load_exits({"CRAWL_PROXY_URL": "http://host.docker.internal:1082"}) == [
        ("host.docker.internal:1082", "http://host.docker.internal:1082")]
    assert load_exits({}) == [("direct", None)]


class _Handler(BaseHTTPRequestHandler):
    homes = 0

    def do_GET(self):
        cookie = b""
        if self.path == "/":  # 模拟首页下发会话 cookie，每次预热得到不同的值
            type(self).homes += 1
            code, body = 200, b"home"
            cookie = f"session-id=s{type(self).homes}; Path=/".encode()
        elif "echo-cookie" in self.path:
            code, body = 200, (self.headers.get("Cookie") or "").encode()
        elif "missing" in self.path:
            code, body = 404, b"<title>Page Not Found</title>"
        else:
            code, body = 200, PRODUCT
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie.decode())
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_scrapling_fetch_against_local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        ok = asyncio.run(scrapling_fetch(f"{base}/dp/{ASIN}", None, 10))
        missing = asyncio.run(scrapling_fetch(f"{base}/dp/missing", None, 10))
    finally:
        server.shutdown()
    assert (ok.status, ok.body) == (200, PRODUCT)
    assert classify(ASIN, ok.status, ok.url, ok.body)[0] == "success"
    assert missing.status == 404


def test_session_fetcher_keeps_cookies_and_renews_after_blocks():
    _Handler.homes = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    async def scenario():
        f = SessionFetcher(home=f"{base}/", reset_after_blocks=2, max_requests=100)
        try:
            first = await f(f"{base}/echo-cookie", None, 10)
            second = await f(f"{base}/echo-cookie", None, 10)
            f.notify(None, "access_restricted"), f.notify(None, "access_restricted")
            renewed = await f(f"{base}/echo-cookie", None, 10)
            return first.body, second.body, renewed.body, f.created, len(f.retired)
        finally:
            await f.aclose()

    try:
        first, second, renewed, created, retired_open = asyncio.run(scenario())
    finally:
        server.shutdown()
    assert first == second == b"session-id=s1"  # 预热拿到的 cookie 在后续请求中复用
    assert renewed == b"session-id=s2" and created == 2  # 连续被拦截后换新会话并重新预热
    assert retired_open == 0  # 旧会话没有在途请求，换新时立即关闭，不泄漏连接句柄
