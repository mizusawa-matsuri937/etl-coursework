-- 任务 2 清单来源：每个 observed ASIN 的来源统计与有效记录评论时间（spec R02、R03、R10、R11）。
-- observed 口径同任务 1 R22 与 sql/task1_distinct_products.sql，含只出现在隔离记录中的 ASIN；
-- 评论时间只取有效记录已转换的 UTC 时间，隔离记录或非法时间不参与。
SELECT r.product_id_observed                                     AS asin,
       count(*)                                                  AS records_total,
       count(*) FILTER (WHERE r.classification = 'valid')        AS records_valid,
       count(*) FILTER (WHERE r.classification = 'quarantined')  AS records_quarantined,
       min(r.record_no)                                          AS first_record_no,
       max(r.record_no)                                          AS last_record_no,
       min(s.review_time_utc)                                    AS earliest_review_time,
       max(s.review_time_utc)                                    AS latest_review_time
FROM review_record r
LEFT JOIN review_staging s
       ON s.extraction_id = r.extraction_id AND s.source_review_id = r.source_review_id
WHERE r.extraction_id = %(eid)s AND r.product_id_observed IS NOT NULL
GROUP BY r.product_id_observed
ORDER BY r.product_id_observed COLLATE "C"
