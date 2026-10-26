-- 不同产品汇总（供任务 2 接续）：来自完整 observed 口径，含隔离记录；不是页面采集清单。
SELECT r.product_id_observed AS product_id,
       count(*)                                              AS records_total,
       count(*) FILTER (WHERE r.classification = 'valid')       AS records_valid,
       count(*) FILTER (WHERE r.classification = 'quarantined') AS records_quarantined,
       min(r.record_no)                                      AS first_record_no,
       max(r.record_no)                                      AS last_record_no,
       coalesce(max(f.n), 0)                                 AS id_format_issue_records
FROM review_record r
LEFT JOIN (
    SELECT r2.product_id_observed AS pid, count(*) AS n
    FROM quality_issue i
    JOIN review_record r2 ON r2.extraction_id = i.extraction_id AND r2.source_review_id = i.source_review_id
    WHERE i.extraction_id = %(eid)s AND i.code = 'nonstandard_product_id_format'
    GROUP BY r2.product_id_observed
) f ON f.pid = r.product_id_observed
WHERE r.extraction_id = %(eid)s AND r.product_id_observed IS NOT NULL
GROUP BY r.product_id_observed
ORDER BY r.product_id_observed COLLATE "C";
