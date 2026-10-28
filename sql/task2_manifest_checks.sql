-- 任务 2 清单核对（spec R02、R03、R08）：清单侧计数与从任务 1 记录现算的 observed/valid 产品数，一行结果。
-- 产品数口径同 sql/task1_counts.sql；全量时为一次带 extraction_id 条件的全表聚合，需数分钟。
SELECT
    (SELECT count(*) FROM task2.product_manifest m WHERE m.extraction_id = %(eid)s)                AS manifest_rows,
    (SELECT count(*) FROM task2.product_manifest m
      WHERE m.extraction_id = %(eid)s AND m.in_valid_records)                                     AS in_valid_true,
    (SELECT count(*) FROM task2.product_manifest m
      WHERE m.extraction_id = %(eid)s AND NOT m.in_valid_records)                                 AS in_valid_false,
    (SELECT count(*) FROM task2.product_manifest m
      WHERE m.extraction_id = %(eid)s AND m.url_issue IS NOT NULL)                                AS url_issue_rows,
    (SELECT count(*) FROM task2.product_manifest m
      WHERE m.extraction_id = %(eid)s AND m.url_template_version <> %(tv)s)                       AS other_template_rows,
    count(DISTINCT r.product_id_observed)                                                         AS products_observed,
    count(DISTINCT r.product_id_observed) FILTER (WHERE r.classification = 'valid')               AS products_valid
FROM review_record r
WHERE r.extraction_id = %(eid)s;
