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
T9-02 追加的集成层翻译器（算法标记 a2；表结构不变）：

| 单元 | 上游 | 级别 | 实体（字段，成员键） |
|---|---|---|---|
| t4_merge | task4.merge_decision（被 product_movie 采用的决策）、task4.product_movie | candidate、derived | edition 或 collection_member（imdb_match，tconst）；product_movie = ASIN（movie_id，成员序号|movie_id） |
| t5_link | task5.link_decision（pmap 的 link 阶段键下全部决策，含 review、reject）、task5.mention_person | candidate、derived | mention（imdb_person，途径|tconst|nconst；person_id） |
| t5_person | task5.person 规范名、task5.person_external_id | derived | person（canonical_name；external_id，imdb） |
| t7_imdb_person | task7.imdb_person_ext | derived | person（imdb_person） |
| t7_credit | task7.credit_alignment、task7.movie_person_resolved | derived | movie（credit_alignment，角色|页面人物|段；director/starring/cast，person_id|角色） |
| t7_genre | task7.movie_genre_term、task7.movie_genre | derived | movie（genre_term，区域|词面；genres，受控词） |
| t7_feature | task7.edition_resolved 的 7 项多值特征 | derived | edition（特征名，值） |
| t6_bound | task6.inferred_release_bound（含两种证据的条件结果与 movie_review_time） | derived | movie（inferred_upper_bound） |

这些断言的父节点由 derivations 阶段写成派生边；多值字段的每个最终成员都有一条 derived 断言，字段选择（T9-03）
只需从选择连到它。上游键里的 task7 版本写冲突消解版本 ID（cv），task5 写 pmap，task6 写 rv，task4 写 map。
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


# ---------------------------------------------------------------- 集成层（T9-02）：合并、人物、演职员、类型、特征、上界

_PM_KEY = "pm.mapping_version_id || '|' || pm.asin || '|' || pm.member_ordinal || '|' || pm.movie_id"
T4_MERGE_SQL = f"""
SELECT DISTINCT ON (d.decision_id) %(sk)s, {sql_as_id("'task4.merge_decision'", "d.decision_id::text")}, 'candidate',
       CASE WHEN strpos(c.left_entity, '#') > 0 THEN 'collection_member' ELSE 'edition' END, c.left_entity,
       'imdb_match', c.right_id, c.right_id, c.right_id, NULL::text,
       jsonb_build_object('candidate_id', c.candidate_id, 'generator', c.generator, 'right_kind', c.right_kind,
                          'snapshot', c.imdb_snapshot_id, 'score', d.score, 'rank', d.rank, 'margin', d.margin,
                          'veto', d.veto, 'features', d.features, 'thresholds', d.thresholds,
                          'decided_by', d.decided_by, 'decided_at', d.decided_at),
       d.rule_version, 'merge:' || d.tier, d.tier, d.reason, 'task4.merge_decision', d.decision_id::text, d.run_id,
       d.confidence, %(run)s
FROM task4.product_movie pm
JOIN task4.merge_decision d ON d.decision_id = pm.decision_id
JOIN task4.candidate c ON c.candidate_id = d.candidate_id
WHERE pm.mapping_version_id = %(map)s AND pm.asin = ANY(%(batch)s)
ORDER BY d.decision_id
"""
T4_PRODUCT_MOVIE_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task4.product_movie'", _PM_KEY)}, 'derived', 'product_movie', pm.asin, 'movie_id',
       pm.member_ordinal || '|' || pm.movie_id, pm.movie_id, pm.movie_id, NULL::text,
       jsonb_build_object('member_ordinal', pm.member_ordinal, 'relation_type', pm.relation_type, 'basis', pm.basis,
                          'decision_id', pm.decision_id, 'human_decision_id', pm.human_decision_id,
                          'match_status', ms.status, 'match_reason', ms.reason),
       mv.rule_version, pm.basis, ms.status, pm.relation_type, 'task4.product_movie', {_PM_KEY}, mv.run_id,
       d.confidence, %(run)s
