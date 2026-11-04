"""目标层 build_selections（spec R04、R05、R23–R27）：每个范围内的最终字段（多值字段按成员）一行字段选择，
记录最终值、状态（R24）、采用与考虑过的断言、决策规则、理由、置信度与决策所属版本（R23），并以 select 边
（child_kind = 'selection'，写在 selections 阶段键下）连到采用的断言；商品类别直接连到页面快照源记录。

| 家族 | 范围与目标表 | 采用的断言 |
|---|---|---|
| f_movie_scalar | 作品 5 个标量，`task7.movie_resolved`（cv） | 任务 7 选中候选（movie_field_selection） |
| f_movie_multi | genres、director、starring、cast 的成员；无成员的字段一行状态 | 任务 7 movie_genre、movie_person_resolved 成员 |
| f_tconst | 有效 `tconst`（movie_resolved） | 任务 6 选中候选（release_selection） |
| f_bound | 推断上界（movie_resolved；没有上界的作品一行 missing） | 任务 6 inferred_release_bound |
| f_movie_alias | 作品别名（movie_title_alias） | IMDb aka/title 行或任务 4 标题断言 |
| f_edition_scalar | 版本 6 个标量（edition_resolved） | 任务 7 选中候选（edition_field_selection） |
| f_edition_feature | 版本 7 项特征成员；空数组一行 missing | 任务 7 edition_resolved 成员（重放补定位） |
| f_category | 参与匹配商品的类别（task3.product_classification，映射版本所用规则） | 页面快照源记录 |
| f_product_movie | 商品所属作品成员（task4.product_movie） | 任务 4 product_movie 断言 |
| f_person | 人物规范名、外部 ID、别名（task5）；任务 7 IMDb 独有人物 | 任务 5 person/external_id、提及、IMDb 人名行 |

范围外实体（未进入 cv 的作品、未参与匹配的商品）不生成选择；评分统计（任务 8 三类统计表）不生成选择，
也没有逐条评论边（R05）。评论层由视图 task9.v_review_lineage 提供（R10）。

trace_status（R25）在阶段最后一个单元里沿派生图一次判定（trace_all），值与目标层逐行比对（R26，check_values）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence

import psycopg

from .assertions import FEATURES, base_params, upstream_params
from .derivations import BUCKETS, RUNS, check_graph
from .rules import ol_of, sql_as_id
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, Unit, add_issues, process_units, register_stage_key,
                  require_stage_complete)

STAGE = "selections"
TRACED_OK = ("traced", "relocated", "manual")
SEL_COLUMNS = ("entity_type, entity_id, field_name, member_key, value_text, value_int, value_date, status, "
               "missing_reason, selected_assertion_ids, considered_assertion_ids, decision_rule, reason, confidence, "
               "decision_version, target_ref")
# 家族 SELECT 的列：SEL_COLUMNS 之后再加边用的 src_ids（源记录父节点）、erule（边的规则版本）、etask、erun。
FAM_COLUMNS = SEL_COLUMNS + ", src_ids, erule, etask, erun"
_SID = ("('fs_' || substr(encode(sha256(convert_to(x.entity_type || '|' || x.entity_id || '|' || x.field_name || '|' "
        "|| x.member_key, 'UTF8')), 'hex'), 1, 16))")


def _as(table: str, key: str) -> str:
    return sql_as_id(f"'{table}'", key)


def _ids(table: str, arr: str) -> str:
    return f"ARRAY(SELECT {_as(table, 'c::text')} FROM unnest({arr}) AS c ORDER BY c)"


_NULL_REASON = "CASE WHEN {v} IS NULL THEN coalesce(nullif({s}.reason, ''), {s}.conflict_type, {s}.status) END"
_E = "'{}'::text[], %(t7rule)s::text, 'task7', %(cvrun)s::text"   # 无源记录父节点、任务 7 规则与运行

# ---------------------------------------------------------------- 作品

_MOVIE_COL = {"title": "r.title", "original_title": "r.original_title", "release_year": "r.release_year::text",
              "runtime_minutes": "r.runtime_minutes::text", "mpaa_rating": "r.mpaa_rating"}
_MV = "CASE s.field_name " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in _MOVIE_COL.items()) + " END"
F_MOVIE_SCALAR = f"""
SELECT 'movie', s.movie_id, s.field_name, '', {_MV},
       CASE s.field_name WHEN 'release_year' THEN r.release_year WHEN 'runtime_minutes' THEN r.runtime_minutes END,
       NULL::date, s.status, {_NULL_REASON.format(v=_MV, s='s')},
       {_ids('task7.field_candidate', 's.selected_candidate_ids')}, {_ids('task7.field_candidate', 's.considered_candidate_ids')},
       s.selection_rule, s.reason, s.confidence, %(cv)s::text,
       jsonb_build_object('table', 'task7.movie_resolved', 'conflict_version_id', %(cv)s::text, 'movie_id', s.movie_id,
                          'column', s.field_name), {_E}
