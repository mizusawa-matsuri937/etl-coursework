-- 任务 1 暂存层：来源、抽取、执行、批次、记录、暂存/隔离子表与质量问题。
-- 幂等：由 etl_task1.db.apply_schema 通过 schema_migration 判断是否已应用。
-- 所有业务查询必须显式限定 extraction_id（样本、全量与不同规则版本互相隔离）。

CREATE TABLE schema_migration (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source_file (
    source_file_id        text PRIMARY KEY CHECK (source_file_id ~ '^[0-9a-f]{64}$'),
    path                  text NOT NULL,
    source_url            text NOT NULL,
    download_completed_at timestamptz,            -- 来源时间未知时为 NULL，不用验证时间代替
    verified_at           timestamptz NOT NULL,   -- 本次实测校验时间
    recorded_size_bytes   bigint NOT NULL CHECK (recorded_size_bytes > 0),
    measured_size_bytes   bigint NOT NULL CHECK (measured_size_bytes > 0),
    recorded_sha256       text NOT NULL CHECK (recorded_sha256 ~ '^[0-9a-f]{64}$'),
    measured_sha256       text NOT NULL CHECK (measured_sha256 ~ '^[0-9a-f]{64}$'),
    sha256_is_official    boolean NOT NULL DEFAULT false,
    CHECK (measured_size_bytes = recorded_size_bytes),
    CHECK (measured_sha256 = recorded_sha256),
    CHECK (source_file_id = measured_sha256)
);

CREATE TABLE extraction (
    extraction_id       text PRIMARY KEY CHECK (extraction_id ~ '^[0-9a-f]{64}$'),
    source_file_id      text NOT NULL REFERENCES source_file,
    parser_version      text NOT NULL,
    rules_version       text NOT NULL,
    config              jsonb NOT NULL,
    scope               text NOT NULL CHECK (scope IN ('full', 'sample')),
    sample_max_records  bigint CHECK (sample_max_records > 0),
    scope_complete      boolean NOT NULL DEFAULT false,
    source_eof_reached  boolean NOT NULL DEFAULT false,
    gzip_verified       boolean NOT NULL DEFAULT false,
    status              text NOT NULL DEFAULT 'loading'
                        CHECK (status IN ('loading', 'complete', 'failed')),
    failure_reason      text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    CHECK ((scope = 'full') = (sample_max_records IS NULL)),
    -- 全量完成必须读到 EOF 并通过 gzip 校验；样本完成只要求范围完成。
    CHECK (status <> 'complete' OR scope_complete),
    CHECK (status <> 'complete' OR scope = 'sample' OR (source_eof_reached AND gzip_verified))
);

CREATE TABLE etl_run (
    run_id           text PRIMARY KEY,
    extraction_id    text NOT NULL REFERENCES extraction,
    dag_id           text,
    airflow_run_id   text,
    program_version  jsonb NOT NULL,
    image_digest     text,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    status           text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'succeeded', 'failed', 'reused')),
    failure_reason   text,
    batches_committed bigint NOT NULL DEFAULT 0,
    records_loaded    bigint NOT NULL DEFAULT 0
);

CREATE TABLE etl_batch (
    extraction_id     text NOT NULL REFERENCES extraction,
    batch_no          integer NOT NULL CHECK (batch_no >= 1),
    first_record_no   bigint NOT NULL CHECK (first_record_no >= 1),
    last_record_no    bigint NOT NULL,
    resume_offset     bigint NOT NULL CHECK (resume_offset >= 0),  -- 最后一条已提交记录的 end_offset
    resume_line       bigint NOT NULL CHECK (resume_line >= 1),    -- 其后的下一物理行号
    valid_count       integer NOT NULL CHECK (valid_count >= 0),
    quarantined_count integer NOT NULL CHECK (quarantined_count >= 0),
    byte_count        bigint NOT NULL CHECK (byte_count >= 0),
    batch_sha256      text NOT NULL CHECK (batch_sha256 ~ '^[0-9a-f]{64}$'),
    run_id            text NOT NULL REFERENCES etl_run,
    committed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (extraction_id, batch_no),
    UNIQUE (extraction_id, first_record_no),
    UNIQUE (extraction_id, last_record_no),
    CHECK (last_record_no >= first_record_no),
    CHECK (valid_count + quarantined_count = last_record_no - first_record_no + 1)
);

CREATE TABLE review_record (
    extraction_id       text NOT NULL,
    source_review_id    text NOT NULL CHECK (source_review_id ~ '^[0-9a-f]{64}$'),
    record_no           bigint NOT NULL CHECK (record_no >= 1),
    start_line          bigint NOT NULL CHECK (start_line >= 1),
    end_line            bigint NOT NULL,
    start_offset        bigint NOT NULL CHECK (start_offset >= 0),
    end_offset          bigint NOT NULL,
    byte_size           bigint NOT NULL CHECK (byte_size > 0),
    raw_sha256          text NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),
    classification      text NOT NULL CHECK (classification IN ('valid', 'quarantined')),
    -- R22：可明确识别的原始 ID（含隔离记录）及状态
    product_id_observed text,
    product_id_status   text NOT NULL
                        CHECK (product_id_status IN ('ok', 'missing', 'blank', 'conflict', 'undecodable')),
    reviewer_id_observed text,
    reviewer_id_status  text NOT NULL
                        CHECK (reviewer_id_status IN ('ok', 'missing', 'blank', 'conflict', 'undecodable')),
    batch_no            integer NOT NULL,
    PRIMARY KEY (extraction_id, source_review_id),
    UNIQUE (extraction_id, record_no),
    UNIQUE (extraction_id, source_review_id, classification),  -- 供子表复合外键，保证互斥
    FOREIGN KEY (extraction_id, batch_no) REFERENCES etl_batch,
    CHECK (end_line >= start_line),
    CHECK (end_offset - start_offset = byte_size),
    CHECK ((product_id_status = 'ok') = (product_id_observed IS NOT NULL)),
    CHECK ((reviewer_id_status = 'ok') = (reviewer_id_observed IS NOT NULL)),
    CHECK (classification <> 'valid' OR product_id_status = 'ok')
);
CREATE INDEX review_record_product_idx  ON review_record (extraction_id, product_id_observed);
CREATE INDEX review_record_reviewer_idx ON review_record (extraction_id, reviewer_id_observed);
CREATE INDEX review_record_batch_idx    ON review_record (extraction_id, batch_no);