FROM task4.product_movie pm
JOIN task4.mapping_version mv ON mv.mapping_version_id = pm.mapping_version_id
LEFT JOIN task4.merge_decision d ON d.decision_id = pm.decision_id
LEFT JOIN task4.product_match_status ms ON ms.mapping_version_id = pm.mapping_version_id AND ms.asin = pm.asin
WHERE pm.mapping_version_id = %(map)s AND pm.asin = ANY(%(batch)s)
"""

# 任务 5：外部 ID 判定（pmap 的 link 阶段键下，含未采用的 review、reject）与提及 → 人物（依据、轮次、决策引用）
T5_LINK_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task5.link_decision'", "d.link_decision_id::text")}, 'candidate', 'mention', c.mention_key,
       'imdb_person', c.route || '|' || c.tconst || '|' || c.nconst, c.nconst, c.nconst, NULL::text,
       jsonb_build_object('link_candidate_id', c.link_candidate_id, 'route', c.route, 'tconst', c.tconst,
                          'nconst', c.nconst, 'category', c.category, 'level', c.level, 'cross_role', c.cross_role,
                          'work_movie_id', c.work_movie_id, 'evidence', c.evidence, 'thresholds', d.thresholds,
                          'decided_by', d.decided_by, 'decided_at', d.decided_at),
       d.rule_version, 'link:' || c.level, d.tier, d.reason, 'task5.link_decision', d.link_decision_id::text, d.run_id,
       d.confidence, %(run)s
FROM task5.person_mention m
JOIN task5.link_candidate c ON c.extraction_id = m.extraction_id AND c.stage_key = %(link_key)s
                           AND c.mention_key = m.mention_key
JOIN task5.link_decision d ON d.link_candidate_id = c.link_candidate_id AND d.rule_version = %(t5rule)s
WHERE m.extraction_id = %(eid)s AND m.parser_version = %(parser)s AND m.norm_version = %(t5norm)s
  AND m.asin = ANY(%(batch)s)
"""
_MP_KEY = "mp.person_mapping_version_id || '|' || mp.mention_key"
T5_MENTION_PERSON_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task5.mention_person'", _MP_KEY)}, 'derived', 'mention', mp.mention_key, 'person_id', '',
       mp.person_id, mp.person_id, NULL::text,
       jsonb_build_object('basis', mp.basis, 'round', mp.round, 'group_key', mp.group_key,
                          'attribution', mp.attribution, 'group_reason', gm.reason,
                          'link_decision_ids', CASE WHEN mp.basis IN ('credits', 'filmography') THEN
                              (SELECT coalesce(jsonb_agg(d.link_decision_id ORDER BY d.link_decision_id), '[]')
                               FROM task5.link_candidate c JOIN task5.link_decision d
                                 ON d.link_candidate_id = c.link_candidate_id AND d.rule_version = %(t5rule)s
                               WHERE c.extraction_id = mp.extraction_id AND c.stage_key = %(link_key)s
                                 AND c.mention_key = mp.mention_key AND d.tier = 'auto') ELSE '[]'::jsonb END,
                          'person_decision_ids', CASE WHEN mp.basis = 'co_credit' THEN
                              (SELECT coalesce(jsonb_agg(jsonb_build_object('person_decision_id', pd.person_decision_id,
                                                                            'round', pd.round)
                                                         ORDER BY pd.round, pd.person_decision_id), '[]')
                               FROM task5.person_candidate pc JOIN task5.person_decision pd
                                 ON pd.person_candidate_id = pc.person_candidate_id AND pd.rule_version = %(t5rule)s
                               WHERE pc.extraction_id = mp.extraction_id AND pc.stage_key = %(link_key)s
                                 AND pd.tier = 'auto' AND (pc.group_a = mp.group_key OR pc.group_b = mp.group_key))
                              ELSE '[]'::jsonb END),
       %(t5rule)s, mp.basis, mp.attribution, NULL::text, 'task5.mention_person', {_MP_KEY}, pv.run_id, NULL::numeric,
       %(run)s