FROM task7.movie_field_selection s
JOIN task7.movie_resolved r ON r.conflict_version_id = s.conflict_version_id AND r.movie_id = s.movie_id
WHERE s.conflict_version_id = %(cv)s::text AND s.field_name IN ('title', 'original_title', 'release_year',
                                                          'runtime_minutes', 'mpaa_rating')"""

_MPR_KEY = "%(cv)s::text || '|' || p.movie_id || '|' || p.person_id || '|' || p.role"
_MG_KEY = "%(cv)s::text || '|' || g.movie_id || '|' || g.genre"
F_MOVIE_MULTI = f"""
SELECT 'movie', g.movie_id, 'genres', g.genre, g.genre, NULL::int, NULL::date, 'resolved', NULL::text,
       ARRAY[{_as('task7.movie_genre', _MG_KEY)}], ARRAY[{_as('task7.movie_genre', _MG_KEY)}], 'genre_union',
       g.source_coverage, NULL::numeric, %(cv)s::text,
       jsonb_build_object('table', 'task7.movie_genre', 'stage_key', %(multi)s::text, 'movie_id', g.movie_id,
                          'genre', g.genre), {_E}
FROM task7.movie_genre g
JOIN task7.movie_resolved r ON r.conflict_version_id = %(cv)s::text AND r.movie_id = g.movie_id
WHERE g.extraction_id = %(eid)s::text AND g.stage_key = %(multi)s::text
UNION ALL
SELECT 'movie', p.movie_id, p.role, p.person_id, p.person_id, NULL::int, NULL::date,
       CASE p.status WHEN 'adopted' THEN 'resolved' ELSE 'conflict_unresolved' END, NULL::text,
       ARRAY[{_as('task7.movie_person_resolved', _MPR_KEY)}], ARRAY[{_as('task7.movie_person_resolved', _MPR_KEY)}],
       'credit_' || p.source_coverage, p.status || coalesce(': ' || (p.resolution_note->>'pending_reason'), ''),
       NULL::numeric, %(cv)s::text,
       jsonb_build_object('table', 'task7.movie_person_resolved', 'stage_key', %(multi)s::text, 'movie_id', p.movie_id,
                          'person_id', p.person_id, 'role', p.role), {_E}
FROM task7.movie_person_resolved p
JOIN task7.movie_resolved r ON r.conflict_version_id = %(cv)s::text AND r.movie_id = p.movie_id
WHERE p.extraction_id = %(eid)s::text AND p.stage_key = %(multi)s::text
UNION ALL
SELECT 'movie', s.movie_id, s.field_name, '', NULL::text, NULL::int, NULL::date, s.status,
       coalesce(nullif(s.reason, ''), s.conflict_type, s.status),
       '{{}}'::text[], {_ids('task7.field_candidate', 's.considered_candidate_ids')}, s.selection_rule, s.reason,
       s.confidence, %(cv)s::text,
       jsonb_build_object('table', 'task7.movie_field_selection', 'conflict_version_id', %(cv)s::text,
                          'movie_id', s.movie_id, 'field_name', s.field_name), {_E}
FROM task7.movie_field_selection s
WHERE s.conflict_version_id = %(cv)s::text AND s.field_name IN ('genres', 'director', 'starring', 'cast')
  AND NOT EXISTS (SELECT 1 FROM task7.movie_genre g WHERE s.field_name = 'genres' AND g.extraction_id = %(eid)s::text
                  AND g.stage_key = %(multi)s::text AND g.movie_id = s.movie_id)
  AND NOT EXISTS (SELECT 1 FROM task7.movie_person_resolved p WHERE p.role = s.field_name
                  AND p.extraction_id = %(eid)s::text AND p.stage_key = %(multi)s::text AND p.movie_id = s.movie_id)"""

F_TCONST = f"""
SELECT 'movie', r.movie_id, 'effective_tconst', '', r.effective_tconst, NULL::int, NULL::date,
       CASE WHEN r.effective_tconst IS NULL THEN 'missing' ELSE 'resolved' END,
       CASE WHEN r.effective_tconst IS NULL THEN coalesce(rs.missing_reason, rs.status, 'no_release_selection') END,
       CASE WHEN r.effective_tconst IS NOT NULL AND rs.selected_candidate_id IS NOT NULL
            THEN ARRAY[{_as('task6.release_candidate', 'rs.selected_candidate_id::text')}] ELSE '{{}}'::text[] END,
       {_ids('task6.release_candidate', "coalesce(rs.considered_candidate_ids, '{}')")},
       coalesce(rs.selection_rule, 'tconst_source'), r.tconst_source, rs.confidence, %(cv)s::text,
       jsonb_build_object('table', 'task7.movie_resolved', 'conflict_version_id', %(cv)s::text, 'movie_id', r.movie_id,
                          'column', 'effective_tconst'),
       '{{}}'::text[], %(t6rule)s::text, 'task6', %(rvrun)s::text
