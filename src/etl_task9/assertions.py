"""暂存层与集成层 build_assertions（spec R11–R13、R15）：把上游行逐条翻译为字段断言，原样保留上游表、上游键、
上游运行与状态。assertion_id = 'as_' + sha256(上游表 '|' 上游键)[:16]（R12），同一上游行在各版本中相同。

本阶段（T9-01）写入的翻译器（单元名 → 上游）：

| 单元 | 上游 | 级别 | 实体 |
|---|---|---|---|
| t3_field | task3.page_field（解析版本） | extract | edition = ASIN |
| t4_assertion | task4.field_assertion（解析、规范化版本） | 无父 extract，有父 derived | edition、collection_member |
| t5_mention | task5.person_mention（解析、t5 规范化版本） | extract | mention = mention_key |
| imdb_title | imdb.title 各列、类型成员、title_aka、title_director、title_principal | extract | imdb_title = tconst |
| imdb_name | imdb.name 各列 | extract | imdb_name = nconst |
| review | public.review_staging 的评论时间（推断上界依赖的评论） | extract | review = 源评论 ID |
| t6_candidate | task6.release_candidate（rv 选择记录考虑过的） | candidate | movie |
| t7_movie、t7_edition | task7.field_candidate（cv 的 scalar、edition 阶段键） | candidate | movie、edition |

抽取级断言经 source_record_id 指向 sources 阶段键下的源记录（页面按 ASIN、IMDb 按行、评论按源评论 ID 对上）。
候选级断言的上游状态与原因原样保存（R15）：任务 7 的 adopted、rejected、conflict_pending、superseded_by_specific
与 status_reason；任务 6 的 selected、not_selected 与选择规则、理由。
后续阶段（T9-02）在 UNITS 之外追加：task4.merge_decision、task5.link_decision、mention_person、person_decision、
task7.credit_alignment、movie_genre_term 等候选级与派生级断言（extra_units 参数）。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import psycopg

from . import sources
from .rules import sql_as_id
from .run import DEFAULT_BATCH, BeforeCommit, RunContext, Unit, process_units, register_stage_key, \
    require_stage_complete

STAGE = "assertions"
COLUMNS = ("stage_key, assertion_id, level, entity_type, entity_id, field_name, member_key, raw_value, normalized_value, "
           "source_record_id, field_locator, extract_rule_version, rule_id, upstream_status, upstream_reason, "
           "upstream_table, upstream_key, upstream_run_id, confidence, run_id")

# 某单元的 SELECT（列顺序同 COLUMNS），命名参数：sk、ssk（sources 阶段键）、run、eid、parser、norm、t5norm、snap、
# rv、scalar、edition、batch（本批实体）以及 review_params 的各项。
UnitFactory = Callable[[psycopg.Connection, RunContext, dict[str, Any]], Unit]


def _sr_join(alias: str, table: str, key: str) -> str:
    return (f"JOIN task9.source_record {alias} ON {alias}.stage_key = %(ssk)s AND {alias}.upstream_table = '{table}' "
            f"AND {alias}.upstream_key = {key}")


def _ins(conn: psycopg.Connection, select_sql: str, params: dict[str, Any]) -> int:
    cur = conn.execute(f"INSERT INTO task9.assertion ({COLUMNS}) {select_sql} ON CONFLICT DO NOTHING", params)
    return cur.rowcount


# ---------------------------------------------------------------- 页面：任务 3 字段、任务 4 断言、任务 5 提及

_T3_KEY = "f.extraction_id || '|' || f.asin || '|' || f.parser_version || '|' || f.section || '|' || f.ordinal"
T3_FIELD_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task3.page_field'", _T3_KEY)}, 'extract', 'edition', f.asin,
       coalesce(f.field_key, f.section), f.section || ':' || f.ordinal, f.value_raw, NULL, sr.source_record_id,
       jsonb_build_object('section', f.section, 'ordinal', f.ordinal, 'label_raw', f.label_raw, 'field_key', f.field_key,
                          'content_hash', pp.content_hash, 'snapshot_path', pp.snapshot_path),
       f.parser_version, NULL, NULL, NULL, 'task3.page_field', {_T3_KEY}, pp.run_id, NULL, %(run)s
FROM task3.page_field f
JOIN task3.page_parse pp ON pp.extraction_id = f.extraction_id AND pp.asin = f.asin AND pp.parser_version = f.parser_version
{_sr_join('sr', 'task2.product_manifest', "f.extraction_id || '|' || f.asin")}
WHERE f.extraction_id = %(eid)s AND f.parser_version = %(parser)s AND f.asin = ANY(%(batch)s)
"""

