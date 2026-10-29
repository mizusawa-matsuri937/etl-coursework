"""产品页面爬虫：令牌桶限速 + 多出口（代理）轮换 + 断点续跑，结果写回 task2.product_manifest。

数据流：按 ASIN 顺序分批读取待采集行 → asyncio worker 从出口池取一个有令牌的出口 → 请求 /dp/<ASIN>
→ classify() 判定状态 → 被拦截或网络错误时换出口重试（每个 ASIN 每次运行最多 max_attempts 次）
→ 最后一次响应体 gzip 落盘 → 在一个事务里写入尝试日志并更新清单的爬虫列。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task2.crawl.crawler --limit 50
出口来自环境变量 CRAWL_PROXY_URLS（逗号分隔）或 CRAWL_PROXY_FILE（每行一个），
都没有时退回 CRAWL_PROXY_URL；再没有则直连。
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import os
import random
import re
import signal
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect, sql_dir

log = logging.getLogger(__name__)

# 依次应用的爬虫迁移：(版本, 文件)
MIGRATIONS = (("102", "102_task2_crawl.sql"), ("103", "103_task2_crawl_evidence.sql"))
PROJECT_ROOT = sql_dir().parent  # 快照路径相对它保存，容器内为 /opt/etl
STATUSES = ("success", "not_found", "access_restricted", "timeout", "parse_failed")
RETRYABLE = ("access_restricted", "timeout")


# ---------------------------------------------------------------- 令牌桶与出口池

class TokenBucket:
    """经典令牌桶：每秒补充 rate 个令牌，最多积累 burst 个；单事件循环内使用，无需加锁。"""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = float(burst)
        self.updated = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def wait_time(self) -> float:
        """距下一个令牌可用的秒数；已有令牌时为 0。"""
        self._refill()
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) / self.rate

    def take(self) -> None:
        self._refill()
        self.tokens -= 1

    def set_rate(self, rate: float) -> None:
        self._refill()
        self.rate = rate


@dataclass
class Exit:
    label: str
    proxy: str | None
    bucket: TokenBucket
    max_rate: float
    window: deque = field(default_factory=deque)  # 最近若干次响应是否被拦截
    cooldown_until: float = 0.0
    block_streak: int = 0
    error_streak: int = 0
    bad_windows: int = 0
    last_eval: float = 0.0
    last_used: float = 0.0
    used: int = 0
    blocked: int = 0


class CrawlBusy(RuntimeError):
    """同一抽取已有爬虫持有锁（暂时错误，Airflow 可稍后重试）。"""


class CrawlStopped(Exception):
    """收到停止信号或到达截止时间，放弃等待出口。"""


class ExitPool:
    """多个出口各自一个令牌桶，另有可选的全局桶；按窗口拦截率自适应调速。

    每个出口每收满 window 次响应、或距上次评估已过 eval_interval 秒且至少有 min_samples 次响应时，
    评估一次拦截率（不含网络错误）。只按次数评估时，低速出口要很久才能攒满窗口，恢复过慢（2026-10-28 30 分钟实测）。
    - 高于 max_block_ratio：速率减半，冷却 base_cooldown·2^(连续超标窗口数-1) 秒（上限 max_cooldown）；
    - 低于 max_block_ratio 的 80%：速率乘 1.5（至少加 max_rate 的 10%），不超过 max_rate。
    偶发 checkpoint 只计入比例，不单独处罚（2026-10-28 实测稳态约 30% 且随机）。
    连续拦截达到 ban_streak 次视为被封，立即按上面的规则减速冷却，不等窗口收满。
    已在冷却中的出口再收到拦截只计数，避免并发中的请求把同一次封禁重复放大。
    连续网络错误 5 次：短暂冷却 base_cooldown/2 秒（多为代理故障）。
    """

    def __init__(self, proxies: list[tuple[str, str | None]], *, rate: float, burst: float,
                 global_rate: float | None = None, base_cooldown: float = 30.0, max_cooldown: float = 600.0,
                 max_block_ratio: float = 0.5, window: int = 50, ban_streak: int = 10, min_rate: float = 0.1,
                 eval_interval: float = 60.0, min_samples: int = 10,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if not proxies:
            raise ValueError("至少需要一个出口")
        self.clock, self.sleep = clock, sleep
        self.exits = [Exit(label, proxy, TokenBucket(rate, burst, clock), rate, last_eval=clock())
                      for label, proxy in proxies]
        self.global_bucket = TokenBucket(global_rate, max(1.0, burst), clock) if global_rate else None
        self.base_cooldown, self.max_cooldown = base_cooldown, max_cooldown
        self.max_block_ratio, self.window, self.ban_streak = max_block_ratio, window, ban_streak
        self.min_rate, self.eval_interval, self.min_samples = min_rate, eval_interval, min_samples

    async def acquire(self, stop: asyncio.Event | None = None) -> Exit:
        while True:
            if stop is not None and stop.is_set():
                raise CrawlStopped
            now = self.clock()
            ready = [e for e in self.exits if e.cooldown_until <= now]
            if ready:
                best = min(ready, key=lambda e: (e.bucket.wait_time(), e.last_used))
                wait = best.bucket.wait_time()
                if self.global_bucket is not None:
                    wait = max(wait, self.global_bucket.wait_time())
                if wait <= 0:
                    best.bucket.take()
                    if self.global_bucket is not None:
                        self.global_bucket.take()
                    best.last_used, best.used = now, best.used + 1
                    return best
            else:  # 全部出口都在冷却：等待，不消耗 ASIN
                wait = min(e.cooldown_until for e in self.exits) - now
            await self.sleep(min(max(wait, 0.001), 1.0))

    def _back_off(self, e: Exit, now: float) -> None:
        e.bad_windows += 1
        e.bucket.set_rate(max(self.min_rate, e.bucket.rate / 2))
        e.cooldown_until = now + min(self.base_cooldown * 2 ** min(e.bad_windows - 1, 20), self.max_cooldown)
        e.window.clear()
        e.block_streak = 0
        e.last_eval = now

    def report(self, e: Exit, outcome: str) -> None:
        now = self.clock()
        if outcome == "timeout":
            e.error_streak += 1
            if e.error_streak >= 5:
                e.cooldown_until = max(e.cooldown_until, now + self.base_cooldown / 2)
                e.error_streak = 0
            return
        e.error_streak = 0
        blocked = outcome == "access_restricted"
        if blocked:
            e.blocked += 1
        if e.cooldown_until > now:
            return
        e.block_streak = e.block_streak + 1 if blocked else 0
        if e.block_streak >= self.ban_streak:
            self._back_off(e, now)
            return
        e.window.append(blocked)
        due = len(e.window) >= self.window or (len(e.window) >= self.min_samples
                                               and now - e.last_eval >= self.eval_interval)
        if not due:
            return
        ratio = sum(e.window) / len(e.window)
        if ratio > self.max_block_ratio:
            self._back_off(e, now)
            return
        if ratio < self.max_block_ratio * 0.8:
            rate = e.bucket.rate
            e.bucket.set_rate(min(e.max_rate, max(rate * 1.5, rate + e.max_rate * 0.1)))
            e.bad_windows = 0
        e.window.clear()
        e.last_eval = now

    def block_ratio(self, e: Exit) -> float | None:
        return sum(e.window) / len(e.window) if e.window else None

    def describe(self) -> str:
        now = self.clock()
        parts = []
        for e in self.exits:
            cool = f" cooling {e.cooldown_until - now:.0f}s" if e.cooldown_until > now else ""
            ratio = self.block_ratio(e)
            win = f" window_block={ratio:.0%}" if ratio is not None else ""
            parts.append(f"{e.label}: {e.bucket.rate:.2f}/s used={e.used} blocked={e.blocked}{win}{cool}")
        return "; ".join(parts)


def load_exits(env: Mapping[str, str]) -> list[tuple[str, str | None]]:
    """读取出口配置，返回 (标签, 代理 URL) 列表；标签不含账号密码，同一 URL 重复出现时加序号区分。"""
    urls: list[str] = []
    if env.get("CRAWL_PROXY_URLS", "").strip():
        urls = [u.strip() for u in env["CRAWL_PROXY_URLS"].split(",") if u.strip()]
    elif env.get("CRAWL_PROXY_FILE", "").strip():
        lines = Path(env["CRAWL_PROXY_FILE"].strip()).read_text(encoding="utf-8").splitlines()
        urls = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    elif env.get("CRAWL_PROXY_URL", "").strip():
        urls = [env["CRAWL_PROXY_URL"].strip()]
    if not urls:
        return [("direct", None)]
    seen: Counter[str] = Counter()
    exits = []
    for url in urls:
        parts = urlsplit(url)
        base = f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)
        seen[base] += 1
        exits.append((base if seen[base] == 1 else f"{base}#{seen[base]}", url))
    return exits


# ---------------------------------------------------------------- 状态判定

_DP_TITLE = re.compile(rb'id="productTitle"')
_AUTO_TITLE = re.compile(rb'data-automation-id="title"')
_TITLE = re.compile(rb'id="productTitle"|data-automation-id="title"')
_NOT_FOUND = re.compile(rb"Page Not Found|couldn't find that page|looking for something\?", re.I)
_CAPTCHA = re.compile(rb"validateCaptcha|Enter the characters you see below|/errors/validateCaptcha", re.I)


def classify(asin: str, http_status: int, final_url: str, body: bytes) -> tuple[str, str]:
    """根据响应判定 (状态, 原因)。HTTP 200 的验证页、跳转页、错误页不算成功。"""
    if "/checkpoint/" in final_url:
        return "access_restricted", "checkpoint_redirect"
    if _CAPTCHA.search(body):
        return "access_restricted", "captcha"
    if http_status in (403, 429) or http_status >= 500:
        return "access_restricted", f"http_{http_status}"
    if http_status == 404 or (http_status == 200 and _NOT_FOUND.search(body) and not _TITLE.search(body)):
        return "not_found", f"http_{http_status}"
    if http_status != 200:
        return "parse_failed", f"http_{http_status}"
    if not _TITLE.search(body):
        return "parse_failed", "no_product_title"
    if asin not in final_url and asin.encode() not in body:
        return "parse_failed", "asin_mismatch"
    return "success", "product_page"


def detect_template(final_url: str, http_status: int, body: bytes) -> str:
    """页面模板标签，作为模板变化的证据（R33）：两种产品页标题、拦截页、错误页或其他。"""
    if "/checkpoint/" in final_url:
        return "checkpoint"
    if _CAPTCHA.search(body):
        return "captcha"
    if _DP_TITLE.search(body):
        return "dp_product_title"
    if _AUTO_TITLE.search(body):
        return "automation_title"
    if http_status == 404 or _NOT_FOUND.search(body):
        return "not_found"
    if http_status >= 500:
        return "service_error"
    return "other"


# ---------------------------------------------------------------- 请求

@dataclass(frozen=True)
class FetchResult:
    status: int
    url: str
    body: bytes
    history: tuple[dict, ...] = ()  # 跳转链 [{status, url, location}]，最终页面不在其中


def redirect_history(page) -> tuple[dict, ...]:
    """从 Scrapling 响应取跳转链：curl_cffi 的 history 只保留每一跳的状态、地址和响应头。"""
    return tuple({"status": int(h.status_code), "url": str(h.url), "location": h.headers.get("location")}
                 for h in getattr(page, "history", None) or ())


Fetch = Callable[[str, str | None, float], Awaitable[FetchResult]]


async def scrapling_fetch(url: str, proxy: str | None, timeout: float) -> FetchResult:
    """Scrapling 的纯 HTTP 异步请求（curl_cffi，模拟 Chrome 指纹）；重试由本模块控制。"""
    from scrapling.fetchers import AsyncFetcher

    page = await AsyncFetcher.get(url, impersonate="chrome", proxy=proxy, timeout=timeout,
                                  retries=1, follow_redirects=True)
    return FetchResult(int(page.status), str(page.url), bytes(page.body), redirect_history(page))


@dataclass
class _Session:
    owner: object   # FetcherSession，负责关闭
    client: object  # 进入异步上下文后返回的会话客户端，负责发请求
    requests: int = 0
    block_streak: int = 0
    expired: bool = False
    inflight: int = 0


class SessionFetcher:
    """每个出口维持一个 Scrapling FetcherSession（curl_cffi 会话，自动保存 cookie）。

    新会话先访问一次首页取得 session-id、ubid-main 等真实 cookie，之后复用，避免每个请求都像无 cookie 的新访客。
    会话连续被拦截 reset_after_blocks 次，或已发出 max_requests 个请求，就换一个新会话重新预热。
    被替换的会话可能还有请求在途，统一在 aclose() 时关闭。
    """

    HOME = "https://www.amazon.com/"

    def __init__(self, *, max_requests: int = 300, reset_after_blocks: int = 3, home: str | None = None,
                 factory: Callable[[str | None, float], object] | None = None):
        self.max_requests, self.reset_after_blocks = max_requests, reset_after_blocks
        self.home = self.HOME if home is None else home
        self.factory = factory or self._scrapling_session
        self.sessions: dict[str | None, _Session] = {}
        self.retired: list[_Session] = []
        self.locks: dict[str | None, asyncio.Lock] = {}
        self.created = 0

    @staticmethod
    def _scrapling_session(proxy: str | None, timeout: float):
        from scrapling.fetchers import FetcherSession

        return FetcherSession(impersonate="chrome", proxy=proxy, timeout=timeout, retries=1, follow_redirects=True)

    async def _session(self, proxy: str | None, timeout: float) -> _Session:
        async with self.locks.setdefault(proxy, asyncio.Lock()):
            current = self.sessions.get(proxy)
            if current is not None and not current.expired:
                return current
            if current is not None:  # 旧会话没有在途请求就立即关闭，否则由最后一个请求关闭
                self.retired.append(current)
                if current.inflight == 0:
                    await self._close(current)
            owner = self.factory(proxy, timeout)
            client = await owner.__aenter__()
            try:
                if self.home:
                    try:  # 预热只为拿 cookie，失败不影响后续请求
                        await client.get(self.home, timeout=timeout)
                    except Exception as exc:
                        log.debug("会话预热失败：%s", exc)
            except BaseException:  # 预热被超时取消：关闭刚建的会话，避免泄漏连接句柄
                await self._close(_Session(owner, client))
                raise
            self.created += 1
            self.sessions[proxy] = current = _Session(owner, client)
            return current

    async def __call__(self, url: str, proxy: str | None, timeout: float) -> FetchResult:
        sess = await self._session(proxy, timeout)
        sess.requests += 1
        if sess.requests >= self.max_requests:
            sess.expired = True
        sess.inflight += 1
        try:
            page = await sess.client.get(url, timeout=timeout)
        finally:
            sess.inflight -= 1
            if sess.inflight == 0 and sess in self.retired:
                await self._close(sess)
        return FetchResult(int(page.status), str(page.url), bytes(page.body), redirect_history(page))

    def notify(self, proxy: str | None, outcome: str) -> None:
        sess = self.sessions.get(proxy)
        if sess is None or outcome == "timeout":
            return
        sess.block_streak = sess.block_streak + 1 if outcome == "access_restricted" else 0
        if sess.block_streak >= self.reset_after_blocks:
            sess.expired = True

    def describe(self) -> str:
        return f"sessions_created={self.created} retired_open={len(self.retired)}"

    async def _close(self, sess: _Session) -> None:
        if sess in self.retired:
            self.retired.remove(sess)
        try:
            await sess.owner.__aexit__(None, None, None)
        except Exception as exc:
            log.debug("关闭会话失败：%s", exc)

    async def aclose(self) -> None:
        for sess in [*self.sessions.values(), *self.retired]:
            await self._close(sess)
        self.sessions.clear()
        self.retired.clear()


# ---------------------------------------------------------------- 数据库与快照

def apply_crawl_schema(conn: psycopg.Connection) -> bool:
    """依次应用爬虫迁移（须先有 101）；已应用的跳过。返回本次是否应用了任何迁移。"""
    applied = False
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task2_schema'))")
        if not conn.execute("SELECT 1 FROM public.schema_migration WHERE version = '101'").fetchone():
            raise RuntimeError("任务 2 清单表结构（101）尚未应用")
        for version, name in MIGRATIONS:
            if not conn.execute("SELECT 1 FROM public.schema_migration WHERE version = %s", (version,)).fetchone():
                conn.execute((sql_dir() / name).read_text(encoding="utf-8"))
                applied = True
    return applied


def latest_full_extraction(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT extraction_id FROM task2.manifest_build WHERE scope = 'full' "
                       "ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("没有全量清单构建，请先运行任务 2 第一部分或用 --extraction-id 指定")
    return row[0]


@dataclass
class Attempt:
    attempt_no: int
    exit_label: str
    requested_url: str
    outcome: str
    reason: str
    started_at: datetime
    elapsed_ms: int
    http_status: int | None = None
    final_url: str | None = None
    body_bytes: int | None = None
    content_hash: str | None = None
    snapshot_path: str | None = None
    redirect_chain: list[dict] | None = None  # None：没有响应（异常）；[]：没有跳转
    page_template: str | None = None


def save_snapshot(snapshot_dir: Path, asin: str, body: bytes) -> str:
    """gzip 保存响应体；按 ASIN 末两位分目录，先写临时文件再原子替换。
    项目目录下的快照记相对 PROJECT_ROOT 的路径，命令行与 Airflow 的工作目录不同也指向同一文件。"""
    path = (snapshot_dir / asin[-2:] / f"{asin}.html.gz").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".gz.tmp")
    tmp.write_bytes(gzip.compress(body, compresslevel=6))
    os.replace(tmp, path)
    return str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT) else str(path)


def resolve_snapshot(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def record_result(conn: psycopg.Connection, eid: str, run_id: str, asin: str, attempts: list[Attempt]) -> None:
    """同一事务内插入尝试日志并更新清单爬虫列；最终状态取最后一次尝试。"""
    last = attempts[-1]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO task2.fetch_attempt (extraction_id, asin, attempt_no, run_id, exit_label, requested_url, "
                "final_url, http_status, outcome, reason, body_bytes, content_hash, snapshot_path, started_at, "
                "elapsed_ms, redirect_chain, page_template) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(eid, asin, a.attempt_no, run_id, a.exit_label, a.requested_url, a.final_url, a.http_status,
                  a.outcome, a.reason, a.body_bytes, a.content_hash, a.snapshot_path, a.started_at, a.elapsed_ms,
                  None if a.redirect_chain is None else json.dumps(a.redirect_chain), a.page_template)
                 for a in attempts])
        conn.execute(
            "UPDATE task2.product_manifest SET status = %s, final_url = %s, http_status = %s, "
            "attempt_count = attempt_count + %s, fetched_at = %s, snapshot_path = %s, content_hash = %s, "
            "status_reason = %s, updated_at = now() WHERE extraction_id = %s AND asin = %s",
            (last.outcome, last.final_url, last.http_status, len(attempts), last.started_at,
             last.snapshot_path, last.content_hash, last.reason, eid, asin))


# ---------------------------------------------------------------- 主循环

@dataclass
class CrawlConfig:
    snapshot_dir: Path = Path("outputs/task2/snapshots")
    concurrency: int = 4
    max_attempts: int = 2
    timeout: float = 30.0
    limit: int | None = None
    deadline_minutes: float | None = None
    retry_statuses: tuple[str, ...] = ()
    batch_size: int = 1000
    log_every: int = 500
    shuffle: bool = False   # 按 md5(asin) 的确定性乱序取行，避免按字母顺序逐个遍历
    jitter: float = 0.0     # 每次请求前额外随机等待 0~jitter 秒，打散固定节奏


@dataclass
class CrawlSummary:
    run_id: str
    status: str
    asin_done: int
    outcomes: dict[str, int] = field(default_factory=dict)


async def crawl(conn: psycopg.Connection, eid: str, cfg: CrawlConfig, pool: ExitPool,
                fetch: Fetch = scrapling_fetch, stop: asyncio.Event | None = None) -> CrawlSummary:
    stop = stop or asyncio.Event()
    run_id = uuid.uuid4().hex
    if not conn.execute("SELECT pg_try_advisory_lock(hashtext('etl_task2_crawl'), hashtext(%s))", (eid,)).fetchone()[0]:
        raise CrawlBusy("同一抽取已有爬虫在运行")
    params = {**{k: str(v) if isinstance(v, Path) else v for k, v in asdict(cfg).items()},
              "exits": [e.label for e in pool.exits]}
    conn.execute("INSERT INTO task2.crawl_run (run_id, extraction_id, params) VALUES (%s, %s, %s)",
                 (run_id, eid, json.dumps(params)))
    statuses = ["not_collected", *cfg.retry_statuses]
    if cfg.deadline_minutes:  # 定时置停止信号：等待出口冷却的 worker 也能及时退出
        asyncio.get_running_loop().call_later(cfg.deadline_minutes * 60, stop.set)
    queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.concurrency * 4)
    outcomes: Counter[str] = Counter()
    started = time.monotonic()

    notify = getattr(fetch, "notify", None)  # 会话型请求器据此决定是否换会话
    describe_fetch = getattr(fetch, "describe", lambda: "")

    def should_stop() -> bool:
        return stop.is_set()

    key = "md5(asin)" if cfg.shuffle else "asin COLLATE \"C\""
    batch_sql = ("SELECT asin, original_url, attempt_count, " + key + " FROM task2.product_manifest "
                 "WHERE extraction_id = %s AND status = ANY(%s) AND original_url IS NOT NULL "
                 "AND " + key + " > %s ORDER BY " + key + " LIMIT %s")

    async def producer() -> None:
        last, sent = "", 0
        try:
            while not should_stop() and (cfg.limit is None or sent < cfg.limit):
                size = cfg.batch_size if cfg.limit is None else min(cfg.batch_size, cfg.limit - sent)
                rows = conn.execute(batch_sql, (eid, statuses, last, size)).fetchall()
                if not rows:
                    break
                for row in rows:
                    if should_stop():
                        return
                    await queue.put(row[:3])
                last, sent = rows[-1][3], sent + len(rows)
        finally:
            for _ in range(cfg.concurrency):
                await queue.put(None)

    async def crawl_one(asin: str, url: str, prev: int) -> list[Attempt]:
        attempts: list[Attempt] = []
        body = None
        for i in range(cfg.max_attempts):
            try:
                ex = await pool.acquire(stop)
            except CrawlStopped:
                break
            if cfg.jitter:
                await asyncio.sleep(random.uniform(0, cfg.jitter))
            t0, at = time.monotonic(), datetime.now(timezone.utc)
            a = Attempt(prev + i + 1, ex.label, url, "timeout", "", at, 0)
            try:
                res = await asyncio.wait_for(fetch(url, ex.proxy, cfg.timeout), cfg.timeout + 10)
                a.outcome, a.reason = classify(asin, res.status, res.url, res.body)
                a.http_status = res.status if 100 <= res.status <= 599 else None
                a.final_url, a.body_bytes = res.url, len(res.body)
                a.redirect_chain = list(res.history)
                a.page_template = detect_template(res.url, res.status, res.body)
                a.content_hash = hashlib.sha256(res.body).hexdigest()
                body = res.body
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 超时、连接错误、代理错误
                a.reason = f"{type(exc).__name__}: {exc}"[:300]
                body = None
            a.elapsed_ms = int((time.monotonic() - t0) * 1000)
            pool.report(ex, a.outcome)
            if notify is not None:
                notify(ex.proxy, a.outcome)
            attempts.append(a)
            if a.outcome not in RETRYABLE:
                break
        if attempts and body is not None and attempts[-1].content_hash:
            attempts[-1].snapshot_path = save_snapshot(cfg.snapshot_dir, asin, body)
        return attempts

    async def worker() -> None:
        while (item := await queue.get()) is not None:
            if should_stop():  # 截止时间或停止信号：排队中的 ASIN 不再请求，保持原状态
                continue
            asin, url, prev = item
            attempts = await crawl_one(asin, url, prev)
            if not attempts:
                continue  # 停止时尚未发出请求：保持原状态，下次续跑
            record_result(conn, eid, run_id, asin, attempts)
            outcomes[attempts[-1].outcome] += 1
            done = sum(outcomes.values())
            if done % cfg.log_every == 0:
                per_min = done / max(time.monotonic() - started, 1e-6) * 60
                log.info("已完成 %d 个 ASIN，%.1f 个/分钟，%s | %s %s", done, per_min, dict(outcomes),
                         pool.describe(), describe_fetch())

    status, failure = "succeeded", None
    try:
        async with asyncio.TaskGroup() as tg:  # 任一任务出错时取消其余任务
            tg.create_task(producer())
            for _ in range(cfg.concurrency):
                tg.create_task(worker())
        if stop.is_set():
            status = "interrupted"
    except BaseException as exc:
        status, failure = "failed", f"{type(exc).__name__}: {exc}"[:1000]
        raise
    finally:
        conn.execute("UPDATE task2.crawl_run SET status = %s, asin_done = %s, outcome_counts = %s, "
                     "finished_at = now(), failure_reason = %s WHERE run_id = %s",
                     (status, sum(outcomes.values()), json.dumps(dict(outcomes)), failure, run_id))
        conn.execute("SELECT pg_advisory_unlock(hashtext('etl_task2_crawl'), hashtext(%s))", (eid,))
        log.info("运行 %s 结束：%s，共 %d 个 ASIN，%s | %s", run_id, status, sum(outcomes.values()),
                 dict(outcomes), pool.describe())
    return CrawlSummary(run_id, status, sum(outcomes.values()), dict(outcomes))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="任务 2 产品页面爬虫")
    p.add_argument("--extraction-id", help="默认取最近一次全量清单构建的抽取")
    p.add_argument("--limit", type=int, help="本次最多处理的 ASIN 数")
    p.add_argument("--deadline", type=float, help="运行分钟数上限，到时停止取新 ASIN")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--rate", type=float, default=1.0, help="每个出口每秒请求数上限")
    p.add_argument("--burst", type=float, default=3.0)
    p.add_argument("--global-rate", type=float, help="所有出口合计每秒请求数上限")
    p.add_argument("--max-attempts", type=int, default=1, help="每个 ASIN 每次运行的最多尝试次数；默认 1：先全量过一遍，被拦截的之后用 --retry-status 补爬")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--max-block-ratio", type=float, default=0.5,
                   help="出口窗口拦截率上限：超过则减速并冷却，低于其 80%% 则逐步提速")
    p.add_argument("--window", type=int, default=50, help="每个出口每多少次响应评估一次拦截率")
    p.add_argument("--ban-streak", type=int, default=10, help="连续被拦截多少次视为被封，立即减速冷却")
    p.add_argument("--cooldown", type=float, default=30.0, help="减速时的基础冷却秒数，连续超标时翻倍")
    p.add_argument("--max-cooldown", type=float, default=600.0)
    p.add_argument("--retry-status", action="append", default=[], choices=RETRYABLE + ("parse_failed",),
                   help="把已是该状态的 ASIN 也重新采集，可重复")
    p.add_argument("--snapshot-dir", default="outputs/task2/snapshots")
    p.add_argument("--log-every", type=int, default=500, help="每完成多少个 ASIN 打印一行进度")
    p.add_argument("--no-session", action="store_true", help="不维持 cookie 会话，每个请求都是新访客（用于对比）")
    p.add_argument("--session-max-requests", type=int, default=300, help="一个会话最多发多少请求后换新会话")
    p.add_argument("--session-reset-blocks", type=int, default=3, help="会话连续被拦截几次后换新会话")
    p.add_argument("--order", choices=("shuffle", "asin"), default="shuffle", help="取 ASIN 的顺序")
    p.add_argument("--jitter", type=float, default=1.0, help="每次请求前随机等待 0~jitter 秒")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Scrapling 首次导入该模块时把自己的 logger 设为 INFO 并加 handler（scrapling.fetchers 是惰性导入，不会触发），先导入再静音
    import scrapling.core.utils._utils  # noqa: F401
    scrapling_log = logging.getLogger("scrapling")
    scrapling_log.setLevel(logging.CRITICAL)  # 每次请求与失败都已写入 fetch_attempt，不再逐条刷屏
    scrapling_log.propagate = False

    pool = ExitPool(load_exits(os.environ), rate=args.rate, burst=args.burst, global_rate=args.global_rate,
                    base_cooldown=args.cooldown, max_cooldown=args.max_cooldown,
                    max_block_ratio=args.max_block_ratio, window=args.window, ban_streak=args.ban_streak)
    cfg = CrawlConfig(snapshot_dir=Path(args.snapshot_dir), concurrency=args.concurrency,
                      max_attempts=args.max_attempts, timeout=args.timeout, limit=args.limit,
                      deadline_minutes=args.deadline, retry_statuses=tuple(args.retry_status),
                      log_every=args.log_every, shuffle=args.order == "shuffle", jitter=args.jitter)

    async def run() -> CrawlSummary:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)  # 收尾当前请求后退出，未开始的 ASIN 保持原状态
        with connect(load_settings()) as conn:
            apply_crawl_schema(conn)
            eid = args.extraction_id or latest_full_extraction(conn)
            log.info("抽取 %s，出口 %s，会话 %s，参数 %s", eid[:12], [e.label for e in pool.exits],
                     "关闭" if args.no_session else "开启", asdict(cfg))
            if args.no_session:
                return await crawl(conn, eid, cfg, pool, stop=stop)
            fetcher = SessionFetcher(max_requests=args.session_max_requests,
                                     reset_after_blocks=args.session_reset_blocks)
            try:
                return await crawl(conn, eid, cfg, pool, fetch=fetcher, stop=stop)
            finally:
                await fetcher.aclose()

    print(json.dumps(asdict(asyncio.run(run())), ensure_ascii=False))


if __name__ == "__main__":
    main()