FROM task7.movie_resolved r
LEFT JOIN task6.release_selection rs ON rs.release_version_id = %(rv)s::text AND rs.movie_id = r.movie_id
WHERE r.conflict_version_id = %(cv)s::text"""

F_BOUND = f"""
SELECT 'movie', r.movie_id, 'inferred_upper_bound', '', r.inferred_upper_bound::text, NULL::int,
       r.inferred_upper_bound, CASE WHEN r.inferred_upper_bound IS NULL THEN 'missing' ELSE 'inferred' END,
       CASE WHEN r.inferred_upper_bound IS NULL THEN coalesce(b.status, 'no_bound_evidence') END,
       CASE WHEN b.movie_id IS NOT NULL AND r.inferred_upper_bound IS NOT NULL
            THEN ARRAY[{_as('task6.inferred_release_bound', "b.release_version_id || '|' || b.movie_id")}]
            ELSE '{{}}'::text[] END,
       CASE WHEN b.movie_id IS NOT NULL
            THEN ARRAY[{_as('task6.inferred_release_bound', "b.release_version_id || '|' || b.movie_id")}]
            ELSE '{{}}'::text[] END,
       coalesce('bound:' || b.chosen_evidence, 'bound'), b.bound_kind, b.confidence, %(rv)s::text,
       jsonb_build_object('table', 'task7.movie_resolved', 'conflict_version_id', %(cv)s::text, 'movie_id', r.movie_id,
                          'column', 'inferred_upper_bound'),
       '{{}}'::text[], %(t6rule)s::text, 'task6', %(rvrun)s::text
FROM task7.movie_resolved r
LEFT JOIN task6.inferred_release_bound b ON b.release_version_id = %(rv)s::text AND b.movie_id = r.movie_id
WHERE r.conflict_version_id = %(cv)s::text"""

_ALIAS_AS = f"""CASE a.source_ref->>'table'
  WHEN 'imdb.title_aka' THEN {_as('imdb.title_aka', "%(snap)s::text || '|' || (a.source_ref->>'tconst') || '|' || (a.source_ref->>'ordering')")}
  WHEN 'imdb.title' THEN {_as('imdb.title', "%(snap)s::text || '|' || (a.source_ref->>'tconst') || '|' || CASE a.alias_kind WHEN 'original' THEN 'original_title' ELSE 'primary_title' END")}
  WHEN 'task4.field_assertion' THEN {_as('task4.field_assertion', "(a.source_ref->>'assertion_id')")} END"""
F_MOVIE_ALIAS = f"""
SELECT 'movie', a.movie_id, 'alias', a.alias_kind || '|' || a.alias_ordinal, a.alias, NULL::int, NULL::date,
       'resolved', NULL::text, array_remove(ARRAY[{_ALIAS_AS}], NULL), array_remove(ARRAY[{_ALIAS_AS}], NULL),
       'alias_' || a.alias_kind, a.source_ref->>'table', NULL::numeric, %(cv)s::text,
       jsonb_build_object('table', 'task7.movie_title_alias', 'stage_key', %(scalar)s::text, 'movie_id', a.movie_id,
                          'alias_kind', a.alias_kind, 'alias_ordinal', a.alias_ordinal), {_E}
FROM task7.movie_title_alias a
JOIN task7.movie_resolved r ON r.conflict_version_id = %(cv)s::text AND r.movie_id = a.movie_id
WHERE a.extraction_id = %(eid)s::text AND a.stage_key = %(scalar)s::text"""

# ---------------------------------------------------------------- 版本与商品

_ED_COL = {"edition_title": "e.edition_title", "runtime_minutes": "e.runtime_minutes::text",
           "mpaa_rating": "e.mpaa_rating", "publisher": "e.publisher",
           "product_release_date": "e.product_release_date::text", "discs": "e.discs::text"}
_EV = "CASE s.field_name " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in _ED_COL.items()) + " END"
F_EDITION_SCALAR = f"""
SELECT 'edition', s.asin, s.field_name, '', {_EV},
       CASE s.field_name WHEN 'runtime_minutes' THEN e.runtime_minutes WHEN 'discs' THEN e.discs END,
       CASE s.field_name WHEN 'product_release_date' THEN e.product_release_date END,
       s.status, {_NULL_REASON.format(v=_EV, s='s')},
       {_ids('task7.field_candidate', 's.selected_candidate_ids')}, {_ids('task7.field_candidate', 's.considered_candidate_ids')},
       s.selection_rule, s.reason, s.confidence, %(cv)s::text,
       jsonb_build_object('table', 'task7.edition_resolved', 'conflict_version_id', %(cv)s::text, 'asin', s.asin,
                          'column', s.field_name), {_E}
