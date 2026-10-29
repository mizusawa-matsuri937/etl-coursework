"""任务 3 集成测试辅助：在 etl_test 里装一个小抽取、建清单，并用 SQL 模拟任务 2 的爬取结果与快照。"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg

import build_fixtures as bf
from etl_task1.loader import load_extraction
from etl_task1.source import verify_source
from etl_task2.crawl.crawler import apply_crawl_schema
from etl_task2.manifest import build_manifest
from etl_task3.classify import apply_task3_schema
from task3.pages import CASES

# 输入组 → (清单状态, HTTP 状态, 页面模板, 最终域名)
GROUP_RESULT = {
    "dp_page": ("success", 200, "dp_product_title", "www.amazon.com"),
    "automation_title": ("success", 200, "automation_title", "www.amazon.com"),
    "other_site": ("success", 200, "dp_product_title", "www.amazon.co.jp"),
    "access_restricted": ("access_restricted", 200, "captcha", "www.amazon.com"),
    "not_found": ("not_found", 404, "not_found", "www.amazon.com"),
    "timeout": ("timeout", None, None, None),
    "parse_failed": ("parse_failed", 200, "other", "www.amazon.com"),
}


@dataclass
class Product:
    asin: str
    group: str
    body: bytes | None = None
    reviews: int = 1


@dataclass
class Fixture:
    eid: str
    snapshot_dir: Path
    products: list[Product]
    records_read: int
    snapshots: dict[str, Path] = field(default_factory=dict)


def asin(i: int) -> str:
    return f"B0T3{i:06d}"


def standard_products() -> list[Product]:
    """AC01 的 10 行：dp_page 3 行（结构化电影、剧集、只有标题），其余七组各 1 行。"""
    pages = {c[0]: c[1] for c in CASES}
    out = [Product(asin(1), "dp_page", pages["structured_movie"], reviews=2),
           Product(asin(2), "dp_page", pages["tv_genre"]),
           Product(asin(3), "dp_page", pages["title_only_dvd"])]
    for i, g in enumerate(("automation_title", "other_site", "access_restricted", "not_found", "timeout",
                           "parse_failed", "not_collected"), start=4):
        out.append(Product(asin(i), g, b"<html><span id='productTitle'>Other</span></html>"
                           if g in GROUP_RESULT and GROUP_RESULT[g][2] else None))
    return out


def setup(conn: psycopg.Connection, make_settings, tmp_path: Path, products: list[Product],
          name: str = "a") -> Fixture:
    """记录：每个产品 reviews 条有效记录，外加 1 条无产品标识的记录。name 区分同一测试里的多个抽取。"""
    records = []
    for i, p in enumerate(products):
        records += [bf.record(product=p.asin.encode(), user=f"U{i}_{j}".encode(), time=b"1000000000")
                    for j in range(p.reviews)]
    records.append(bf.record(product=None, user=b"NO_PRODUCT"))
    src = tmp_path / f"t3src_{name}"
    src.mkdir(parents=True, exist_ok=True)
    gz = bf.gzip_bytes(bf.build_bytes(records))
    (src / "s.txt.gz").write_bytes(gz)
    bf.write_manifest(src / "s.manifest.json", gz)
    settings = make_settings(source_gzip_path=src / "s.txt.gz", source_manifest_path=src / "s.manifest.json")
    eid = load_extraction(settings, verify_source(src / "s.txt.gz", src / "s.manifest.json"), conn).extraction_id
    build_manifest(conn, eid, allow_sample=True)
    apply_crawl_schema(conn)
    apply_task3_schema(conn)
    fx = Fixture(eid, tmp_path / f"snapshots_{name}", products, len(records))
    simulate_crawl(conn, fx)
    return fx


def simulate_crawl(conn: psycopg.Connection, fx: Fixture) -> None:
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    with conn.transaction():
        conn.execute("INSERT INTO task2.crawl_run (run_id, extraction_id, status, params, finished_at) "
                     "VALUES ('t3fixture', %s, 'succeeded', '{}', %s) ON CONFLICT DO NOTHING", (fx.eid, now))
        for p in fx.products:
            if p.group == "not_collected":
                continue
            status, http, template, host = GROUP_RESULT[p.group]
            url = f"https://www.amazon.com/dp/{p.asin}"
            final = f"https://{host}/dp/{p.asin}" if host else None
            path = digest = None
            if p.body is not None:
                path = fx.snapshot_dir / p.asin[-2:] / f"{p.asin}.html.gz"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(p.body))
                digest = hashlib.sha256(p.body).hexdigest()
                fx.snapshots[p.asin] = path
            conn.execute(
                "INSERT INTO task2.fetch_attempt (extraction_id, asin, attempt_no, run_id, exit_label, requested_url, "
                "final_url, http_status, outcome, reason, body_bytes, content_hash, snapshot_path, started_at, "
                "elapsed_ms, redirect_chain, page_template) VALUES (%s, %s, 1, 't3fixture', 'fixture', %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, 1, %s, %s)",
                (fx.eid, p.asin, url, final, http, status, "fixture", len(p.body) if p.body else None, digest,
                 str(path) if path else None, now, "[]" if http else None, template))
            conn.execute(
                "UPDATE task2.product_manifest SET status = %s, final_url = %s, http_status = %s, attempt_count = 1, "
                "fetched_at = %s, snapshot_path = %s, content_hash = %s, status_reason = 'fixture', updated_at = %s "
                "WHERE extraction_id = %s AND asin = %s",
                (status, final, http, now, str(path) if path else None, digest, now, fx.eid, p.asin))


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    tables = ["public.review_record", "public.review_staging", "public.quarantine_record", "public.quality_issue",
              "public.extraction", "task2.product_manifest", "task2.fetch_attempt", "task2.crawl_run",
              "task2.manifest_build", "task2.manifest_run"]
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def task3_counts(conn: psycopg.Connection, eid: str) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM task3.{t} WHERE extraction_id = %s", (eid,)).fetchone()[0]
            for t in ("page_parse", "page_field", "product_classification", "classification_evidence",
                      "quality_issue")}
