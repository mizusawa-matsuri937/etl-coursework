"""任务 4 集成测试辅助：在 etl_test 里建任务 3 结构的清单、分类与页面字段行（直接写表，不经页面快照），并装载合成 IMDb 快照。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from _task3_helpers import Product, setup
from etl_task3 import LABEL_MAP_VERSION, PARSER_VERSION, RULE_VERSION as T3_RULE
from etl_task4 import NORM_VERSION
from etl_task4.imdb import apply_imdb_schema, load_snapshot
from etl_task4.run import RunInputs, apply_task4_schema

NO_PAGE_GROUPS = ("not_found", "timeout", "not_collected")


@dataclass
class P:
    """一个合成商品：类别 + 页面字段（署名行与详情要点分开写）。"""
    asin: str
    kind: str                                    # movie、collection、tv_series、concert_other、non_movie、pending、nopage
    title: str | None = None
    directors: tuple[str, ...] = ()              # 署名行，每行一人（原样写入，可含 “姓, 名”）
    directors_detail: str | None = None          # 详情要点里的逗号串
    actors: tuple[str, ...] = ()
    actors_detail: str | None = None
    runtime: str | None = None                   # 详情要点 Run time
    runtime_overview: str | None = None
    release_date: str | None = None
    genre: str | None = None
    extra: tuple[tuple[str, str, str, str], ...] = ()   # (区域, 标签, 值, 字段键)
    reviews: int = 1
    group: str = "not_found"                     # kind == nopage 时的输入组


def page_fields(p: P) -> list[tuple[str, int, str | None, str, str | None]]:
    rows: list[tuple[str, int, str | None, str, str | None]] = []
    if p.title is not None:
        rows.append(("title", 1, None, p.title, "title"))
    by = [("Director", d, "director") for d in p.directors] + [("Actor", a, "actors") for a in p.actors]
    rows += [("byline", i, label, v, key) for i, (label, v, key) in enumerate(by, start=1)]
    det: list[tuple[str, str, str | None]] = []
    if p.directors_detail:
        det.append(("Director", p.directors_detail, "director"))
    if p.actors_detail:
        det.append(("Actors", p.actors_detail, "actors"))
    if p.runtime:
        det.append(("Run time", p.runtime, "runtime"))
    if p.release_date:
        det.append(("Release date", p.release_date, "product_release_date"))
    rows += [("detail_bullets", i, label, v, key) for i, (label, v, key) in enumerate(det, start=1)]
    ov: list[tuple[str, str, str | None]] = []
    if p.genre:
        ov.append(("Genre", p.genre, "genre"))
    if p.runtime_overview:
        ov.append(("Runtime", p.runtime_overview, "runtime"))
    rows += [("product_overview", i, label, v, key) for i, (label, v, key) in enumerate(ov, start=1)]
    for sec, label, value, key in p.extra:
        n = 1 + sum(1 for r in rows if r[0] == sec)
        rows.append((sec, n, label, value, key))
    return rows


def build(conn: psycopg.Connection, make_settings, tmp_path: Path, products: list[P], name: str = "a"):
    """装载抽取与清单（_task3_helpers.setup），再写任务 3 的 page_parse、page_field 与 product_classification。"""
    t3 = []
    for p in products:
        if p.kind == "nopage":
            t3.append(Product(p.asin, p.group, None, p.reviews))
        else:
            t3.append(Product(p.asin, "dp_page", f"<html>{p.asin}</html>".encode(), p.reviews))
    fx = setup(conn, make_settings, tmp_path, t3, name=name)
    apply_imdb_schema(conn)
    apply_task4_schema(conn)
    run = f"t4fix_{name}"
    conn.execute("INSERT INTO task3.classify_run (run_id, extraction_id, parser_version, label_map_version, "
                 "rule_version, status, finished_at) VALUES (%s, %s, %s, %s, %s, 'succeeded', now())",
                 (run, fx.eid, PARSER_VERSION, LABEL_MAP_VERSION, T3_RULE))
    for p in products:
        if p.kind == "nopage":
            conn.execute(
                "INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, category, "
                "basis_level, rule_id, pending_reason, run_id) VALUES (%s, %s, %s, %s, 'pending', 'missing', 'NP1', "
                "%s, %s)", (fx.eid, p.asin, T3_RULE, p.group, p.group, run))
            continue
        rows = page_fields(p)
        m = conn.execute("SELECT content_hash, snapshot_path, final_url, fetched_at FROM task2.product_manifest "
                         "WHERE extraction_id = %s AND asin = %s", (fx.eid, p.asin)).fetchone()
        conn.execute(
            "INSERT INTO task3.page_parse (extraction_id, asin, parser_version, content_hash, snapshot_path, "
            "final_url, fetched_at, attempt_no, parse_status, sections_present, field_count, run_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'parsed', '{}', %s, %s)",
            (fx.eid, p.asin, PARSER_VERSION, m[0], m[1], m[2], m[3], len(rows), run))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task3.page_field (extraction_id, asin, parser_version, section, ordinal, "
                            "label_raw, value_raw, field_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            [(fx.eid, p.asin, PARSER_VERSION, *r) for r in rows])
        pending = p.kind == "pending"
        conn.execute(
            "INSERT INTO task3.product_classification (extraction_id, asin, rule_version, input_group, category, "
            "basis_level, rule_id, pending_reason, parser_version, run_id) VALUES (%s, %s, %s, 'dp_page', %s, %s, "
            "%s, %s, %s, %s)",
            (fx.eid, p.asin, T3_RULE, p.kind, "missing" if pending else "structured", "PD1" if pending else "MV1",
             "insufficient_evidence" if pending else None, PARSER_VERSION, run))
    return fx


def inputs_for(fx, imdb_snapshot_id: str, **over) -> RunInputs:
    return RunInputs(extraction_id=fx.eid, imdb_snapshot_id=imdb_snapshot_id, **over)


def load_imdb(conn: psycopg.Connection, tmp_path: Path, data: dict, date: str = "2026-10-30") -> str:
    from task4 import imdb_files as ifx

    apply_imdb_schema(conn)
    directory = ifx.build_snapshot(tmp_path / f"imdb_{date}" / date, date=date, **data)
    return load_snapshot(conn, directory).snapshot_id


def reset_task4(conn: psycopg.Connection) -> None:
    """imdb 与 task4 的表不随 conn 夹具清空（IMDb 表没有指向清单的外键），每个测试开始时手动清。"""
    apply_imdb_schema(conn)
    apply_task4_schema(conn)
    conn.execute("TRUNCATE imdb.snapshot CASCADE")
    conn.execute("TRUNCATE task4.run CASCADE")
    conn.execute("TRUNCATE task4.source_record, task4.movie_identity, task4.human_decision CASCADE")


def standard_products() -> list[P]:
    """AC01 的 12 行：movie 4、collection 2、tv_series、concert_other、non_movie 各 1、有页面的 pending 1、无页面 2。"""
    return [
        P("B0T4000001", "movie", "Casablanca [DVD]", directors=("Michael Curtiz",), actors=("Humphrey Bogart",),
          runtime="1 hour and 42 minutes", reviews=3),
        P("B0T4000002", "movie", "Casablanca (Ultimate Collector's Edition)", directors=("Michael Curtiz",),
          runtime="1 hour and 42 minutes", reviews=2),
        P("B0T4000003", "movie", "Rocky II [VHS]", directors_detail="Sylvester Stallone"),
        P("B0T4000004", "movie", "The Omen (2006)", directors=("John Moore",)),
        P("B0T4000005", "collection", "A / B (Double Feature) [DVD]", reviews=4),
        P("B0T4000006", "collection", "Hollywood Hoodlums Collection", reviews=5),
        P("B0T4000007", "tv_series", "Some Series Season 1", reviews=6),
        P("B0T4000008", "concert_other", "Live in Concert", reviews=1),
        P("B0T4000009", "non_movie", "A CD", reviews=1),
        P("B0T4000010", "pending", "Just A Title [VHS]", reviews=1),
        P("B0T4000011", "nopage", group="not_found", reviews=7),
        P("B0T4000012", "nopage", group="timeout", reviews=1),
    ]


def match_products() -> list[P]:
    """AC05 的 10 个案例 + AC06 的合集（IMDb 一侧见 imdb_files.MATCH）。ASIN 末两位是案例序号。"""
    cas = dict(runtime="1 hour and 42 minutes")
    return [
        # 1 同一作品的三个版本：DVD、Blu-ray、加长版（片长多 40%）
        P("B0T4M00A01", "movie", "Casablanca [DVD]", directors=("Michael Curtiz",),
          actors=("Humphrey Bogart", "Ingrid Bergman"), **cas),
        P("B0T4M00A02", "movie", "Casablanca [Blu-ray]", directors=("Michael Curtiz",),
          actors=("Humphrey Bogart", "Ingrid Bergman"), **cas),
        P("B0T4M00A03", "movie", "Casablanca (Extended Edition)", directors=("Michael Curtiz",),
          actors=("Humphrey Bogart", "Ingrid Bergman"), runtime="2 hours and 24 minutes"),
        # 2 同名作品 1960 与 1983 年版，导演不同
        P("B0T4M00B01", "movie", "Breathless (1960)", directors=("Jean-Luc Godard",), actors=("Jean Seberg",),
          runtime="1 hour and 30 minutes"),
        P("B0T4M00B02", "movie", "Breathless (1983)", directors=("Jim McBride",), actors=("Richard Gere",),
          runtime="1 hour and 40 minutes"),
        # 3 同名真人版与动画版（IMDb 里没有），一个无导演，演员不重叠
        P("B0T4M00C01", "movie", "Cinderella", directors=("Clyde Geronimi",),
          actors=("Ilene Woods", "Eleanor Audley", "Verna Felton")),
        P("B0T4M00C02", "movie", "Cinderella [VHS]", actors=("Brandy", "Whitney Houston", "Bernadette Peters")),
        # 4 Rocky 与 Rocky II
        P("B0T4M00D01", "movie", "Rocky", directors=("John G. Avildsen",), actors=("Sylvester Stallone",),
          runtime="1 hour and 59 minutes"),
        P("B0T4M00D02", "movie", "Rocky II [VHS]", directors=("Sylvester Stallone",),
          actors=("Sylvester Stallone",), runtime="1 hour and 59 minutes"),
        # 5 两个无 IMDb 关联、标题键相同、无年份、导演一致
        P("B0T4M00E01", "movie", "Unlisted Film", directors=("Pat Poe",)),
        P("B0T4M00E02", "movie", "Unlisted Film [DVD]", directors=("Pat Poe",)),
        # 6 两个无 IMDb 关联、标题年份相同、导演一致
        P("B0T4M00F01", "movie", "Unlisted Film Two (1950)", directors=("Sam Same",)),
        P("B0T4M00F02", "movie", "Unlisted Film Two (1950 VHS)", directors=("Sam Same",)),
        # 7 只在 tvSeries 中有匹配
        P("B0T4M00G01", "movie", "Only Series", directors=("Someone Else",)),
        # 8 两个 IMDb 作品得分并列
        P("B0T4M00H01", "movie", "Twin Peak", directors=("Pat Roe",), actors=("Ann Lee",),
          runtime="1 hour and 40 minutes"),
        # 9 仅标题一致、无人物与年份
        P("B0T4M00I01", "movie", "Lonely Title"),
        # 10 同一作品的西班牙语版（另有英语版 A01）
        P("B0T4M00J01", "movie", "Casablanca (Spanish Version)", directors=("Michael Curtiz",),
          actors=("Humphrey Bogart",), **cas),
        # 别名匹配：只等于 IMDb 的别名 “Sin aliento”
        P("B0T4M00S01", "movie", "Sin Aliento", directors=("Jean-Luc Godard",), actors=("Jean Seberg",),
          runtime="1 hour and 30 minutes"),
        # 合集：两部成员 / 四部成员 / 拆不出
        P("B0T4M00K01", "collection", "Alien / Aliens (Double Feature) [DVD]",
          directors=("Ridley Scott", "James Cameron"), actors=("Sigourney Weaver",), reviews=4),
        P("B0T4M00K02", "collection", "Hollywood Hoodlums Collection", reviews=5),
        P("B0T4M00K03", "collection", "The Alien Legacy (Alien / Aliens / Alien 3 / Alien: Resurrection)",
          directors=("Ridley Scott", "James Cameron"), actors=("Sigourney Weaver",), reviews=2),
    ]


def run_decisions(conn, make_settings, tmp_path, products=None, rules=None, batch_size=500, data=None, name="a"):
    """从商品版本到判定（含 G3）的全部阶段，返回 (fx, ctx)。"""
    from etl_task4 import candidates, editions
    from etl_task4.match import DEFAULT_RULES
    from etl_task4.run import start_run
    from task4 import imdb_files as ifx

    fx = build(conn, make_settings, tmp_path, products if products is not None else match_products(), name=name)
    reset_task4(conn)
    sid = load_imdb(conn, tmp_path, data if data is not None else ifx.MATCH)
    ctx = start_run(conn, inputs_for(fx, sid))
    editions.build_editions(conn, ctx)
    editions.normalize_extraction(conn, ctx, batch_size=batch_size)
    candidates.build_imdb_keys(conn, ctx)
    candidates.decide_entities(conn, ctx, rules or DEFAULT_RULES, batch_size=batch_size)
    candidates.decide_pairs(conn, ctx, rules or DEFAULT_RULES, batch_size=batch_size)
    return fx, ctx
