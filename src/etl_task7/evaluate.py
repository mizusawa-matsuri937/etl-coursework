"""任务 7 评价工具（spec R61–R66，AC15）：开发池冻结、七层盲测表与 AI 预标注草稿导出、标注导入、指标计算。

用法（项目根目录或容器内）：
    python -m etl_task7.evaluate freeze [--extraction-id <eid>]
    python -m etl_task7.evaluate export [--cv <cv>] [--output-dir outputs] [--force]
    python -m etl_task7.evaluate import <eval_items.csv> --annotator <name> [--cv <cv>] [--draft <draft.csv>] [--allow-partial]
    python -m etl_task7.evaluate metrics [--cv <cv>] [--output-dir outputs]

开发池按 md5(movie_id) 排序取前 20%（freeze 先幂等应用迁移 601，须在首次全量运行前执行）；评价池不含开发池作品，
版本层抽样时任一关联作品在开发池的 ASIN 也排除。七层 120 项：E1 标题 15、E2 年份 15、E3 片长 20、E4 演职员 20
（导演 8、署名对齐 8、署名顺序 4）、E5 类型 15、E6 分级 15、E7 版本层 20（发行商 10、发售日 10）；
E1/E2/E3/E6 优先纳入系统判为 conflict_unresolved 的项（至少 min(可用数, 4)）。抽样可以读系统状态，
盲测表的证据由上游原始表重新生成（不读 task7 候选与选择表），按 (source, source_key) 排序，不含胜出值、状态、冲突类型与层号。
AI 草稿只接收盲测表的行（entity_context 与 raw_candidates_json），不接受数据库连接，因而不可能读到 task7 的选择结果；
ai_reason 只出现在草稿里，不进入 eval_label。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction
from etl_task4.normalize import split_title, strict_name

from . import DEFAULT_T4_MAPPING_VERSION, sources
from .check import cv_row, latest_cv
from .report import output_root
from .rules import CONTROLLED_GENRES, worst_diff_class
from .run import RunInputs, apply_task7_schema

DEV_FRACTION = 5                  # 前 1/5 = 20% 作品属于开发池
TARGET_TOTAL = 120
UNRESOLVED_FLOOR = 4              # R63：E1/E2/E3/E6 每层至少 min(可用数, 4) 项 conflict_unresolved
LAYER_PLAN = {"E1": 15, "E2": 15, "E3": 20, "E4": 20, "E5": 15, "E6": 15, "E7": 20}
FILL_ORDER = ("E3", "E4", "E1", "E7")      # R62：不足时的补足顺序
IMDB_SOURCES = ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur")

# 每层的抽样位：(层, 子类, 项数, 轮转的桶顺序, 是否先取 conflict_unresolved 保底, 预留桶 (名, 项数) 或 None)
# 桶是同一子类内按特征分的组：保底之后按桶顺序轮流各取一项，直到取满；桶内按 md5 稳定排序；不足时用该层余项补足。
SLOTS: tuple[dict[str, Any], ...] = (
    {"layer": "E1", "kind": "title", "n": 15, "floor": True, "reserve": None,
     "buckets": ("cross_source_mismatch_halt", "cross_source_override", "formatting_or_alias", "none")},
    {"layer": "E2", "kind": "release_year", "n": 15, "floor": True, "reserve": None,
     "buckets": ("cross_source_override", "minor_tolerance", "bound_conflict", "none")},
    {"layer": "E3", "kind": "runtime_minutes", "n": 20, "floor": True, "reserve": None,
     "buckets": ("unexplained_gt15", "unexplained_6_15", "cut_or_multidisc", "pal_evidence", "variance_3_5",
                 "tolerance_1_2", "exact", "no_imdb_diff")},
    {"layer": "E4", "kind": "director", "n": 8, "floor": False, "reserve": None,
     "buckets": ("conflict_unresolved", "director_source_disagreement", "formatting_or_alias", "complementary_union",
                 "none")},
    {"layer": "E4", "kind": "credit_align", "n": 8, "floor": False, "reserve": ("L4", 4),
     "buckets": ("tie_not_aligned", "L3", "L2", "LI", "L1")},
    {"layer": "E4", "kind": "billing", "n": 4, "floor": False, "reserve": None,
     "buckets": ("billing_conflict_resolved",)},
    {"layer": "E5", "kind": "genres", "n": 15, "floor": False, "reserve": ("has_unmapped", 7),
     "buckets": ("complementary_union", "none", "has_unmapped")},
    {"layer": "E6", "kind": "mpaa_rating", "n": 15, "floor": True, "reserve": None,
     "buckets": ("unrated_vs_rated", "none")},
    {"layer": "E7", "kind": "publisher", "n": 10, "floor": False, "reserve": None,
     "buckets": ("intra_asin_publisher_conflict", "formatting_or_alias", "none")},
    {"layer": "E7", "kind": "product_release_date", "n": 10, "floor": False, "reserve": None,
     "buckets": ("date_flags", "semantic_distinction", "none")},
)

BLIND_COLUMNS = ("item_id", "entity_type", "entity_id", "target_field", "entity_context", "raw_candidates_json",
                 "human_action", "human_winning_value", "human_evidence", "note")
DRAFT_COLUMNS = ("item_id", "entity_type", "entity_id", "target_field", "entity_context", "raw_candidates_json",
                 "ai_action", "ai_winning_value", "ai_doubt", "ai_reason",
                 "human_action", "human_winning_value", "human_evidence", "note")
ACTIONS = ("resolve", "keep_conflict", "unsure")
SYSTEM_ACTION = {"resolved": "resolve", "conflict_unresolved": "keep_conflict"}

LABELING_GUIDE = """# 任务 7 冲突消解评价标注口径说明

本评价集共 120 项（七层），每行是一个“实体 × 目标字段”：`entity_context` 给出实体概况，`raw_candidates_json` 给出上游原始来源
的候选证据（已按来源排序，不含系统的胜出值、状态、冲突类型与层号）。请独立判断后填写四列：

1. `human_action`（必填）：`resolve`（有可核实的唯一答案）、`keep_conflict`（证据互相矛盾或不足以裁决，应保留冲突）、
   `unsure`（你自己也无法判断，不计入准确率分母）。
2. `human_winning_value`：`resolve` 时必填，`keep_conflict` 与 `unsure` 时留空。格式随目标字段：
   - `title`、`mpaa_rating`、`publisher`：文本（大小写不敏感）；`mpaa_rating` 取 G、PG、PG-13、R、NC-17、X、NR/Unrated。
   - `release_year`、`runtime_minutes`：整数（年份 / 分钟）；`release_year` 是作品首次公开发行年份，不是 DVD 或重制版年份。
   - `product_release_date`：`YYYY-MM-DD`，该商品（光盘或磁带）的发售日。
   - `genres`：受控词表中的词，用 `|` 分隔，顺序无关；词表见 `etl_task7.rules.CONTROLLED_GENRES`（30 词）。
   - `director`：导演姓名，用 `|` 分隔，顺序无关（按严格姓名键比较）。
   - `credit_align:<角色>:<页面人物 ID>`：页面署名与哪位 IMDb 成员是同一人，填该成员的 `nconst`；没有同一人填 `none`。
   - `billing:<角色>:<人物 ID>`：该人物在影片中的署名顺序（整数，从 1 起）。
3. `human_evidence`：可核实的外部依据（网址、片尾字幕、官方资料等）。**AI 草稿标了 `ai_doubt = true` 的项，
   或你的判断与 AI 草稿不同的项，必须填写，否则导入会被拒绝**（`unsure` 除外）。
4. `note`：自己的备注。不得原样粘贴草稿的 `ai_reason`，导入会拒绝。

## 关于 AI 预标注草稿（eval_items_draft.csv）

