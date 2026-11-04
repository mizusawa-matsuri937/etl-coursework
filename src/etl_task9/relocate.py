"""只读重放补定位（spec R21，用户确认 H03）与评论证据推断上界的全部依赖评论（R22）。

上游只保存到“ASIN + 区域”或只有时间的三处断链，用上游同一规则函数在内存里重放，补出每个最终成员来自哪些页面
断言（区域、序号、任务 4 断言 ID）或哪条评论；重放结果与上游最终值逐项比对：
- 类型词面（target_kind = genre_term）：etl_task7.sources.product_links + genre_terms 读同一批任务 4 genre 断言与任务 3
  面包屑字段，再用 etl_task7.rules.classify_genre_term 归类（与任务 7 _genre_part 相同的分组）；与
  task7.movie_genre_term 的（区域、词面、处置、受控词、商品集合）一致才写定位。
- 版本多值特征（edition_feature）：etl_task7.sources.assertions / page_fields 取与任务 7 resolve_editions 相同的输入，
  整个商品调用 etl_task7.editions.feature_values 必须等于 edition_resolved.attributes_detail；再对每条断言、每个字段
  单独调用同一函数，得到每个特征值来自哪些断言与字段。
- 第二早评论（second_review）：按任务 6 reviews.product_review_times / movie_review_times 的同一排序（商品内按时间、
  源评论 ID 取前两条；作品内按（时间、ASIN）稳定排序）得到最早与第二早评论，核对最早评论 ID、两条时间与 B3 判定
  （etl_task6.rules 的 b3_days 与 _shift）都等于上游；任务 6 只存了第二早评论的时间，ID 由这一步补出。
一致：写 relocation 行（consistent = true）与 relocate 边（relocated = true）；不一致：写 relocation 行
（consistent = false，附上游值与重放值）与 replay_mismatch 质量问题，不写重放边，字段在 T9-03 保持 partial。
全程只读上游表（SELECT），产出只写 task9。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from . import sources
from .rules import assertion_id, sql_as_id
from .run import RunContext, Unit, add_issues

# 与任务 7 resolve_editions 读取的断言字段相同（feature_values 只用其中的特征字段）
EDITION_FIELDS = ["title", "runtime_min", "edition.publisher", "edition.discs", "edition.packaging",
                  "edition.media_format", "edition.language", "edition.subtitles", "edition.aspect_ratio",
                  "edition.region", "edition.cut_type", "edition.video_system"]
REPLAY_GENRE = "etl_task7.sources.genre_terms + etl_task7.rules.classify_genre_term"
REPLAY_FEATURE = "etl_task7.editions.feature_values"
REPLAY_REVIEW = "etl_task6.reviews.movie_review_times (排序) + etl_task6.rules.b3_days"


def t7_inputs(ctx: RunContext, t7rule: str):
    from etl_task7.run import RunInputs as T7Inputs
    i = ctx.inputs
    return T7Inputs(extraction_id=i.extraction_id, t4_mapping_version_id=i.t4_mapping_version_id,
                    t5_person_mapping_version_id=i.t5_person_mapping_version_id,
                    t6_release_version_id=i.t6_release_version_id, imdb_snapshot_id=i.imdb_snapshot_id,
                    parser_version=i.parser_version, norm_version=i.norm_version, rule_version=t7rule)


def _iso(x: Any) -> Any:
    return x.isoformat() if isinstance(x, datetime) else x


def _write_relocations(conn: psycopg.Connection, ctx: RunContext, dk: str, kind: str,
                       rows: list[tuple[str, dict[str, Any], bool, Any, Any]]) -> None:
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO task9.relocation (stage_key, target_kind, target_id, located, consistent, "
                        "upstream_value, replay_value, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING",
                        [(dk, kind, t, Jsonb(loc), ok, Jsonb(up), Jsonb(rep), ctx.run_id) for t, loc, ok, up, rep in rows])
    add_issues(conn, ctx, dk, [("relocation", f"{kind}:{t}", "replay_mismatch", {"upstream": up, "replay": rep})
                               for t, _loc, ok, up, rep in rows if not ok])


def _run_ids(conn: psycopg.Connection, ak: str, ids: list[str]) -> dict[str, str | None]:
    return dict(conn.execute("SELECT assertion_id, upstream_run_id FROM task9.assertion WHERE stage_key = %s "
                             "AND assertion_id = ANY(%s)", (ak, ids)).fetchall())


# ---------------------------------------------------------------- 类型词面

def replay_genre_terms(conn: psycopg.Connection, ti, t4: str, movies: Sequence[str]
                       ) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    """movie → (区域, 词面小写) → {term, disposition, mapped, asins, items}；分组与任务 7 _genre_part 相同。"""
    from etl_task7 import sources as t7s
    from etl_task7.rules import classify_genre_term
    links = t7s.product_links(conn, t4, list(movies))
    terms = t7s.genre_terms(conn, ti, links)
    crumb_asins = sorted({t["asin"] for ts in terms.values() for t in ts if t["section"] == "breadcrumb"})
    crumb_section = {(asin, f["ordinal"], f["value_raw"]): f["section"]
                     for asin, fs in t7s.page_fields(conn, ti, crumb_asins, ["breadcrumb"]).items() for f in fs}
    out: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for movie in movies:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for t in terms.get(movie, ()):
            term, disposition, mapped = classify_genre_term(t["term"])
            if not term:
                continue
            r = rows.setdefault((t["section"], term.casefold()), {"term": term, "disposition": disposition,
                                                                  "mapped": list(mapped), "asins": set(), "items": []})
            r["asins"].add(t["asin"])
            item = {"asin": t["asin"], "ordinal": t["ordinal"]}
            if t["assertion_id"] is not None:
                item["task4_assertion_id"] = t["assertion_id"]
            else:
                item["page_section"] = crumb_section.get((t["asin"], t["ordinal"], t["term"]), t["section"])
            if item not in r["items"]:
                r["items"].append(item)
        out[movie] = rows
    return out


def _genre_unit(conn: psycopg.Connection, ctx: RunContext, p: dict[str, Any]) -> Unit:
    from .derivations import insert_edges
    i = ctx.inputs
    ti = t7_inputs(ctx, p["t7rule"])
    movies = [r[0] for r in conn.execute(
        "SELECT DISTINCT movie_id FROM task7.movie_genre_term WHERE extraction_id = %s AND stage_key = %s ORDER BY 1",
        (i.extraction_id, p["multi"]))]

    def write(batch: Sequence[str], _: Any) -> int:
        replay = replay_genre_terms(conn, ti, i.t4_mapping_version_id, batch)
        upstream: dict[tuple[str, str, str], tuple] = {}
        for movie, section, term, disposition, mapped, asins in conn.execute(
                "SELECT movie_id, section, term, disposition, mapped_genres, asins FROM task7.movie_genre_term "
                "WHERE extraction_id = %s AND stage_key = %s AND movie_id = ANY(%s)",
                (i.extraction_id, p["multi"], list(batch))):
            upstream[(movie, section, term.casefold())] = (term, disposition, list(mapped), sorted(asins))
        relocations, edges = [], []
        for movie in batch:
            rows = replay.get(movie, {})
            keys = {k[1:] for k in upstream if k[0] == movie} | set(rows)
            for section, low in sorted(keys):
                up = upstream.get((movie, section, low))
                rep = rows.get((section, low))
                rep_v = None if rep is None else (rep["term"], rep["disposition"], rep["mapped"], sorted(rep["asins"]))
                ok = up is not None and rep_v is not None and tuple(rep_v) == tuple(up)
                term = up[0] if up else rep["term"]
                target = f"{movie}|{section}|{term}"
                relocations.append((target, {"items": rep["items"] if rep else []}, ok,
                                    None if up is None else dict(zip(("term", "disposition", "mapped", "asins"), up)),
                                    None if rep_v is None else dict(zip(("term", "disposition", "mapped", "asins"),
                                                                        rep_v))))
                if not ok:
                    continue
                child = assertion_id("task7.movie_genre_term", f"{i.conflict_version_id}|{target}")
                for it in rep["items"]:
                    parent = (assertion_id("task4.field_assertion", str(it["task4_assertion_id"]))
                              if "task4_assertion_id" in it else
                              assertion_id("task3.page_field", f"{i.extraction_id}|{it['asin']}|{i.parser_version}|"
                                                               f"{it['page_section']}|{it['ordinal']}"))
                    edges.append(dict(child_id=child, parent_id=parent, operation="relocate",
                                      rule_id="replay:classify_genre_term", rule_version=p["t7rule"],
                                      upstream_task="task7", relocated=True, detail={**it, "replay": REPLAY_GENRE}))
        runs = _run_ids(conn, p["ak"], sorted({e["child_id"] for e in edges}))
        for e in edges:
            e["upstream_run_id"] = runs.get(e["child_id"])
        _write_relocations(conn, ctx, p["dk"], "genre_term", relocations)
        return insert_edges(conn, ctx, p["dk"], edges) + len(relocations)

    return Unit("r_genre", movies, write)


# ---------------------------------------------------------------- 版本多值特征

FEATURES = ("media_format", "language", "subtitles", "aspect_ratio", "region", "cut_type", "video_system")


def replay_features(conn: psycopg.Connection, ti, asins: Sequence[str]
                    ) -> dict[str, tuple[dict[str, dict[str, list[str]]], dict[tuple[str, str], list[dict[str, Any]]]]]:
    """asin → (整商品的 feature_values 结果, (特征, 值) → 来源断言或字段列表)。"""
    from etl_task7 import sources as t7s
    from etl_task7.editions import feature_values
    asrt = t7s.assertions(conn, ti, EDITION_FIELDS, list(asins))
    fields = t7s.page_fields(conn, ti, list(asins), ["mpaa_rating", "format_swatch"])
    out = {}
    for asin in asins:
        rows, fs = asrt.get(asin, []), fields.get(asin, [])
        whole = feature_values(asin, rows, fs)
        contrib: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for a in rows:
            for feat, vals in feature_values(asin, [a], []).items():
                for v in vals:
                    contrib[(feat, v)].append({"task4_assertion_id": a["assertion_id"], "field": a["field_name"],
                                               "section": a["section"], "page_ordinal": a["page_ordinal"]})
        for f in fs:
            for feat, vals in feature_values(asin, [], [f]).items():
                for v in vals:
                    contrib[(feat, v)].append({"page_section": f["section"], "ordinal": f["ordinal"],
                                               "field_key": f["field_key"]})
        out[asin] = (whole, contrib)
    return out


def _feature_unit(conn: psycopg.Connection, ctx: RunContext, p: dict[str, Any]) -> Unit:
    from .derivations import insert_edges
    i = ctx.inputs
    ti = t7_inputs(ctx, p["t7rule"])
    any_feature = " OR ".join(f"cardinality({f}) > 0" for f in FEATURES)
    asins = [r[0] for r in conn.execute(
        f"SELECT asin FROM task7.edition_resolved WHERE conflict_version_id = %s AND ({any_feature}) ORDER BY asin",
        (i.conflict_version_id,))]

    def write(batch: Sequence[str], _: Any) -> int:
        replay = replay_features(conn, ti, batch)
        relocations, edges = [], []
        for row in conn.execute(f"SELECT asin, attributes_detail, {', '.join(FEATURES)} FROM task7.edition_resolved "
                                "WHERE conflict_version_id = %s AND asin = ANY(%s) ORDER BY asin",
                                (i.conflict_version_id, list(batch))).fetchall():
            asin, detail, arrays = row[0], row[1], row[2:]
            whole, contrib = replay[asin]
            same_asin = whole == detail
            for feat, values in zip(FEATURES, arrays):
                for v in values:
                    items = contrib.get((feat, v), [])
                    ok = same_asin and bool(items)
                    target = f"{asin}|{feat}|{v}"
                    relocations.append((target, {"items": items}, ok,
                                        {"sections": (detail.get(feat) or {}).get(v), "asin_detail_equal": same_asin},
                                        {"sections": (whole.get(feat) or {}).get(v)}))
                    if not ok:
                        continue
                    child = assertion_id("task7.edition_resolved", f"{i.conflict_version_id}|{target}")
                    for it in items:
                        parent = (assertion_id("task4.field_assertion", str(it["task4_assertion_id"]))
                                  if "task4_assertion_id" in it else
                                  assertion_id("task3.page_field", f"{i.extraction_id}|{asin}|{i.parser_version}|"
                                                                   f"{it['page_section']}|{it['ordinal']}"))
                        edges.append(dict(child_id=child, parent_id=parent, operation="relocate",
                                          rule_id="replay:feature_values", rule_version=p["t7rule"],
                                          upstream_task="task7", relocated=True, detail={**it, "replay": REPLAY_FEATURE}))
        runs = _run_ids(conn, p["ak"], sorted({e["child_id"] for e in edges}))
        for e in edges:
            e["upstream_run_id"] = runs.get(e["child_id"])
        _write_relocations(conn, ctx, p["dk"], "edition_feature", relocations)
        return insert_edges(conn, ctx, p["dk"], edges) + len(relocations)

    return Unit("r_feature", asins, write)


# ---------------------------------------------------------------- 第二早评论与全部依赖评论（R21、R22）

REVIEWS_SQL = """
SELECT pm.movie_id, s.product_id, s.source_review_id, s.review_time_utc
FROM task4.product_movie pm
JOIN task4.product_edition pe ON pe.extraction_id = %(eid)s AND pe.t3_rule_version = %(t3)s AND pe.asin = pm.asin
                             AND pe.product_kind = 'movie'