FROM task7.edition_field_selection s
JOIN task7.edition_resolved e ON e.conflict_version_id = s.conflict_version_id AND e.asin = s.asin
WHERE s.conflict_version_id = %(cv)s::text"""

_FEAT_KEY = "%(cv)s::text || '|' || e.asin || '|' || f.feat || '|' || f.val"
_FEATS = " UNION ALL ".join(f"SELECT '{x}'::text, e.{x}" for x in FEATURES)
F_EDITION_FEATURE = f"""
SELECT 'edition', e.asin, f.feat, coalesce(v.val, ''), v.val, NULL::int, NULL::date,
       CASE WHEN v.val IS NULL THEN 'missing' ELSE 'resolved' END,
       CASE WHEN v.val IS NULL THEN 'no_page_value' END,
       CASE WHEN v.val IS NULL THEN '{{}}'::text[]
            ELSE ARRAY[{_as('task7.edition_resolved', _FEAT_KEY.replace('f.val', 'v.val'))}] END,
       CASE WHEN v.val IS NULL THEN '{{}}'::text[]
            ELSE ARRAY[{_as('task7.edition_resolved', _FEAT_KEY.replace('f.val', 'v.val'))}] END,
       'feature_values', NULL::text, NULL::numeric, %(cv)s::text,
       jsonb_build_object('table', 'task7.edition_resolved', 'conflict_version_id', %(cv)s::text, 'asin', e.asin,
                          'column', f.feat), {_E}
FROM task7.edition_resolved e
CROSS JOIN LATERAL ({_FEATS}) AS f(feat, vals)
LEFT JOIN LATERAL unnest(CASE WHEN cardinality(f.vals) > 0 THEN f.vals ELSE ARRAY[NULL::text] END) AS v(val) ON true
WHERE e.conflict_version_id = %(cv)s::text"""

F_CATEGORY = """
SELECT 'edition', e.asin, 'category', '', CASE WHEN c.category IS DISTINCT FROM 'pending' THEN c.category END,
       NULL::int, NULL::date, CASE WHEN c.category IS NULL OR c.category = 'pending' THEN 'missing' ELSE 'resolved' END,
       CASE WHEN c.category IS NULL THEN 'not_classified' WHEN c.category = 'pending'
            THEN coalesce(c.pending_reason, 'pending') END,
       '{}'::text[], '{}'::text[], c.rule_id, c.basis_level, NULL::numeric, %(t3rule)s::text,
       jsonb_build_object('table', 'task3.product_classification', 'rule_version', %(t3rule)s::text, 'asin', e.asin),
       array_remove(ARRAY[sr.source_record_id], NULL), %(t3rule)s::text, 'task3', c.run_id
FROM task7.edition_resolved e
LEFT JOIN task3.product_classification c ON c.extraction_id = %(eid)s::text AND c.asin = e.asin AND c.rule_version = %(t3rule)s::text
LEFT JOIN task9.source_record sr ON sr.stage_key = %(sk)s::text AND sr.upstream_table = 'task2.product_manifest'
     AND sr.upstream_key = %(eid)s::text || '|' || e.asin
WHERE e.conflict_version_id = %(cv)s::text"""

_PM_KEY = "pm.mapping_version_id || '|' || pm.asin || '|' || pm.member_ordinal || '|' || pm.movie_id"
F_PRODUCT_MOVIE = f"""
SELECT 'product_movie', pm.asin, 'movie_id', pm.member_ordinal || '|' || pm.movie_id, pm.movie_id, NULL::int,
       NULL::date, 'resolved', NULL::text, ARRAY[{_as('task4.product_movie', _PM_KEY)}],
       ARRAY[{_as('task4.product_movie', _PM_KEY)}], pm.basis, pm.relation_type || coalesce(': ' || ms.status, ''),
       NULL::numeric, %(map)s::text,
       jsonb_build_object('table', 'task4.product_movie', 'mapping_version_id', %(map)s::text, 'asin', pm.asin,
                          'member_ordinal', pm.member_ordinal, 'movie_id', pm.movie_id),
       '{{}}'::text[], %(t4rule)s::text, 'task4', %(t4run)s::text
FROM task4.product_movie pm
LEFT JOIN task4.product_match_status ms ON ms.mapping_version_id = pm.mapping_version_id AND ms.asin = pm.asin
WHERE pm.mapping_version_id = %(map)s::text"""

# ---------------------------------------------------------------- 人物

_P5 = "'{}'::text[], %(t5rule)s::text, 'task5', %(pmaprun)s::text"
_MENTION = _as('task5.person_mention', "%(eid)s::text || '|' || %(parser)s::text || '|' || %(t5norm)s::text || '|' || mp.mention_key")
F_PERSON = f"""
SELECT 'person', p.person_id, 'canonical_name', '', p.canonical_name, NULL::int, NULL::date, 'resolved', NULL::text,
       ARRAY[{_as('task5.person', "p.person_mapping_version_id || '|' || p.person_id")}],
       ARRAY[{_as('task5.person', "p.person_mapping_version_id || '|' || p.person_id")}],
       'canonical_source', p.canonical_source, NULL::numeric, %(pmap)s::text,
       jsonb_build_object('table', 'task5.person', 'person_mapping_version_id', %(pmap)s::text, 'person_id', p.person_id,
                          'column', 'canonical_name'), {_P5}