_T4_SELECT = f"""
SELECT %(sk)s, {sql_as_id("'task4.field_assertion'", "a.assertion_id::text")},
       CASE WHEN a.parent_assertion_id IS NULL THEN 'extract' ELSE 'derived' END,
       CASE a.entity_type WHEN 'product_edition' THEN 'edition' ELSE 'collection_member' END, a.entity_id,
       a.field_name, a.ordinal::text, a.raw_value, a.normalized_value, sr.source_record_id,
       coalesce(a.source_locator, '{{}}'::jsonb)
         || jsonb_build_object('ordinal', a.ordinal, 'qualifier', a.qualifier, 'task4_assertion_id', a.assertion_id,
                               'parent_assertion_id', a.parent_assertion_id, 'task4_source_record_id', a.source_record_id)
         || CASE WHEN a.extra IS NULL THEN '{{}}'::jsonb ELSE jsonb_build_object('extra', a.extra) END,
       a.norm_version, a.rule_id, NULL::text, NULL::text, 'task4.field_assertion', a.assertion_id::text, NULL::text,
       NULL::numeric, %(run)s
FROM task4.field_assertion a
LEFT JOIN task4.source_record s4 ON s4.source_record_id = a.source_record_id
LEFT JOIN task9.source_record sr ON sr.stage_key = %(ssk)s AND sr.upstream_table = 'task2.product_manifest'
     AND sr.upstream_key = (s4.locator->>'extraction_id') || '|' || (s4.locator->>'asin')
WHERE a.extraction_id = %(eid)s AND a.parser_version = %(parser)s AND a.norm_version = %(norm)s
"""
T4_ASSERTION_SQL = (f"{_T4_SELECT} AND a.entity_id = ANY(%(batch)s) UNION ALL {_T4_SELECT} "
                    "AND a.entity_type = 'collection_member' AND split_part(a.entity_id, '#', 1) = ANY(%(batch)s)")

_T5_KEY = "m.extraction_id || '|' || m.parser_version || '|' || m.norm_version || '|' || m.mention_key"
T5_MENTION_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task5.person_mention'", _T5_KEY)}, 'extract', 'mention', m.mention_key, m.role_class, '',
       m.raw_value, m.display_name, sr.source_record_id,
       jsonb_build_object('asin', m.asin, 'section', m.section, 'page_ordinal', m.page_ordinal,
                          'list_position', m.list_position, 'label_raw', m.label_raw, 'is_byline', m.is_byline,
                          'byline_rank', m.byline_rank, 'strict_key', m.strict_key, 'flags', m.flags,
                          't4_assertion_id', m.t4_assertion_id, 'task4_source_record_id', m.source_record_id),
       m.norm_version, NULL, m.status, NULL, 'task5.person_mention', {_T5_KEY}, m.run_id, NULL, %(run)s
FROM task5.person_mention m
{_sr_join('sr', 'task2.product_manifest', "m.extraction_id || '|' || m.asin")}
WHERE m.extraction_id = %(eid)s AND m.parser_version = %(parser)s AND m.norm_version = %(t5norm)s
  AND m.asin = ANY(%(batch)s)
"""


# ---------------------------------------------------------------- IMDb 行

_TITLE_COLS = """(VALUES ('primary_title', '', t.primary_title), ('original_title', '', t.original_title),
                         ('title_type', '', t.title_type), ('start_year', '', t.start_year::text),
                         ('runtime_minutes', '', t.runtime_minutes::text))"""
IMDB_TITLE_SQL = f"""
SELECT %(sk)s, {sql_as_id("'imdb.title'", "t.snapshot_id || '|' || t.tconst || '|' || c.col || CASE WHEN c.member = '' THEN '' ELSE '|' || c.member END")}, 'extract',
       'imdb_title', t.tconst, c.col, c.member, c.raw, c.raw, sr.source_record_id,
       jsonb_build_object('file', 'title.basics.tsv.gz', 'line_no', t.line_no, 'column', c.col),
       t.snapshot_id, NULL, NULL, NULL, 'imdb.title', t.snapshot_id || '|' || t.tconst || '|' || c.col || CASE WHEN c.member = '' THEN '' ELSE '|' || c.member END,
       NULL, NULL, %(run)s