CREATE TABLE review_staging (
    extraction_id       text NOT NULL,
    source_review_id    text NOT NULL,
    classification      text NOT NULL DEFAULT 'valid' CHECK (classification = 'valid'),
    product_id_raw      text NOT NULL,
    product_id          text NOT NULL,
    reviewer_id_raw     text,
    reviewer_id         text,
    profile_name_raw    text,
    helpfulness_raw     text,
    helpful_votes       bigint CHECK (helpful_votes >= 0),
    total_votes         bigint CHECK (total_votes >= 0),
    helpfulness_ratio   numeric(12, 10) CHECK (helpfulness_ratio BETWEEN 0 AND 1),
    score_raw           text,
    score               numeric CHECK (score BETWEEN 1 AND 5),
    review_time_raw     text,
    review_time_unix    bigint,
    review_time_utc     timestamptz,
    summary_raw         text,
    review_text_raw     text,
    field_present       jsonb NOT NULL,
    field_start_lines   jsonb NOT NULL,
    rules_version       text NOT NULL,
    PRIMARY KEY (extraction_id, source_review_id),
    FOREIGN KEY (extraction_id, source_review_id, classification)
        REFERENCES review_record (extraction_id, source_review_id, classification),
    CHECK (product_id_raw = product_id AND btrim(product_id) <> ''),
    CHECK ((helpful_votes IS NULL) = (total_votes IS NULL)),
    CHECK (helpful_votes IS NULL OR helpful_votes <= total_votes),
    CHECK ((helpfulness_ratio IS NULL) = (total_votes IS NULL OR total_votes = 0)),
    CHECK (reviewer_id IS NULL OR (reviewer_id = reviewer_id_raw AND btrim(reviewer_id) <> '')),
    CHECK (review_time_utc IS NULL OR review_time_unix IS NOT NULL)
);
CREATE INDEX review_staging_product_idx  ON review_staging (extraction_id, product_id);
CREATE INDEX review_staging_reviewer_idx ON review_staging (extraction_id, reviewer_id);

CREATE TABLE quarantine_record (
    extraction_id     text NOT NULL,
    source_review_id  text NOT NULL,
    classification    text NOT NULL DEFAULT 'quarantined' CHECK (classification = 'quarantined'),
    reasons           text[] NOT NULL CHECK (cardinality(reasons) >= 1),
    raw_bytes         bytea,      -- 常规隔离记录的完整原始字节
    spill_path        text,       -- 超限记录的旁路文件（先完整落盘再由事务引用）
    spill_sha256      text CHECK (spill_sha256 ~ '^[0-9a-f]{64}$'),
    spill_size        bigint CHECK (spill_size > 0),
    rules_version     text NOT NULL,
    PRIMARY KEY (extraction_id, source_review_id),
    FOREIGN KEY (extraction_id, source_review_id, classification)
        REFERENCES review_record (extraction_id, source_review_id, classification),
    CHECK ((raw_bytes IS NOT NULL) <> (spill_path IS NOT NULL)),
    CHECK ((spill_path IS NULL) = (spill_sha256 IS NULL AND spill_size IS NULL))
);

CREATE TABLE quality_issue (
    extraction_id     text NOT NULL,
    issue_key         text NOT NULL CHECK (issue_key ~ '^[0-9a-f]{64}$'),  -- 确定性键，重试不重复
    source_review_id  text NOT NULL,
    field             text,
    code              text NOT NULL,
    severity          text NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
    evidence          text,
    details           jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_version      text NOT NULL,
    PRIMARY KEY (extraction_id, issue_key),
    FOREIGN KEY (extraction_id, source_review_id) REFERENCES review_record
);
CREATE INDEX quality_issue_record_idx ON quality_issue (extraction_id, source_review_id);
CREATE INDEX quality_issue_code_idx   ON quality_issue (extraction_id, code);

-- 完备性：有效记录必须有暂存子行，隔离记录必须有隔离子行（事务提交时检查）。
CREATE FUNCTION check_review_record_child() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.classification = 'valid' AND NOT EXISTS (
        SELECT 1 FROM review_staging s
        WHERE s.extraction_id = NEW.extraction_id AND s.source_review_id = NEW.source_review_id) THEN
        RAISE EXCEPTION 'valid record % has no review_staging row', NEW.source_review_id
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF NEW.classification = 'quarantined' AND NOT EXISTS (
        SELECT 1 FROM quarantine_record s
        WHERE s.extraction_id = NEW.extraction_id AND s.source_review_id = NEW.source_review_id) THEN
        RAISE EXCEPTION 'quarantined record % has no quarantine_record row', NEW.source_review_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER review_record_child_complete
    AFTER INSERT ON review_record
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_review_record_child();

INSERT INTO schema_migration (version) VALUES ('001');
