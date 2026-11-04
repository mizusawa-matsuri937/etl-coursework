"""原始层 build_sources（spec R06–R10）：登记四类源记录、获取时间及其来源列、快照哈希核验。

- 页面（page_snapshot）：参与匹配商品的快照。定位取任务 2 清单（抽取、ASIN、最终地址、快照路径、正文哈希）、保留快照的
  那次尝试序号（task2.fetch_attempt）与任务 4 页面源记录 ID；获取时间取 fetched_at。每批先在事务外逐个解压快照重算
  正文 SHA-256（线程池并行读文件），再与该批源记录一起写入：缺失写 snapshot_missing，不一致写
  snapshot_hash_mismatch，并把 evidence_status 标出（R09）。
- IMDb（imdb_row）：被下游引用的作品、别名、导演（title.crew 一行）、主要演职员与人名行（sources 模块定义的集合）；
  定位为（快照、文件、文件 SHA-256、行号、tconst 或 nconst），获取时间取 imdb.snapshot.downloaded_at（H06）。
- 评论（review_record）：只登记推断上界依赖的评论（R10、R22）；定位为任务 1 的记录序号、行号、字节偏移与记录哈希，
  文件哈希与获取时间取 public.source_file。
- 人工修订（manual_revision）：任务 4、5 的 human_decision（正式库 0 行，H07）。
获取时间取不到时保留 NULL 并写 missing_acquired_at，不用其他时间代替（R07）。
全部 INSERT … SELECT 在库内完成，ID 由 rules.sql_sr_id 生成；同一定位重复出现时 ON CONFLICT DO NOTHING（幂等）。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

import psycopg

from . import sources
from .rules import sql_sr_id, resolve_snapshot, snapshot_status
from .run import DEFAULT_BATCH, BeforeCommit, RunContext, Unit, add_issues, process_units, register_stage_key

STAGE = "sources"
COLUMNS = ("stage_key, source_record_id, source_kind, locator, locator_key, content_hash, hash_scope, acquired_at, "
           "acquired_at_basis, upstream_table, upstream_key, evidence_status, run_id")


def default_snapshot_root() -> Path:
    """任务 2 记录快照路径时相对的项目根目录（容器内为 /opt/etl）；可用 ETL_SNAPSHOT_ROOT 覆盖。"""
    env = os.environ.get("ETL_SNAPSHOT_ROOT")
    if env:
        return Path(env)
    from etl_task2.crawl.crawler import PROJECT_ROOT
    return PROJECT_ROOT


def _insert(conn: psycopg.Connection, select_sql: str, params: dict[str, Any]) -> list[tuple]:
    """执行 INSERT … SELECT，返回新插入行的 (source_record_id, acquired_at, evidence_status, locator)。"""
    return conn.execute(f"INSERT INTO task9.source_record ({COLUMNS}) {select_sql} ON CONFLICT DO NOTHING "
                        "RETURNING source_record_id, acquired_at, evidence_status, locator", params).fetchall()


def _missing_acquired(rows: list[tuple], kind: str) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [("source_record", r[0], "missing_acquired_at", {"source_kind": kind}) for r in rows if r[1] is None]


# ---------------------------------------------------------------- 页面快照

PAGE_SQL = f"""
SELECT %(sk)s, {sql_sr_id('k.lk')}, 'page_snapshot',
       jsonb_build_object('extraction_id', m.extraction_id, 'asin', m.asin, 'final_url', m.final_url,
                          'snapshot_path', m.snapshot_path, 'attempt_no', a.attempt_no, 'content_hash', m.content_hash,
                          'task4_source_record_id', s.source_record_id),
       k.lk, m.content_hash, 'page_body_sha256', m.fetched_at,
       CASE WHEN m.fetched_at IS NOT NULL THEN 'task2.product_manifest.fetched_at' END,
       'task2.product_manifest', m.extraction_id || '|' || m.asin, v.status, %(run)s