FROM imdb.title t
{_sr_join('sr', 'imdb.title', "t.snapshot_id || '|' || t.tconst")}
CROSS JOIN LATERAL (SELECT v.col, v.member, v.raw FROM {_TITLE_COLS} AS v(col, member, raw) WHERE v.raw IS NOT NULL
                    UNION ALL SELECT 'genres', g, g FROM unnest(t.genres) AS g) c
WHERE t.snapshot_id = %(snap)s AND t.tconst = ANY(%(batch)s)
"""
IMDB_AKA_SQL = f"""
SELECT %(sk)s, {sql_as_id("'imdb.title_aka'", "k.snapshot_id || '|' || k.tconst || '|' || k.ordering")}, 'extract',
       'imdb_title', k.tconst, 'aka_title', k.ordering::text, k.title, k.title, sr.source_record_id,
       jsonb_build_object('file', 'title.akas.tsv.gz', 'line_no', k.line_no, 'ordering', k.ordering, 'region', k.region,
                          'language', k.language, 'types', k.types, 'attributes', k.attributes,
                          'is_original', k.is_original),
       k.snapshot_id, NULL, NULL, NULL, 'imdb.title_aka', k.snapshot_id || '|' || k.tconst || '|' || k.ordering,
       NULL, NULL, %(run)s
FROM imdb.title_aka k
{_sr_join('sr', 'imdb.title_aka', "k.snapshot_id || '|' || k.tconst || '|' || k.ordering")}
WHERE k.snapshot_id = %(snap)s AND k.tconst = ANY(%(batch)s)
"""
IMDB_DIRECTOR_SQL = f"""
SELECT %(sk)s, {sql_as_id("'imdb.title_director'", "d.snapshot_id || '|' || d.tconst || '|' || d.ordinal")}, 'extract',
       'imdb_title', d.tconst, 'director', d.ordinal::text, d.nconst, d.nconst, sr.source_record_id,
       jsonb_build_object('file', 'title.crew.tsv.gz', 'line_no', d.line_no, 'ordinal', d.ordinal),
       d.snapshot_id, NULL, NULL, NULL, 'imdb.title_director', d.snapshot_id || '|' || d.tconst || '|' || d.ordinal,
       NULL, NULL, %(run)s
FROM imdb.title_director d
{_sr_join('sr', 'imdb.title_director', "d.snapshot_id || '|' || d.tconst")}
WHERE d.snapshot_id = %(snap)s AND d.tconst = ANY(%(batch)s)
"""
IMDB_PRINCIPAL_SQL = f"""
SELECT %(sk)s, {sql_as_id("'imdb.title_principal'", "p.snapshot_id || '|' || p.tconst || '|' || p.ordering")},
       'extract', 'imdb_title', p.tconst, 'principal', p.ordering::text, p.nconst, p.category, sr.source_record_id,
       jsonb_build_object('file', 'title.principals.tsv.gz', 'line_no', p.line_no, 'ordering', p.ordering,
                          'category', p.category, 'job', p.job, 'characters', p.characters),
       p.snapshot_id, NULL, NULL, NULL, 'imdb.title_principal', p.snapshot_id || '|' || p.tconst || '|' || p.ordering,
       NULL, NULL, %(run)s
FROM imdb.title_principal p
{_sr_join('sr', 'imdb.title_principal', "p.snapshot_id || '|' || p.tconst || '|' || p.ordering")}
WHERE p.snapshot_id = %(snap)s AND p.tconst = ANY(%(batch)s)
"""
IMDB_NAME_SQL = f"""
SELECT %(sk)s, {sql_as_id("'imdb.name'", "n.snapshot_id || '|' || n.nconst || '|' || c.col")}, 'extract',
       'imdb_name', n.nconst, c.col, '', c.raw, c.raw, sr.source_record_id,
       jsonb_build_object('file', 'name.basics.tsv.gz', 'line_no', n.line_no, 'column', c.col),
       n.snapshot_id, NULL, NULL, NULL, 'imdb.name', n.snapshot_id || '|' || n.nconst || '|' || c.col,
       NULL, NULL, %(run)s