FROM task5.mention_person mp
JOIN task5.person_mapping_version pv ON pv.person_mapping_version_id = mp.person_mapping_version_id
JOIN task5.person_mention m ON m.extraction_id = mp.extraction_id AND m.parser_version = mp.parser_version
                           AND m.norm_version = mp.norm_version AND m.mention_key = mp.mention_key
LEFT JOIN task5.group_member gm ON gm.extraction_id = mp.extraction_id AND gm.stage_key = %(link_key)s
                               AND gm.mention_key = mp.mention_key
WHERE mp.person_mapping_version_id = %(pmap)s AND mp.extraction_id = %(eid)s AND m.asin = ANY(%(batch)s)
"""
T5_PERSON_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task5.person'", "p.person_mapping_version_id || '|' || p.person_id")}, 'derived', 'person',
       p.person_id, 'canonical_name', '', p.canonical_name, p.canonical_name, NULL::text,
       jsonb_build_object('status', p.status, 'has_external_id', p.has_external_id, 'external_route', p.external_route,
                          'anchor_key', p.anchor_key, 'canonical_source', p.canonical_source,
                          'mention_count', p.mention_count, 'work_count', p.work_count,
                          'pending_candidates', p.pending_candidates),
       %(t5rule)s, 'canonical_source', p.status, p.canonical_source, 'task5.person',
       p.person_mapping_version_id || '|' || p.person_id, pv.run_id, NULL::numeric, %(run)s
FROM task5.person p
JOIN task5.person_mapping_version pv ON pv.person_mapping_version_id = p.person_mapping_version_id
WHERE p.person_mapping_version_id = %(pmap)s AND p.person_id = ANY(%(batch)s)
UNION ALL
SELECT %(sk)s, {sql_as_id("'task5.person_external_id'", "x.person_mapping_version_id || '|' || x.person_id || '|' || x.system")},
       'derived', 'person', x.person_id, 'external_id', x.system, x.external_id, x.external_id, NULL::text,
       jsonb_build_object('system', x.system, 'snapshot', x.snapshot_id, 'name_line_no', x.name_line_no,
                          'birth_year', x.birth_year, 'death_year', x.death_year,
                          'primary_profession', x.primary_profession, 'basis_decision_ids', x.basis_decision_ids,
                          'external_route', p.external_route),
       %(t5rule)s, coalesce(p.external_route, 'external_id'), p.status, NULL::text, 'task5.person_external_id',
       x.person_mapping_version_id || '|' || x.person_id || '|' || x.system, pv.run_id, NULL::numeric, %(run)s
FROM task5.person_external_id x
JOIN task5.person p ON p.person_mapping_version_id = x.person_mapping_version_id AND p.person_id = x.person_id
JOIN task5.person_mapping_version pv ON pv.person_mapping_version_id = x.person_mapping_version_id
WHERE x.person_mapping_version_id = %(pmap)s AND x.person_id = ANY(%(batch)s)
"""
_IPE_KEY = "e.extraction_id || '|' || e.imdb_snapshot_id || '|' || e.person_id"
T7_IMDB_PERSON_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task7.imdb_person_ext'", _IPE_KEY)}, 'derived', 'person', e.person_id, 'imdb_person', '',
       e.primary_name, e.nconst, NULL::text,
       jsonb_build_object('nconst', e.nconst, 'birth_year', e.birth_year, 'death_year', e.death_year,
                          'in_task5_identity', e.in_task5_identity, 'snapshot', e.imdb_snapshot_id),
       %(t7rule)s, 'imdb_person_ext', NULL::text, NULL::text, 'task7.imdb_person_ext', {_IPE_KEY}, e.run_id,
       NULL::numeric, %(run)s
