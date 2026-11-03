r"""任务 9 集成测试辅助：在 etl_test 里造出与正式库同形的任务 1–8 上游结果，再按需运行任务 9 的阶段。

上游结果的来源（tests/fixtures/task9/README.md 有逐表说明）：
- 任务 3–6 与 IMDb：`_task7_helpers.seed`（任务 7 的夹具写法，已与正式库对照过形状；类型断言由任务 4 的
  genre_tags 生成、姓名键由任务 5 的 names.parse 生成）。
- 任务 7：`etl_task7.runner.execute_conflict` 真实运行两次（规则 t7r3 → cv0001、t7r1 → cv0002），field_candidate、
  movie_resolved、movie_title_alias 等全部由任务 7 自己的代码写入。
- 任务 1 评论：`_task8_helpers.load` 用任务 1 的装载器装入合成评论（另一个抽取，作为“样本抽取”干扰），再把
  review_record、review_staging 及其批次行原样复制到任务 7 夹具的抽取下，并让该抽取指向装载器登记的来源文件
  （download_completed_at 来自合成 manifest）。
- 任务 2 采集证据：按 `_task3_helpers.simulate_crawl` 的写法补 crawl_run 与 fetch_attempt；快照文件写在 tmp 目录，
  正文为 ASIN 字节（任务 7 夹具登记的正文哈希即 sha256(ASIN)），所以重算哈希与登记值一致。
- IMDb 来源文件：imdb.source_file 五个文件各一行（合成 SHA-256；真实装载器写入的列与约束相同）。
- 任务 4 人工决策：`etl_task4.review.import_decisions`（任务 4 自己的导入函数）。
- 任务 8 版本：dedup_version 两行（rd0001 基于 cv0001 且 verified，rd0002 基于 cv0002），连同 task8.run 与四个阶段键
  （任务 9 的 T9-01 只读这几行元数据；评论层视图的测试在 T9-03 另用任务 8 的真实运行）。

上游表的写入只存在于测试里（etl_test）；任务 9 代码只读这些表。
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import _task7_helpers as h7
import _task8_helpers as h8
from _task7_worlds import rich_world
from etl_task4.review import HumanRow, import_decisions
from etl_task4.run import RunInputs as T4Inputs
from etl_task7.runner import execute_conflict
from etl_task8.run import apply_task8_schema
from etl_task9.run import RunInputs, apply_task9_schema
from etl_task9.runner import run_stages
from task7 import world as w7
from task8.world import Rec, unix

EID = h7.EXTRACTION
SNAPSHOT = h7.SNAPSHOT
DOWNLOADED = h7.DOWNLOADED
SOURCE_DOWNLOADED = "2026-10-21T02:25:40+00:00"
CRAWLED = datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc)
IMDB_FILES = ("title.basics.tsv.gz", "title.akas.tsv.gz", "title.crew.tsv.gz", "title.principals.tsv.gz",
              "name.basics.tsv.gz")
UPSTREAM_SCHEMAS = ("public", "task2", "task3", "imdb", "task4", "task5", "task6", "task7", "task8")


def legend_cases() -> tuple[list, list]:
    """仿正式库 Legend 的作品 Lg：IMDb 1985 被采用、页面标题年份 1986 被拒；推断上界来自评论（1999-08-23）。
    商品：B0Lg（标题与 IMDb 相同 → adopted，标题带年份，任务 4 拆出有父断言）、B0LgB（标题是 IMDb 别名 →
    superseded_by_specific）、B0LgD（无关标题 → rejected）三个 edition 商品，与含它的合集 B0LgC（合集评论不进作品）。
    返回 (cases, 额外商品)。"""
    lg = h7.case("Lg", "imdb_t4_link", imdb_title="Legend Picture", imdb_year=1985, title_years=(1986,),
                 page_title="Legend Picture (1986) [DVD]", akas=(("Legend Of Darkness", "US", "en"),),
                 release_extra=dict(inferred=("1999-08-23", "day", "review")))
    extra = [w7.Ed("B0LgB", "Legend Of Darkness [VHS]", movie="Lg", runtime=((100, "detail_bullets"),)),
             w7.Ed("B0LgD", "Unicorn Chase Adventure [DVD]", movie="Lg"),
             w7.Ed("B0LgC", "Legend Picture / Other Picture (Double Feature)", kind="collection",
                   members=((1, "Lg", "Legend Picture"),))]
    return [lg], extra


def t9_world() -> w7.World:
    """任务 7 的 rich_world（各类标量冲突、多值并集、署名对齐、类型）加上 Legend 案例与干扰版本。"""
    world = rich_world()
    cases, extra = legend_cases()
    for imdb, movie, ed in cases:
        world.imdb.append(imdb)
        world.movies.append(movie)
        world.editions.append(ed)
    world.editions += extra
    world.decoys = True              # 其他映射、上映、规范化、解析版本与旧 IMDb 快照的干扰行（AC01）
    return world


def t9_reviews() -> list[Rec]:
    """评论：Legend 三个 edition 商品共 6 条（最早 1999-08-23 在 B0Lg），合集 1 条，其他商品若干。"""
    legend_day = (datetime(1999, 8, 23, tzinfo=timezone.utc) - datetime(2007, 6, 25, tzinfo=timezone.utc)).days
    recs = [Rec("B0Lg", "LU1", time=unix(legend_day), text="earliest legend review"),
            Rec("B0Lg", "LU2", time=unix(legend_day + 3), text="second legend review"),
            Rec("B0Lg", "LU3", time=unix(legend_day + 40), text="third legend review"),
            Rec("B0LgB", "LU4", time=unix(legend_day + 10), text="vhs legend review"),
            Rec("B0LgB", "LU5", time=unix(legend_day + 90), text="another vhs review"),
            Rec("B0LgC", "LU6", time=unix(legend_day - 100), text="collection review is earlier but excluded")]
    recs += [Rec("B0LgD", "LU7", time=unix(legend_day + 200), text="unicorn edition review"),
             Rec("B0Y1", "YU1", text="y1 review"), Rec("B0Sam", "SU1", text="samurai review")]
    return recs


@dataclass
class T9:
    eid: str
    cv: str                              # 任务 9 默认读的冲突消解版本（t7r3，相当于正式库 cv0003）
    other_cv: str                        # 干扰：另一条规则版本的 cv
    rd: str = "rd0001"
    snapshot_root: Path = Path("/nonexistent")
    review_eid: str = ""                 # 装载器生成的另一个抽取（干扰）
    world: w7.World = field(default_factory=w7.World)

    def inputs(self, **over: Any) -> RunInputs:
        base = dict(extraction_id=self.eid, conflict_version_id=self.cv, review_dedup_version_id=self.rd,
                    t4_mapping_version_id=h7.T4, t5_person_mapping_version_id=h7.PMAP, t6_release_version_id=h7.RV,
                    imdb_snapshot_id=SNAPSHOT)
        return RunInputs(**{**base, **over})

    def mid(self, name: str) -> str:
        return w7.movie_id(name)


def reset(conn: psycopg.Connection) -> None:
    """清空 task9（不随 conn 夹具清空）；task8 表结构按需建好（seed 会写 dedup_version）。"""
    conn.execute("DROP SCHEMA IF EXISTS task9 CASCADE")
    conn.execute("DELETE FROM public.schema_migration WHERE version = '801'")


def seed(conn: psycopg.Connection, make_settings, tmp_path: Path, world: w7.World | None = None, *,
         reviews: list[Rec] | None = None, human: bool = True) -> T9:
    reset(conn)
    world = world if world is not None else t9_world()
    s = h7.seed(conn, world)                    # 任务 3–6 与 IMDb（清空并重建 task3–task7）
    _title_children(conn, "B0Lg")
    apply_task8_schema(conn)
    conn.execute("TRUNCATE task8.run, task8.stage_key CASCADE")
    cv = execute_conflict(conn, s.inputs(), batch_size=50).conflict_version_id
    other = execute_conflict(conn, s.inputs(rule_version="t7r1"), batch_size=50).conflict_version_id
    review_eid = _copy_reviews(conn, make_settings, tmp_path, reviews if reviews is not None else t9_reviews())
    root = tmp_path / "t9root"
    _crawl(conn, root, world)
    _imdb_files(conn)
    if human:
        _human(conn)
    _dedup_versions(conn, [(cv, "rd0001", "verified"), (other, "rd0002", "verified")])
    apply_task9_schema(conn)
    return T9(EID, cv, other, "rd0001", root, review_eid, world)


def _copy_reviews(conn: psycopg.Connection, make_settings, tmp_path: Path, recs: list[Rec]) -> str:
    """任务 1 装载器装入合成评论（抽取 E2），再把记录行复制到 EID 下并让 EID 指向同一来源文件。"""
    src = tmp_path / "t9reviews"
    src.mkdir(parents=True, exist_ok=True)
    import build_fixtures as bf
    from etl_task1.loader import load_extraction
    from etl_task1.source import verify_source
    gz = bf.gzip_bytes(bf.build_bytes([r.lines() for r in recs]))
    (src / "s.txt.gz").write_bytes(gz)
    bf.write_manifest(src / "s.manifest.json", gz)
    manifest = json.loads((src / "s.manifest.json").read_text())
    manifest["download_completed_at"] = SOURCE_DOWNLOADED
    (src / "s.manifest.json").write_text(json.dumps(manifest))
    settings = make_settings(source_gzip_path=src / "s.txt.gz", source_manifest_path=src / "s.manifest.json",
                             scope="sample", sample_max_records=10_000)
    e2 = load_extraction(settings, verify_source(src / "s.txt.gz", src / "s.manifest.json"), conn).extraction_id
    with conn.transaction():
        conn.execute("UPDATE public.extraction SET source_file_id = (SELECT source_file_id FROM public.extraction "
                     "WHERE extraction_id = %s) WHERE extraction_id = %s", (e2, EID))
        conn.execute("INSERT INTO public.etl_run (run_id, extraction_id, program_version, started_at, finished_at, status) "
                     "SELECT run_id || '_t9', %s, program_version, started_at, finished_at, status FROM public.etl_run "
                     "WHERE extraction_id = %s", (EID, e2))
        conn.execute("INSERT INTO public.etl_batch SELECT %s, batch_no, first_record_no, last_record_no, resume_offset, "
                     "resume_line, valid_count, quarantined_count, byte_count, batch_sha256, run_id || '_t9', committed_at "
                     "FROM public.etl_batch WHERE extraction_id = %s", (EID, e2))
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'review_record' ORDER BY ordinal_position")]
        conn.execute(f"INSERT INTO public.review_record ({', '.join(cols)}) SELECT %s, "
                     f"{', '.join(cols[1:])} FROM public.review_record WHERE extraction_id = %s", (EID, e2))
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'review_staging' ORDER BY ordinal_position")]
        conn.execute(f"INSERT INTO public.review_staging ({', '.join(cols)}) SELECT %s, "
                     f"{', '.join(cols[1:])} FROM public.review_staging WHERE extraction_id = %s", (EID, e2))
    return e2


def _title_children(conn: psycopg.Connection, asin: str) -> None:
    """任务 4 由标题拆出的有父断言（说明段、说明带来的版本属性、标题年份）：用任务 4 自己的 _title_block 生成，
    父断言是夹具已写入的标题断言，来源记录与定位与父断言相同（与正式库 8,015 + 995 条有父断言同形）。"""
    from etl_task4.editions import _Builder, _title_block
    row = conn.execute("SELECT assertion_id, raw_value, source_record_id, source_locator FROM task4.field_assertion "
                       "WHERE extraction_id = %s AND entity_id = %s AND field_name = 'title' AND parser_version = 't3p1' "
                       "AND norm_version = 't4n1'", (EID, asin)).fetchone()
    if row is None:
        return
    parent_id, raw, rec, loc = row
    b = _Builder(asin)
    title = _title_block(b, asin, "product_edition", raw, loc)
    for a in b.out.assertions:
        if a.key == title.key:
            continue
        assert a.parent == title.key
        conn.execute(
            "INSERT INTO task4.field_assertion (extraction_id, parser_version, norm_version, entity_type, entity_id, "
            "field_name, ordinal, raw_value, normalized_value, qualifier, extra, source_record_id, source_locator, "
            "parent_assertion_id, rule_id) VALUES (%s, 't3p1', 't4n1', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (EID, a.entity_type, a.entity_id, a.field_name, a.ordinal, a.raw_value, a.normalized_value, a.qualifier,
             Jsonb(a.extra) if a.extra is not None else None, rec, Jsonb(a.source_locator), parent_id, a.rule_id))


def snapshot_body(asin: str) -> bytes:
    """快照正文：ASIN 字节（任务 7 夹具登记的 content_hash = sha256(ASIN)）。"""
    return asin.encode()


def _crawl(conn: psycopg.Connection, root: Path, world: w7.World) -> None:
    """按 _task3_helpers.simulate_crawl 的写法补采集运行与尝试日志，并写快照文件。B0Y1 先超时一次再成功（尝试序号 2）。"""
    conn.execute("INSERT INTO task2.crawl_run (run_id, extraction_id, status, params, finished_at) "
                 "VALUES ('t9fixture', %s, 'succeeded', '{}', %s)", (EID, CRAWLED))
    for ed in world.editions:
        asin = ed.asin
        path = root / "snapshots" / f"{asin}.html.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = snapshot_body(asin)
        path.write_bytes(gzip.compress(body))
        attempts = [("timeout", None, None)] if asin == "B0Y1" else []
        attempts.append(("success", 200, f"snapshots/{asin}.html.gz"))
        for n, (outcome, http, snap) in enumerate(attempts, start=1):
            conn.execute(
                "INSERT INTO task2.fetch_attempt (extraction_id, asin, attempt_no, run_id, exit_label, requested_url, "
                "final_url, http_status, outcome, reason, body_bytes, content_hash, snapshot_path, started_at, "
                "elapsed_ms, redirect_chain, page_template) VALUES (%s, %s, %s, 't9fixture', 'fixture', %s, %s, %s, %s, "
                "'fixture', %s, %s, %s, %s, 1, %s, %s)",
                (EID, asin, n, f"https://www.amazon.com/dp/{asin}", f"https://example.invalid/dp/{asin}" if http else None,
                 http, outcome, len(body) if http else None, hashlib.sha256(body).hexdigest() if http else None, snap,
                 CRAWLED, "[]" if http else None, "dp_product_title" if http else None))
        conn.execute("UPDATE task2.product_manifest SET attempt_count = %s WHERE extraction_id = %s AND asin = %s",
                     (len(attempts), EID, asin))


def _imdb_files(conn: psycopg.Connection) -> None:
    for name in IMDB_FILES:
        conn.execute("INSERT INTO imdb.source_file (snapshot_id, file_name, url, size_bytes, sha256, rows_read, rows_kept) "
                     "VALUES (%s, %s, %s, 100, %s, 10, 10) ON CONFLICT DO NOTHING",
                     (SNAPSHOT, name, f"https://example.invalid/{name}", file_sha256(name)))


def file_sha256(name: str) -> str:
    return hashlib.sha256(f"fixture:{name}".encode()).hexdigest()


def _human(conn: psycopg.Connection) -> None:
    """一条任务 4 人工决策（must_link B0R1 与 B0R1B），经任务 4 自己的导入函数写入。"""
    import_decisions(conn, T4Inputs(extraction_id=EID, imdb_snapshot_id=SNAPSHOT),
                     [HumanRow("must_link", "B0R1", "B0R1B", "same film")], annotator="fixture-annotator",
                     file_sha256=hashlib.sha256(b"fixture human csv").hexdigest())


def _dedup_versions(conn: psycopg.Connection, rows: list[tuple[str, str, str]]) -> None:
    """任务 8 版本行（列与约束同迁移 701）：每个版本一条 task8.run 与四个阶段键。"""
    for cv, rd, status in rows:
        run = f"t9fix_{rd}"
        conn.execute("INSERT INTO task8.run (run_id, kind, extraction_id, t3_rule_version, conflict_version_id, "
                     "rule_version, input_key, status, finished_at) VALUES (%s, 'dedup', %s, 't3r2', %s, 't8r1', %s, "
                     "'succeeded', now())", (run, EID, cv, f"fix|{rd}"))
        keys, parent = [], None
        for stage, prefix in (("review_clean", "c"), ("review_family", "f"), ("review_dedup", "d"),
                              ("review_attribution", "a")):
            key = prefix + hashlib.sha256(f"{rd}|{stage}".encode()).hexdigest()[:15]
            conn.execute("INSERT INTO task8.stage_key (stage_key, extraction_id, stage, full_key, parent_stage_key, "
                         "rule_version) VALUES (%s, %s, %s, %s, %s, 't8r1')", (key, EID, stage, f"{rd}|{stage}", parent))
            keys.append(key)
            parent = key
        conn.execute("INSERT INTO task8.dedup_version (review_dedup_version_id, extraction_id, run_id, input_key, "
                     "t3_rule_version, conflict_version_id, rule_version, clean_stage_key, family_stage_key, "
                     "dedup_stage_key, attribution_stage_key, status, verified_at) VALUES (%s, %s, %s, %s, 't3r2', %s, "
                     "'t8r1', %s, %s, %s, %s, %s, %s)",
                     (rd, EID, run, f"fix|{rd}", cv, *keys, status, datetime.now(timezone.utc)
                      if status == "verified" else None))
        conn.execute("UPDATE task8.run SET review_dedup_version_id = %s WHERE run_id = %s", (rd, run))


def run(conn: psycopg.Connection, t9: T9, through: str = "assertions", **kw) -> dict[str, Any]:
    kw.setdefault("snapshot_root", t9.snapshot_root)
    kw.setdefault("snapshot_workers", 2)
    return run_stages(conn, t9.inputs(**kw.pop("over", {})), through=through, **kw)


def stage_key(conn: psycopg.Connection, stage: str) -> str:
    """最近登记的某阶段短键（测试里每个阶段只有一组输入时唯一）。"""
    return conn.execute("SELECT stage_key FROM task9.stage_key WHERE stage = %s ORDER BY created_at DESC LIMIT 1",
                        (stage,)).fetchone()[0]


def upstream_digest(conn: psycopg.Connection) -> dict[str, tuple[int, str]]:
    """上游各 schema 全部表的行数与内容摘要：任务 9 运行前后必须相同（AC01）。"""
    tables = [f"{s}.{t}" for s, t in conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
        "AND table_schema = ANY(%s) AND table_name <> 'schema_migration' ORDER BY 1, 2", (list(UPSTREAM_SCHEMAS),))]
    return {t: conn.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                            f"FROM {t} x").fetchone() for t in tables}
