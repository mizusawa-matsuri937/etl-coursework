"""人物共现反查（spec R08–R10）：对没有 IMDb 关联的作品，按页面导演、演员的严格键在 IMDb 演职员里反查作品。

评分类：页面导演命中作品的导演记 2 分、每位页面演员命中其主要演员记 1 分；得分 ≥1 的作品都写评分记录；
≥2 分的作品再过标题条件（IMDb 标题键实词 ⊆ 商品标题键实词）与编号条件；通过的作品多于一部时先记 ambiguous，
共现消解（resolve，依赖评论年份提及）在择优阶段做。实行方法（design“人物共现反查”）：先取目标作品的页面姓名键，
一次性流式扫描 imdb.name 只保留键相同的人，再按姓名对应的 nconst 查演职员。

设计修订（design“实现中的修订”）：反查对象是**所有没有 tconst 的作品**（含已有线索候选的作品），
否则 R16 的 tconst_disagreement 与 AC08 的 L 案例无法成立；择优先取线索，多出的共现候选只作分歧证据。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task1 import reporting
from etl_task4.normalize import strict_name

from .candidates import CAND_COLUMNS, World
from .rules import RuleSet, cooccur_number_guard, cooccur_title_ok, number_tokens, top_review_year
from .run import DEFAULT_BATCH, BeforeCommit, RunContext, add_quality_issue, copy_rows, mark_batch, mark_entities, \
    next_batch_no
from . import sources

SCORE_COLUMNS = ("extraction_id", "stage_key", "movie_id", "tconst", "score", "director_hits", "actor_hits",
                 "title_ok", "matched_title_key", "matched_asin", "key_source", "title_key_source_line",
                 "number_guard", "number_guard_detail", "passed", "run_id")
RESULT_COLUMNS = ("extraction_id", "stage_key", "movie_id", "outcome", "chosen_tconst", "passed_tconsts",
                 "owner_movie_id", "run_id")
PRINCIPAL_CATEGORIES = ("actor", "actress", "self")


def _page_names(conn: psycopg.Connection, world: World) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """movie → {'director': [断言...], 'actor': [断言...]}；只用严格键可信的署名（byline/reversed/detail）。"""
    out: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"director": [], "actor": []})
    eid, pv = world.inputs.extraction_id, world.inputs.parser_version
    for field, role in (("director", "director"), ("actor", "actor")):
        for a in sources.assertions_by_entity(conn, eid, pv, field):
            if a["normalized_value"] and a["qualifier"] in sources.STRONG_QUALIFIERS \
                    and a["entity_type"] == "product_edition":
                movie = world.asin_movie.get((a["entity_id"], 0))
                if movie:
                    out[movie][role].append(a)
    return out


def name_index(conn: psycopg.Connection, snapshot_id: str, keys: set[str]) -> dict[str, list[str]]:
    """严格键 → nconst 列表；只保留与页面姓名键相同的人（一次流式扫描 imdb.name，内存受键数限制，R 前提）。"""
    out: dict[str, list[str]] = defaultdict(list)
    for nconst, primary_name in conn.execute("SELECT nconst, primary_name FROM imdb.name WHERE snapshot_id = %s",
                                             (snapshot_id,)):
        key = strict_name(primary_name or "")
        if key in keys:
            out[key].append(nconst)
    return out


def _title_keys_of(conn: psycopg.Connection, snapshot_id: str, tconsts: list[str]) -> dict[str, list[tuple]]:
    """tconst → [(title_key, key_source, source_line)]，按 key_source、ordering 排序（primary 优先）。"""
    out: dict[str, list[tuple]] = defaultdict(list)
    for k in range(0, len(tconsts), 500):
        chunk = tconsts[k:k + 500]
        for tc, tk, ks, line in conn.execute(
                "SELECT tconst, title_key, key_source, source_line FROM task4.imdb_title_key "
                "WHERE imdb_snapshot_id = %s AND norm_version = %s AND tconst = ANY(%s) "
                "ORDER BY tconst, key_source, aka_ordering", (snapshot_id, sources.T4_NORM_VERSION, chunk)):
            out[tc].append((tk, ks, int(line)))
    return out


def score_movies(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *,
                 batch_size: int = DEFAULT_BATCH, before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R09–R10：按作品分批评分与写评分记录；实体标记与其他阶段一致。"""
    i = ctx.inputs
    world = World(conn, ctx)
    targets = [m for m, tc in world.movies if not tc]
    names = _page_names(conn, world)
    keys = {a["normalized_value"] for m in targets for role in ("director", "actor") for a in names.get(m, {}).get(role, ())}
    index = name_index(conn, i.imdb_snapshot_id, keys) if keys else {}
    title_cache: dict[str, dict[str, list[tuple]]] = {}
    product_keys_cache: dict[str, list[tuple[str, str]]] = {}

    def product_keys(movie: str) -> list[tuple[str, str]]:
        if movie not in product_keys_cache:
            cache = getattr(world, "_title_map", None)
            if cache is None:
                cache = world._title_map = {
                    a["entity_id"]: a["normalized_value"] for a in sources.assertions_by_entity(
                        conn, i.extraction_id, i.parser_version, "title")}
            product_keys_cache[movie] = [(a, cache[a]) for a in sorted(world.edition_asins.get(movie, ()))
                                         if cache.get(a)]
        return product_keys_cache[movie]

    key, stage = i.stage_key("cooccur"), "cooccur"
    unmarked = conn.execute(
        "SELECT m.movie_id FROM task4.movie m LEFT JOIN task6.entity_stage es ON es.extraction_id = %s "
        "AND es.stage = 'cooccur' AND es.stage_key = %s AND es.entity_id = m.movie_id "
        "WHERE m.mapping_version_id = %s AND m.tconst IS NULL AND es.entity_id IS NULL "
        "ORDER BY m.movie_id COLLATE \"C\"", (i.extraction_id, key, i.t4_mapping_version_id)).fetchall()
    todo = [r[0] for r in unmarked]
    counts: dict[str, Any] = {"movies": 0, "scored_rows": 0, "score_min_1": 0, "passed": 0,
                               "excluded_by_title": 0, "excluded_by_number": 0}
    batch_no = next_batch_no(conn, ctx, stage)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        with conn.transaction():
            # 本批页面姓名对应的 nconst → IMDb 演职员行
            nconsts = sorted({n for m in batch for role in ("director", "actor") for a in names.get(m, {}).get(role, ())
                              for n in index.get(a["normalized_value"], ())})
            credits: dict[str, dict[str, list]] = defaultdict(lambda: {"directors": [], "principals": []})
            if nconsts:
                for tconst, nconst, line in conn.execute(
                        "SELECT tconst, nconst, line_no FROM imdb.title_director WHERE snapshot_id = %s "
                        "AND nconst = ANY(%s)", (i.imdb_snapshot_id, nconsts)):
                    credits[tconst]["directors"].append((nconst, int(line)))
                for tconst, nconst, line, cat in conn.execute(
                        "SELECT tconst, nconst, line_no, category FROM imdb.title_principal WHERE snapshot_id = %s "
                        "AND nconst = ANY(%s) AND category = ANY(%s)",
                        (i.imdb_snapshot_id, nconsts, list(PRINCIPAL_CATEGORIES))):
                    credits[tconst]["principals"].append((nconst, int(line), cat))
            # 标题条件需要的标题键：本批有演职员命中的作品
            missing = sorted(set(credits) - set(title_cache))
            if missing:
                for tc, rows in _title_keys_of(conn, i.imdb_snapshot_id, missing).items():
                    title_cache[tc] = rows
                for tc in set(missing) - set(title_cache):
                    title_cache[tc] = []
            rows: list[tuple] = []
            entities: list[tuple[str, int]] = []
            for movie in batch:
                n = 0
                page = names.get(movie, {})
                dir_nconsts: dict[str, list[dict]] = defaultdict(list)   # nconst → 命中的页面断言（导演）
                for a in page.get("director", ()):
                    for nconst in index.get(a["normalized_value"], ()):
                        dir_nconsts[nconst].append({"page_key": a["normalized_value"],
                                                    "assertion_id": a["assertion_id"], "asin": a["entity_id"]})
                act_nconsts: dict[str, list[dict]] = defaultdict(list)
                for a in page.get("actor", ()):
                    for nconst in index.get(a["normalized_value"], ()):
                        act_nconsts[nconst].append({"page_key": a["normalized_value"],
                                                    "assertion_id": a["assertion_id"], "asin": a["entity_id"]})
                scores: dict[str, dict[str, Any]] = {}
                for tconst, cr in credits.items():
                    d_hits = [{"nconst": n, "crew_line_no": line, **hit}
                              for n, line in cr["directors"] for hit in dir_nconsts.get(n, ())]
                    a_hits = [{"nconst": n, "crew_line_no": line, **hit}
                              for n, line, _cat in cr["principals"] for hit in act_nconsts.get(n, ())]
                    # 导演只要命中任一导演行记 2 分；每位页面演员（断言）命中主要演员记 1 分
                    score = (rules.cooccur_director_points if d_hits else 0) + \
                        rules.cooccur_actor_points * len({h["assertion_id"] for h in a_hits})
                    if d_hits or a_hits:
                        scores[tconst] = {"score": score, "director_hits": d_hits, "actor_hits": a_hits}
                for tconst, s in sorted(scores.items()):
                    pk = product_keys(movie)
                    title_ok = guard_ok = False
                    matched_key = matched_asin = key_source = None
                    guard_line = None
                    for tk, ks, line in title_cache.get(tconst, ()):
                        if not any(cooccur_title_ok(tk, p) for _, p in pk):
                            continue
                        for asin, p in pk:
                            if cooccur_title_ok(tk, p):
                                matched_key, matched_asin, key_source, title_ok = tk, asin, ks, True
                                guard_ok = cooccur_number_guard(tk, p)
                                guard_line = line
                                break
                        break
                    passed = bool(s["score"] >= rules.cooccur_min_score and title_ok and guard_ok)
                    detail = None
                    if title_ok and not guard_ok:
                        t, p = matched_key, next(pk_ for a, pk_ in pk if a == matched_asin)
                        detail = (f"商品编号 {sorted(number_tokens(p))} 与 IMDb 编号 "
                                  f"{sorted(number_tokens(t))} 不一致")
                    rows.append((i.extraction_id, key, movie, tconst, s["score"], Jsonb(s["director_hits"]),
                                 Jsonb(s["actor_hits"]), title_ok, matched_key, matched_asin, key_source,
                                 guard_line if title_ok else None, guard_ok if title_ok else None, detail, passed,
                                 ctx.run_id))
                    n += 1
                    counts["score_min_1"] += 1
                    if passed:
                        counts["passed"] += 1
                    elif title_ok:
                        counts["excluded_by_number"] += 1
                    else:
                        counts["excluded_by_title"] += 1
                entities.append((movie, n))
            copy_rows(conn, "task6.cooccur_score", SCORE_COLUMNS, rows)
            mark_entities(conn, ctx, stage, batch_no, entities)
            mark_batch(conn, ctx, stage, batch_no, batch[0], batch[-1], len(rows))
            if before_commit is not None:
                before_commit(stage, batch_no)
        counts["movies"] += len(batch)
        counts["scored_rows"] += 0        # 由阶段末统一汇总
        batch_no += 1
    counts["scored_rows"] = conn.execute(
        "SELECT count(*) FROM task6.cooccur_score WHERE extraction_id = %s AND stage_key = %s",
        (i.extraction_id, key)).fetchone()[0]
    return counts