FROM task7.imdb_person_ext e
WHERE e.extraction_id = %(eid)s AND e.imdb_snapshot_id = %(snap)s AND e.person_id = ANY(%(batch)s)
"""

# 任务 7 作品层多值：署名对齐、演职员成员、类型词面与类型成员（cv 的 multivalue 阶段键）
_CA_KEY = ("%(cv)s || '|' || ca.movie_id || '|' || ca.role || '|' || ca.page_person_id || '|' || ca.segment")
_MPR_KEY = "%(cv)s || '|' || r.movie_id || '|' || r.person_id || '|' || r.role"
_GT_KEY = "%(cv)s || '|' || t.movie_id || '|' || t.section || '|' || t.term"
_MG_KEY = "%(cv)s || '|' || g.movie_id || '|' || g.genre"
T7_CREDIT_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task7.credit_alignment'", _CA_KEY)}, 'derived', 'movie', ca.movie_id, 'credit_alignment',
       ca.role || '|' || ca.page_person_id || '|' || ca.segment, ca.raw_name, ca.segment_name, NULL::text,
       jsonb_build_object('role', ca.role, 'page_person_id', ca.page_person_id, 'segment', ca.segment,
                          'segment_name', ca.segment_name, 'resolved_person_id', ca.resolved_person_id,
                          'nconst', ca.nconst, 'level', ca.level, 'outcome', ca.outcome),
       %(t7rule)s, coalesce(ca.level, ca.outcome), ca.outcome, NULL::text, 'task7.credit_alignment', {_CA_KEY},
       ca.run_id, NULL::numeric, %(run)s
FROM task7.credit_alignment ca
WHERE ca.extraction_id = %(eid)s AND ca.stage_key = %(multi)s AND ca.movie_id = ANY(%(batch)s)
UNION ALL
SELECT %(sk)s, {sql_as_id("'task7.movie_person_resolved'", _MPR_KEY)}, 'derived', 'movie', r.movie_id, r.role,
       r.person_id || '|' || r.role, array_to_string(r.raw_names, ' / '), r.person_id, NULL::text,
       jsonb_build_object('person_id', r.person_id, 'role', r.role, 'source_coverage', r.source_coverage,
                          'credit_order', r.credit_order, 'imdb_ordering', r.imdb_ordering, 'nconst', r.nconst,
                          'raw_names', r.raw_names, 'resolution_note', r.resolution_note, 'tconst', mr.effective_tconst),
       %(t7rule)s, r.source_coverage, r.status, r.resolution_note->>'pending_reason', 'task7.movie_person_resolved',
       {_MPR_KEY}, r.run_id, NULL::numeric, %(run)s
FROM task7.movie_person_resolved r
LEFT JOIN task7.movie_resolved mr ON mr.conflict_version_id = %(cv)s AND mr.movie_id = r.movie_id
WHERE r.extraction_id = %(eid)s AND r.stage_key = %(multi)s AND r.movie_id = ANY(%(batch)s)
"""
T7_GENRE_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task7.movie_genre_term'", _GT_KEY)}, 'derived', 'movie', t.movie_id, 'genre_term',
       t.section || '|' || t.term, t.term, array_to_string(t.mapped_genres, '|'), NULL::text,
       jsonb_build_object('section', t.section, 'term', t.term, 'disposition', t.disposition,
                          'mapped_genres', t.mapped_genres, 'asins', t.asins),
       %(t7rule)s, 'classify_genre_term', t.disposition, NULL::text, 'task7.movie_genre_term', {_GT_KEY}, t.run_id,
       NULL::numeric, %(run)s
FROM task7.movie_genre_term t
WHERE t.extraction_id = %(eid)s AND t.stage_key = %(multi)s AND t.movie_id = ANY(%(batch)s)
UNION ALL
SELECT %(sk)s, {sql_as_id("'task7.movie_genre'", _MG_KEY)}, 'derived', 'movie', g.movie_id, 'genres', g.genre,
       array_to_string(g.raw_terms, '|'), g.genre, NULL::text,
       jsonb_build_object('source_coverage', g.source_coverage, 'raw_terms', g.raw_terms, 'source_ref', g.source_ref),
       %(t7rule)s, 'genre_union', g.source_coverage, NULL::text, 'task7.movie_genre', {_MG_KEY}, g.run_id,
       NULL::numeric, %(run)s
