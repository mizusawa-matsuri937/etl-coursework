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
    _prod_shape_t6_candidates(conn)              # 以下三步把任务 4–6 的行补成正式库的形状（任务 7 运行前）
    _merge_decisions(conn, world)
    _missing_mentions(conn)
    _person_links(conn, world)
    apply_task8_schema(conn)
    conn.execute("TRUNCATE task8.run, task8.stage_key CASCADE")
    cv = execute_conflict(conn, s.inputs(), batch_size=50).conflict_version_id
    other = execute_conflict(conn, s.inputs(rule_version="t7r1"), batch_size=50).conflict_version_id
    review_eid = _copy_reviews(conn, make_settings, tmp_path, reviews if reviews is not None else t9_reviews())
    _review_times_and_bounds(conn, s)
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


# ---------------------------------------------------------------- 补成正式库形状（T9-02）
# 任务 7 夹具的任务 4–6 行省略了几处正式库都有的列（任务 6 候选的 source_ref、合并决策、外部 ID 判定、评论时间、
# 推断上界的证据）。任务 9 的派生边要从这些列找到父节点，所以按正式库的形状补齐（形状逐项对照 etl_movies 的只读查询，
# 见 tests/fixtures/task9/README.md）；评论时间直接调用任务 6 自己的 reviews.product_review_times / movie_review_times。

def _prod_shape_t6_candidates(conn: psycopg.Connection) -> None:
    """任务 6 候选的 source_ref 与 source_key 按正式库：IMDb 为（文件、tconst、行号、快照、titleType），
    页面标题年份为（via、ASIN、区域、序号、任务 4 断言 ID、来源记录），source_key = <ASIN>:<断言 ID>。"""
    for cid, source, key, movie, stage in conn.execute(
            "SELECT candidate_id, source, source_key, movie_id, stage_key FROM task6.release_candidate "
            "WHERE extraction_id = %s ORDER BY candidate_id", (EID,)).fetchall():
        if source != "amazon_title_year":
            line = conn.execute("SELECT line_no FROM imdb.title WHERE snapshot_id = %s AND tconst = %s",
                                (SNAPSHOT, key)).fetchone()
            conn.execute("UPDATE task6.release_candidate SET source_ref = %s WHERE candidate_id = %s",
                         (Jsonb({"file": "title.basics.tsv.gz", "tconst": key, "line_no": line[0] if line else None,
                                 "snapshot": SNAPSHOT, "title_type": "movie"}), cid))
            continue
        t4 = "map0001" if stage.endswith("rv0002") else h7.T4
        row = conn.execute(
            "SELECT a.assertion_id, a.entity_id, a.source_record_id, a.source_locator FROM task4.product_movie pm "
            "JOIN task4.field_assertion a ON a.extraction_id = %s AND a.entity_id = pm.asin AND a.parser_version = 't3p1' "
            "AND a.norm_version = 't4n1' AND a.field_name IN ('title_year', 'title') "
            "WHERE pm.mapping_version_id = %s AND pm.movie_id = %s AND pm.relation_type = 'edition' "
            "ORDER BY (a.field_name = 'title_year') DESC, pm.asin, a.assertion_id LIMIT 1", (EID, t4, movie)).fetchone()
        if row is None:
            continue
        aid, asin, rec, loc = row
        n = conn.execute("SELECT count(*) FROM task6.release_candidate WHERE extraction_id = %s AND stage_key = %s "
                         "AND movie_id = %s AND source = 'amazon_title_year' AND starts_with(source_key, %s)",
                         (EID, stage, movie, f"{asin}:{aid}")).fetchone()[0]
        conn.execute("UPDATE task6.release_candidate SET source_key = %s, source_ref = %s WHERE candidate_id = %s",
                     (f"{asin}:{aid}" + (f":{n}" if n else ""),
                      Jsonb({"via": "edition", "asin": asin, "ordinal": (loc or {}).get("ordinal"),
                             "section": (loc or {}).get("section"), "assertion_id": aid, "member_ordinal": 0,
                             "source_record_id": rec, "final_url": f"https://example.invalid/dp/{asin}"}), cid))


