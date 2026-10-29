-- 任务 3：电影页面筛选（schema task3）。
-- 幂等：由 etl_task3.classify.apply_task3_schema 通过 public.schema_migration 判断是否已应用。
-- 只引用 public.extraction 与 task2.product_manifest，不在任务 1、任务 2 的表上加列或约束（spec R22、R29）。
-- 所有查询必须显式限定 extraction_id；解析结果按 parser_version、分类按 rule_version 分开保存（R24、R25）。

CREATE SCHEMA task3;

-- 每次运行的审计（含复用与中断）。
CREATE TABLE task3.classify_run (
    run_id            text PRIMARY KEY,
    extraction_id     text NOT NULL REFERENCES public.extraction,
    parser_version    text NOT NULL,
    label_map_version text NOT NULL,
    rule_version      text NOT NULL,
    dag_id            text,
    airflow_run_id    text,
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'succeeded', 'failed', 'reused', 'interrupted')),
    params            jsonb NOT NULL DEFAULT '{}',
    parsed            bigint NOT NULL DEFAULT 0 CHECK (parsed >= 0),       -- 本次新解析的页面数
    classified        bigint NOT NULL DEFAULT 0 CHECK (classified >= 0),   -- 本次新写入的分类行数
    started_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at       timestamptz,
    failure_reason    text,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

-- dp_page 组每个 ASIN、每个解析版本一行；证据定位信息复制自任务 2，便于直接回到原页面（R05、R17）。
CREATE TABLE task3.page_parse (
    extraction_id    text NOT NULL,
    asin             text NOT NULL,
    parser_version   text NOT NULL,
    content_hash     text NOT NULL,
    snapshot_path    text NOT NULL,
    final_url        text NOT NULL,
    fetched_at       timestamptz NOT NULL,
    attempt_no       integer NOT NULL CHECK (attempt_no >= 1),
    parse_status     text NOT NULL
                     CHECK (parse_status IN ('parsed', 'snapshot_missing', 'snapshot_hash_mismatch', 'parse_error')),
    sections_present jsonb,           -- 7 个证据区域各自是否存在；未解析时为 NULL
    field_count      integer NOT NULL DEFAULT 0 CHECK (field_count >= 0),
    run_id           text NOT NULL REFERENCES task3.classify_run,
    PRIMARY KEY (extraction_id, asin, parser_version),
    FOREIGN KEY (extraction_id, asin) REFERENCES task2.product_manifest,
    CHECK (parse_status = 'parsed' OR (field_count = 0 AND sections_present IS NULL))
);

-- 抽取字段：一个值一行；区域 + 序号在一个快照内唯一，配合 content_hash 回到原页面位置（R06、R07）。
CREATE TABLE task3.page_field (
    extraction_id  text NOT NULL,
    asin           text NOT NULL,
    parser_version text NOT NULL,
    section        text NOT NULL CHECK (section IN ('title', 'breadcrumb', 'detail_bullets', 'product_overview',
                                                    'product_details', 'byline', 'format_swatch')),
    ordinal        integer NOT NULL CHECK (ordinal >= 1),
    label_raw      text,
    value_raw      text NOT NULL,
    field_key      text,              -- 标签映射结果（LABEL_MAP_VERSION），未知标签为 NULL
    PRIMARY KEY (extraction_id, asin, parser_version, section, ordinal),
    FOREIGN KEY (extraction_id, asin, parser_version) REFERENCES task3.page_parse ON DELETE CASCADE
);

CREATE INDEX page_field_key_idx ON task3.page_field (extraction_id, parser_version, field_key);