FROM task7.movie_genre g
WHERE g.extraction_id = %(eid)s AND g.stage_key = %(multi)s AND g.movie_id = ANY(%(batch)s)
"""
FEATURES = ("media_format", "language", "subtitles", "aspect_ratio", "region", "cut_type", "video_system")
_FEAT_KEY = "%(cv)s || '|' || e.asin || '|' || f.feat || '|' || f.val"
T7_FEATURE_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task7.edition_resolved'", _FEAT_KEY)}, 'derived', 'edition', e.asin, f.feat, f.val, f.val,
       f.val, NULL::text, jsonb_build_object('feature', f.feat, 'sections', e.attributes_detail->f.feat->f.val),
       %(t7rule)s, 'feature_values', NULL::text, NULL::text, 'task7.edition_resolved', {_FEAT_KEY}, es.run_id,
       NULL::numeric, %(run)s
FROM task7.edition_resolved e
JOIN task7.edition_stage_result es ON es.extraction_id = %(eid)s AND es.stage_key = %(edition)s AND es.asin = e.asin
CROSS JOIN LATERAL ({' UNION ALL '.join(f"SELECT '{x}'::text, unnest(e.{x})" for x in FEATURES)}) AS f(feat, val)
WHERE e.conflict_version_id = %(cv)s AND e.asin = ANY(%(batch)s)
"""

# 任务 6 推断上界：两种证据的条件结果、最早与第二早评论时间与算法版本都记在定位里（R22）
_B_KEY = "b.release_version_id || '|' || b.movie_id"
T6_BOUND_SQL = f"""
SELECT %(sk)s, {sql_as_id("'task6.inferred_release_bound'", _B_KEY)}, 'derived', 'movie', b.movie_id,
       'inferred_upper_bound', '', to_char(b.bound_time, 'YYYY-MM-DD'), to_char(b.bound_time, 'YYYY-MM-DD'), NULL::text,
       jsonb_build_object(
         'bound_precision', b.bound_precision, 'bound_kind', b.bound_kind, 'chosen_evidence', b.chosen_evidence,
         'basis', b.basis, 'conditions', b.conditions, 'algorithm', 'etl_task6.bounds.build_bounds',
         'evidence', (SELECT jsonb_agg(jsonb_build_object('evidence', e.evidence, 'evidence_time', e.evidence_time,
                                                          'precision', e.evidence_precision, 'basis', e.basis,
                                                          'checks', e.checks, 'passed', e.passed,
                                                          'failed_checks', e.failed_checks,
                                                          'confidence', e.confidence) ORDER BY e.evidence)
                      FROM task6.bound_evidence e
                      WHERE e.release_version_id = b.release_version_id AND e.movie_id = b.movie_id),
         'review_time', (SELECT jsonb_build_object('earliest_review_time', t.earliest_review_time,
                                                   'earliest_source_review_id', t.earliest_source_review_id,
                                                   'earliest_asin', t.earliest_asin,
                                                   'second_review_time', t.second_review_time,
                                                   'review_count', t.review_count,
                                                   'reviewed_asin_count', t.reviewed_asin_count,
                                                   'edition_asin_count', t.edition_asin_count)
                         FROM task6.movie_review_time t
                         WHERE t.extraction_id = %(eid)s AND t.t4_mapping_version_id = %(map)s
                           AND t.movie_id = b.movie_id)),
       b.rule_version, b.chosen_evidence, b.status, NULL::text, 'task6.inferred_release_bound', {_B_KEY}, rv.run_id,
       b.confidence, %(run)s
FROM task6.inferred_release_bound b
JOIN task6.release_version rv ON rv.release_version_id = b.release_version_id
WHERE b.release_version_id = %(rv)s AND b.movie_id = ANY(%(batch)s)
"""


def _sorted_ids(conn: psycopg.Connection, sql: str, params: tuple) -> list[str]:
    return sorted({r[0] for r in conn.execute(sql, params)})


