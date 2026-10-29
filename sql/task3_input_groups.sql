-- 任务 3 输入分组（spec R03）：每个清单 ASIN 恰好归入一组，附带最后一次尝试的证据定位。
-- 参数：%(eid)s 抽取 ID。只读。
SELECT m.asin,
       CASE
           WHEN m.status = 'success' AND m.final_url NOT LIKE 'https://www.amazon.com/%%' THEN 'other_site'
           WHEN m.status = 'success' AND a.page_template = 'dp_product_title' THEN 'dp_page'
           WHEN m.status = 'success' AND a.page_template = 'automation_title' THEN 'automation_title'
           WHEN m.status = 'success' THEN NULL  -- 未知模板：检查步骤报错，不猜测归组
           ELSE m.status
       END AS input_group,
       m.status, m.status_reason, m.attempt_count, m.snapshot_path, m.content_hash, m.final_url, m.fetched_at,
       a.page_template
FROM task2.product_manifest m
LEFT JOIN task2.fetch_attempt a
       ON a.extraction_id = m.extraction_id AND a.asin = m.asin AND a.attempt_no = m.attempt_count
WHERE m.extraction_id = %(eid)s
