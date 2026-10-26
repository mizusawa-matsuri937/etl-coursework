-- 任务 1 数量核对：从记录明细汇总，显式限定 extraction_id，一行结果。
-- observed 统计所有记录中可明确识别的原始 ID（含隔离记录）；valid 只统计有效记录（spec R22/R23）。
SELECT
    count(*)                                                         AS records_read,
    count(*) FILTER (WHERE classification = 'valid')                 AS records_valid,
    count(*) FILTER (WHERE classification = 'quarantined')           AS records_quarantined,
    count(DISTINCT product_id_observed)                              AS distinct_products_observed,
    count(DISTINCT reviewer_id_observed)                             AS distinct_users_observed,
    count(DISTINCT product_id_observed) FILTER (WHERE classification = 'valid')  AS distinct_products_valid,
    count(DISTINCT reviewer_id_observed) FILTER (WHERE classification = 'valid') AS distinct_users_valid,
    count(*) FILTER (WHERE product_id_status IN ('conflict', 'undecodable'))  AS products_unidentifiable,
    count(*) FILTER (WHERE reviewer_id_status IN ('conflict', 'undecodable')) AS users_unidentifiable,
    count(*) FILTER (WHERE product_id_status = 'missing')            AS product_id_missing,
    count(*) FILTER (WHERE product_id_status = 'blank')              AS product_id_empty,
    count(*) FILTER (WHERE reviewer_id_status = 'missing')           AS user_id_missing,
    count(*) FILTER (WHERE reviewer_id_status = 'blank')             AS user_id_empty,
    count(*) FILTER (WHERE reviewer_id_observed = 'unknown')         AS user_id_unknown_literal,
    (SELECT count(DISTINCT source_review_id) FROM quality_issue q WHERE q.extraction_id = %(eid)s)
                                                                     AS records_with_quality_issues,
    (SELECT count(*) FROM quality_issue q WHERE q.extraction_id = %(eid)s) AS quality_issue_rows,
    (SELECT count(*) FROM review_staging s WHERE s.extraction_id = %(eid)s) AS staging_rows,
    (SELECT count(*) FROM quarantine_record c WHERE c.extraction_id = %(eid)s) AS quarantine_rows,
    (SELECT coalesce(sum(valid_count), 0) FROM etl_batch b WHERE b.extraction_id = %(eid)s) AS batch_valid_sum,
    (SELECT coalesce(sum(quarantined_count), 0) FROM etl_batch b WHERE b.extraction_id = %(eid)s) AS batch_quarantined_sum,
    (SELECT count(*) FROM etl_batch b WHERE b.extraction_id = %(eid)s) AS batches_committed,
    0                                                                AS records_removed_by_dedup
FROM review_record
WHERE extraction_id = %(eid)s;