MERGED = ("B0Lg", "B0LgC#1", "B0C1")     # 经任务 4 自动合并决策归到作品的商品（其余为 singleton）


def _merge_decisions(conn: psycopg.Connection, world: w7.World) -> None:
    """MERGED 中的商品改为 auto_decision：写任务 4 候选与合并决策（特征的证据为页面标题、导演断言与 IMDb 作品、
    演职员行，形状同正式库 features.evidence），并让 product_movie 指向该决策。"""
    tconst_of = {w7.movie_id(m.name): m.release.tconst for m in world.movies}
    for entity in MERGED:
        asin, _, member = entity.partition("#")
        ordinal = int(member or 0)
        row = conn.execute("SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s "
                           "AND member_ordinal = %s", (h7.T4, asin, ordinal)).fetchone()
        if row is None or not tconst_of.get(row[0]):
            continue
        movie, tconst = row[0], tconst_of[row[0]]
        etype = "collection_member" if member else "product_edition"
        page = [r[0] for r in conn.execute(
            "SELECT assertion_id FROM task4.field_assertion WHERE extraction_id = %s AND entity_id = %s "
            "AND entity_type = %s AND field_name = 'title' AND norm_version = 't4n1' AND parser_version = 't3p1'",
            (EID, entity, etype))]
        title_line = conn.execute("SELECT line_no FROM imdb.title WHERE snapshot_id = %s AND tconst = %s",
                                  (SNAPSHOT, tconst)).fetchone()[0]
        actors = [{"file": "title.principals.tsv.gz", "nconst": n, "line_no": ln} for n, ln in conn.execute(
            "SELECT nconst, line_no FROM imdb.title_principal WHERE snapshot_id = %s AND tconst = %s ORDER BY ordering",
            (SNAPSHOT, tconst))]
        directors = [{"file": "title.crew.tsv.gz", "nconst": n, "line_no": ln} for n, ln in conn.execute(
            "SELECT nconst, line_no FROM imdb.title_director WHERE snapshot_id = %s AND tconst = %s ORDER BY ordinal",
            (SNAPSHOT, tconst))]
        cand = conn.execute("INSERT INTO task4.candidate (extraction_id, norm_version, imdb_snapshot_id, left_entity, "
                            "right_kind, right_id, generator) VALUES (%s, 't4n1', %s, %s, 'imdb', %s, 'G1') "
                            "RETURNING candidate_id", (EID, SNAPSHOT, entity, tconst)).fetchone()[0]
        features = {"title": {"key": "fixture", "score": 2, "value": "G1"},
                    "evidence": {"page": {"title": page, "director": [], "actor": [], "runtime": [], "title_year": []},
                                 "imdb": {"title": {"file": "title.basics.tsv.gz", "line_no": title_line},
                                          "actors": actors, "directors": directors}}}
        dec = conn.execute("INSERT INTO task4.merge_decision (candidate_id, rule_version, run_id, tier, score, "
                           "confidence, rank, margin, reason, features, thresholds) VALUES (%s, 't4r2', 't4fix', "
                           "'auto', 6, 0.95, 1, 6, NULL, %s, '{}') RETURNING decision_id",
                           (cand, Jsonb(features))).fetchone()[0]
        conn.execute("UPDATE task4.product_movie SET basis = 'auto_decision', decision_id = %s WHERE "
                     "mapping_version_id = %s AND asin = %s AND member_ordinal = %s", (dec, h7.T4, asin, ordinal))