FROM imdb.name n
{_sr_join('sr', 'imdb.name', "n.snapshot_id || '|' || n.nconst")}
CROSS JOIN LATERAL (VALUES ('primary_name', n.primary_name), ('birth_year', n.birth_year::text),
                           ('death_year', n.death_year::text)) AS c(col, raw)
WHERE n.snapshot_id = %(snap)s AND n.nconst = ANY(%(batch)s) AND c.raw IS NOT NULL
"""


# ---------------------------------------------------------------- 评论时间（推断上界依赖的评论）

REVIEW_SQL = f"""
SELECT DISTINCT ON (s.source_review_id) %(sk)s,
       {sql_as_id("'public.review_staging'", "s.extraction_id || '|' || s.source_review_id")}, 'extract', 'review',
       s.source_review_id, 'review_time', '', s.review_time_raw,
       to_char(s.review_time_utc AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), sr.source_record_id,
       jsonb_build_object('product_id', s.product_id, 'review_time_unix', s.review_time_unix,
                          'field_start_line', s.field_start_lines->'review_time'),
       s.rules_version, NULL, NULL, NULL, 'public.review_staging', s.extraction_id || '|' || s.source_review_id,
       NULL, NULL, %(run)s
FROM ({sources.REVIEW_DEPENDENCIES_SQL}) dep
JOIN public.review_staging s ON s.extraction_id = %(eid)s AND s.source_review_id = dep.source_review_id
{_sr_join('sr', 'public.review_record', "s.extraction_id || '|' || s.source_review_id")}
ORDER BY s.source_review_id
"""


# ---------------------------------------------------------------- 候选：任务 6 年份候选、任务 7 标量候选

T6_CANDIDATE_SQL = f"""
SELECT DISTINCT ON (c.candidate_id) %(sk)s, {sql_as_id("'task6.release_candidate'", "c.candidate_id::text")},
       'candidate', 'movie', c.movie_id, 'release_year', '', c.raw_value, c.value_year::text, NULL,
       c.source_ref || jsonb_build_object('source', c.source, 'source_key', c.source_key, 'flags', c.flags,
                                          'precision', c.precision, 'semantic', c.semantic,
                                          'shared_with_movie_id', c.shared_with_movie_id,
                                          'source_acquired_at', c.source_acquired_at, 'stage_key', c.stage_key),
       c.rule_version, c.source,
       CASE WHEN c.candidate_id = s.selected_candidate_id THEN 'selected' ELSE 'not_selected' END,
       s.selection_rule || ': ' || s.reason, 'task6.release_candidate', c.candidate_id::text, c.run_id, c.confidence,
       %(run)s
FROM task6.release_selection s
CROSS JOIN LATERAL unnest(CASE WHEN s.selected_candidate_id IS NULL THEN s.considered_candidate_ids
                               ELSE s.considered_candidate_ids || s.selected_candidate_id END) AS x(id)
JOIN task6.release_candidate c ON c.candidate_id = x.id
WHERE s.release_version_id = %(rv)s AND s.movie_id = ANY(%(batch)s)
ORDER BY c.candidate_id
"""


def _t7_sql(entity_type: str, key_param: str) -> str:
    return f"""
SELECT %(sk)s, {sql_as_id("'task7.field_candidate'", "f.candidate_id::text")}, 'candidate', f.entity_type, f.entity_id,
       f.field_name, '', f.raw_value, f.normalized_value, NULL,
       f.source_ref || jsonb_build_object('source', f.source, 'source_key', f.source_key, 'precision', f.precision,
                                          'source_acquired_at', f.source_acquired_at, 'stage_key', f.stage_key),
       f.rule_version, f.source, f.status, f.status_reason, 'task7.field_candidate', f.candidate_id::text, f.run_id,
       f.match_reliability, %(run)s
FROM task7.field_candidate f
WHERE f.extraction_id = %(eid)s AND f.stage_key = %({key_param})s AND f.entity_type = '{entity_type}'
  AND f.entity_id = ANY(%(batch)s)
