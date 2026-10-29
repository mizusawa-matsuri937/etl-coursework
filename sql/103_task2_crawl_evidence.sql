-- 任务 2 爬虫证据补充（schema task2）：每次请求的完整跳转链与页面模板（spec R32、R33）。
-- 幂等：由 etl_task2.crawl.crawler.apply_crawl_schema 通过 public.schema_migration 判断是否已应用。
-- 103 之前的尝试没有记录跳转链，redirect_chain 保持 NULL；空数组表示确认没有跳转。

ALTER TABLE task2.fetch_attempt
    ADD COLUMN redirect_chain jsonb
        CHECK (redirect_chain IS NULL OR jsonb_typeof(redirect_chain) = 'array'),
    ADD COLUMN page_template text
        CHECK (page_template IN ('dp_product_title', 'automation_title', 'checkpoint', 'captcha',
                                 'not_found', 'service_error', 'other'));

COMMENT ON COLUMN task2.fetch_attempt.redirect_chain IS
    '跳转链：按顺序的 [{status, url, location}]，最终页面不在其中；NULL 表示未记录（103 之前或请求异常）';
COMMENT ON COLUMN task2.fetch_attempt.page_template IS
    '响应页面模板（detect_template）；NULL 表示没有响应体或尚未回填';

INSERT INTO public.schema_migration (version) VALUES ('103');