def _missing_mentions(conn: psycopg.Connection) -> None:
    """任务 7 夹具里没有署名行序号的页面人物（如 page_dirs、只写 nconst 的导演）没有提及；正式库里每条
    movie_person 都至少有一条来源提及（R34）。按 seed_task5 的写法在作品的第一个 edition 商品上补一条署名行提及。"""
    from etl_task5.names import parse as parse_name
    rows = conn.execute(
        "SELECT mp.movie_id, mp.person_id, mp.role, p.canonical_name, "
        "(SELECT min(pm.asin) FROM task4.product_movie pm WHERE pm.mapping_version_id = %s AND pm.movie_id = mp.movie_id "
        " AND pm.relation_type = 'edition') FROM task5.movie_person mp JOIN task5.person p USING "
        "(person_mapping_version_id, person_id) WHERE mp.person_mapping_version_id = %s AND NOT EXISTS (SELECT 1 FROM "
        "task5.movie_person_source s WHERE s.person_mapping_version_id = mp.person_mapping_version_id "
        "AND s.movie_id = mp.movie_id AND s.person_id = mp.person_id AND s.role = mp.role) "
        "ORDER BY mp.movie_id, mp.person_id, mp.role", (h7.T4, h7.PMAP)).fetchall()
    keys: dict[tuple[str, str], str] = {}
    for n, (movie, pid, role, name, asin) in enumerate(rows, start=1):
        if asin is None:
            continue
        key = keys.get((movie, pid))
        if key is None:
            key = keys[(movie, pid)] = f"{asin}:byline:{90 + n}:1"
            nm = parse_name(name)
            kind, rec = conn.execute(
                "SELECT e.product_kind, s.source_record_id FROM task4.product_edition e JOIN task4.source_record s "
                "ON s.locator->>'asin' = e.asin WHERE e.extraction_id = %s AND e.asin = %s", (EID, asin)).fetchone()
            conn.execute(
                "INSERT INTO task5.person_mention (extraction_id, parser_version, norm_version, mention_key, asin, "
                "product_kind, section, page_ordinal, list_position, label_raw, role_class, is_byline, byline_rank, "
                "raw_value, display_name, nickname, disambiguator, paren_text, suffix, honorific, strict_key, loose_key, "
                "tokenset_key, family_key, initial_key, flags, status, source_record_id, run_id) "
                "VALUES (%s, 't3p1', 't5n1', %s, %s, %s, 'byline', %s, 1, %s, %s, true, 1, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, '{}', 'resolvable', %s, 't5fix')",
                (EID, key, asin, kind, 90 + n, "Director" if role == "director" else "Actors",
                 "director" if role == "director" else "actor", name, nm.display, nm.nickname, nm.disambiguator,
                 nm.paren_text, nm.suffix, nm.honorific, nm.strict, nm.loose, nm.tokenset, nm.family, nm.initial, rec))
            conn.execute("INSERT INTO task5.mention_person (person_mapping_version_id, extraction_id, parser_version, "
                         "norm_version, mention_key, person_id, group_key, attribution, basis) "
                         "VALUES (%s, %s, 't3p1', 't5n1', %s, %s, %s, 'movie', 'singleton')",
                         (h7.PMAP, EID, key, pid, "g:" + key))
        conn.execute("INSERT INTO task5.movie_person_source (person_mapping_version_id, movie_id, person_id, role, "
                     "mention_key, asin, byline_rank, list_position) VALUES (%s, %s, %s, %s, %s, %s, 1, 1)",
                     (h7.PMAP, movie, pid, role, key, asin))


LINK_KEY = f"{h7.T4}|t3p1|t5n1|t5r1|{SNAPSHOT}"


