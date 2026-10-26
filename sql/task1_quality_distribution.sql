-- 质量问题分布：问题行数与涉及的记录数分别统计（spec R15、R23）。
SELECT code, severity, count(*) AS issue_rows, count(DISTINCT source_review_id) AS records
FROM quality_issue
WHERE extraction_id = %(eid)s
GROUP BY code, severity
ORDER BY issue_rows DESC, code;