草稿里的 `ai_action`、`ai_winning_value`、`ai_doubt`、`ai_reason` 由规则式脚本只依据本表的 `raw_candidates_json` 生成
（不读系统的消解结果），仅作人工核对时的线索，绝非事实依据；`ai_reason` 不会写入标注表。请把审核后的结论填在
`human_*` 列，保存为新文件后用 `evaluate import` 导入。
"""


class EvalError(RuntimeError):
    pass


def eval_set_id(cv: str) -> str:
    return f"ev_{cv}"


def eval_dir(output_base: Path, eid: str, cv: str) -> Path:
    return output_root(output_base) / eid / cv / "eval"


def _md5(val: str) -> str:
    return hashlib.md5(val.encode("utf-8")).hexdigest()


def base_field(target_field: str) -> str:
    """credit_align:cast:ps_x → credit_align；其余字段原样。"""
    return target_field.split(":", 1)[0]


# ------------------------------------------------------------------ 1. 开发池冻结（R61）

def freeze_dev_pool(conn: psycopg.Connection, extraction_id: str, t4_mapping_version_id: str) -> dict[str, Any]:
    """按 md5(movie_id) 排序前 20% 作品冻结入 dev_pool。已冻结时直接返回已有信息（幂等，不重算）。"""
    have = conn.execute("SELECT count(*), min(frozen_at), min(t4_mapping_version_id) FROM task7.dev_pool "
                        "WHERE extraction_id = %s", (extraction_id,)).fetchone()
    first_run = conn.execute("SELECT min(started_at) FROM task7.run WHERE extraction_id = %s AND kind = 'resolve'",
                             (extraction_id,)).fetchone()[0]
    if have[0] > 0:
        return {"status": "already_frozen", "dev_movies": have[0], "t4_mapping_version_id": have[2],
                "frozen_at": have[1].isoformat(),
                "frozen_before_first_run": first_run is None or have[1] < first_run}
    movies = sources.movie_ids(conn, t4_mapping_version_id)
    if not movies:
        raise EvalError(f"任务 4 映射版本 {t4_mapping_version_id} 没有作品")
    movies.sort(key=lambda m: (_md5(m), m))
    dev = movies[:math.ceil(len(movies) / DEV_FRACTION)]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("INSERT INTO task7.dev_pool (extraction_id, movie_id, t4_mapping_version_id) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        [(extraction_id, m, t4_mapping_version_id) for m in dev])
    return {"status": "frozen", "dev_movies": len(dev), "total_movies": len(movies),
            "fraction": round(len(dev) / len(movies), 4), "t4_mapping_version_id": t4_mapping_version_id,
            "frozen_before_first_run": first_run is None}


def dev_movie_ids(conn: psycopg.Connection, extraction_id: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT movie_id FROM task7.dev_pool WHERE extraction_id = %s",
                                       (extraction_id,))}


# ------------------------------------------------------------------ 2. 抽样（R62、R63）

def _entry(entity_type: str, entity_id: str, target_field: str, bucket: str, status: str, value: str | None,
           conflict_type: str, **extra: Any) -> dict[str, Any]:
    return {"entity_type": entity_type, "entity_id": entity_id, "target_field": target_field, "bucket": bucket,
            "system_status": status, "system_value": value, "conflict_type": conflict_type,
            "order": _md5(f"{entity_id}:{target_field}"), "unresolved": status == "conflict_unresolved", **extra}


def build_pools(conn: psycopg.Connection, cv: dict[str, Any], dev: set[str]) -> dict[str, list[dict[str, Any]]]:
    """每个抽样位（kind）的候选池：评价池内（不含开发池）且系统给出了 resolved 或 conflict_unresolved 的项。
    missing 没有候选，无从评价，不入池。这里可以读系统状态，只用来分层，不进入盲测表。"""
    cvid, eid = cv["conflict_version_id"], cv["extraction_id"]
    pools: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def movie_rows(field: str) -> list[tuple]:
        return [r for r in conn.execute(
            "SELECT movie_id, status, winning_value, conflict_type FROM task7.movie_field_selection "
            "WHERE conflict_version_id = %s AND field_name = %s AND status IN ('resolved', 'conflict_unresolved')",
            (cvid, field)) if r[0] not in dev]

    def bucket_of(status: str, conflict_type: str) -> str:
        return "conflict_unresolved" if status == "conflict_unresolved" else conflict_type

    bound = {r[0] for r in conn.execute(
        "SELECT entity_id FROM task6.quality_issue WHERE extraction_id = %s AND issue_code = 'bound_before_release_year'",
        (eid,))}
    diff: dict[str, list[str]] = defaultdict(list)
    for m, c in conn.execute("SELECT movie_id, diff_class FROM task7.runtime_diff WHERE extraction_id = %s "
                             "AND stage_key = %s", (eid, cv["scalar_stage_key"])):
        diff[m].append(c)
    unmapped = {r[0] for r in conn.execute(
        "SELECT DISTINCT movie_id FROM task7.movie_genre_term WHERE extraction_id = %s AND stage_key = %s "
        "AND disposition = 'unmapped'", (eid, cv["multivalue_stage_key"]))}

    for m, status, value, ctype in movie_rows("title"):
        pools["title"].append(_entry("movie", m, "title", bucket_of(status, ctype), status, value, ctype))
    for m, status, value, ctype in movie_rows("release_year"):
        b = bucket_of(status, ctype)
        if b == "none" and m in bound:
            b = "bound_conflict"
        pools["release_year"].append(_entry("movie", m, "release_year", b, status, value, ctype))
    for m, status, value, ctype in movie_rows("runtime_minutes"):
        worst = worst_diff_class(diff.get(m, ())) or "no_imdb_diff"
        pools["runtime_minutes"].append(_entry("movie", m, "runtime_minutes", bucket_of(status, worst), status, value,
                                               ctype))
    for m, status, value, ctype in movie_rows("director"):
        pools["director"].append(_entry("movie", m, "director", bucket_of(status, ctype), status, value, ctype))
    for m, status, value, ctype in movie_rows("genres"):
        b = "has_unmapped" if m in unmapped else ctype
        pools["genres"].append(_entry("movie", m, "genres", b, status, value, ctype))
    for m, status, value, ctype in movie_rows("mpaa_rating"):
        pools["mpaa_rating"].append(_entry("movie", m, "mpaa_rating", bucket_of(status, ctype), status, value, ctype))

    # 署名对齐：单人署名（segment = 0）对齐成功（aligned）或同等级并列未对齐（tie_not_aligned）；多人串不抽
    for m, role, pid, outcome, level, nconst in conn.execute(
            "SELECT movie_id, role, page_person_id, outcome, level, nconst FROM task7.credit_alignment "
            "WHERE extraction_id = %s AND stage_key = %s AND segment = 0 AND outcome IN ('aligned', 'tie_not_aligned') "
            "AND role IN ('director', 'cast') AND NOT EXISTS (SELECT 1 FROM task7.credit_alignment b "
            "WHERE b.extraction_id = credit_alignment.extraction_id AND b.stage_key = credit_alignment.stage_key "
            "AND b.movie_id = credit_alignment.movie_id AND b.role = credit_alignment.role "
            "AND b.page_person_id = credit_alignment.page_person_id AND b.segment > 0)", (eid, cv["multivalue_stage_key"])):
        if m in dev:
            continue
        aligned = outcome == "aligned"
        pools["credit_align"].append(_entry(
            "movie", m, f"credit_align:{role}:{pid}", level if aligned else "tie_not_aligned",
            "resolved" if aligned else "conflict_unresolved", nconst if aligned else None,
            "formatting_or_alias" if aligned else "intra_source_tie_unresolved"))
    # 署名顺序：跨版本署名序号不一致的人物
    for m, pid, role, order, note in conn.execute(
            "SELECT movie_id, person_id, role, credit_order, resolution_note FROM task7.movie_person_resolved "
            "WHERE extraction_id = %s AND stage_key = %s AND status = 'adopted' AND credit_order IS NOT NULL "
            "AND (SELECT count(*) FROM jsonb_object_keys(coalesce(resolution_note -> 'billing', '{}'))) > 1",
            (eid, cv["multivalue_stage_key"])):
        if m in dev:
            continue
        pools["billing"].append(_entry("movie", m, f"billing:{role}:{pid}", "billing_conflict_resolved", "resolved",
                                       str(order), "billing_conflict_resolved",
                                       page_person_ids=list((note or {}).get("page_person_ids") or [pid])))
    # 版本层：任一关联作品在开发池的 ASIN 排除
    for asin, field, status, value, ctype, movies, flags in conn.execute(
            "SELECT s.asin, s.field_name, s.status, s.winning_value, s.conflict_type, r.movie_ids, r.date_flags "
            "FROM task7.edition_field_selection s JOIN task7.edition_resolved r ON r.conflict_version_id = "
            "s.conflict_version_id AND r.asin = s.asin WHERE s.conflict_version_id = %s "
            "AND s.field_name IN ('publisher', 'product_release_date') AND s.status IN ('resolved', 'conflict_unresolved')",
            (cvid,)):
        if dev & set(movies or ()):
            continue
        b = "date_flags" if field == "product_release_date" and flags else bucket_of(status, ctype)
        pools[field].append(_entry("edition", asin, field, b, status, value, ctype))
    return pools


def pick(pool: list[dict[str, Any]], n: int, buckets: tuple[str, ...], *, floor: bool,
         reserve: tuple[str, int] | None) -> list[dict[str, Any]]:
    """从一个抽样位的候选池取 n 项（确定性）：① floor 时先取 min(可用, 4) 项 conflict_unresolved；② 预留桶先取
    min(可用, 预留数) 项；③ 按桶顺序轮流各取一项（桶内按 md5 排序）；④ 仍不足用余项按 md5 补足。"""
    ordered = sorted(pool, key=lambda x: x["order"])
    chosen: list[dict[str, Any]] = []
    used: set[int] = set()

    def take(item: dict[str, Any]) -> bool:
        if len(chosen) >= n or id(item) in used:
            return False
        used.add(id(item))
        chosen.append(item)
        return True

    if floor:
        taken = 0
        for item in ordered:
            if taken >= min(UNRESOLVED_FLOOR, n) or not item["unresolved"]:
                continue
            taken += take(item)
    if reserve:
        name, count = reserve
        taken = 0
        for item in ordered:
            if taken >= count:
                break
            if item["bucket"] == name:
                taken += take(item)
    queues = {b: [x for x in ordered if x["bucket"] == b and id(x) not in used] for b in buckets}
    while len(chosen) < n and any(queues.values()):
        for b in buckets:
            while queues[b] and id(queues[b][0]) in used:
                queues[b].pop(0)
            if queues[b]:
                take(queues[b].pop(0))
    for item in ordered:
        take(item)
    return chosen


def sample_items(pools: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """七层抽样：返回 (选中的项, 各层分布)。分布写入 eval_set.strata（目标数、实际数、补足数、各桶与子类计数）。"""
    chosen: list[dict[str, Any]] = []
    strata: dict[str, Any] = {}
    layer_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for slot in SLOTS:
        pool = pools.get(slot["kind"], [])
        layer_pool[slot["layer"]].extend(pool)
        got = pick(pool, slot["n"], slot["buckets"], floor=slot["floor"], reserve=slot["reserve"])
        for x in got:
            x["stratum"] = slot["layer"]
        chosen.extend(got)
        s = strata.setdefault(slot["layer"], {"target": LAYER_PLAN[slot["layer"]], "actual": 0, "filled": 0,
                                              "available": 0, "available_unresolved": 0, "kinds": {}, "buckets": {}})
        s["actual"] += len(got)
        s["available"] += len(pool)
        s["available_unresolved"] += sum(1 for x in pool if x["unresolved"])
        s["kinds"][slot["kind"]] = len(got)
        for b, c in Counter(x["bucket"] for x in got).items():
            s["buckets"][b] = s["buckets"].get(b, 0) + c
    needed = TARGET_TOTAL - len(chosen)
    for layer in FILL_ORDER:
        if needed <= 0:
            break
        have = {(x["entity_id"], x["target_field"]) for x in chosen}
        extra = [x for x in sorted(layer_pool[layer], key=lambda x: x["order"])
                 if (x["entity_id"], x["target_field"]) not in have][:needed]
        for x in extra:
            x["stratum"] = layer
            s = strata[layer]
            s["buckets"][x["bucket"]] = s["buckets"].get(x["bucket"], 0) + 1
        chosen.extend(extra)
        strata[layer]["filled"] += len(extra)
        strata[layer]["actual"] += len(extra)
        needed -= len(extra)
    return chosen, strata


# ------------------------------------------------------------------ 3. 盲测证据（R64）：只读上游原始表

def inputs_of(cv: dict[str, Any]) -> RunInputs:
    return RunInputs(cv["extraction_id"], cv["t4_mapping_version_id"], cv["t5_person_mapping_version_id"],
                     cv["t6_release_version_id"], cv["imdb_snapshot_id"], cv["parser_version"], cv["norm_version"],
                     cv["rule_version"])


def _ev(source: str, key: Any, value: Any, **extra: Any) -> dict[str, Any]:
    return {"source": source, "source_key": str(key), "value": value, **extra}


def _sorted(ev: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(ev, key=lambda e: (e["source"], e["source_key"]))


def movie_background(conn: psycopg.Connection, i: RunInputs, movie: str) -> dict[str, Any]:
    """作品概况与 IMDb 关联（来自任务 4 映射与任务 6 采用候选，均为上游）。"""
    links = sources.product_links(conn, i.t4_mapping_version_id, [movie]).get(movie, [])
    title = conn.execute("SELECT display_title FROM task4.movie WHERE mapping_version_id = %s AND movie_id = %s",
                         (i.t4_mapping_version_id, movie)).fetchone()
    sel = sources.release_selections(conn, i.t6_release_version_id, [movie]).get(movie)
    cand = None
    if sel and sel["selected_candidate_id"]:
        cand = sources.release_candidates(conn, [sel["selected_candidate_id"]]).get(sel["selected_candidate_id"])
    tconst = cand["source_key"] if cand and cand["source"] in IMDB_SOURCES else None
    imdb = sources.imdb_titles(conn, i.imdb_snapshot_id, [tconst]).get(tconst) if tconst else None
    bound = sources.inferred_bounds(conn, i.t6_release_version_id, [movie]).get(movie)
    return {"links": links, "movie_title": title[0] if title else None, "sel6": sel, "tconst": tconst,
            "link_source": cand["source"] if tconst else None, "imdb": imdb, "bound": bound}


def _movie_context(bg: dict[str, Any]) -> dict[str, Any]:
    ctx: dict[str, Any] = {"movie_title": bg["movie_title"],
                           "asins": [{"asin": r["asin"], "relation": r["relation_type"]} for r in bg["links"]],
                           "tconst": bg["tconst"], "imdb_url": f"https://www.imdb.com/title/{bg['tconst']}/"
                           if bg["tconst"] else None, "link_source": bg["link_source"],
                           "imdb_title": bg["imdb"]["primary_title"] if bg["imdb"] else None}
    if bg["bound"]:
        ctx["task6_upper_bound"] = {"time": str(bg["bound"]["bound_time"]), "evidence": bg["bound"]["chosen_evidence"]}
    return ctx


def _edition_asins(bg: dict[str, Any]) -> list[str]:
    return sorted({r["asin"] for r in bg["links"] if r["relation_type"] == "edition"})


def movie_evidence(conn: psycopg.Connection, i: RunInputs, movie: str, kind: str, target_field: str,
                   bg: dict[str, Any]) -> list[dict[str, Any]]:
    """某作品某字段的原始候选证据：页面断言与字段、任务 5 页面署名、任务 6 采用与考虑过的候选、IMDb 表。"""
    ev: list[dict[str, Any]] = []
    tconst, imdb = bg["tconst"], bg["imdb"]
    editions = _edition_asins(bg)
    if kind == "title":
        for a in sources.page_titles(conn, i, {movie: bg["links"]}).get(movie, ()):
            ev.append(_ev("page_title", f"{a['asin']}:{a['assertion_id']}", a["raw_value"], asin=a["asin"],
                          relation=a["relation_type"], member_ordinal=a["member_ordinal"]))
        if imdb:
            ev.append(_ev("imdb_primary", tconst, imdb["primary_title"]))
            if imdb["original_title"]:
                ev.append(_ev("imdb_original", tconst, imdb["original_title"]))
            for a in sources.imdb_akas(conn, i.imdb_snapshot_id, [tconst]).get(tconst, ()):
                ev.append(_ev("imdb_aka", f"{tconst}:{a['ordering']}", a["title"], region=a["region"],
                              language=a["language"]))
    elif kind == "release_year":
        sel = bg["sel6"]
        for c in sources.release_candidates(conn, list(sel["considered"] if sel else ())).values():
            ev.append(_ev(c["source"], c["source_key"], c["raw_value"], year=c["value_year"],
                          flags=list(c["flags"] or ())))
    elif kind == "runtime_minutes":
        fields = ["runtime_min", "edition.cut_type", "edition.discs", "edition.video_system", "title"]
        found = sources.assertions(conn, i, fields, editions)
        for asin in editions:
            by = defaultdict(list)
            for a in found.get(asin, ()):
                by[a["field_name"]].append(a)
            attrs = {"title": (by["title"][0]["raw_value"] if by["title"] else None),
                     "cut_type": [a["raw_value"] for a in by["edition.cut_type"]],
                     "discs": [a["raw_value"] for a in by["edition.discs"]],
                     "video_system": [a["raw_value"] for a in by["edition.video_system"]]}
            for a in by["runtime_min"]:
                ev.append(_ev("page_runtime", f"{asin}:{a['assertion_id']}", a["raw_value"], asin=asin,
                              section=a["section"], edition=attrs))
        if imdb and imdb["runtime_minutes"] is not None:
            ev.append(_ev("imdb_runtime", tconst, imdb["runtime_minutes"]))
    elif kind == "mpaa_rating":
        for asin, rows in sources.page_fields(conn, i, editions, ["mpaa_rating"]).items():
            for r in rows:
                ev.append(_ev("page_rating", f"{asin}:{r['section']}:{r['ordinal']}", r["value_raw"], asin=asin,
                              section=r["section"]))
    elif kind == "genres":
        if imdb:
            ev.append(_ev("imdb_genres", tconst, list(imdb["genres"])))
        for t in sources.genre_terms(conn, i, {movie: bg["links"]}).get(movie, ()):
            ev.append(_ev(f"page_{t['section']}", f"{t['asin']}:{t['section']}:{t['ordinal']}", t["term"],
                          asin=t["asin"]))
    elif kind in ("director", "credit_align"):
        role = "director" if kind == "director" else target_field.split(":")[1]
        want = None if kind == "director" else target_field.split(":")[2]
        for p in sources.page_credits(conn, i, [movie]).get(movie, {}).get(role, ()):
            if want is None or p["person_id"] == want:
                ev.append(_ev("page_director" if kind == "director" else "page_credit", p["person_id"], p["names"],
                              nconst=p["nconst"], mentions=[{"asin": a, "byline_rank": r} for a, r in p["sources"]]))
        if tconst:
            if role == "director":
                for d in sources.imdb_directors(conn, i.imdb_snapshot_id, [tconst]).get(tconst, ()):
                    ev.append(_ev("imdb_director", d["nconst"], d["name"], ordinal=d["ordinal"]))
            else:
                for d in sources.imdb_principals(conn, i.imdb_snapshot_id, [tconst]).get(tconst, ()):
                    ev.append(_ev("imdb_credit", d["nconst"], d["name"], ordinal=d["ordinal"],
                                  category=d["category"]))
    return _sorted(ev)


def billing_evidence(conn: psycopg.Connection, i: RunInputs, movie: str, role: str, page_person_ids: list[str],
                     bg: dict[str, Any]) -> list[dict[str, Any]]:
    relation = {r["asin"]: r["relation_type"] for r in bg["links"]}
    ev = []
    for asin, mention, rank, pid, raw in conn.execute(
            "SELECT s.asin, s.mention_key, s.byline_rank, s.person_id, m.raw_value FROM task5.movie_person_source s "
            "LEFT JOIN task5.person_mention m ON m.extraction_id = %s AND m.mention_key = s.mention_key "
            "WHERE s.person_mapping_version_id = %s AND s.movie_id = %s AND s.role = %s AND s.person_id = ANY(%s)",
            (i.extraction_id, i.t5_person_mapping_version_id, movie, role, page_person_ids)):
        ev.append(_ev("page_billing", f"{asin}:{mention}", rank, asin=asin, relation=relation.get(asin),
                      raw_name=raw, page_person_id=pid))
    return _sorted(ev)


def edition_evidence(conn: psycopg.Connection, i: RunInputs, asin: str, kind: str
                     ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    title = sources.assertions(conn, i, ["title"], [asin]).get(asin, [])
    movies = sources.edition_movies(conn, i.t4_mapping_version_id, [asin]).get(asin, [])
    titles = [r[0] for r in conn.execute("SELECT display_title FROM task4.movie WHERE mapping_version_id = %s "
                                         "AND movie_id = ANY(%s) ORDER BY movie_id", (i.t4_mapping_version_id, movies))]
    ctx = {"asin": asin, "amazon_url": f"https://www.amazon.com/dp/{asin}",
           "product_title": title[0]["raw_value"] if title else None, "movies": titles}
    ev: list[dict[str, Any]] = []
    if kind == "publisher":
        for a in sources.assertions(conn, i, ["edition.publisher"], [asin]).get(asin, ()):
            ev.append(_ev("page_publisher", f"{asin}:{a['assertion_id']}", a["raw_value"], section=a["section"]))
    else:
        for d in sources.product_dates(conn, i, [asin]).get(asin, ()):
            ev.append(_ev(f"page_date_{d['kind']}", f"{asin}:{d['assertion_id']}", d["value_date"].isoformat(),
                          raw=d["raw_value"], label=d["label_raw"], section=d["section"], flags=list(d["flags"] or ())))
    return ctx, _sorted(ev)


def director_names(conn: psycopg.Connection, cv: dict[str, Any], movie: str) -> str:
    """系统采用的导演姓名（IMDb 姓名优先，否则页面写法），用 | 连接——盲测的标注人填姓名，比较也按姓名。"""
    names = []
    for nconst, raw_names, pid in conn.execute(
            "SELECT nconst, raw_names, person_id FROM task7.movie_person_resolved WHERE extraction_id = %s "
            "AND stage_key = %s AND movie_id = %s AND role = 'director' AND status = 'adopted' "
            "ORDER BY credit_order NULLS LAST, person_id", (cv["extraction_id"], cv["multivalue_stage_key"], movie)):
        name = None
        if nconst:
            row = conn.execute("SELECT primary_name FROM imdb.name WHERE snapshot_id = %s AND nconst = %s",
                               (cv["imdb_snapshot_id"], nconst)).fetchone()
            name = row[0] if row else None
        if not name and raw_names:
            name = raw_names[0]
        if not name:
            row = conn.execute("SELECT canonical_name FROM task5.person WHERE person_mapping_version_id = %s "
                               "AND person_id = %s", (cv["t5_person_mapping_version_id"], pid)).fetchone()
            name = row[0] if row else pid
        names.append(name)
    return "|".join(names)


def blind_row(conn: psycopg.Connection, i: RunInputs, item: dict[str, Any]) -> dict[str, Any]:
    """一项的盲测行：entity_context 与 raw_candidates_json。只调用上游读取函数，不碰 task7 的候选与选择表。"""
    kind, target = base_field(item["target_field"]), item["target_field"]
    if item["entity_type"] == "edition":
        ctx, ev = edition_evidence(conn, i, item["entity_id"], kind)
    else:
        bg = movie_background(conn, i, item["entity_id"])
        ctx = _movie_context(bg)
        if kind == "billing":
            _, role, _pid = target.split(":", 2)
            ev = billing_evidence(conn, i, item["entity_id"], role, item["page_person_ids"], bg)
        else:
            ev = movie_evidence(conn, i, item["entity_id"], kind, target, bg)
    return {"item_id": item["item_id"], "entity_type": item["entity_type"], "entity_id": item["entity_id"],
            "target_field": target, "entity_context": json.dumps(ctx, ensure_ascii=False, sort_keys=True, default=str),
            "raw_candidates_json": json.dumps(ev, ensure_ascii=False, sort_keys=True, default=str)}


# ------------------------------------------------------------------ 4. AI 预标注草稿（R66）：只接收盲测行

_VOCAB = {g.casefold(): g for g in CONTROLLED_GENRES}
_GENRE_ALIASES = {"science fiction": "Sci-Fi", "sci fi": "Sci-Fi", "film noir": "Film-Noir", "kids & family": "Family",
                  "sport": "Sport", "sports": "Sport", "musicals": "Musical", "animated": "Animation"}
_GENRE_NOISE = {"movies & tv", "movies", "dvd", "blu-ray", "vhs", "featured categories", "general", "classics",
                "classic", "feature", "digital movie", "buy movie", "rent movie", "prime video"}
_RATING_RE = re.compile(r"^\s*(NC[- ]?17|PG[- ]?13|PG|G|R|X|NR|UNRATED|NOT RATED)\b", re.I)
_PUBLISHER_NOISE = {"home", "video", "entertainment", "pictures", "studios", "inc", "corp", "the", "company", "co"}


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", t).split())


def _ratio(a: str, b: str) -> float:
    ta, tb = " ".join(sorted(_fold(a).split())), " ".join(sorted(_fold(b).split()))
    return SequenceMatcher(None, ta, tb).ratio()


def _ints(texts: list[Any]) -> list[int]:
    """页面片长文本 → 分钟：支持 "96 minutes"、"1 hour and 24 minutes"、"2 hours"，其余取第一个整数。"""
    out = []
    for t in texts:
        text = str(t)
        h = re.search(r"(\d+)\s*h(?:ours?|rs?)?\b", text, re.I)
        m = re.search(r"(\d+)\s*m(?:in(?:ute)?s?)?\b", text, re.I)
        if h:
            out.append(int(h.group(1)) * 60 + (int(m.group(1)) if m else 0))
        elif (first := re.search(r"\d+", text)):
            out.append(int(first.group()))
    return out


def _mode(values: list[Any]) -> Any:
    top = Counter(values).most_common()
    best = max(c for _, c in top)
    return sorted(v for v, c in top if c == best)[0]


def _draft(action: str, value: str, doubt: bool, reason: str) -> dict[str, str]:
    return {"ai_action": action, "ai_winning_value": value, "ai_doubt": "true" if doubt else "false",
            "ai_reason": reason}


def _suspect_link(ctx: dict[str, Any]) -> bool:
    """IMDb 关联看来是错片：有 IMDb 标题，但与作品标题既不相近也不互含。"""
    a, b = ctx.get("movie_title"), ctx.get("imdb_title")
    if not ctx.get("tconst") or not a or not b:
        return False
    fa, fb = _fold(a), _fold(b)
    return not (fa in fb or fb in fa or _ratio(a, b) >= 0.6)


def generate_ai_draft(row: dict[str, Any]) -> dict[str, str]:
    """AI 预标注：只依据盲测行的 entity_context 与 raw_candidates_json，用简单规则给出建议，不接受数据库连接。
    它是人工核对的线索，不是结论；凡证据互相矛盾或需要外部知识的项一律 ai_doubt = true。"""
    ctx = json.loads(row["entity_context"])
    ev = json.loads(row["raw_candidates_json"])
    kind = base_field(row["target_field"])
    by = defaultdict(list)
    for e in ev:
        by[e["source"]].append(e)
    suspect = _suspect_link(ctx)
    if kind == "title":
        imdb_keys = {split_title(e["value"]).key for s in ("imdb_primary", "imdb_original", "imdb_aka") for e in by[s]}
        pages = by["page_title"]
        page_keys = {split_title(e["value"]).key for e in pages}
        bodies = [split_title(e["value"]).body.strip() for e in pages if split_title(e["value"]).body.strip()]
        if by["imdb_primary"] and page_keys & imdb_keys:
            return _draft("resolve", by["imdb_primary"][0]["value"], False, "页面标题键与 IMDb 标题键一致，取 IMDb 主标题")
        if by["imdb_primary"] and bodies:
            return _draft("resolve", _mode(bodies), True, "页面标题与 IMDb 标题不一致，疑为错配，暂取页面标题")
        if bodies and len(page_keys) == 1:
            return _draft("resolve", _mode(bodies), False, "无 IMDb，页面标题键一致")
        return _draft("keep_conflict", "", True, "页面标题互不一致且无 IMDb 佐证")
    if kind == "release_year":
        imdb = [e["year"] for e in ev if e["source"] in IMDB_SOURCES and e.get("year")]
        page = [e["year"] for e in by["amazon_title_year"] if e.get("year")]
        if imdb:
            gap = max((abs(y - imdb[0]) for y in page), default=0)
            if suspect or gap >= 2:
                return _draft("keep_conflict", "", True, f"IMDb {imdb[0]} 与页面标题年份 {sorted(set(page))} 相差 ≥2 年或关联存疑")
            return _draft("resolve", str(imdb[0]), gap == 1, f"IMDb 起始年 {imdb[0]}，页面标题年份 {sorted(set(page))}")
        if len(set(page)) == 1:
            return _draft("resolve", str(page[0]), False, "只有页面标题年份且一致")
        return _draft("keep_conflict", "", True, "没有 IMDb，页面标题年份缺失或不一致")
    if kind == "runtime_minutes":
        imdb = [int(e["value"]) for e in by["imdb_runtime"]]
        pages = [(_ints([e["value"]]), e) for e in by["page_runtime"]]
        mins = [m[0] for m, _ in pages if m]
        if imdb:
            if suspect:
                return _draft("keep_conflict", "", True, "IMDb 关联存疑，片长不可比较")
            worst = max((abs(m - imdb[0]) for m in mins), default=0)
            return _draft("resolve", str(imdb[0]), worst > 5, f"取 IMDb 片长 {imdb[0]}，页面最大差 {worst} 分钟")
        if mins and max(mins) - min(mins) <= 5:
            return _draft("resolve", str(_mode(mins)), False, f"无 IMDb，页面片长 {sorted(set(mins))} 相差不超过 5 分钟")
        return _draft("keep_conflict", "", True, "无 IMDb 且页面片长相差较大或缺失")
    if kind == "mpaa_rating":
        found = []
        for e in by["page_rating"]:
            m = _RATING_RE.match(str(e["value"] or ""))
            if m:
                tok = m.group(1).upper().replace(" ", "-")
                found.append("NR/Unrated" if tok in ("NR", "UNRATED", "NOT-RATED") else
                             tok if tok in ("G", "PG", "R", "X") else tok.replace("NC17", "NC-17").replace("PG13", "PG-13"))
        concrete = sorted({f for f in found if f != "NR/Unrated"})
        if len(concrete) == 1:
            return _draft("resolve", concrete[0], False, f"具体分级 {concrete[0]}，未评级写法不与之冲突")
        if not concrete and found:
            return _draft("resolve", "NR/Unrated", False, "只有未评级")
        return _draft("keep_conflict", "", True, f"分级缺失或有多种具体分级 {concrete}")
    if kind == "genres":
        words = {w for e in by["imdb_genres"] for w in e["value"] if w.casefold() in _VOCAB}
        unknown = []
        for e in by["page_genre"] + by["page_breadcrumb"]:
            t = " ".join(e["value"].replace(" see more", "").replace("_", " ").casefold().split())
            w = _VOCAB.get(t) or _GENRE_ALIASES.get(t)
            if w:
                words.add(w)
            elif t not in _GENRE_NOISE and t:
                unknown.append(e["value"])
        if not words:
            return _draft("keep_conflict", "", True, "没有可对应受控词表的类型")
        return _draft("resolve", "|".join(sorted(words)), bool(unknown), f"词表命中 {sorted(words)}，未识别词面 {sorted(set(unknown))[:5]}")
    if kind == "director":
        imdb = [e["value"] for e in by["imdb_director"]]
        page = [e["value"][0] for e in by["page_director"] if e["value"]]
        ik = {strict_name(n) for n in imdb}
        pk = {strict_name(n) for n in page}
        if imdb and not suspect:
            return _draft("resolve", "|".join(imdb), not pk <= ik, "取 IMDb 导演" + ("" if pk <= ik else f"；页面另有 {sorted(pk - ik)}"))
        if page:
            return _draft("resolve", "|".join(page), suspect or len(page) > 1, "取页面署名导演" + ("（IMDb 关联存疑）" if suspect else ""))
        return _draft("keep_conflict", "", True, "没有导演证据")
    if kind == "credit_align":
        names = [n for e in by["page_credit"] for n in e["value"]]
        cands = by["imdb_credit"] + by["imdb_director"]
        scored = sorted(((max((_ratio(n, c["value"]) for n in names), default=0.0), c["source_key"]) for c in cands),
                        reverse=True)
        if not scored:
            return _draft("keep_conflict", "", True, "没有可对照的 IMDb 成员")
        top = scored[0]
        if len(scored) > 1 and top[0] - scored[1][0] < 0.02 and top[0] >= 0.75:
            return _draft("keep_conflict", "", True, f"多名 IMDb 成员同样相近（{top[1]}、{scored[1][1]}）")
        if top[0] >= 0.75:
            return _draft("resolve", top[1], top[0] < 0.97, f"最相近的 IMDb 成员 {top[1]}（相似度 {top[0]:.2f}）")
        return _draft("resolve", "none", top[0] >= 0.6, f"没有相近的 IMDb 成员（最高相似度 {top[0]:.2f}）")
    if kind == "billing":
        ranks = [int(e["value"]) for e in by["page_billing"] if e["value"] is not None]
        if not ranks:
            return _draft("keep_conflict", "", True, "没有署名序号")
        return _draft("resolve", str(_mode(ranks)), len(set(ranks)) > 1, f"各商品署名序号 {sorted(Counter(ranks).items())}，取最常见")
    if kind == "publisher":
        vals = [str(e["value"]) for e in by["page_publisher"] if e["value"]]
        roots = [" ".join(w for w in _fold(v).split() if w not in _PUBLISHER_NOISE) for v in vals]
        longest = max(vals, key=len) if vals else ""
        if vals and all(r and (roots[0].startswith(r) or r.startswith(roots[0])) for r in roots):
            return _draft("resolve", longest, False, "各写法仅格式或厂牌后缀不同，取信息更完整者")
        return _draft("keep_conflict", "", True, f"发行商写法不同：{sorted(set(vals))}")
    if kind == "product_release_date":
        media = sorted({e["value"] for e in by["page_date_media_release"]})
        listing = sorted({e["value"] for e in by["page_date_listing_first_available"]})
        flagged = any(e["flags"] for s in by for e in by[s])
        if len(media) == 1:
            return _draft("resolve", media[0], flagged or bool(listing and listing != media), "取 media_release 发售日")
        if not media and len(listing) == 1:
            return _draft("resolve", listing[0], True, "只有上架日，未必是发售日")
        return _draft("keep_conflict", "", True, "发售日缺失或有多个不同值")
    return _draft("keep_conflict", "", True, "未知字段")


# ------------------------------------------------------------------ 5. 值的校验与比较规范（design 盲测评价设计 4）

def validate_value(field: str, value: str) -> None:
    """human_winning_value 的格式校验，不合格抛 ValueError。"""
    v = value.strip()
    if field in ("release_year", "runtime_minutes", "billing"):
        if not re.fullmatch(r"\d{1,4}", v):
            raise ValueError(f"{field} 需为整数，实际为 {value!r}")
    elif field == "product_release_date":
        date.fromisoformat(v)
    elif field == "genres":
        bad = [w for w in v.split("|") if w.strip().casefold() not in _VOCAB]
        if bad or not v:
            raise ValueError(f"genres 含不在受控词表里的词 {bad}")
    elif field == "credit_align":
        if not (v.casefold() == "none" or re.fullmatch(r"nm\d+", v)):
            raise ValueError(f"credit_align 需为 nconst（如 nm0000001）或 none，实际为 {value!r}")
    elif field == "mpaa_rating":
        if v.casefold() not in {r.casefold() for r in ("G", "PG", "PG-13", "R", "NC-17", "X", "NR/Unrated")}:
            raise ValueError(f"mpaa_rating 需为 G、PG、PG-13、R、NC-17、X 或 NR/Unrated，实际为 {value!r}")
    elif not v:
        raise ValueError(f"{field} 不能为空")


def norm_value(field: str, value: str | None) -> Any:
    """比较用的规范形式：标量去空白；整数按数值；标题、发行商、分级大小写折叠；多值字段无序；导演按严格姓名键。"""
    if value is None or not str(value).strip():
        return None
    v = " ".join(str(value).split())
    if field in ("release_year", "runtime_minutes", "billing"):
        return int(v) if v.isdigit() else v
    if field == "product_release_date":
        return v
    if field == "genres":
        return frozenset(w.strip().casefold() for w in v.split("|") if w.strip())
    if field == "director":
        return frozenset(strict_name(w) for w in v.split("|") if w.strip())
    return v.casefold()


# ------------------------------------------------------------------ 6. 导出（R62–R64）

def export_eval_set(conn: psycopg.Connection, cv: str, directory: Path, *, force: bool = False) -> dict[str, Any]:
    """生成评价集并导出 eval_items.csv、eval_items_draft.csv 与 LABELING.md；写 eval_set 与 eval_item。"""
    row = cv_row(conn, cv)
    eid, sid = row["extraction_id"], eval_set_id(cv)
    dev = dev_movie_ids(conn, eid)
    if not dev:
        raise EvalError("开发池尚未冻结，请先执行 evaluate freeze（须在首次全量运行前）")
    if conn.execute("SELECT 1 FROM task7.eval_label WHERE eval_set_id = %s LIMIT 1", (sid,)).fetchone() and not force:
        raise EvalError(f"评价集 {sid} 已有导入的标注；重新导出会清除它们，确需重导请加 --force")
    pools = build_pools(conn, row, dev)
    items, strata = sample_items(pools)
    if len(items) != TARGET_TOTAL:
        raise EvalError(f"评价池只够 {len(items)} 项，不足 {TARGET_TOTAL}")
    items.sort(key=lambda x: (_md5(f"{x['entity_id']}:{x['target_field']}"), x["entity_id"], x["target_field"]))
    for n, it in enumerate(items, start=1):
        it["item_id"] = f"t7_eval_{n:04d}"
        if it["target_field"] == "director" and it["system_status"] == "resolved":
            it["system_value"] = director_names(conn, row, it["entity_id"])
    i = inputs_of(row)
    blind = [blind_row(conn, i, it) for it in items]
    drafts = [{**b, **generate_ai_draft(b)} for b in blind]
    directory.mkdir(parents=True, exist_ok=True)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM task7.eval_label WHERE eval_set_id = %s", (sid,))
        cur.execute("DELETE FROM task7.eval_item WHERE eval_set_id = %s", (sid,))
        cur.execute("DELETE FROM task7.eval_set WHERE eval_set_id = %s", (sid,))
        cur.execute("INSERT INTO task7.eval_set (eval_set_id, extraction_id, conflict_version_id, rule_version, strata, "
                    "exported_file) VALUES (%s, %s, %s, %s, %s, %s)",
                    (sid, eid, cv, row["rule_version"], Jsonb(strata), str(directory / "eval_items.csv")))
        cur.executemany(
            "INSERT INTO task7.eval_item (eval_set_id, item_id, stratum, entity_type, entity_id, target_field, "
            "system_status, system_value, conflict_type) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(sid, it["item_id"], it["stratum"], it["entity_type"], it["entity_id"], it["target_field"],
              it["system_status"], it["system_value"], it["conflict_type"]) for it in items])
    _write_csv(directory / "eval_items.csv", BLIND_COLUMNS, blind)
    _write_csv(directory / "eval_items_draft.csv", DRAFT_COLUMNS, drafts)
    reporting.write_atomic(directory / "LABELING.md", LABELING_GUIDE.encode("utf-8"))
    first_run = conn.execute("SELECT min(started_at) FROM task7.run WHERE extraction_id = %s AND kind = 'resolve'",
                             (eid,)).fetchone()[0]
    frozen = conn.execute("SELECT min(frozen_at) FROM task7.dev_pool WHERE extraction_id = %s", (eid,)).fetchone()[0]
    return {"eval_set_id": sid, "conflict_version_id": cv, "items_total": len(items), "strata": strata,
            "dev_pool_movies": len(dev), "frozen_before_first_run": first_run is None or frozen < first_run,
            "eval_items_file": str(directory / "eval_items.csv"), "draft_file": str(directory / "eval_items_draft.csv")}


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in columns})


# ------------------------------------------------------------------ 7. 导入（R66）

def _truthy(text: str | None) -> bool:
    return (text or "").strip().casefold() in ("true", "1", "yes", "是")


def import_labels(conn: psycopg.Connection, sid: str, csv_path: Path, annotator: str, *,
                  draft_path: Path | None = None, allow_partial: bool = False) -> dict[str, Any]:
    """导入标注：校验动作与值的格式；ai_doubt 为真或与 AI 建议不同的项必须有 human_evidence（unsure 除外）；
    note 不得原样采用 ai_reason；ai_reason 本身不入库。全部通过才写入（要么 120 条全进，要么拒绝）。"""
    if not annotator or not annotator.strip():
        raise EvalError("必须提供标注人姓名（--annotator）")
    if not csv_path.exists():
        raise EvalError(f"标注文件不存在：{csv_path}")
    items = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT item_id, target_field, entity_type FROM task7.eval_item WHERE eval_set_id = %s", (sid,))}
    if not items:
        raise EvalError(f"评价集 {sid} 不存在或没有项目，请先执行 evaluate export")
    ref: dict[str, dict[str, str]] = {}
    sibling = draft_path or csv_path.parent / "eval_items_draft.csv"
    if sibling.exists():
        with sibling.open("r", encoding="utf-8") as f:
            ref = {r["item_id"]: r for r in csv.DictReader(f)}
    errors: list[str] = []
    records: dict[str, dict[str, Any]] = {}
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        has_ai = bool(ref) or "ai_action" in (reader.fieldnames or ())
        missing_cols = [c for c in ("item_id", "human_action", "human_winning_value", "human_evidence", "note")
                        if c not in (reader.fieldnames or ())]
        if missing_cols:
            raise EvalError(f"标注文件缺少列 {missing_cols}")
        for n, r in enumerate(reader, start=2):
            iid = (r.get("item_id") or "").strip()
            if not iid:
                continue
            tag = f"第 {n} 行（{iid}）"
            if iid not in items:
                errors.append(f"{tag}：item_id 不属于评价集 {sid}")
                continue
            if iid in records:
                errors.append(f"{tag}：item_id 重复")
                continue
            field = base_field(items[iid][0])
            action = (r.get("human_action") or "").strip()
            value = (r.get("human_winning_value") or "").strip()
            evidence = (r.get("human_evidence") or "").strip()
            note = (r.get("note") or "").strip()
            if action not in ACTIONS:
                errors.append(f"{tag}：human_action 必须是 {'/'.join(ACTIONS)}，实际为 {action!r}")
                continue
            if action == "resolve":
                try:
                    validate_value(field, value)
                except ValueError as exc:
                    errors.append(f"{tag}：{exc}")
                    continue
            elif value:
                errors.append(f"{tag}：{action} 时 human_winning_value 必须留空")
                continue
            a = ref.get(iid, {})
            ai_action = (r.get("ai_action") or a.get("ai_action") or "").strip()
            ai_value = (r.get("ai_winning_value") or a.get("ai_winning_value") or "").strip()
            ai_doubt = _truthy(r.get("ai_doubt") or a.get("ai_doubt"))
            ai_reason = (r.get("ai_reason") or a.get("ai_reason") or "").strip()
            changed = bool(ai_action) and (action != ai_action or (action == "resolve"
                                                                    and norm_value(field, value) != norm_value(field, ai_value)))
            if action != "unsure" and (ai_doubt or changed) and not evidence:
                errors.append(f"{tag}：{'AI 标为存疑' if ai_doubt else '与 AI 建议不同'}，必须填写 human_evidence（可核实的外部依据）")
                continue
            if ai_reason and note and note == ai_reason:
                errors.append(f"{tag}：note 不得原样采用 AI 草稿的 ai_reason")
                continue
            records[iid] = {"item_id": iid, "human_action": action, "human_winning_value": value or None,
                            "human_evidence": evidence or None, "ai_doubt": ai_doubt, "note": note or None}
    absent = sorted(set(items) - set(records))
    if absent and not allow_partial and not errors:
        errors.append(f"缺少 {len(absent)} 项标注（如 {absent[0]}）；确需部分导入请加 --allow-partial")
    if errors:
        raise EvalError("导入被拒绝：\n  " + "\n  ".join(errors[:30]) + (f"\n  …共 {len(errors)} 处" if len(errors) > 30 else ""))
    sha = str(reporting.file_digest(csv_path)["sha256"])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM task7.eval_label WHERE eval_set_id = %s", (sid,))
        cur.executemany(
            "INSERT INTO task7.eval_label (eval_set_id, item_id, human_action, human_winning_value, human_evidence, "
            "ai_doubt, note, annotator, import_file_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(sid, r["item_id"], r["human_action"], r["human_winning_value"], r["human_evidence"], r["ai_doubt"],
              r["note"], annotator.strip(), sha) for r in records.values()])
    return {"eval_set_id": sid, "annotator": annotator.strip(), "imported_items": len(records),
            "unlabeled_items": len(absent), "file_sha256": sha,
            "by_action": dict(Counter(r["human_action"] for r in records.values())),
            "ai_doubt_items": sum(1 for r in records.values() if r["ai_doubt"]),
            "ai_reference": "有" if has_ai else "无"}


# ------------------------------------------------------------------ 8. 指标（R65）

def _ratio_or_none(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def metrics_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一组已标注项的指标（纯函数，便于手算核对）。unsure 单独计数，不进任何分母；分母为零记 None（无定义）。
    决策动作：系统 resolved ↔ resolve，conflict_unresolved ↔ keep_conflict。
    · action_accuracy = 动作一致数 / 非 unsure 项数
    · value_accuracy = 系统 resolved 且人工 resolve 的项里，值一致的比例（动作不一致的项由动作准确率体现）
    · conflict_precision = 系统 conflict_unresolved 的非 unsure 项里，人工也 keep_conflict 的比例
    · conflict_recall = 人工 keep_conflict 的项里，系统也 conflict_unresolved 的比例"""
    decided = [r for r in rows if r["human_action"] != "unsure"]
    action_ok = sum(1 for r in decided if SYSTEM_ACTION[r["system_status"]] == r["human_action"])
    pairs = [r for r in decided if r["system_status"] == "resolved" and r["human_action"] == "resolve"]
    value_ok = sum(1 for r in pairs if norm_value(base_field(r["target_field"]), r["system_value"])
                   == norm_value(base_field(r["target_field"]), r["human_winning_value"]))
    sys_keep = [r for r in decided if r["system_status"] == "conflict_unresolved"]
    human_keep = [r for r in decided if r["human_action"] == "keep_conflict"]
    keep_both = sum(1 for r in sys_keep if r["human_action"] == "keep_conflict")
    recalled = sum(1 for r in human_keep if r["system_status"] == "conflict_unresolved")
    return {"labeled": len(rows), "unsure": len(rows) - len(decided), "decided": len(decided),
            "action_correct": action_ok, "action_accuracy": _ratio_or_none(action_ok, len(decided)),
            "value_pairs": len(pairs), "value_correct": value_ok, "value_accuracy": _ratio_or_none(value_ok, len(pairs)),
            "system_conflict": len(sys_keep), "conflict_both": keep_both,
            "conflict_precision": _ratio_or_none(keep_both, len(sys_keep)),
            "human_conflict": len(human_keep), "conflict_recalled": recalled,
            "conflict_recall": _ratio_or_none(recalled, len(human_keep))}