def _person_links(conn: psycopg.Connection, world: w7.World) -> None:
    """任务 5 的外部 ID 判定与归并依据按正式库：页面人物的规范名来源为 mention:<提及键>；已关联 IMDb 的页面人物，其第一条提及经 credits 途径（link_candidate
    与 auto 判定，证据为 title_principal/title_director 的序号与行号），其余提及经 co_credit 第 2 轮跨作品决策并入；
    person_external_id.basis_decision_ids 指向该判定。"""
    tconst_of = {w7.movie_id(m.name): m.release.tconst for m in world.movies}
    # 页面人物的规范名来源是其提及（正式库为 mention:<mention_key>；任务 7 夹具写的是锚点 mention:fix:<姓名>）
    conn.execute("UPDATE task5.person p SET canonical_source = 'mention:' || x.k FROM (SELECT person_id, "
                 "min(mention_key) AS k FROM task5.mention_person WHERE person_mapping_version_id = %s GROUP BY 1) x "
                 "WHERE p.person_mapping_version_id = %s AND p.person_id = x.person_id "
                 "AND starts_with(p.canonical_source, 'mention:fix:')", (h7.PMAP, h7.PMAP))
    rows = conn.execute(
        "SELECT mp.mention_key, mp.person_id, mp.group_key, x.external_id, s.movie_id, m.role_class "
        "FROM task5.mention_person mp JOIN task5.person_external_id x ON x.person_mapping_version_id = "
        "mp.person_mapping_version_id AND x.person_id = mp.person_id "
        "JOIN task5.person_mention m ON m.extraction_id = mp.extraction_id AND m.mention_key = mp.mention_key "
        "JOIN LATERAL (SELECT min(movie_id) AS movie_id FROM task5.movie_person_source s "
        "              WHERE s.person_mapping_version_id = mp.person_mapping_version_id AND s.mention_key = mp.mention_key) s ON true "
        "WHERE mp.person_mapping_version_id = %s ORDER BY mp.person_id, mp.mention_key", (h7.PMAP,)).fetchall()
    first_group: dict[str, str] = {}
    for key, pid, group, nconst, movie, role in rows:
        tconst = tconst_of.get(movie)
        if pid not in first_group:
            first_group[pid] = group
            table = "title_director" if role == "director" else "title_principal"
            col = "ordinal" if table == "title_director" else "ordering"
            hit = conn.execute(f"SELECT {col}, line_no FROM imdb.{table} WHERE snapshot_id = %s AND tconst = %s "
                               "AND nconst = %s", (SNAPSHOT, tconst, nconst)).fetchone() if tconst else None
            name = conn.execute("SELECT primary_name, line_no FROM imdb.name WHERE snapshot_id = %s AND nconst = %s",
                                (SNAPSHOT, nconst)).fetchone()
            if hit is None or name is None:
                continue
            lc = conn.execute(
                "INSERT INTO task5.link_candidate (extraction_id, stage_key, mention_key, route, tconst, nconst, "
                "category, level, work_movie_id, evidence) VALUES (%s, %s, %s, 'credits', %s, %s, %s, 'X1', %s, %s) "
                "RETURNING link_candidate_id",
                (EID, LINK_KEY, key, tconst, nconst, "director" if role == "director" else "actor", movie,
                 Jsonb({"table": table, "ordinal": hit[0], "snapshot": SNAPSHOT, "imdb_name": name[0],
                        "name_line_no": name[1], "credit_line_no": hit[1]}))).fetchone()[0]
            ld = conn.execute("INSERT INTO task5.link_decision (link_candidate_id, rule_version, run_id, tier, reason, "
                              "confidence, thresholds) VALUES (%s, 't5r1', 't5fix', 'auto', 'x1_unique_credit', 0.95, "
                              "'{}') RETURNING link_decision_id", (lc,)).fetchone()[0]
            conn.execute("UPDATE task5.mention_person SET basis = 'credits' WHERE person_mapping_version_id = %s "
                         "AND mention_key = %s", (h7.PMAP, key))
            conn.execute("UPDATE task5.person_external_id SET basis_decision_ids = basis_decision_ids || %s::bigint, "
                         "name_line_no = %s WHERE person_mapping_version_id = %s AND person_id = %s",
                         (ld, name[1], h7.PMAP, pid))
        else:
            a, b = first_group[pid], group          # 比较按数据库排序规则（CHECK group_a < group_b）
            pc = conn.execute("INSERT INTO task5.person_candidate (extraction_id, stage_key, group_a, group_b, generator) "
                              "VALUES (%s, %s, least(%s::text, %s::text), greatest(%s::text, %s::text), 'C2') "
                              "ON CONFLICT DO NOTHING RETURNING person_candidate_id",
                              (EID, LINK_KEY, a, b, a, b)).fetchone()
            if pc is None:
                continue
            for rnd, tier in ((1, "review"), (2, "auto")):
                conn.execute("INSERT INTO task5.person_decision (person_candidate_id, rule_version, round, run_id, tier, "
                             "score, confidence, reason, features, thresholds) VALUES (%s, 't5r1', %s, 't5fix', %s, 5, "
                             "0.9, 'co_credit_collaborators', %s, '{}')",
                             (pc[0], rnd, tier, Jsonb({"collaborators": 2, "n_level": "N1"})))
            conn.execute("UPDATE task5.mention_person SET basis = 'co_credit', round = 2 "
                         "WHERE person_mapping_version_id = %s AND mention_key = %s", (h7.PMAP, key))