FROM unnest(%(asins)s::text[], %(statuses)s::text[]) AS v(asin, status)
JOIN task2.product_manifest m ON m.extraction_id = %(eid)s AND m.asin = v.asin
CROSS JOIN LATERAL (SELECT 'page|' || m.extraction_id || '|' || m.asin || '|' || m.content_hash AS lk) k
LEFT JOIN LATERAL (SELECT max(f.attempt_no) AS attempt_no FROM task2.fetch_attempt f
                   WHERE f.extraction_id = m.extraction_id AND f.asin = m.asin AND f.snapshot_path IS NOT NULL) a ON true
LEFT JOIN task4.source_record s ON s.source_type = 'page'
     AND s.locator_key = 'page:' || m.extraction_id || ':' || m.asin || ':' || m.content_hash
"""


def _page_unit(conn: psycopg.Connection, ctx: RunContext, sk: str, root: Path, workers: int) -> Unit:
    i = ctx.inputs

    def prepare(batch: Sequence[str]) -> dict[str, tuple[str, dict[str, Any]]]:
        rows = conn.execute("SELECT asin, snapshot_path, content_hash FROM task2.product_manifest "
                            "WHERE extraction_id = %s AND asin = ANY(%s)", (i.extraction_id, list(batch))).fetchall()

        def check(row: tuple) -> tuple[str, tuple[str, dict[str, Any]]]:
            asin, path, digest = row
            if not path:
                return asin, ("snapshot_missing", {"path": None})
            return asin, snapshot_status(resolve_snapshot(path, root), digest)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return dict(pool.map(check, rows))

    def write(batch: Sequence[str], checked: dict[str, tuple[str, dict[str, Any]]]) -> int:
        asins = [a for a in batch if a in checked]
        inserted = _insert(conn, PAGE_SQL, {"sk": sk, "run": ctx.run_id, "eid": i.extraction_id, "asins": asins,
                                            "statuses": [checked[a][0] for a in asins]})
        issues = _missing_acquired(inserted, "page_snapshot")
        for sr, _acq, status, loc in inserted:
            if status != "ok":
                issues.append(("source_record", sr, status, {"asin": loc["asin"], **checked[loc["asin"]][1]}))
        add_issues(conn, ctx, sk, issues)
        return len(inserted)

    return Unit("page", sources.page_asins(conn, i), write, prepare)


# ---------------------------------------------------------------- IMDb 文件行

def _imdb_select(table: str, file_name: str, alias: str, locator: str, upstream_key: str, line: str,
                 where: str, *, group: str = "") -> str:
    lk = f"'imdb|' || {alias}.snapshot_id || '|{file_name}|' || {line}"
    return f"""
SELECT %(sk)s, {sql_sr_id(lk)}, 'imdb_row',
       jsonb_build_object('snapshot', {alias}.snapshot_id, 'file', '{file_name}', 'file_sha256', min(f.sha256),
                          'line_no', {line}, 'table', '{table}') || {locator},
       {lk}, min(f.sha256), 'imdb_file_sha256', min(sn.downloaded_at), 'imdb.snapshot.downloaded_at',
       '{table}', {upstream_key}, 'not_checked', %(run)s
