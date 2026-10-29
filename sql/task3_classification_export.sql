-- 任务 3 分类导出（spec R23）：每个清单 ASIN 一行，含商品标题与命中规则用到的信号；不含评论正文与整页 HTML。
-- 参数：%(eid)s 抽取 ID、%(rv)s 规则版本。只读。
SELECT c.asin, c.input_group, c.category, c.basis_level, c.rule_id, c.pending_reason,
       (SELECT f.value_raw FROM task3.page_field f
        WHERE f.extraction_id = c.extraction_id AND f.asin = c.asin AND f.parser_version = c.parser_version
          AND f.section = 'title' AND f.ordinal = 1) AS title,
       (SELECT string_agg(e.signal, ',' ORDER BY e.seq) FROM task3.classification_evidence e
        WHERE e.extraction_id = c.extraction_id AND e.asin = c.asin AND e.rule_version = c.rule_version
          AND e.used) AS used_signals,
       m.records_total, m.records_valid, m.records_quarantined
FROM task3.product_classification c
JOIN task2.product_manifest m ON m.extraction_id = c.extraction_id AND m.asin = c.asin
WHERE c.extraction_id = %(eid)s AND c.rule_version = %(rv)s
ORDER BY c.asin COLLATE "C"