def _review_times_and_bounds(conn: psycopg.Connection, s) -> None:
    """评论时间由任务 6 自己的 reviews.product_review_times / movie_review_times 写入；推断上界补上正式库都有的
    bound_evidence 行（basis、B1–B5 或 M1–M4）与 inferred_release_bound 的 basis、conditions。"""
    from etl_task6 import reviews as t6reviews
    from etl_task6.rules import ruleset as t6ruleset, _shift
    from etl_task6.run import RunInputs as T6Inputs, start_run as t6_start
    ctx6 = t6_start(conn, T6Inputs(extraction_id=EID, t4_mapping_version_id=h7.T4, t5_person_mapping_version_id=h7.PMAP,
                                   imdb_snapshot_id=SNAPSHOT))
    t6reviews.product_review_times(conn, ctx6)
    t6reviews.movie_review_times(conn, ctx6)
    conn.execute("UPDATE task6.run SET status = 'succeeded', finished_at = now() WHERE run_id = %s", (ctx6.run_id,))
    b3 = t6ruleset("t6r1").b3_days
    for movie, when, precision, evidence in conn.execute(
            "SELECT movie_id, bound_time, bound_precision, chosen_evidence FROM task6.inferred_release_bound "
            "WHERE release_version_id = %s", (h7.RV,)).fetchall():
        if evidence == "review":
            t = conn.execute("SELECT earliest_source_review_id, earliest_asin, earliest_review_time, second_review_time "
                             "FROM task6.movie_review_time WHERE extraction_id = %s AND t4_mapping_version_id = %s "
                             "AND movie_id = %s", (EID, h7.T4, movie)).fetchone()
            if t is None:
                continue
            basis = {"source_review_id": t[0], "asin": t[1]}
            ok3 = t[3] is not None and t[3].date().isoformat() <= _shift(t[2].date().isoformat(), b3)
            checks = {"B1": True, "B2": True, "B3": ok3, "B4": True, "B5": True}
            when = t[2].date()
        else:
            d = conn.execute("SELECT d.asin, d.assertion_id, d.value_date FROM task6.product_release_date d "
                             "JOIN task4.product_movie pm ON pm.mapping_version_id = %s AND pm.asin = d.asin "
                             "AND pm.relation_type = 'edition' WHERE d.extraction_id = %s AND pm.movie_id = %s "
                             "AND d.kind = 'media_release' ORDER BY d.value_date, d.assertion_id LIMIT 1",
                             (h7.T4, EID, movie)).fetchone()
            if d is None:
                continue
            basis = {"asin": d[0], "assertion_id": d[1]}
            checks = {"M1": True, "M2": True, "M3": True, "M4": True}
            when = d[2]
        conn.execute("DELETE FROM task6.bound_evidence WHERE release_version_id = %s AND movie_id = %s AND evidence = %s",
                     (h7.RV, movie, evidence))
        conn.execute("INSERT INTO task6.bound_evidence (release_version_id, movie_id, evidence, evidence_time, "
                     "evidence_precision, basis, checks, passed, failed_checks, confidence) VALUES (%s, %s, %s, %s, %s, "
                     "%s, %s, %s, %s, 0.6)", (h7.RV, movie, evidence, when, precision, Jsonb(basis), Jsonb(checks),
                                              all(checks.values()), [k for k, v in checks.items() if not v]))
        conn.execute("UPDATE task6.inferred_release_bound SET bound_time = %s, conditions = %s, basis = %s "
                     "WHERE release_version_id = %s AND movie_id = %s",
                     (when, Jsonb(checks), Jsonb({"note": "最早评论" if evidence == "review"
                                                  else "最早的 media_release 商品发售日期",
                                                  "evidence_time": when.isoformat()}), h7.RV, movie))


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


