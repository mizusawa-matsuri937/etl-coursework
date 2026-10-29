-- 任务 2 第二部分：爬虫运行审计与逐次尝试日志（schema task2）。
-- 幂等：由 etl_task2.crawl.crawler.apply_crawl_schema 通过 public.schema_migration 判断是否已应用。
-- 清单行只更新爬虫拥有的列；每次请求的明细放在 fetch_attempt，不在清单行里堆叠历史（design“爬虫接口约定”）。

-- 每次爬虫运行一行：参数、起止时间与本次各最终状态计数。
CREATE TABLE task2.crawl_run (
    run_id          text PRIMARY KEY,
    extraction_id   text NOT NULL REFERENCES public.extraction,
    status          text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'interrupted', 'failed')),
    params          jsonb NOT NULL,
    asin_done       bigint NOT NULL DEFAULT 0 CHECK (asin_done >= 0),
    outcome_counts  jsonb,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    failure_reason  text,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

-- 每次 HTTP 尝试一行；attempt_no 在同一 ASIN 内跨运行累计，与清单的 attempt_count 对应。
CREATE TABLE task2.fetch_attempt (
    extraction_id   text NOT NULL,
    asin            text NOT NULL,
    attempt_no      integer NOT NULL CHECK (attempt_no >= 1),
    run_id          text NOT NULL REFERENCES task2.crawl_run,
    exit_label      text NOT NULL,          -- 出口标签（host:port 或 direct），不含账号密码
    requested_url   text NOT NULL,
    final_url       text,
    http_status     integer CHECK (http_status BETWEEN 100 AND 599),
    outcome         text NOT NULL CHECK (outcome IN ('success', 'not_found', 'access_restricted',
                                                     'timeout', 'parse_failed')),
    reason          text,
    body_bytes      integer CHECK (body_bytes >= 0),
    content_hash    text,
    snapshot_path   text,                   -- 只有被保留快照的那次尝试（每个 ASIN 的最后一次）有值
    started_at      timestamptz NOT NULL,
    elapsed_ms      integer NOT NULL CHECK (elapsed_ms >= 0),
    PRIMARY KEY (extraction_id, asin, attempt_no),
    FOREIGN KEY (extraction_id, asin) REFERENCES task2.product_manifest ON DELETE CASCADE
);

CREATE INDEX fetch_attempt_run_idx ON task2.fetch_attempt (run_id);

INSERT INTO public.schema_migration (version) VALUES ('102');