# ---------------------------------------------------------------- 共现消解（R10；择优阶段调用，因为它依赖评论年份提及）

def resolve(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet) -> dict[str, int]:
    """对每个没有 tconst 的作品写 cooccur_result；通过的作品恰好一部（或经评论年份唯一选出）时产生
    imdb_t6_cooccur 候选；被占用的标 shared_tconst 并写质量问题。调用方在一个事务里调用本函数。

    共现阶段键不含人物映射版本（反查不读任务 5），换映射重跑会再次进入本函数：cooccur_result 先删后写
    （无外键引用、确定性重算）；共现候选不删（旧上映信息版本的选择记录按外键引用它们），幂等插入，
    同键已有行保留（阶段键相同即输入相同，内容一致）。"""
    i = ctx.inputs
    key = i.stage_key("cooccur")
    world = World(conn, ctx)
    targets = {m for m, tc in world.movies if not tc}
    conn.execute("DELETE FROM task6.cooccur_result WHERE extraction_id = %s AND stage_key = %s",
                 (i.extraction_id, key))
    # 共现命中的 tconst 不在 World 预取的 involved（关联作品与线索）里，补齐它们的作品行（年份与引用）
    scored = {t for (t,) in conn.execute("SELECT DISTINCT tconst FROM task6.cooccur_score WHERE extraction_id = %s "
                                         "AND stage_key = %s", (i.extraction_id, key))}
    world.imdb.update(sources.imdb_titles(conn, i.imdb_snapshot_id, sorted(scored - set(world.imdb))))
    votes: dict[str, dict[int, int]] = {}
    for movie, year, v in conn.execute("SELECT movie_id, year, votes FROM task6.review_year_mention "
                                       "WHERE extraction_id = %s AND stage_key = %s",
                                       (i.extraction_id, i.stage_key("review_years"))):
        votes.setdefault(movie, {})[year] = v
    passed_by_movie: dict[str, list[tuple[str, int | None]]] = defaultdict(list)
    for movie, tconst in conn.execute("SELECT movie_id, tconst FROM task6.cooccur_score WHERE extraction_id = %s "
                                      "AND stage_key = %s AND passed", (i.extraction_id, key)):
        t = world.imdb.get(tconst, {})
        passed_by_movie[movie].append((tconst, t.get("start_year")))
    result_rows, cand_rows, shared = [], [], 0
    now = reporting.utc_now()
    for movie in sorted(targets):
        passed = sorted(passed_by_movie.get(movie, ()), key=lambda x: x[0])
        tconsts = [t for t, _ in passed]
        chosen, outcome = None, "none"
        if len(passed) == 1:
            chosen, outcome = tconsts[0], "unique"
        elif len(passed) > 1:
            top = top_review_year(votes.get(movie, {}), rules)
            hit = sorted(t for t, y in passed if top is not None and y == top)
            if top is not None and len(hit) == 1:
                chosen, outcome = hit[0], "resolved_by_review_year"
            else:
                outcome = "ambiguous"
        owner = world.tconst_owner.get(chosen) if chosen and world.tconst_owner.get(chosen) != movie else None
        result_rows.append((i.extraction_id, key, movie, outcome, chosen, tconsts, owner, ctx.run_id))
        if chosen:
            year = (world.imdb.get(chosen) or {}).get("start_year")
            if year is None:
                continue                    # 快照里没有该作品或没有年份：不产生候选
            flags = []
            if outcome == "resolved_by_review_year":
                flags.append("review_year_tiebreak")
            if owner:
                flags.append("shared_tconst")
                shared += 1
                add_quality_issue(conn, ctx, movie, "shared_tconst_with_movie",
                                  {"tconst": chosen, "owner_movie_id": owner, "origin": "cooccur"})
            cand_rows.append((i.extraction_id, key, movie, "imdb_t6_cooccur", chosen, str(year), year, "year",
                              "first_release", None, flags, owner, Jsonb(world.imdb_ref(chosen)),
                              world.downloaded_at, now,
                              Jsonb({"outcome": outcome, "review_year_used": outcome == "resolved_by_review_year",
                                     "person_mapping_version": None}),
                              rules.confidence_of("imdb_t6_cooccur", tuple(flags)), i.rule_version, ctx.run_id))
    copy_rows(conn, "task6.cooccur_result", RESULT_COLUMNS, result_rows)
    if cand_rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO task6.release_candidate (" + ", ".join(CAND_COLUMNS) + ") VALUES ("
                + ", ".join(["%s"] * len(CAND_COLUMNS)) + ") ON CONFLICT (extraction_id, stage_key, movie_id, "
                "source, source_key) DO NOTHING", cand_rows)
    return {"movies": len(result_rows), "candidates": len(cand_rows), "shared": shared,
            "unique": sum(1 for r in result_rows if r[3] == "unique"),
            "ambiguous": sum(1 for r in result_rows if r[3] == "ambiguous"),
            "resolved_by_review_year": sum(1 for r in result_rows if r[3] == "resolved_by_review_year"),
            "none": sum(1 for r in result_rows if r[3] == "none")}