JOIN public.review_staging s ON s.extraction_id = %(eid)s AND s.product_id = pm.asin
WHERE pm.mapping_version_id = %(map)s AND pm.relation_type = 'edition' AND pm.member_ordinal = 0
  AND pm.movie_id = ANY(%(movies)s)
ORDER BY pm.movie_id, s.product_id, s.review_time_utc, s.source_review_id
"""


def earliest_two(rows: Sequence[tuple[str, str, datetime]]) -> tuple[tuple, tuple | None]:
    """任务 6 同一算法：rows 为一部作品的 (商品, 源评论 ID, 时间)，已按商品、时间、源评论 ID 排序。商品内取前两条；
    作品内按商品最早评论（时间、ASIN）排列后逐个追加最早、第二早，再按（时间、ASIN）稳定排序。返回
    ((时间, 源评论 ID, ASIN), 第二早或 None)。任务 6 的作品层只保存第二早的时间，这里同时带出 ID。"""
    first: dict[str, tuple[str, datetime]] = {}
    second: dict[str, tuple[str, datetime]] = {}
    for asin, rid, when in rows:
        if asin not in first:
            first[asin] = (rid, when)
        elif asin not in second:
            second[asin] = (rid, when)
    ordered = sorted(first, key=lambda a: (first[a][1], a))
    all_times = []
    for asin in ordered:
        all_times.append((first[asin][1], first[asin][0], asin))
        if asin in second:
            all_times.append((second[asin][1], second[asin][0], asin))
    all_times.sort(key=lambda t: (t[0], t[2]))
    return all_times[0], (all_times[1] if len(all_times) > 1 else None)


_DEP_EDGES = f"""
SELECT 'assertion', b.assertion_id, 'assertion', {sql_as_id("'public.review_staging'", "%(eid)s || '|' || dep.source_review_id")},
       'aggregate', 'review_bound:edition_reviews', b.extract_rule_version, 'task6', b.upstream_run_id, NULL::timestamptz,
       NULL::numeric,
       CASE WHEN dep.source_review_id IN (x.earliest, x.second) THEN 'direct_dependency' END,
       dep.source_review_id IS NOT DISTINCT FROM x.second,
       CASE WHEN dep.source_review_id = x.earliest THEN '{{"rank": 1}}'::jsonb
            WHEN dep.source_review_id = x.second THEN '{{"rank": 2, "relocated_by": "replay"}}'::jsonb
            ELSE '{{}}'::jsonb END
