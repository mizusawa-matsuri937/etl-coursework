-- 任务 2 清单导出（spec R07、R14）：每个 ASIN 一行，含构建列与爬虫列；不含评论正文。
SELECT asin, original_url, url_issue, url_template_version, status, final_url, http_status, attempt_count,
       fetched_at, snapshot_path, content_hash, status_reason, in_valid_records, records_total,
       records_valid, records_quarantined, first_record_no, last_record_no,
       earliest_review_time, latest_review_time
FROM task2.product_manifest
WHERE extraction_id = %(eid)s
ORDER BY asin COLLATE "C"
