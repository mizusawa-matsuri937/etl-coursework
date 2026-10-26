-- 任务 1 完整性核对：每行是一项检查，violations 必须为 0。
-- 用法（psycopg 命名参数）：conn.execute(open(...).read(), {"eid": extraction_id})
-- 所有检查限定同一 extraction_id，不混入样本或其他规则版本。
SELECT 'valid_without_staging' AS check_name, count(*) AS violations
FROM review_record r
WHERE r.extraction_id = %(eid)s AND r.classification = 'valid'
  AND NOT EXISTS (SELECT 1 FROM review_staging s
                  WHERE s.extraction_id = r.extraction_id AND s.source_review_id = r.source_review_id)
UNION ALL
SELECT 'quarantined_without_quarantine_row', count(*)
FROM review_record r
WHERE r.extraction_id = %(eid)s AND r.classification = 'quarantined'
  AND NOT EXISTS (SELECT 1 FROM quarantine_record q
                  WHERE q.extraction_id = r.extraction_id AND q.source_review_id = r.source_review_id)
UNION ALL
SELECT 'record_numbers_not_contiguous', CASE WHEN count(*) = 0 THEN 0
       WHEN min(record_no) = 1 AND max(record_no) = count(*) THEN 0 ELSE 1 END
FROM review_record WHERE extraction_id = %(eid)s
UNION ALL
SELECT 'batch_counts_differ_from_records', count(*)
FROM etl_batch b
LEFT JOIN (  -- 一次分组汇总各批实际行，避免对每个批次反复扫描（全量时为二次方耗时）
    SELECT batch_no,
           count(*) FILTER (WHERE classification = 'valid') AS valid_n,
           count(*) FILTER (WHERE classification = 'quarantined') AS quarantined_n,
           min(record_no) AS first_no, max(record_no) AS last_no, sum(byte_size) AS bytes
    FROM review_record WHERE extraction_id = %(eid)s GROUP BY batch_no) a USING (batch_no)
WHERE b.extraction_id = %(eid)s AND (
    a.batch_no IS NULL
 OR b.valid_count <> a.valid_n OR b.quarantined_count <> a.quarantined_n
 OR b.first_record_no <> a.first_no OR b.last_record_no <> a.last_no OR b.byte_count <> a.bytes)
UNION ALL
SELECT 'batches_not_contiguous', count(*)
FROM etl_batch b
WHERE b.extraction_id = %(eid)s AND b.batch_no > 1 AND b.first_record_no <>
      (SELECT p.last_record_no + 1 FROM etl_batch p WHERE p.extraction_id = b.extraction_id AND p.batch_no = b.batch_no - 1)
UNION ALL
SELECT 'records_without_committed_batch', count(*)
FROM review_record r
WHERE r.extraction_id = %(eid)s AND NOT EXISTS (
    SELECT 1 FROM etl_batch b WHERE b.extraction_id = r.extraction_id AND b.batch_no = r.batch_no)
UNION ALL
SELECT 'staging_product_differs_from_observed', count(*)
FROM review_staging s JOIN review_record r USING (extraction_id, source_review_id)
WHERE s.extraction_id = %(eid)s AND s.product_id IS DISTINCT FROM r.product_id_observed
UNION ALL
SELECT 'quarantine_bytes_differ_from_record', count(*)
FROM quarantine_record q JOIN review_record r USING (extraction_id, source_review_id)
WHERE q.extraction_id = %(eid)s AND (
    (q.raw_bytes IS NOT NULL AND (octet_length(q.raw_bytes) <> r.byte_size
        OR encode(sha256(q.raw_bytes), 'hex') <> r.raw_sha256))
 OR (q.spill_path IS NOT NULL AND (q.spill_size <> r.byte_size OR q.spill_sha256 <> r.raw_sha256)))
UNION ALL
SELECT 'issue_without_record', count(*)
FROM quality_issue i
WHERE i.extraction_id = %(eid)s AND NOT EXISTS (
    SELECT 1 FROM review_record r WHERE r.extraction_id = i.extraction_id AND r.source_review_id = i.source_review_id)
UNION ALL
SELECT 'complete_extraction_count_mismatch', count(*)
FROM extraction e
WHERE e.extraction_id = %(eid)s AND e.status = 'complete' AND
      (SELECT coalesce(sum(valid_count + quarantined_count), 0) FROM etl_batch b WHERE b.extraction_id = e.extraction_id)
      <> (SELECT count(*) FROM review_record r WHERE r.extraction_id = e.extraction_id)
ORDER BY check_name;