def add_review_mapping(conn: psycopg.Connection, t9: T9, rd: str = "rd0001") -> int:
    """给任务 8 版本补评论映射（列与约束同迁移 701）：EID 下每条有产品 ID 的评论记录自成一条逻辑评论（代表记录），
    每个 ASIN 一个家族，按该 cv 所用映射版本的 task4.product_movie 归属到作品。返回归属行数。"""
    fam, ded, att = conn.execute("SELECT family_stage_key, dedup_stage_key, attribution_stage_key "
                                 "FROM task8.dedup_version WHERE review_dedup_version_id = %s", (rd,)).fetchone()
    t4 = conn.execute("SELECT t4_mapping_version_id FROM task7.conflict_version WHERE conflict_version_id = %s",
                      (t9.cv,)).fetchone()[0]
    conn.execute("DELETE FROM task8.review_family WHERE family_stage_key = %s", (fam,))     # 这些表不随 seed 的
    conn.execute("DELETE FROM task8.review_mapping WHERE dedup_stage_key = %s", (ded,))     # TRUNCATE 级联清空
    conn.execute("DELETE FROM task8.logical_review WHERE dedup_stage_key = %s", (ded,))
    conn.execute("DELETE FROM task8.logical_review_work WHERE attribution_stage_key = %s", (att,))
    recs = conn.execute("SELECT rr.record_no, rr.source_review_id, rs.product_id, rs.reviewer_id, rs.review_time_utc, "
                        "rs.score, coalesce(rs.review_text_raw, '') FROM public.review_record rr "
                        "JOIN public.review_staging rs USING (extraction_id, source_review_id) "
                        "WHERE rr.extraction_id = %s AND rs.product_id IS NOT NULL ORDER BY rr.record_no",
                        (EID,)).fetchall()
    n = 0
    for asin in sorted({r[2] for r in recs}):
        fid = "rf_" + hashlib.sha256(f"{rd}|{asin}".encode()).hexdigest()[:16]
        conn.execute("INSERT INTO task8.review_family (family_stage_key, family_id, min_asin, asin_count, "
                     "signature_count, max_review_count, weak_support) VALUES (%s, %s, %s, 1, 1, 1, false)",
                     (fam, fid, asin))
    for no, sid, asin, who, at, score, text in recs:
        lr = "lr_" + hashlib.sha256(f"{rd}|{no}".encode()).hexdigest()[:20]
        fid = "rf_" + hashlib.sha256(f"{rd}|{asin}".encode()).hexdigest()[:16]
        conn.execute("INSERT INTO task8.logical_review (dedup_stage_key, logical_review_id, reviewer_id, review_day, "
                     "score, content_hash, text_md5, family_id, representative_record_no, copy_count, asin_count, "
                     "partition_no) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 1, 0)",
                     (ded, lr, who or "unknown", (at or CRAWLED).date(), score or 0,
                      hashlib.sha256(text.encode()).digest(), hashlib.md5(text.encode()).digest(), fid, no))
        conn.execute("INSERT INTO task8.review_mapping (dedup_stage_key, record_no, source_review_id, asin, "
                     "logical_review_id, mapping_status, match_level, partition_no, rule_version) "
                     "VALUES (%s, %s, %s, %s, %s, 'mapped', 'representative', 0, 't8r1')", (ded, no, sid, asin, lr))
        for (movie,) in conn.execute("SELECT DISTINCT movie_id FROM task4.product_movie WHERE mapping_version_id = %s "
                                     "AND asin = %s", (t4, asin)).fetchall():
            conn.execute("INSERT INTO task8.logical_review_work (attribution_stage_key, logical_review_id, movie_id, "
                         "via_asins, also_on_collection) VALUES (%s, %s, %s, %s, false)", (att, lr, movie, [asin]))
            n += 1
    return n


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