FROM {table} {alias}
JOIN imdb.snapshot sn ON sn.snapshot_id = {alias}.snapshot_id
LEFT JOIN imdb.source_file f ON f.snapshot_id = {alias}.snapshot_id AND f.file_name = '{file_name}'
WHERE {alias}.snapshot_id = %(snap)s AND {where}
GROUP BY {alias}.snapshot_id, {group or line}
"""


TITLE_SQL = _imdb_select("imdb.title", "title.basics.tsv.gz", "t", "jsonb_build_object('tconst', t.tconst)",
                         "t.snapshot_id || '|' || t.tconst", "t.line_no", "t.tconst = ANY(%(titles)s)",
                         group="t.tconst, t.line_no")
CREW_SQL = _imdb_select("imdb.title_director", "title.crew.tsv.gz", "d",
                        "jsonb_build_object('tconst', d.tconst, 'nconsts', array_agg(d.nconst ORDER BY d.ordinal))",
                        "d.snapshot_id || '|' || d.tconst", "d.line_no", "d.tconst = ANY(%(credits)s)",
                        group="d.tconst, d.line_no")
PRINCIPAL_SQL = _imdb_select("imdb.title_principal", "title.principals.tsv.gz", "p",
                             "jsonb_build_object('tconst', p.tconst, 'ordering', p.ordering, 'nconst', p.nconst)",
                             "p.snapshot_id || '|' || p.tconst || '|' || p.ordering", "p.line_no",
                             "p.tconst = ANY(%(credits)s)", group="p.tconst, p.ordering, p.nconst, p.line_no")
AKA_SQL = _imdb_select("imdb.title_aka", "title.akas.tsv.gz", "k",
                       "jsonb_build_object('tconst', k.tconst, 'ordering', k.ordering)",
                       "k.snapshot_id || '|' || k.tconst || '|' || k.ordering", "k.line_no",
                       "(k.tconst, k.line_no) IN (SELECT * FROM unnest(%(aka_t)s::text[], %(aka_l)s::bigint[]))",
                       group="k.tconst, k.ordering, k.line_no")
NAME_SQL = _imdb_select("imdb.name", "name.basics.tsv.gz", "n", "jsonb_build_object('nconst', n.nconst)",
                        "n.snapshot_id || '|' || n.nconst", "n.line_no", "n.nconst = ANY(%(nconsts)s)",
                        group="n.nconst, n.line_no")


def _imdb_units(conn: psycopg.Connection, ctx: RunContext, sk: str) -> list[Unit]:
    i = ctx.inputs
    titles = sources.title_tconsts(conn, i)
    credits = sources.credit_tconsts(conn, i)
    akas = sources.aka_lines(conn, i)
    base = {"sk": sk, "run": ctx.run_id, "snap": i.imdb_snapshot_id}

    def write_titles(batch: Sequence[str], _: Any) -> int:
        t = [x for x in batch if x in titles]
        c = [x for x in batch if x in credits]
        pairs = [(x, line) for x in batch for line in akas.get(x, ())]
        p = {**base, "titles": t, "credits": c, "aka_t": [x for x, _ in pairs], "aka_l": [y for _, y in pairs]}
        inserted = []
        for sql in (TITLE_SQL, CREW_SQL, PRINCIPAL_SQL, AKA_SQL):
            inserted += _insert(conn, sql, p)
        add_issues(conn, ctx, sk, _missing_acquired(inserted, "imdb_row"))
        return len(inserted)

    def write_names(batch: Sequence[str], _: Any) -> int:
        inserted = _insert(conn, NAME_SQL, {**base, "nconsts": list(batch)})
        add_issues(conn, ctx, sk, _missing_acquired(inserted, "imdb_row"))
        return len(inserted)

    return [Unit("imdb_title", sorted(titles | credits | set(akas)), write_titles),
            Unit("imdb_name", sources.name_nconsts(conn, i, credits), write_names)]


# ---------------------------------------------------------------- 评论记录

REVIEW_SQL = f"""
SELECT DISTINCT ON (r.source_review_id) %(sk)s, {sql_sr_id("'review|' || r.extraction_id || '|' || r.source_review_id")},
       'review_record',
       jsonb_build_object('extraction_id', r.extraction_id, 'source_file_sha256', sf.measured_sha256,
                          'source_review_id', r.source_review_id, 'record_no', r.record_no,
                          'start_line', r.start_line, 'end_line', r.end_line, 'start_offset', r.start_offset,
                          'end_offset', r.end_offset, 'byte_size', r.byte_size, 'raw_sha256', r.raw_sha256),
       'review|' || r.extraction_id || '|' || r.source_review_id, r.raw_sha256, 'review_record_sha256',
       sf.download_completed_at,
       CASE WHEN sf.download_completed_at IS NOT NULL THEN 'public.source_file.download_completed_at' END,
       'public.review_record', r.extraction_id || '|' || r.source_review_id, 'not_checked', %(run)s