def metrics(conn: psycopg.Connection, cv: str) -> dict[str, Any]:
    sid = eval_set_id(cv)
    es = conn.execute("SELECT rule_version, strata, extraction_id FROM task7.eval_set WHERE eval_set_id = %s",
                      (sid,)).fetchone()
    if not es:
        raise EvalError(f"评价集 {sid} 不存在，请先执行 evaluate export")
    cols = ("item_id", "stratum", "entity_type", "entity_id", "target_field", "system_status", "system_value",
            "conflict_type", "human_action", "human_winning_value", "annotator")
    rows = [dict(zip(cols, r)) for r in conn.execute(
        "SELECT i.item_id, i.stratum, i.entity_type, i.entity_id, i.target_field, i.system_status, i.system_value, "
        "i.conflict_type, l.human_action, l.human_winning_value, l.annotator FROM task7.eval_item i "
        "LEFT JOIN task7.eval_label l USING (eval_set_id, item_id) WHERE i.eval_set_id = %s ORDER BY i.item_id", (sid,))]
    labeled = [r for r in rows if r["human_action"]]
    if not labeled:
        raise EvalError(f"评价集 {sid} 尚未导入任何标注")
    by_stratum = {s: metrics_of([r for r in labeled if r["stratum"] == s]) for s in sorted({r["stratum"] for r in rows})}
    by_field = {f: metrics_of([r for r in labeled if base_field(r["target_field"]) == f])
                for f in sorted({base_field(r["target_field"]) for r in rows})}
    dev = dev_movie_ids(conn, es[2])
    overlap = sum(1 for r in rows if r["entity_type"] == "movie" and r["entity_id"] in dev)
    return {"eval_set_id": sid, "conflict_version_id": cv, "rule_version": es[0], "items_total": len(rows),
            "labeled_total": len(labeled), "unlabeled_total": len(rows) - len(labeled),
            "annotators": sorted({r["annotator"] for r in labeled}), "strata_design": es[1],
            "dev_pool_overlap": overlap, "overall": metrics_of(labeled), "by_stratum": by_stratum, "by_field": by_field,
            "scope_note": "指标只代表评价样本（分层抽样刻意多取难例与 conflict_unresolved 项），不代表全量准确率；"
                          "unsure 单独计数，不进分母；分母为零记“无定义”。",
            "tool_note": "盲测表证据由上游原始表生成；AI 草稿是规则式脚本（不是大模型）依据盲测证据给出的线索，"
                         "须经标注人逐项核对，ai_reason 不入标注表；对草稿存疑或改判的项必须附外部依据。"}