NOT_COLLECTED = "B0NC"     # 清单里有、从未采集的商品（AC12）


def add_products(conn: psycopg.Connection, t9: T9) -> None:
    """补齐商品层（AC12）：任务 3 类别、任务 4 匹配状态与一个未采集商品，形状同正式库。
    - 未采集商品：清单行取默认状态 not_collected（列与约束同迁移 101），类别按任务 3 `_classify_no_page` 的写法记为
      pending / missing / NP1、pending_reason = 输入组（夹具的 final_url 不是 amazon.com，任务 3 的分组 SQL 会把全部
      夹具商品归为 other_site，所以不能直接调用该函数）。
    - 已采集的参与匹配商品：dp_page 组、解析版本 t3p1，电影单品 MV1、合集 CL2（正式库 t3r2 的结构化规则）。
    - 匹配状态：同任务 4 cluster 的口径，合并簇里的商品 merged，其余 singleton，合集 collection_split
      “已链接/成员数 members linked”。"""
    run3 = conn.execute("SELECT run_id FROM task3.classify_run WHERE extraction_id = %s ORDER BY started_at LIMIT 1",
                        (t9.eid,)).fetchone()[0]
    build = conn.execute("SELECT first_build_id FROM task2.product_manifest WHERE extraction_id = %s LIMIT 1",
                         (t9.eid,)).fetchone()[0]
    conn.execute("INSERT INTO task2.product_manifest (extraction_id, asin, original_url, url_template_version, "
                 "first_build_id, in_valid_records, records_total, records_valid, records_quarantined, first_record_no, "
                 "last_record_no) VALUES (%s, %s, 'u', 'v1', %s, true, 1, 1, 0, 1, 1)", (t9.eid, NOT_COLLECTED, build))
    conn.execute("INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, category, "
                 "basis_level, rule_id, pending_reason, run_id) VALUES (%s, %s, 't3r2', 'not_collected', 'pending', "
                 "'missing', 'NP1', 'not_collected', %s)", (t9.eid, NOT_COLLECTED, run3))
    conn.execute("INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, category, "
                 "basis_level, rule_id, parser_version, run_id) "
                 "SELECT e.extraction_id, e.asin, e.t3_rule_version, 'dp_page', e.product_kind, 'structured', "
                 "CASE e.product_kind WHEN 'collection' THEN 'CL2' ELSE 'MV1' END, e.parser_version, %s "
                 "FROM task4.product_edition e WHERE e.extraction_id = %s AND e.t3_rule_version = 't3r2' "
                 "AND e.match_scope = 'matched' AND e.parser_version = 't3p1' ON CONFLICT DO NOTHING", (run3, t9.eid))
    conn.execute("INSERT INTO task4.product_match_status (mapping_version_id, asin, status, reason) "
                 "SELECT pm.mapping_version_id, pm.asin, CASE WHEN e.product_kind = 'collection' THEN 'collection_split' "
                 "WHEN count(*) OVER (PARTITION BY pm.mapping_version_id, pm.movie_id) > 1 THEN 'merged' "
                 "ELSE 'singleton' END, CASE WHEN e.product_kind = 'collection' THEN "
                 "(SELECT count(*) FROM task4.product_movie c WHERE c.mapping_version_id = pm.mapping_version_id "
                 " AND c.asin = pm.asin) || '/' || (SELECT count(*) FROM task4.product_movie c "
                 " WHERE c.mapping_version_id = pm.mapping_version_id AND c.asin = pm.asin) || ' members linked' END "
                 "FROM (SELECT DISTINCT ON (mapping_version_id, asin) * FROM task4.product_movie "
                 "      WHERE mapping_version_id = %s ORDER BY mapping_version_id, asin, member_ordinal) pm "
                 "JOIN task4.product_edition e ON e.extraction_id = %s AND e.asin = pm.asin AND e.t3_rule_version = 't3r2' "
                 "ON CONFLICT DO NOTHING", (h7.T4, t9.eid))
