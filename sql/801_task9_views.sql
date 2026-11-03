-- 任务 9 评论层与商品层血缘视图（R10、R29）：只拼接任务 1–8 的表，不复制评论映射。
-- 由 etl_task9.run.apply_task9_schema 在 801 之后每次执行，必须可重复执行（CREATE OR REPLACE）。
-- 视图带版本列（review_dedup_version_id、mapping_version_id、conflict_version_id），查询时须限定版本。

-- 原始评论 → 文件定位 → 清洗原值 → 某去重版本下的映射（mapped/quarantined/incomplete）、逻辑评论、去重层次、
-- 评论家族 → 归属作品（只在合集上的逻辑评论没有作品行，movie_id 为 NULL）→ 统计组。
-- 每条原始评论在每个去重版本下、每个归属作品一行。
CREATE OR REPLACE VIEW task9.v_review_lineage AS
SELECT rr.extraction_id,
       rr.source_review_id,
       rr.record_no,
       rr.start_line,
       rr.end_line,
       rr.start_offset,
       rr.end_offset,
       rr.raw_sha256,
       rr.classification,
       rs.product_id,
       rs.review_time_utc,
       dv.review_dedup_version_id,
       dv.status            AS dedup_version_status,
       dv.dedup_stage_key,
       m.mapping_status,
       m.match_level,
       m.asin,
       m.logical_review_id,
       lr.family_id,
       f.basis              AS family_basis,
       lr.representative_record_no,
       lr.copy_count,
       w.movie_id,
       w.also_on_collection,
       sg.stat_group_id
FROM public.review_record rr
JOIN task8.dedup_version dv ON dv.extraction_id = rr.extraction_id
LEFT JOIN public.review_staging rs ON rs.extraction_id = rr.extraction_id AND rs.source_review_id = rr.source_review_id
LEFT JOIN task8.review_mapping m ON m.dedup_stage_key = dv.dedup_stage_key AND m.record_no = rr.record_no
LEFT JOIN task8.logical_review lr ON lr.dedup_stage_key = dv.dedup_stage_key AND lr.logical_review_id = m.logical_review_id
LEFT JOIN task8.review_family f ON f.family_stage_key = dv.family_stage_key AND f.family_id = lr.family_id
LEFT JOIN task8.logical_review_work w ON w.attribution_stage_key = dv.attribution_stage_key
     AND w.logical_review_id = m.logical_review_id
LEFT JOIN task8.stat_group_member sg ON sg.attribution_stage_key = dv.attribution_stage_key AND sg.movie_id = w.movie_id;

-- 商品 → 采集状态与证据 → 任务 3 类别（映射版本所用的分类规则）→ 任务 4 版本记录、所属作品、关系类型、匹配状态
-- → 任务 7 版本属性（各冲突版本）。每个商品在每个映射版本、每个冲突版本、每个所属作品成员一行；
-- 没有任务 4 结果的商品（如未采集）mapping 列为 NULL，collection_status 仍给出清单状态。
CREATE OR REPLACE VIEW task9.v_product_lineage AS
SELECT pm.extraction_id,
       pm.asin,
       pm.status            AS collection_status,
       pm.status_reason,
       pm.fetched_at,
       pm.snapshot_path,
       pm.content_hash,
       mv.mapping_version_id,
       pc.rule_version      AS t3_rule_version,
       pc.input_group,
       pc.category,
       pc.rule_id           AS category_rule_id,
       pc.basis_level       AS category_basis,
       pe.product_kind,
       pe.match_scope,
       ms.status            AS match_status,
       ms.reason            AS match_reason,
       pmv.member_ordinal,
       pmv.movie_id,
       pmv.relation_type,
       pmv.basis            AS movie_basis,
       pmv.decision_id,
       pmv.human_decision_id,
       cv.conflict_version_id,
       er.edition_title,
       er.runtime_minutes,
       er.mpaa_rating,
       er.publisher,
       er.product_release_date,
       er.discs,
       er.media_format,
       er.language,
       er.subtitles,
       er.aspect_ratio,
       er.region,
       er.cut_type,
       er.video_system
FROM task2.product_manifest pm
LEFT JOIN task4.mapping_version mv ON mv.extraction_id = pm.extraction_id
LEFT JOIN task3.product_classification pc ON pc.extraction_id = pm.extraction_id AND pc.asin = pm.asin
     AND pc.rule_version = mv.t3_rule_version
LEFT JOIN task4.product_edition pe ON pe.extraction_id = pm.extraction_id AND pe.asin = pm.asin
     AND pe.t3_rule_version = mv.t3_rule_version
LEFT JOIN task4.product_match_status ms ON ms.mapping_version_id = mv.mapping_version_id AND ms.asin = pm.asin
LEFT JOIN task4.product_movie pmv ON pmv.mapping_version_id = mv.mapping_version_id AND pmv.asin = pm.asin
LEFT JOIN task7.conflict_version cv ON cv.t4_mapping_version_id = mv.mapping_version_id
LEFT JOIN task7.edition_resolved er ON er.conflict_version_id = cv.conflict_version_id AND er.asin = pm.asin;
