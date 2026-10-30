"""任务 5 集成测试辅助：用任务 4 的辅助写任务 3 数据、装载合成 IMDb 快照并真正运行任务 4 的匹配，得到真实的映射版本。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
from task5 import people

import _task4_helpers as t4h
from etl_task4.runner import RunResult, execute_match
from etl_task5.run import RunContext, RunInputs, apply_task5_schema, start_run


@dataclass
class World:
    fx: object                 # _task3_helpers.Fixture
    sid: str                   # IMDb 快照
    t4: RunResult              # 任务 4 的匹配结果（mapping_version_id 即任务 5 的输入）

    @property
    def mapping(self) -> str:
        return self.t4.mapping_version_id


def reset_task5(conn: psycopg.Connection) -> None:
    """task5 的表不随 conn 夹具清空（只在 TRUNCATE extraction CASCADE 时被连带清空），每个测试开始时手动清。"""
    apply_task5_schema(conn)
    tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'task5'").fetchall()]
    conn.execute("TRUNCATE " + ", ".join(f"task5.{t}" for t in tables) + " RESTART IDENTITY CASCADE")


def make_world(conn: psycopg.Connection, make_settings, tmp_path: Path, products=None, imdb=None,
               name: str = "a") -> World:
    fx = t4h.build(conn, make_settings, tmp_path, products if products is not None else people.products(), name=name)
    t4h.reset_task4(conn)
    reset_task5(conn)
    sid = t4h.load_imdb(conn, tmp_path, imdb if imdb is not None else people.IMDB)
    return World(fx, sid, execute_match(conn, t4h.inputs_for(fx, sid)))


def remake_world(conn: psycopg.Connection, world: World, tmp_path: Path, imdb, date: str = "2026-10-31") -> World:
    """同一个抽取与页面数据，清空任务 4、5 的结果后重新装载 IMDb 快照并重跑任务 4（用来和“一次跑完”的结果对照）。"""
    t4h.reset_task4(conn)
    reset_task5(conn)
    sid = t4h.load_imdb(conn, tmp_path, imdb, date=date)
    return World(world.fx, sid, execute_match(conn, t4h.inputs_for(world.fx, sid)))


def clone_mapping(conn: psycopg.Connection, world: World, asin: str, new_id: str = "map9002") -> str:
    """复制任务 4 映射版本，并把一个商品从它所在的作品里拆出来成为单独的新作品（用来测试“换映射版本”）。"""
    old = world.mapping
    conn.execute(
        "INSERT INTO task4.mapping_version (mapping_version_id, extraction_id, run_id, input_key, t3_rule_version, "
        "parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, counts) "
        "SELECT %s, extraction_id, run_id, input_key || '|alt', t3_rule_version, parser_version, norm_version, "
        "rule_version, imdb_snapshot_id, human_set_hash, counts FROM task4.mapping_version WHERE mapping_version_id = %s",
        (new_id, old))
    conn.execute("INSERT INTO task4.movie_identity (anchor_key, movie_id, created_run) SELECT 'asin:' || %s, "
                 "'mv_00000000000000aa', run_id FROM task4.mapping_version WHERE mapping_version_id = %s", (asin, old))
    conn.execute("INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, tconst, display_title, "
                 "edition_count, contains_count) SELECT %s, movie_id, status, anchor_key, tconst, display_title, "
                 "edition_count, contains_count FROM task4.movie WHERE mapping_version_id = %s", (new_id, old))
    conn.execute("INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, display_title, edition_count) "
                 "VALUES (%s, 'mv_00000000000000aa', 'page_only', 'asin:' || %s, 'Split Off', 1)", (new_id, asin))
    conn.execute("INSERT INTO task4.product_movie (mapping_version_id, asin, member_ordinal, movie_id, relation_type, basis) "
                 "SELECT %s, asin, member_ordinal, CASE WHEN asin = %s THEN 'mv_00000000000000aa' ELSE movie_id END, "
                 "relation_type, 'singleton' FROM task4.product_movie WHERE mapping_version_id = %s", (new_id, asin, old))
    conn.execute("INSERT INTO task4.product_match_status (mapping_version_id, asin, status, reason) SELECT %s, asin, "
                 "status, reason FROM task4.product_match_status WHERE mapping_version_id = %s", (new_id, old))
    return new_id


def inputs_for(world: World, **over) -> RunInputs:
    base = dict(extraction_id=world.fx.eid, t4_mapping_version_id=world.mapping, imdb_snapshot_id=world.sid)
    return RunInputs(**{**base, **over})


def start5(conn: psycopg.Connection, world: World, **over) -> RunContext:
    return start_run(conn, inputs_for(world, **over))


def run_links(conn, world: World, *, batch_size: int = 500, **over) -> RunContext:
    """提及 → 外部 ID 途径一 → 途径二（T5-05 的 runner 之前，测试直接串起各阶段）。"""
    from etl_task5 import external, mentions

    ctx = start5(conn, world, **over)
    mentions.extract_mentions(conn, ctx, batch_size=batch_size)
    external.link_credits(conn, ctx, batch_size=batch_size)
    external.link_filmography(conn, ctx, batch_size=batch_size)
    return ctx


def linked_status(conn, world: World) -> dict[str, tuple]:
    """商品 → (作品状态, tconst)，用来核对任务 4 的映射符合夹具的设计。"""
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT pm.asin, m.status, m.tconst FROM task4.product_movie pm JOIN task4.movie m "
        "ON m.mapping_version_id = pm.mapping_version_id AND m.movie_id = pm.movie_id "
        "WHERE pm.mapping_version_id = %s AND pm.member_ordinal <= 1 ORDER BY 1", (world.mapping,)).fetchall()}


def combined_inputs(extra_films: bool = False):
    """AC01–AC08 的商品与 IMDb 数据合在一起，外加两个同名导演的单品；extra_films 再加作品年表夹具与一对近似名。"""
    from task5 import links, movie_person as mpfx, people as base, resolve as resfx

    from _task4_helpers import P

    parts = [base.IMDB, resfx.CASES_IMDB, mpfx.MP_IMDB] + ([links.FILM_IMDB] if extra_films else [])
    imdb = {k: sum((x[k] for x in parts), []) for k in base.IMDB}
    products = base.products() + resfx.case_products() + mpfx.mp_products() + [
        # 两位同名导演：没有 IMDb 关联、没有合作者，所以是两个人物
        P("B0T5Q00A01", "movie", "Twin One (1991)", directors=("Twin Dir",), actors=("Twin Star",)),
        P("B0T5Q00A02", "movie", "Twin Two (1992)", directors=("Twin Dir",), actors=("Other Star",))]
    if extra_films:
        products += links.film_products() + [
            # 同一位演员（nm0000309）在两部 IMDb 关联作品里写法不同：倒序与正序
            P("B0T5Q00C01", "movie", "Alien (1979)", directors=("Ridley Scott",), actors=("Weaver, Sigourney",)),
            P("B0T5Q00C02", "movie", "Aliens (1986)", directors=("James Cameron",), actors=("Sigourney Weaver",)),
            # 近似名（拼写差一个字母）分属两个没有 IMDb 关联的作品
            P("B0T5Q00N01", "movie", "Near One (1993)", directors=("Near Dir One",), actors=("Jon Smythe",)),
            P("B0T5Q00N02", "movie", "Near Two (1994)", directors=("Near Dir Two",), actors=("John Smythe",))]
    return products, imdb