def integration_units(conn: psycopg.Connection, ctx: RunContext, p: dict[str, Any]) -> list[Unit]:
    """T9-02 的集成层翻译器（见模块说明）。实体集合只取 R01 指定版本下的行。"""
    i = ctx.inputs
    p = {**p, **upstream_params(conn, ctx)}
    pm_asins = _sorted_ids(conn, "SELECT DISTINCT asin FROM task4.product_movie WHERE mapping_version_id = %s",
                           (i.t4_mapping_version_id,))
    mention_asins = _sorted_ids(conn, "SELECT DISTINCT asin FROM task5.person_mention WHERE extraction_id = %s "
                                "AND parser_version = %s AND norm_version = %s",
                                (i.extraction_id, i.parser_version, i.t5_norm_version))
    persons = _sorted_ids(conn, "SELECT person_id FROM task5.person WHERE person_mapping_version_id = %s",
                          (i.t5_person_mapping_version_id,))
    ext = _sorted_ids(conn, "SELECT person_id FROM task7.imdb_person_ext WHERE extraction_id = %s "
                      "AND imdb_snapshot_id = %s", (i.extraction_id, i.imdb_snapshot_id))
    movies = _sorted_ids(conn, "SELECT movie_id FROM task7.movie_resolved WHERE conflict_version_id = %s",
                         (i.conflict_version_id,))
    editions = _sorted_ids(conn, "SELECT asin FROM task7.edition_resolved WHERE conflict_version_id = %s",
                           (i.conflict_version_id,))
    bounds = _sorted_ids(conn, "SELECT movie_id FROM task6.inferred_release_bound WHERE release_version_id = %s",
                         (i.t6_release_version_id,))
    return [
        _unit(conn, "t4_merge", pm_asins, [T4_MERGE_SQL, T4_PRODUCT_MOVIE_SQL], p),
        _unit(conn, "t5_link", mention_asins, [T5_LINK_SQL, T5_MENTION_PERSON_SQL], p),
        _unit(conn, "t5_person", persons, [T5_PERSON_SQL], p),
        _unit(conn, "t7_imdb_person", ext, [T7_IMDB_PERSON_SQL], p),
        _unit(conn, "t7_credit", movies, [T7_CREDIT_SQL], p),
        _unit(conn, "t7_genre", movies, [T7_GENRE_SQL], p),
        _unit(conn, "t7_feature", editions, [T7_FEATURE_SQL], p),
        _unit(conn, "t6_bound", bounds, [T6_BOUND_SQL], p),
    ]


# ---------------------------------------------------------------- 阶段入口

def _unit(conn: psycopg.Connection, name: str, entities: list[str], sqls: Sequence[str], params: dict[str, Any],
          extra: Callable[[Sequence[str]], dict[str, Any]] | None = None) -> Unit:
    def write(batch: Sequence[str], _: Any) -> int:
        p = {**params, "batch": list(batch), **(extra(batch) if extra else {})}
        return sum(_ins(conn, sql, p) for sql in sqls)
    return Unit(name, entities, write)


def base_params(ctx: RunContext, sk: str, ssk: str) -> dict[str, Any]:
    i = ctx.inputs
    edition, _link, scalar, multi = i.cv_stage_keys
    return {"sk": sk, "ssk": ssk, "run": ctx.run_id, "eid": i.extraction_id, "parser": i.parser_version,
            "norm": i.norm_version, "t5norm": i.t5_norm_version, "snap": i.imdb_snapshot_id,
            "rv": i.t6_release_version_id, "scalar": scalar, "edition": edition, "multi": multi,
            "map": i.t4_mapping_version_id, "pmap": i.t5_person_mapping_version_id, "cv": i.conflict_version_id}


def upstream_params(conn: psycopg.Connection, ctx: RunContext) -> dict[str, Any]:
    """集成层翻译器与派生边共用的上游参数：任务 5 的 link 阶段键与规则版本、任务 7 的规则版本。"""
    i = ctx.inputs
    m, n, r, s = conn.execute(
        "SELECT t4_mapping_version_id, norm_version, rule_version, imdb_snapshot_id FROM task5.person_mapping_version "
        "WHERE person_mapping_version_id = %s", (i.t5_person_mapping_version_id,)).fetchone()
    t7 = conn.execute("SELECT rule_version FROM task7.conflict_version WHERE conflict_version_id = %s",
                      (i.conflict_version_id,)).fetchone()[0]
    return {"link_key": "|".join((m, i.parser_version, n, r, s)), "t5rule": r, "t7rule": t7}


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
        *integration_units(conn, ctx, p),
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