FROM ({sources.REVIEW_DEPENDENCIES_SQL}) dep
JOIN unnest(%(m)s::text[], %(e)s::text[], %(s)s::text[]) AS x(movie_id, earliest, second) ON x.movie_id = dep.movie_id
JOIN task9.assertion b ON b.stage_key = %(ak)s
     AND b.assertion_id = {sql_as_id("'task6.inferred_release_bound'", "%(rv)s || '|' || dep.movie_id")}
"""


def _review_unit(conn: psycopg.Connection, ctx: RunContext, p: dict[str, Any]) -> Unit:
    from etl_task6.rules import _shift, ruleset as t6ruleset
    from .derivations import edge_insert
    i = ctx.inputs
    movies = sources.review_bound_movies(conn, i)
    statement = edge_insert(_DEP_EDGES)

    def write(batch: Sequence[str], _: Any) -> int:
        per_movie: dict[str, list[tuple]] = defaultdict(list)
        for movie, asin, rid, when in conn.execute(REVIEWS_SQL, {"eid": i.extraction_id, "t3": i.t3_rule_version,
                                                                 "map": i.t4_mapping_version_id, "movies": list(batch)}):
            per_movie[movie].append((asin, rid, when))
        upstream = {r[0]: r[1:] for r in conn.execute(
            "SELECT t.movie_id, t.earliest_review_time, t.earliest_source_review_id, t.earliest_asin, "
            "t.second_review_time, (e.checks->>'B3')::boolean, b.rule_version FROM task6.movie_review_time t "
            "JOIN task6.inferred_release_bound b ON b.release_version_id = %s AND b.movie_id = t.movie_id "
            "LEFT JOIN task6.bound_evidence e ON e.release_version_id = b.release_version_id "
            "AND e.movie_id = t.movie_id AND e.evidence = 'review' "
            "WHERE t.extraction_id = %s AND t.t4_mapping_version_id = %s AND t.movie_id = ANY(%s)",
            (i.t6_release_version_id, i.extraction_id, i.t4_mapping_version_id, list(batch)))}
        relocations, m, e, s = [], [], [], []
        for movie in batch:
            up = upstream.get(movie)
            rows = per_movie.get(movie, [])
            rep = earliest_two(rows) if rows else None
            up_v = None if up is None else {"earliest_review_time": _iso(up[0]), "earliest_source_review_id": up[1],
                                            "earliest_asin": up[2], "second_review_time": _iso(up[3]), "B3": up[4]}
            rep_v, sec = None, None
            if rep is not None:
                (t1, r1, a1), sec = rep
                b3 = None
                if up is not None:
                    b3 = sec is not None and sec[0].date().isoformat() <= _shift(t1.date().isoformat(),
                                                                                t6ruleset(up[5]).b3_days)
                rep_v = {"earliest_review_time": _iso(t1), "earliest_source_review_id": r1, "earliest_asin": a1,
                         "second_review_time": _iso(sec[0]) if sec else None, "B3": b3,
                         "second_source_review_id": sec[1] if sec else None, "second_asin": sec[2] if sec else None}
            compared = ("earliest_review_time", "earliest_source_review_id", "earliest_asin", "second_review_time")
            ok = (up_v is not None and rep_v is not None and sec is not None
                  and all(rep_v[k] == up_v[k] for k in compared)
                  and (up_v["B3"] is None or rep_v["B3"] == up_v["B3"]))
            located = {} if rep_v is None else {k: rep_v[k] for k in ("earliest_source_review_id", "earliest_asin",
                                                                      "second_source_review_id", "second_asin",
                                                                      "second_review_time")}
            relocations.append((movie, located, ok, up_v, rep_v))
            m.append(movie)
            e.append(up[1] if up else None)
            s.append(rep_v["second_source_review_id"] if ok else None)
        _write_relocations(conn, ctx, p["dk"], "second_review", relocations)
        params = {**p, **sources.review_params(i, list(batch)), "m": m, "e": e, "s": s}
        return conn.execute(statement, params).rowcount + len(relocations)

    return Unit("r_review", movies, write)


def units(conn: psycopg.Connection, ctx: RunContext, p: dict[str, Any]) -> list[Unit]:
    return [_genre_unit(conn, ctx, p), _feature_unit(conn, ctx, p), _review_unit(conn, ctx, p)]