"""


T7_MOVIE_SQL = _t7_sql("movie", "scalar")
T7_EDITION_SQL = _t7_sql("edition", "edition")


# ---------------------------------------------------------------- 阶段入口

def _unit(conn: psycopg.Connection, name: str, entities: list[str], sqls: Sequence[str], params: dict[str, Any],
          extra: Callable[[Sequence[str]], dict[str, Any]] | None = None) -> Unit:
    def write(batch: Sequence[str], _: Any) -> int:
        p = {**params, "batch": list(batch), **(extra(batch) if extra else {})}
        return sum(_ins(conn, sql, p) for sql in sqls)
    return Unit(name, entities, write)


def base_params(ctx: RunContext, sk: str, ssk: str) -> dict[str, Any]:
    i = ctx.inputs
    edition, _link, scalar, _multi = i.cv_stage_keys
    return {"sk": sk, "ssk": ssk, "run": ctx.run_id, "eid": i.extraction_id, "parser": i.parser_version,
            "norm": i.norm_version, "t5norm": i.t5_norm_version, "snap": i.imdb_snapshot_id,
            "rv": i.t6_release_version_id, "scalar": scalar, "edition": edition}


def units(conn: psycopg.Connection, ctx: RunContext, sk: str, ssk: str) -> list[Unit]:
    """T9-01 的翻译器（顺序即写入顺序）。实体集合与 sources 阶段一致：页面按 ASIN、IMDb 按 tconst、nconst。"""
    i = ctx.inputs
    p = base_params(ctx, sk, ssk)
    asins = sources.page_asins(conn, i)
    tconsts = [r[0] for r in conn.execute(
        "SELECT DISTINCT locator->>'tconst' FROM task9.source_record WHERE stage_key = %s AND source_kind = 'imdb_row' "
        "AND upstream_table <> 'imdb.name' ORDER BY 1", (ssk,))]
    nconsts = [r[0] for r in conn.execute(
        "SELECT locator->>'nconst' FROM task9.source_record WHERE stage_key = %s AND upstream_table = 'imdb.name' "
        "ORDER BY 1", (ssk,))]
    movies7, editions7 = sources.t7_entities(conn, i)
    return [
        _unit(conn, "t3_field", asins, [T3_FIELD_SQL], p),
        _unit(conn, "t4_assertion", asins, [T4_ASSERTION_SQL], p),
        _unit(conn, "t5_mention", asins, [T5_MENTION_SQL], p),
        _unit(conn, "imdb_title", sorted(tconsts), [IMDB_TITLE_SQL, IMDB_AKA_SQL, IMDB_DIRECTOR_SQL, IMDB_PRINCIPAL_SQL],
              p),
        _unit(conn, "imdb_name", sorted(nconsts), [IMDB_NAME_SQL], p),
        _unit(conn, "review", sources.review_bound_movies(conn, i), [REVIEW_SQL], p,
              lambda b: sources.review_params(i, list(b))),
        _unit(conn, "t6_candidate", sources.t6_movies(conn, i), [T6_CANDIDATE_SQL], p),
        _unit(conn, "t7_movie", movies7, [T7_MOVIE_SQL], p),
        _unit(conn, "t7_edition", editions7, [T7_EDITION_SQL], p),
    ]


def build_assertions(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                     before_commit: BeforeCommit | None = None,
                     extra_units: Sequence[UnitFactory] = ()) -> dict[str, Any]:
    """翻译全部断言（调用方已持锁并建好运行；sources 阶段须已完成）。extra_units 供后续阶段追加翻译器。"""
    ssk = require_stage_complete(conn, ctx, "sources")
    sk = register_stage_key(conn, ctx, STAGE)
    us = units(conn, ctx, sk, ssk)
    p = base_params(ctx, sk, ssk)
    us += [f(conn, ctx, p) for f in extra_units]
    counts: dict[str, Any] = {"stage_key": sk, "units": process_units(conn, ctx, STAGE, sk, us, batch_size=batch_size,
                                                                      before_commit=before_commit)}
    counts.update(summary(conn, sk))
    return counts


def summary(conn: psycopg.Connection, sk: str) -> dict[str, Any]:
    by_table = {f"{t}/{lvl}": n for t, lvl, n in conn.execute(
        "SELECT upstream_table, level, count(*) FROM task9.assertion WHERE stage_key = %s GROUP BY 1, 2 ORDER BY 1, 2",
        (sk,))}
    by_level = dict(conn.execute("SELECT level, count(*) FROM task9.assertion WHERE stage_key = %s GROUP BY 1 "
                                 "ORDER BY 1", (sk,)).fetchall())
    return {"by_table_level": by_table, "by_level": by_level}
