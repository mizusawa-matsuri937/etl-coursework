-- 任务 2 第一部分：产品采集清单（schema task2）。
-- 幂等：由 etl_task2.manifest.apply_task2_schema 通过 public.schema_migration 判断是否已应用。
-- 只引用任务 1 的 public.extraction，不在任务 1 的表上加列或约束（spec R15）。
-- 所有查询必须显式限定 extraction_id。

CREATE SCHEMA task2;

-- 一次“抽取 × 链接模板版本 × 规则版本”的构建定义；同输入同规则得到同一 build_id。
CREATE TABLE task2.manifest_build (
    build_id              text PRIMARY KEY CHECK (build_id ~ '^[0-9a-f]{64}$'),
    extraction_id         text NOT NULL REFERENCES public.extraction,
    scope                 text NOT NULL CHECK (scope IN ('full', 'sample')),
    url_template          text NOT NULL,
    url_template_version  text NOT NULL,
    rule_version          text NOT NULL,
    asin_count            bigint CHECK (asin_count >= 0),  -- 最近一次成功构建后的清单行数
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (extraction_id, url_template_version, rule_version)
);

-- 每次执行的审计（含复用与失败）。
CREATE TABLE task2.manifest_run (
    run_id         text PRIMARY KEY,
    build_id       text NOT NULL REFERENCES task2.manifest_build,
    dag_id         text,
    airflow_run_id text,
    status         text NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'succeeded', 'failed', 'reused')),
    inserted_rows  bigint NOT NULL DEFAULT 0 CHECK (inserted_rows >= 0),
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    failure_reason text,
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK (status = 'failed' OR failure_reason IS NULL)
);

-- 采集清单：每个 ASIN 一行。构建拥有的列只在首次插入时写入；爬虫拥有的列构建永不触碰（design“爬虫接口约定”）。
CREATE TABLE task2.product_manifest (
    extraction_id         text NOT NULL REFERENCES public.extraction,
    asin                  text NOT NULL,
    -- 构建拥有：候选链接（R05、R06）
    original_url          text,
    url_issue             text,
    url_template_version  text NOT NULL,
    first_build_id        text NOT NULL REFERENCES task2.manifest_build,
    -- 构建拥有：来源定位与评论时间（R03、R10、R11）
    in_valid_records      boolean NOT NULL,
    records_total         bigint NOT NULL CHECK (records_total >= 1),
    records_valid         bigint NOT NULL CHECK (records_valid >= 0),
    records_quarantined   bigint NOT NULL CHECK (records_quarantined >= 0),
    first_record_no       bigint NOT NULL CHECK (first_record_no >= 1),
    last_record_no        bigint NOT NULL,
    earliest_review_time  timestamptz,
    latest_review_time    timestamptz,
    -- 爬虫拥有：处理状态与采集证据（R07–R09）
    status                text NOT NULL DEFAULT 'not_collected'
                          CHECK (status IN ('success', 'not_found', 'access_restricted',
                                            'timeout', 'parse_failed', 'not_collected')),
    final_url             text,
    http_status           integer CHECK (http_status BETWEEN 100 AND 599),
    attempt_count         integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    fetched_at            timestamptz,
    snapshot_path         text,
    content_hash          text,
    status_reason         text,
    updated_at            timestamptz,
    PRIMARY KEY (extraction_id, asin),
    CHECK ((original_url IS NULL) = (url_issue IS NOT NULL)),
    CHECK (records_total = records_valid + records_quarantined),
    CHECK (in_valid_records = (records_valid > 0)),
    CHECK (last_record_no >= first_record_no),
    CHECK (earliest_review_time IS NULL OR latest_review_time >= earliest_review_time),
    CHECK ((earliest_review_time IS NULL) = (latest_review_time IS NULL)),
    -- R09：成功必须带齐证据，不能用空记录冒充成功
    CHECK (status <> 'success' OR (final_url IS NOT NULL AND http_status IS NOT NULL AND fetched_at IS NOT NULL
                                   AND snapshot_path IS NOT NULL AND content_hash IS NOT NULL))
);

-- 爬虫按状态取待采集行。
CREATE INDEX product_manifest_status_idx ON task2.product_manifest (extraction_id, status, asin);

INSERT INTO public.schema_migration (version) VALUES ('101');
