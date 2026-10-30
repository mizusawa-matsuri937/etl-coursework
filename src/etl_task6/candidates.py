"""年份候选：IMDb 关联、任务 5 升级线索（含被占用 tconst）与商品标题年份（spec R06–R08、R12–R13）。

按作品分批（R29）；每批的目标行、实体标记与批次审计在同一事务提交。线索处理记录（hint_use）与候选同批写入；
合集成员与多 tconst 的线索只记录（R08）。任务 4、任务 5 的表只读。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task1 import reporting

from .rules import RuleSet
from .run import DEFAULT_BATCH, BeforeCommit, RunContext, add_quality_issue, copy_rows, hint_stage_key, \
    mark_batch, mark_entities, next_batch_no
from . import sources

CAND_COLUMNS = ("extraction_id", "stage_key", "movie_id", "source", "source_key", "raw_value", "value_year",
                "precision", "semantic", "region", "flags", "shared_with_movie_id", "source_ref",
                "source_acquired_at", "extracted_at", "match_evidence", "confidence", "rule_version", "run_id")
HINT_COLUMNS = ("extraction_id", "stage_key", "asin", "member_ordinal", "tconst", "movie_id", "action",
                "owner_movie_id", "run_id")


class World:
    """一次运行输入下所有作品的静态输入（只读查询一次，供各批共用）。

    candidates、cooccur 与 select 各阶段对任务 4/5 输入的需求一致，集中在这里维护。"""

    def __init__(self, conn: psycopg.Connection, ctx: RunContext):
        i = ctx.inputs
        self.inputs = i
        self.t4 = i.t4_mapping_version_id
        self.stage_key = i.stage_key("candidates")
        self.snapshot = i.imdb_snapshot_id
        hints = sources.hints(conn, i.extraction_id, hint_stage_key(conn, i.t5_person_mapping_version_id))
        self.movies = sources.movies(conn, self.t4)                       # [(movie_id, tconst)]
        self.tconst_of = dict(self.movies)
        self.tconst_owner = {tc: m for m, tc in self.movies if tc}
        self.t3 = sources.t3_rule_of(conn, self.t4)
        self.products = sources.product_rows(conn, i.extraction_id, self.t3)
        self.relations = sources.relation_rows(conn, self.t4)             # asin → [(ordinal, movie, rel)]
        self.asin_movie: dict[tuple[str, int], str] = {}                  # (asin, member_ordinal) → movie
        self.edition_asins: dict[str, set[str]] = defaultdict(set)
        self.relations_of: dict[str, list[dict[str, Any]]] = defaultdict(list)  # movie → 关联依据
        for asin, rels in self.relations.items():
            for ordinal, movie, rel in rels:
                self.asin_movie[(asin, ordinal)] = movie
                if rel == "edition":
                    self.edition_asins[movie].add(asin)
        for (asin, ordinal, movie), (b, d) in sources.relation_basis(conn, self.t4).items():
            self.relations_of[movie].append({"asin": asin, "member_ordinal": ordinal, "basis": b,
                                             "decision_id": d})
        involved = sorted({tc for _, tc in self.movies if tc} | {tc for _, _, tc, *_ in hints})
        self.hints = hints
        self.imdb = sources.imdb_titles(conn, i.imdb_snapshot_id, involved)
        self.basics_file = sources.imdb_basics_file(conn, i.imdb_snapshot_id)
        self.downloaded_at = sources.snapshot_downloaded_at(conn, i.imdb_snapshot_id)
        self.title_years = self._title_years(conn, i)

    def _title_years(self, conn: psycopg.Connection, i) -> dict[str, list[dict[str, Any]]]:
        """movie → 标题年份断言（product_edition 经 edition 关系、collection_member 经 contains 关系归到作品）。"""
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in sources.assertions_by_entity(conn, i.extraction_id, i.parser_version, "title_year"):
            entity = a["entity_id"]
            if a["entity_type"] == "product_edition":
                movie = self.asin_movie.get((entity, 0))
                via = "edition"
                ordinal = 0
            else:                      # collection_member：entity_id = <ASIN>#<成员序号>
                asin, _, no = entity.partition("#")
                ordinal = int(no)
                movie = self.asin_movie.get((asin, ordinal))
                via = "collection_member"
            if movie and a["normalized_value"]:
                out[movie].append({**a, "asin": entity.split("#")[0], "member_ordinal": ordinal, "via": via})
        return out

    def imdb_ref(self, tconst: str) -> dict[str, Any]:
        t = self.imdb.get(tconst, {})
        return {"snapshot": self.snapshot, "file": self.basics_file, "line_no": t.get("line_no"),
                "tconst": tconst, "title_type": t.get("title_type")}


def classify_hints(world: World):
    """线索逐条分类（R07、R08）。返回 (处理记录, 每部作品的候选线索, 多 tconst 的作品, 共享质量问题)。

    分类顺序：合集成员 → 商品所在作品已有 tconst → 同一作品多个 tconst（整组只记录）→ 被占用 → 普通候选。"""
    rows: list[dict[str, Any]] = []
    per_movie: dict[str, list[dict[str, Any]]] = defaultdict(list)
    multiple: set[str] = set()
    quality: list[tuple[str, dict[str, Any]]] = []
    collected: dict[str, dict[str, dict[str, Any]]] = {}     # movie → tconst → 线索
    for asin, ordinal, tconst, mention_keys, decision_ids, mention_count in world.hints:
        movie = world.asin_movie.get((asin, ordinal))
        rec = {"asin": asin, "member_ordinal": ordinal, "tconst": tconst, "movie_id": movie,
               "mention_keys": list(mention_keys), "link_decision_ids": list(decision_ids),
               "mention_count": mention_count, "action": None}
        if ordinal > 0:
            rec["action"] = "collection_member"
            rows.append(rec)
            continue
        if movie is None:
            continue                        # 商品不在当前映射版本里，无法归到作品；不产生候选
        if world.tconst_of.get(movie):
            rec["action"] = "movie_already_linked"
            rows.append(rec)
            continue
        m = collected.setdefault(movie, {})
        if tconst not in m:
            m[tconst] = rec
    for movie, by_tconst in collected.items():
        if len(by_tconst) >= 2:
            multiple.add(movie)
            for rec in by_tconst.values():
                rec["action"] = "multiple_tconst"
                rows.append(rec)
            continue
        for tconst, rec in by_tconst.items():
            owner = world.tconst_owner.get(tconst)
            if owner and owner != movie:
                rec["action"] = "candidate_shared"
                rec["owner_movie_id"] = owner
                quality.append((movie, {"tconst": tconst, "owner_movie_id": owner, "asin": rec["asin"],
                                        "mention_keys": rec["mention_keys"]}))
            else:
                rec["action"] = "candidate"
            rows.append(rec)
            per_movie[movie].append(rec)
    return rows, per_movie, multiple, quality


def extract_candidates(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, *,
                       batch_size: int = DEFAULT_BATCH, limit: int | None = None,
                       before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R06–R08、R12–R13：三类候选与线索处理记录，按作品分批。limit 只限制本次处理的未完成作品数（试跑）。"""
    i = ctx.inputs
    world = World(conn, ctx)
    hint_rows, hint_cands, multiple_movies, hint_quality = classify_hints(world)
    hints_by_movie: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in hint_rows:
        if rec.get("movie_id"):
            hints_by_movie[rec["movie_id"]].append(rec)

    key, stage = i.stage_key("candidates"), "candidates"
    unmarked = conn.execute(
        "SELECT m.movie_id FROM task4.movie m LEFT JOIN task6.entity_stage es ON es.extraction_id = %s "
        "AND es.stage = 'candidates' AND es.stage_key = %s AND es.entity_id = m.movie_id "
        "WHERE m.mapping_version_id = %s AND es.entity_id IS NULL ORDER BY m.movie_id COLLATE \"C\"",
        (i.extraction_id, key, i.t4_mapping_version_id)).fetchall()
    todo = [r[0] for r in unmarked]
    if limit is not None:
        todo = todo[:limit]
    counts: dict[str, Any] = {"movies": 0, "candidates_by_source": {}, "hint_actions": {},
                              "multiple_tconst_movies": len(multiple_movies),
                              "quality_shared_tconst": len(hint_quality)}
    batch_no = next_batch_no(conn, ctx, stage)
    quality_by_movie: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for movie, detail in hint_quality:
        quality_by_movie[movie].append(detail)
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        with conn.transaction():
            cand_rows: list[tuple] = []
            use_rows: list[tuple] = []
            entities: list[tuple[str, int]] = []
            now = reporting.utc_now()
            for movie in batch:
                n = 0
                for rec in hints_by_movie.get(movie, ()):
                    use_rows.append((i.extraction_id, key, rec["asin"], rec["member_ordinal"], rec["tconst"], movie,
                                     rec["action"], rec.get("owner_movie_id"), ctx.run_id))
                    n += 1
                tconst = world.tconst_of.get(movie)
                if tconst:
                    t = world.imdb.get(tconst)
                    if t is None:
                        add_quality_issue(conn, ctx, movie, "tconst_not_in_snapshot",
                                          {"tconst": tconst, "note": "任务 4 关联的 tconst 不在 IMDb 快照里"})
                    elif t["start_year"] is None:
                        pass                                     # startYear 为 NULL：不产生候选（R06）
                    else:
                        cand_rows.append((
                            i.extraction_id, key, movie, "imdb_t4_link", tconst, str(t["start_year"]),
                            t["start_year"], "year", "first_release", None, [], None,
                            Jsonb(world.imdb_ref(tconst)), world.downloaded_at, now,
                            Jsonb({"basis_products": world.relations_of.get(movie, []),
                                   "person_mapping_version": None}),
                            rules.confidence_of("imdb_t4_link"), i.rule_version, ctx.run_id))
                        n += 1
                for rec in hint_cands.get(movie, ()):
                    t = world.imdb.get(rec["tconst"])
                    year = None if t is None else t["start_year"]
                    if year is None:
                        continue                    # tconst 不在快照或没有年份：不产生候选
                    flags = ["shared_tconst"] if rec["action"] == "candidate_shared" else []
                    cand_rows.append((
                        i.extraction_id, key, movie, "imdb_t5_hint", rec["tconst"], str(year), year, "year",
                        "first_release", None, flags, rec.get("owner_movie_id"),
                        Jsonb(world.imdb_ref(rec["tconst"])), world.downloaded_at, now,
                        Jsonb({"mention_keys": rec["mention_keys"], "link_decision_ids": rec["link_decision_ids"],
                               "mention_count": rec["mention_count"],
                               "person_mapping_version": i.t5_person_mapping_version_id,
                               "hint_asin": rec["asin"]}),
                        rules.confidence_of("imdb_t5_hint"), i.rule_version, ctx.run_id))
                    n += 1
                for a in world.title_years.get(movie, ()):
                    year = int(a["normalized_value"])
                    source_key = f"{a['asin']}:{a['assertion_id']}"
                    p = world.products.get(a["asin"], {})
                    cand_rows.append((
                        i.extraction_id, key, movie, "amazon_title_year", source_key, a["raw_value"], year, "year",
                        "first_release", None, [], None,
                        Jsonb({"asin": a["asin"], "assertion_id": a["assertion_id"],
                               "section": (a["source_locator"] or {}).get("section"),
                               "ordinal": (a["source_locator"] or {}).get("ordinal"),
                               "source_record_id": a["source_record_id"], "final_url": p.get("final_url"),
                               "via": a["via"], "member_ordinal": a["member_ordinal"]}),
                        p.get("fetched_at"), now,
                        Jsonb({"product_asin": a["asin"], "via": a["via"], "person_mapping_version": None}),
                        rules.confidence_of("amazon_title_year"), i.rule_version, ctx.run_id))
                    n += 1
                entities.append((movie, n))
            for movie in batch:
                for detail in quality_by_movie.get(movie, ()):
                    add_quality_issue(conn, ctx, movie, "shared_tconst_with_movie", detail)
            copy_rows(conn, "task6.release_candidate", CAND_COLUMNS, cand_rows)
            copy_rows(conn, "task6.hint_use", HINT_COLUMNS, use_rows)
            mark_entities(conn, ctx, stage, batch_no, entities)
            mark_batch(conn, ctx, stage, batch_no, batch[0], batch[-1], len(cand_rows) + len(use_rows))
            if before_commit is not None:
                before_commit(stage, batch_no)
        counts["movies"] += len(batch)
        batch_no += 1
    for source, n in conn.execute("SELECT source, count(*) FROM task6.release_candidate WHERE extraction_id = %s "
                                  "AND stage_key = %s GROUP BY 1", (i.extraction_id, key)):
        counts["candidates_by_source"][source] = n
    for action, n in conn.execute("SELECT action, count(*) FROM task6.hint_use WHERE extraction_id = %s "
                                  "AND stage_key = %s GROUP BY 1", (i.extraction_id, key)):
        counts["hint_actions"][action] = n
    return counts