FROM ({sources.REVIEW_DEPENDENCIES_SQL}) dep
JOIN public.review_record r ON r.extraction_id = %(eid)s AND r.source_review_id = dep.source_review_id
JOIN public.extraction e ON e.extraction_id = r.extraction_id
JOIN public.source_file sf ON sf.source_file_id = e.source_file_id
ORDER BY r.source_review_id
"""


def _review_unit(conn: psycopg.Connection, ctx: RunContext, sk: str) -> Unit:
    i = ctx.inputs

    def write(batch: Sequence[str], _: Any) -> int:
        inserted = _insert(conn, REVIEW_SQL, {"sk": sk, "run": ctx.run_id, **sources.review_params(i, list(batch))})
        add_issues(conn, ctx, sk, _missing_acquired(inserted, "review_record"))
        return len(inserted)

    return Unit("review", sources.review_bound_movies(conn, i), write)


# ---------------------------------------------------------------- 人工修订

def _manual_sql(table: str) -> str:
    lk = f"'manual|{table}|' || h.human_decision_id"
    return f"""
SELECT %(sk)s, {sql_sr_id(lk)}, 'manual_revision',
       jsonb_build_object('table', '{table}', 'human_decision_id', h.human_decision_id, 'kind', h.kind,
                          'left_entity', h.left_entity, 'right_entity', h.right_entity, 'annotator', h.annotator,
                          'set_hash', h.set_hash, 'import_file_sha256', h.import_file_sha256),
       {lk}, h.import_file_sha256, 'import_file_sha256', h.decided_at, 'human_decision.decided_at',
       '{table}', h.human_decision_id::text, 'not_checked', %(run)s
FROM {table} h WHERE h.extraction_id = %(eid)s
"""


def _manual_unit(conn: psycopg.Connection, ctx: RunContext, sk: str) -> Unit:
    def write(batch: Sequence[str], _: Any) -> int:
        inserted = []
        for table in batch:
            inserted += _insert(conn, _manual_sql(table), {"sk": sk, "run": ctx.run_id,
                                                           "eid": ctx.inputs.extraction_id})
        add_issues(conn, ctx, sk, _missing_acquired(inserted, "manual_revision"))
        return len(inserted)

    return Unit("manual", ["task4.human_decision", "task5.human_decision"], write)


# ---------------------------------------------------------------- 阶段入口

def build_sources(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                  snapshot_root: str | Path | None = None, snapshot_workers: int = 8,
                  before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """登记全部源记录（调用方已持锁并建好运行）。返回各单元计数与按种类、证据状态的汇总。"""
    sk = register_stage_key(conn, ctx, STAGE)
    root = Path(snapshot_root) if snapshot_root is not None else default_snapshot_root()
    units = [_page_unit(conn, ctx, sk, root, snapshot_workers), *_imdb_units(conn, ctx, sk),
             _review_unit(conn, ctx, sk), _manual_unit(conn, ctx, sk)]
    counts: dict[str, Any] = {"stage_key": sk, "units": process_units(conn, ctx, STAGE, sk, units,
                                                                      batch_size=batch_size,
                                                                      before_commit=before_commit)}
    counts.update(summary(conn, sk))
    return counts


def summary(conn: psycopg.Connection, sk: str) -> dict[str, Any]:
    """按种类与证据状态的源记录数，以及该阶段键下的质量问题数。"""
    by_kind = dict(conn.execute("SELECT source_kind, count(*) FROM task9.source_record WHERE stage_key = %s "
                                "GROUP BY 1 ORDER BY 1", (sk,)).fetchall())
    by_evidence = dict(conn.execute("SELECT evidence_status, count(*) FROM task9.source_record WHERE stage_key = %s "
                                    "AND source_kind = 'page_snapshot' GROUP BY 1 ORDER BY 1", (sk,)).fetchall())
    by_table = dict(conn.execute("SELECT upstream_table, count(*) FROM task9.source_record WHERE stage_key = %s "
                                 "GROUP BY 1 ORDER BY 1", (sk,)).fetchall())
    issues = dict(conn.execute("SELECT issue_code, count(*) FROM task9.quality_issue WHERE stage_key = %s "
                               "GROUP BY 1 ORDER BY 1", (sk,)).fetchall())
    return {"by_kind": by_kind, "page_evidence": by_evidence, "by_table": by_table, "issues": issues}