-- 每个清单 ASIN、每个规则版本一行（含无页面的七组）（R02、R08、R16）。
CREATE TABLE task3.product_classification (
    extraction_id  text NOT NULL,
    asin           text NOT NULL,
    rule_version   text NOT NULL,
    input_group    text NOT NULL CHECK (input_group IN ('dp_page', 'automation_title', 'other_site',
                                                        'access_restricted', 'not_found', 'timeout',
                                                        'parse_failed', 'not_collected')),
    category       text NOT NULL CHECK (category IN ('movie', 'non_movie', 'collection', 'tv_series',
                                                     'concert_other', 'pending')),
    basis_level    text NOT NULL CHECK (basis_level IN ('structured', 'inferred', 'missing')),
    rule_id        text NOT NULL,
    pending_reason text,
    parser_version text,              -- dp_page 组为解析版本，其余为 NULL
    run_id         text NOT NULL REFERENCES task3.classify_run,
    classified_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (extraction_id, asin, rule_version),
    FOREIGN KEY (extraction_id, asin) REFERENCES task2.product_manifest,
    FOREIGN KEY (extraction_id, asin, parser_version) REFERENCES task3.page_parse,
    CHECK ((category = 'pending') = (basis_level = 'missing')),
    CHECK ((category = 'pending') = (pending_reason IS NOT NULL)),
    CHECK (input_group = 'dp_page' OR category = 'pending'),
    CHECK ((input_group = 'dp_page') = (parser_version IS NOT NULL))
);

CREATE INDEX product_classification_cat_idx ON task3.product_classification (extraction_id, rule_version, category);

-- 每条信号一行；used=false 的是命中规则以外的信号，其中指向别的类别的就是冲突证据（R17）。
CREATE TABLE task3.classification_evidence (
    extraction_id  text NOT NULL,
    asin           text NOT NULL,
    rule_version   text NOT NULL,
    seq            integer NOT NULL CHECK (seq >= 1),
    signal         text NOT NULL,
    supports       text CHECK (supports IN ('movie', 'non_movie', 'collection', 'tv_series', 'concert_other')),
    used           boolean NOT NULL,
    parser_version text,              -- 页面信号指向 page_field；清单与快照信号为 NULL
    section        text,
    ordinal        integer,
    value_raw      text,
    value_num      numeric,
    source_ref     jsonb,             -- 清单状态、尝试序号或快照检查结果
    PRIMARY KEY (extraction_id, asin, rule_version, seq),
    FOREIGN KEY (extraction_id, asin, rule_version) REFERENCES task3.product_classification ON DELETE CASCADE,
    FOREIGN KEY (extraction_id, asin, parser_version, section, ordinal) REFERENCES task3.page_field,
    CHECK ((section IS NULL) = (ordinal IS NULL)),
    CHECK (section IS NULL OR parser_version IS NOT NULL),
    CHECK (section IS NOT NULL OR source_ref IS NOT NULL)
);

-- 质量问题：快照缺失、哈希不一致、解析异常、证据区域全缺。
CREATE TABLE task3.quality_issue (
    extraction_id  text NOT NULL,
    asin           text NOT NULL,
    parser_version text NOT NULL,
    issue_code     text NOT NULL CHECK (issue_code IN ('snapshot_missing', 'snapshot_hash_mismatch', 'parse_error',
                                                       'no_evidence_sections')),
    detail         jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (extraction_id, asin, parser_version, issue_code),
    FOREIGN KEY (extraction_id, asin, parser_version) REFERENCES task3.page_parse ON DELETE CASCADE
);

-- 人工抽查：只存人工结论，不回写自动分类（R28）。
CREATE TABLE task3.manual_review (
    extraction_id    text NOT NULL,
    asin             text NOT NULL,
    rule_version     text NOT NULL,
    sample_stratum   text NOT NULL,
    auto_category    text NOT NULL,
    human_category   text NOT NULL CHECK (human_category IN ('movie', 'non_movie', 'collection', 'tv_series',
                                                             'concert_other', 'pending')),
    note             text,
    reviewer         text NOT NULL,
    reviewed_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_hash text NOT NULL,
    PRIMARY KEY (extraction_id, asin, rule_version),
    FOREIGN KEY (extraction_id, asin, rule_version) REFERENCES task3.product_classification
);

INSERT INTO public.schema_migration (version) VALUES ('201');