FROM task5.person p WHERE p.person_mapping_version_id = %(pmap)s::text
UNION ALL
SELECT 'person', x.person_id, 'external_id', x.system, x.external_id, NULL::int, NULL::date, 'resolved', NULL::text,
       ARRAY[{_as('task5.person_external_id', "x.person_mapping_version_id || '|' || x.person_id || '|' || x.system")}],
       ARRAY[{_as('task5.person_external_id', "x.person_mapping_version_id || '|' || x.person_id || '|' || x.system")}],
       'external_id', NULL::text, NULL::numeric, %(pmap)s::text,
       jsonb_build_object('table', 'task5.person_external_id', 'person_mapping_version_id', %(pmap)s::text,
                          'person_id', x.person_id, 'system', x.system), {_P5}
FROM task5.person_external_id x WHERE x.person_mapping_version_id = %(pmap)s::text
UNION ALL
SELECT 'person', a.person_id, 'alias', a.alias, a.alias, NULL::int, NULL::date, 'resolved', NULL::text,
       coalesce(CASE a.source WHEN 'imdb' THEN
                  (SELECT array_agg({_as('imdb.name', "%(snap)s::text || '|' || x.external_id || '|primary_name'")})
                   FROM task5.person_external_id x WHERE x.person_mapping_version_id = a.person_mapping_version_id
                   AND x.person_id = a.person_id AND x.system = 'imdb')
                ELSE (SELECT array_agg(m.assertion_id ORDER BY m.assertion_id) FROM task5.mention_person mp
                      JOIN task9.assertion m ON m.stage_key = %(ak)s::text AND m.assertion_id = {_MENTION}
                      WHERE mp.person_mapping_version_id = a.person_mapping_version_id AND mp.person_id = a.person_id
                      AND a.alias IN (m.raw_value, m.normalized_value)) END, '{{}}'),
       '{{}}'::text[], 'alias_' || a.source, array_to_string(a.alias_types, ','), NULL::numeric, %(pmap)s::text,
       jsonb_build_object('table', 'task5.person_alias', 'person_mapping_version_id', %(pmap)s::text,
                          'person_id', a.person_id, 'alias', a.alias), {_P5}
FROM task5.person_alias a WHERE a.person_mapping_version_id = %(pmap)s::text
UNION ALL
SELECT 'person', e.person_id, f.field, f.member, f.val, NULL::int, NULL::date,
       CASE WHEN f.val IS NULL THEN 'missing' ELSE 'resolved' END,
       CASE WHEN f.val IS NULL THEN 'imdb_name_missing' END,
       ARRAY[{_as('task7.imdb_person_ext', "e.extraction_id || '|' || e.imdb_snapshot_id || '|' || e.person_id")}],
       ARRAY[{_as('task7.imdb_person_ext', "e.extraction_id || '|' || e.imdb_snapshot_id || '|' || e.person_id")}],
       'imdb_person_ext', NULL::text, NULL::numeric, %(cv)s::text,
       jsonb_build_object('table', 'task7.imdb_person_ext', 'person_id', e.person_id, 'column', f.field), {_E}
FROM task7.imdb_person_ext e
CROSS JOIN LATERAL (VALUES ('canonical_name', '', e.primary_name), ('external_id', 'imdb', e.nconst)) AS f(field, member, val)
WHERE e.extraction_id = %(eid)s::text AND e.imdb_snapshot_id = %(snap)s::text AND NOT e.in_task5_identity"""

FAMILIES = {"f_movie_scalar": F_MOVIE_SCALAR, "f_movie_multi": F_MOVIE_MULTI, "f_tconst": F_TCONST,
            "f_bound": F_BOUND, "f_movie_alias": F_MOVIE_ALIAS, "f_edition_scalar": F_EDITION_SCALAR,
            "f_edition_feature": F_EDITION_FEATURE, "f_category": F_CATEGORY, "f_product_movie": F_PRODUCT_MOVIE,
            "f_person": F_PERSON}

_SEL_T, _SEL_S = ol_of("select")


def selection_insert(family_sql: str) -> str:
    """把一个家族的 SELECT 包成一条语句：按选择 ID 的桶过滤，写字段选择（trace_status 先记 untraced，由 trace_all
    判定），同一语句里写 select 边（父为已存在的采用断言，或家族给出的源记录）。"""
    return f"""
WITH x AS (SELECT * FROM ({family_sql}) AS f({FAM_COLUMNS})),
b AS (SELECT {_SID} AS sid, x.* FROM x WHERE substr({_SID}, 4, 3) = ANY(%(batch)s)),
ins AS (
  INSERT INTO task9.field_selection (stage_key, selection_id, {SEL_COLUMNS}, trace_status, run_id)
  SELECT %(lk)s, b.sid, {', '.join('b.' + c.strip() for c in SEL_COLUMNS.split(','))}, 'untraced', %(run)s FROM b
  ON CONFLICT DO NOTHING RETURNING selection_id),