def render_metrics(m: dict[str, Any]) -> str:
    pct = lambda x: "无定义" if x is None else f"{x * 100:.1f}%"  # noqa: E731

    def table(title: str, groups: dict[str, dict[str, Any]]) -> list[str]:
        out = ["", f"## {title}", "",
               "| 范围 | 标注 | unsure | 动作准确率 | 值准确率（对/总） | conflict 精确率 | conflict 召回率 |",
               "|---|---|---|---|---|---|---|"]
        for name, g in groups.items():
            out.append(f"| {name} | {g['labeled']} | {g['unsure']} | {pct(g['action_accuracy'])}"
                       f"（{g['action_correct']}/{g['decided']}） | {pct(g['value_accuracy'])}"
                       f"（{g['value_correct']}/{g['value_pairs']}） | {pct(g['conflict_precision'])}"
                       f"（{g['conflict_both']}/{g['system_conflict']}） | {pct(g['conflict_recall'])}"
                       f"（{g['conflict_recalled']}/{g['human_conflict']}） |")
        return out

    lines = [f"# 任务 7 评价指标报告（{m['eval_set_id']}）", "",
             f"- **冲突消解版本**：`{m['conflict_version_id']}`（规则 `{m['rule_version']}`）",
             f"- **样本**：{m['items_total']} 项，已标注 {m['labeled_total']}，未标注 {m['unlabeled_total']}",
             f"- **标注人**：{', '.join(m['annotators']) or '无'}",
             f"- **开发池重叠**：{m['dev_pool_overlap']}（应为 0）"]
    lines += table("总体", {"总体": m["overall"]}) + table("按层", m["by_stratum"]) + table("按字段", m["by_field"])
    lines += ["", "## 说明", f"- {m['scope_note']}", f"- {m['tool_note']}"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 9. 命令行

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 7 评价工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fz = sub.add_parser("freeze", help="冻结开发池（先幂等应用迁移 601）")
    p_fz.add_argument("--extraction-id", help="抽取 ID")
    p_fz.add_argument("--t4", help="任务 4 映射版本（默认 map0002）")
    p_exp = sub.add_parser("export", help="导出盲测表与 AI 预标注草稿")
    p_exp.add_argument("--cv", help="冲突消解版本（默认最近一次）")
    p_exp.add_argument("--extraction-id", help="抽取 ID")
    p_exp.add_argument("--output-dir", default="outputs", help="输出根目录")
    p_exp.add_argument("--force", action="store_true", help="已有标注时仍重新导出（会清除标注）")
    p_imp = sub.add_parser("import", help="导入人工标注 CSV")
    p_imp.add_argument("csv_path", help="标注 CSV 文件路径")
    p_imp.add_argument("--annotator", required=True, help="标注人姓名")
    p_imp.add_argument("--cv", help="冲突消解版本")
    p_imp.add_argument("--extraction-id", help="抽取 ID")
    p_imp.add_argument("--draft", help="AI 草稿 CSV（默认取标注文件同目录的 eval_items_draft.csv）")
    p_imp.add_argument("--allow-partial", action="store_true", help="允许只导入部分项")
    p_met = sub.add_parser("metrics", help="计算指标并输出报告")
    p_met.add_argument("--cv", help="冲突消解版本")
    p_met.add_argument("--extraction-id", help="抽取 ID")
    p_met.add_argument("--output-dir", default="outputs", help="输出根目录")
    args = parser.parse_args(argv)
    with connect(load_settings()) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        try:
            if args.cmd == "freeze":
                apply_task7_schema(conn)
                res = freeze_dev_pool(conn, eid, args.t4 or DEFAULT_T4_MAPPING_VERSION)
                print(json.dumps(res, ensure_ascii=False, indent=2))
                return 0
            cv = args.cv or latest_cv(conn, eid)
            if args.cmd == "export":
                res = export_eval_set(conn, cv, eval_dir(Path(args.output_dir), eid, cv), force=args.force)
            elif args.cmd == "import":
                draft = Path(args.draft) if args.draft else None
                res = import_labels(conn, eval_set_id(cv), Path(args.csv_path), args.annotator, draft_path=draft,
                                    allow_partial=args.allow_partial)
            else:
                directory = eval_dir(Path(args.output_dir), eid, cv)
                res = metrics(conn, cv)
                reporting.write_atomic(directory / "metrics.json",
                                       (json.dumps(res, ensure_ascii=False, indent=2, default=str) + "\n").encode())
                reporting.write_atomic(directory / "metrics.md", render_metrics(res).encode())
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        except EvalError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