par AS (
  SELECT b.sid, 'assertion' AS pk, u.pid, b.decision_rule, b.erule, b.etask, b.erun, b.confidence
  FROM b JOIN ins ON ins.selection_id = b.sid
  CROSS JOIN LATERAL unnest(b.selected_assertion_ids) AS u(pid)
  JOIN task9.assertion p ON p.stage_key = %(ak)s AND p.assertion_id = u.pid
  UNION ALL
  SELECT b.sid, 'source_record', u.pid, b.decision_rule, b.erule, b.etask, b.erun, b.confidence
  FROM b JOIN ins ON ins.selection_id = b.sid CROSS JOIN LATERAL unnest(b.src_ids) AS u(pid)),
edges AS (
  INSERT INTO task9.derivation (stage_key, child_kind, child_id, parent_kind, parent_id, operation, ol_type,
                                ol_subtype, rule_id, rule_version, upstream_task, upstream_run_id, occurred_at,
                                confidence, role, relocated, detail, run_id)
  SELECT DISTINCT ON (par.sid, par.pk, par.pid) %(lk)s, 'selection', par.sid, par.pk, par.pid, 'select', '{_SEL_T}',
         '{_SEL_S}', coalesce(par.decision_rule, 'select'), coalesce(par.erule, 'unknown'), par.etask, par.erun,
         coalesce(r.at, clock_timestamp()), par.confidence, 'selected', false, '{{}}'::jsonb, %(run)s
  FROM par LEFT JOIN {RUNS} r ON r.run_id = par.erun
  ORDER BY par.sid, par.pk, par.pid
  ON CONFLICT DO NOTHING RETURNING 1)
SELECT (SELECT count(*) FROM ins) + (SELECT count(*) FROM edges)"""


def selection_params(conn: psycopg.Connection, ctx: RunContext, lk: str, dk: str, ak: str, sk: str) -> dict[str, Any]:
    """各家族共用的参数：阶段键、R01 各版本、上游规则版本与运行（边的 rule_version 与 upstream_run_id）。"""
    i = ctx.inputs
    p = {**base_params(ctx, ak, sk), **upstream_params(conn, ctx), "lk": lk, "dk": dk, "ak": ak, "sk": sk}
    p["cvrun"] = conn.execute("SELECT run_id FROM task7.conflict_version WHERE conflict_version_id = %s",
                              (i.conflict_version_id,)).fetchone()[0]
    p["t6rule"], p["rvrun"] = conn.execute("SELECT rule_version, run_id FROM task6.release_version "
                                           "WHERE release_version_id = %s", (i.t6_release_version_id,)).fetchone()
    p["t4rule"], p["t4run"], p["t3rule"] = conn.execute(
        "SELECT rule_version, run_id, t3_rule_version FROM task4.mapping_version WHERE mapping_version_id = %s",
        (i.t4_mapping_version_id,)).fetchone()
    p["pmaprun"] = conn.execute("SELECT run_id FROM task5.person_mapping_version WHERE person_mapping_version_id = %s",
                                (i.t5_person_mapping_version_id,)).fetchone()[0]
    return p


def _family_unit(conn: psycopg.Connection, name: str, sql: str, params: dict[str, Any]) -> Unit:
    stmt = selection_insert(sql)

    def write(batch: Sequence[str], _: Any) -> int:
        return conn.execute(stmt, {**params, "batch": list(batch)}).fetchone()[0]

    return Unit(name, BUCKETS, write)


def build_selections(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                     before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """写全部字段选择与 select 边；最后一个单元判定 trace_status、核对目标层值并检查派生图（R19、R25、R26）。"""
    sk = require_stage_complete(conn, ctx, "sources")
    ak = require_stage_complete(conn, ctx, "assertions")
    dk = require_stage_complete(conn, ctx, "derivations")
    lk = register_stage_key(conn, ctx, STAGE)
    p = selection_params(conn, ctx, lk, dk, ak, sk)
    units = [_family_unit(conn, name, sql, p) for name, sql in FAMILIES.items()]
    final: dict[str, Any] = {}

    def finish(_batch: Sequence[str], _: Any) -> int:
        final["trace"] = trace_all(conn, ctx, p, issue=True)
        final["values"] = check_values(conn, ctx, p, issue=True)
        final["graph"] = check_graph(conn, ctx, edge_keys=[dk, lk], ak=ak, sk=sk, issue_key=lk)
        return final["trace"]["updated"]

    units.append(Unit("trace_check", ["all"], finish))
    counts: dict[str, Any] = {"stage_key": lk, "units": process_units(conn, ctx, STAGE, lk, units,
                                                                      batch_size=batch_size,
                                                                      before_commit=before_commit)}
    counts.update(final or {"skipped": "trace_check 已在之前的运行中完成"})
    counts.update(summary(conn, lk))
    return counts


# ---------------------------------------------------------------- trace_status（R25）

_OK, _BAD, _MANUAL, _RELOC, _PARTIAL, _BROKEN = 1, 2, 4, 8, 16, 32


def partial_assertions(conn: psycopg.Connection, dk: str, p: dict[str, Any]) -> set[str]:
    """重放与上游不一致（replay_mismatch）的对象对应的断言：类型词面、版本特征成员、推断上界（R21）。
    它们只有 ASIN + 区域，路径到此为止，记为 partial 而不是 untraced。"""
    rows = conn.execute(
        f"""SELECT CASE split_part(q.entity_id, ':', 1)
              WHEN 'genre_term' THEN {_as('task7.movie_genre_term', "%(cv)s || '|' || substr(q.entity_id, 12)")}
              WHEN 'edition_feature' THEN {_as('task7.edition_resolved', "%(cv)s || '|' || substr(q.entity_id, 17)")}
              WHEN 'second_review' THEN {_as('task6.inferred_release_bound', "%(rv)s || '|' || substr(q.entity_id, 15)")}
            END
            FROM task9.quality_issue q WHERE q.stage_key = %(dk)s AND q.issue_code = 'replay_mismatch'""",
        {**p, "dk": dk}).fetchall()
    return {r[0] for r in rows if r[0]}


def trace_all(conn: psycopg.Connection, ctx: RunContext | None, p: dict[str, Any], *,
              issue: bool = False) -> dict[str, Any]:
    """沿派生图（derivations 与 selections 两个阶段键的边）判定每个字段选择的 trace_status：
    - 每个父断言沿全部父边走到源记录（没有父边的断言即断链）；
    - 断链 → untraced；断链处是重放不一致的断言 → partial；
    - 到达的源记录有快照缺失或哈希不符 → evidence_unverified；
    - 到达人工修订源记录 → manual；路径含重放边（relocated）→ relocated；否则 traced。
    优先级 untraced > partial > evidence_unverified > manual > relocated > traced。没有采用断言的选择（缺失、
    未消解）按考虑过的断言判定，都没有时为 untraced；只有非空最终值的非 traced/relocated/manual 计入缺口（R25）。"""
    lk, dk, sk = p["lk"], p["dk"], p["sk"]
    partial = partial_assertions(conn, dk, p)
    bad_src = {r[0]: r[1] for r in conn.execute(
        "SELECT source_record_id, CASE WHEN source_kind = 'manual_revision' THEN 'manual' ELSE 'bad' END "
        "FROM task9.source_record WHERE stage_key = %s AND (source_kind = 'manual_revision' "
        "OR evidence_status IN ('snapshot_missing', 'snapshot_hash_mismatch'))", (sk,))}
    parents: dict[str, list[tuple[bool, str, bool]]] = defaultdict(list)   # 子 → (父是断言, 父 ID, 边是重放)
    sel_parents: dict[str, list[tuple[bool, str, bool]]] = defaultdict(list)
    with conn.transaction(), conn.cursor(name="t9_trace") as cur:
        cur.itersize = 100_000
        cur.execute("SELECT child_kind = 'selection', child_id, parent_kind = 'assertion', parent_id, relocated "
                    "FROM task9.derivation WHERE stage_key = ANY(%s)", ([dk, lk],))
        for is_sel, c, pa, pid, rel in cur:
            (sel_parents if is_sel else parents)[c].append((pa, pid, rel))
    memo: dict[str, int] = {}

    def src_flags(sid: str) -> int:
        k = bad_src.get(sid)
        return _OK | (_MANUAL if k == "manual" else _BAD if k == "bad" else 0)

    def node(aid: str) -> int:
        """迭代 DFS（后序）：断言的标记 = 全部父边标记之并；没有父边即断链。环上的父节点按断链计。"""
        if aid in memo:
            return memo[aid]
        stack: list[tuple[str, bool]] = [(aid, False)]
        onpath: set[str] = set()
        while stack:
            n, expanded = stack.pop()
            if n in memo or (not expanded and n in onpath):
                continue
            ps = parents.get(n)
            if not expanded:
                if not ps:
                    memo[n] = (_OK | _PARTIAL) if n in partial else _BROKEN
                    continue
                onpath.add(n)
                stack.append((n, True))
                stack.extend((pid, False) for pa, pid, _ in ps if pa and pid not in memo and pid not in onpath)
                continue
            onpath.discard(n)
            f = _PARTIAL if n in partial else 0
            for pa, pid, rel in ps:
                f |= (memo.get(pid, _BROKEN) if pa else src_flags(pid)) | (_RELOC if rel else 0)
            memo[n] = f
        return memo[aid]

    def status(flags: int) -> str:
        if flags & _BROKEN and not flags & _PARTIAL:
            return "untraced"
        if flags & _PARTIAL:
            return "partial"
        if flags & _BAD:
            return "evidence_unverified"
        if flags & _MANUAL:
            return "manual"
        if flags & _RELOC:
            return "relocated"
        return "traced"

    rows = conn.execute("SELECT selection_id, selected_assertion_ids, considered_assertion_ids, value_text IS NOT NULL "
                        "FROM task9.field_selection WHERE stage_key = %s", (lk,)).fetchall()
    out: list[tuple[str, str]] = []
    gaps = []
    for sid, sel, con, has_value in rows:
        f = 0
        for pa, pid, rel in sel_parents.get(sid, ()):
            if not pa:
                f |= src_flags(pid) | (_RELOC if rel else 0)
        ids = sel or con
        linked = {pid for pa, pid, _ in sel_parents.get(sid, ()) if pa}
        for a in ids:
            f |= _BROKEN if sel and a not in linked else node(a)    # 采用的断言不存在 → 断链
        if not ids and not sel_parents.get(sid):
            f = _BROKEN
        st = status(f)
        out.append((sid, st))
        if has_value and st not in TRACED_OK:
            gaps.append((sid, st))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS t9_trace_tmp (selection_id text PRIMARY KEY, trace_status text) "
                    "ON COMMIT DROP")
        cur.execute("TRUNCATE t9_trace_tmp")
        with cur.copy("COPY t9_trace_tmp (selection_id, trace_status) FROM STDIN") as cp:
            for r in out:
                cp.write_row(r)
        updated = cur.execute("UPDATE task9.field_selection f SET trace_status = t.trace_status FROM t9_trace_tmp t "
                              "WHERE f.stage_key = %s AND f.selection_id = t.selection_id "
                              "AND f.trace_status IS DISTINCT FROM t.trace_status", (lk,)).rowcount
    if issue and ctx is not None:
        add_issues(conn, ctx, lk, [("field_selection", s, "untraced_value", {"trace_status": st}) for s, st in gaps])
    by: dict[str, int] = defaultdict(int)
    for _s, st in out:
        by[st] += 1
    return {"selections": len(out), "updated": updated, "by_trace_status": dict(by), "gaps": len(gaps)}


# ---------------------------------------------------------------- 与目标层一致（R26）

def check_values(conn: psycopg.Connection, ctx: RunContext | None, p: dict[str, Any], *,
                 issue: bool = False) -> dict[str, Any]:
    """重新从目标层表取各家族的最终值与状态，和 field_selection 逐行比对（实体、字段、成员）：值或状态不同、
    目标层有而选择缺、选择有而目标层没有，都记为 value_mismatch（issue 为真时写质量问题）。返回计数与样例。"""
    union = " UNION ALL ".join(f"SELECT entity_type, entity_id, field_name, member_key, value_text, status "
                               f"FROM ({sql}) AS f({FAM_COLUMNS})" for sql in FAMILIES.values())
    rows = conn.execute(
        f"""WITH t AS ({union}),
            s AS (SELECT selection_id, entity_type, entity_id, field_name, member_key, value_text, status
                  FROM task9.field_selection WHERE stage_key = %(lk)s)
            SELECT coalesce(s.selection_id, t.entity_type || ':' || t.entity_id || ':' || t.field_name || ':'
                            || t.member_key), s.value_text, t.value_text, s.status, t.status,
                   CASE WHEN s.selection_id IS NULL THEN 'missing_selection' WHEN t.entity_id IS NULL
                        THEN 'not_in_target' ELSE 'value_or_status' END
            FROM s FULL JOIN t ON (t.entity_type, t.entity_id, t.field_name, t.member_key)
                                = (s.entity_type, s.entity_id, s.field_name, s.member_key)
            WHERE s.selection_id IS NULL OR t.entity_id IS NULL OR s.value_text IS DISTINCT FROM t.value_text
               OR s.status IS DISTINCT FROM t.status""", p).fetchall()
    if issue and ctx is not None:
        add_issues(conn, ctx, p["lk"], [("field_selection", r[0], "value_mismatch",
                                         {"selection": r[1], "target": r[2], "selection_status": r[3],
                                          "target_status": r[4], "kind": r[5]}) for r in rows])
    return {"mismatches": len(rows), "samples": [list(r) for r in rows[:5]]}


def summary(conn: psycopg.Connection, lk: str) -> dict[str, Any]:
    """选择数、状态与 trace_status 分布、非空值覆盖率（traced + relocated + manual 占非空值的比例，R25）。"""
    by_status = dict(conn.execute("SELECT status, count(*) FROM task9.field_selection WHERE stage_key = %s "
                                  "GROUP BY 1 ORDER BY 1", (lk,)).fetchall())
    by_trace = dict(conn.execute("SELECT trace_status, count(*) FROM task9.field_selection WHERE stage_key = %s "
                                 "GROUP BY 1 ORDER BY 1", (lk,)).fetchall())
    total, ok = conn.execute("SELECT count(*), count(*) FILTER (WHERE trace_status = ANY(%s)) FROM task9.field_selection "
                             "WHERE stage_key = %s AND value_text IS NOT NULL", (list(TRACED_OK), lk)).fetchone()
    edges = conn.execute("SELECT count(*) FROM task9.derivation WHERE stage_key = %s", (lk,)).fetchone()[0]
    issues = dict(conn.execute("SELECT issue_code, count(*) FROM task9.quality_issue WHERE stage_key = %s "
                               "GROUP BY 1 ORDER BY 1", (lk,)).fetchall())
    return {"by_status": by_status, "by_trace_status": by_trace, "non_null": total, "non_null_ok": ok,
            "coverage": round(ok / total, 4) if total else None, "select_edges": edges, "issues": issues}
