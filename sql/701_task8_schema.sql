-- 任务 8：评论清洗与重复识别（schema task8）。
-- 幂等：由 etl_task8.run.apply_task8_schema 通过 public.schema_migration 判断是否已应用（须先有 001 与 601）。
-- 只引用 public.extraction 与 task7.conflict_version，不在任务 1–7 的表上加列或约束（spec R39）。
-- 输入键 = 抽取 × 任务 3 分类规则版本 × 任务 7 冲突消解版本 × 规则版本（R01、R33）。
-- 阶段键（R34）：review_clean → review_family → review_dedup → review_attribution，下游包含上游。
-- 大表按阶段键保存、被多个去重版本复用；阶段键在大表里是短键（阶段首字母 + 完整串 SHA-256 前 15 位），
-- 完整串、所属抽取与上游阶段登记在 task8.stage_key（design“数据模型”）。大表不再重复存 extraction_id，
-- 也不对 stage_key 建外键（逐行外键检查拖慢分批写入；由 check 核对每个短键都已登记）。
-- 只有小表按去重版本（rd0001…）物化。

CREATE SCHEMA task8;

-- ------------------------------------------------------------------ 阶段键、运行与批次（R33–R36）

CREATE TABLE task8.stage_key (
    stage_key        text PRIMARY KEY CHECK (stage_key ~ '^[cfda][0-9a-f]{15}$'),
    extraction_id    text NOT NULL REFERENCES public.extraction,
    stage            text NOT NULL CHECK (stage IN ('review_clean', 'review_family', 'review_dedup',
                                                    'review_attribution')),
    full_key         text NOT NULL,          -- 输入版本与规则签名的完整串（RunInputs.stage_key）
    parent_stage_key text REFERENCES task8.stage_key,
    rule_version     text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (stage, full_key),
    CHECK ((stage = 'review_clean') = (parent_stage_key IS NULL))
);

CREATE TABLE task8.run (
    run_id            text PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('dedup', 'stage', 'evaluate')),   -- stage：只跑若干阶段（演练与调试）
    extraction_id     text NOT NULL REFERENCES public.extraction,
    t3_rule_version   text NOT NULL,
    conflict_version_id text NOT NULL,       -- 输入的任务 7 版本；存在与否由 check_inputs 核对（stage 运行不读它）
    rule_version      text NOT NULL,
    input_key         text NOT NULL,
    review_dedup_version_id text,            -- 本次运行产生的去重版本；reused 或未到物化步骤时为 NULL
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'succeeded', 'failed', 'reused', 'interrupted')),
    params            jsonb NOT NULL DEFAULT '{}',   -- batch_size、partitions
    counts            jsonb NOT NULL DEFAULT '{}',   -- 各阶段新写入的行数与统计
    dag_id            text,
    airflow_run_id    text,
    started_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at       timestamptz,
    failure_reason    text,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE INDEX run_input_idx ON task8.run (extraction_id, input_key, status);

-- 与该批数据在同一事务提交的批次审计（R36）。
CREATE TABLE task8.run_batch (
    run_id       text NOT NULL REFERENCES task8.run,
    stage        text NOT NULL,
    batch_no     integer NOT NULL CHECK (batch_no >= 1),
    first_entity text,                    -- 记录号区间、分区号或步骤名
    last_entity  text,
    rows_written bigint NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage, batch_no)
);

-- 某阶段键下已提交的批次单元：review_clean 为记录号区间（r<首>-<末>），review_family 为签名与重叠计数分区
-- （sig<p>/<N>、ovl<p>/<N>），review_dedup 为未映射记录（unmapped）与用户分区（p<p>/<N>）；
-- '__complete__' 与该阶段最后一个单元同事务写入，表示阶段完成。重跑只处理没有标记的单元（R36）。
CREATE TABLE task8.entity_stage (
    stage_key     text NOT NULL REFERENCES task8.stage_key,
    entity_id     text NOT NULL,
    stage         text NOT NULL,
    run_id        text NOT NULL REFERENCES task8.run,
    batch_no      integer NOT NULL,
    rows_written  bigint NOT NULL DEFAULT 0,
    committed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (stage_key, entity_id)
);

-- ------------------------------------------------------------------ 暂存层：清洗键（review_clean，R06–R09）

-- 每条有效记录一行（隔离记录不进入，R05）。正文与摘要只存 MD5，内容键 content_hash 是五项原值长度前缀串的 SHA-256；
-- 显示名与 helpfulness 存原值，供核验层次与 Reviewer 统计（R10、R14、R15）。
CREATE TABLE task8.review_clean (
    clean_stage_key    text NOT NULL,
    record_no          bigint NOT NULL,
    source_review_id   text NOT NULL,
    batch_no           integer NOT NULL,          -- 本阶段的批号（记录号区间）
    asin               text NOT NULL,
    reviewer_id        text,
    review_day         date,                      -- review_time_utc 的 UTC 日期（R08）
    score              numeric,
    helpfulness_raw    text,
    helpfulness_status text NOT NULL CHECK (helpfulness_status IN ('ok', 'zero_zero', 'numerator_exceeds_denominator',
                                                                   'negative', 'unparseable', 'missing')),
    helpful_votes      bigint,                    -- 任务 1 的规范值（异常时为 NULL）
    total_votes        bigint,
    profile_name_raw   text,
    summary_md5        bytea,
    text_md5           bytea,
    content_hash       bytea,                     -- 键不完整时为 NULL
    key_complete       boolean NOT NULL,
    asin_bucket        smallint NOT NULL CHECK (asin_bucket BETWEEN 0 AND 1023),
    reviewer_bucket    smallint NOT NULL CHECK (reviewer_bucket BETWEEN 0 AND 1023),
    PRIMARY KEY (clean_stage_key, record_no),
    CHECK ((helpfulness_status IN ('ok', 'zero_zero')) = (total_votes IS NOT NULL)),
    CHECK (key_complete = (content_hash IS NOT NULL)),
    CHECK (NOT key_complete OR (reviewer_id IS NOT NULL AND review_day IS NOT NULL AND score IS NOT NULL
                                AND text_md5 IS NOT NULL))
);

CREATE INDEX review_clean_reviewer_idx ON task8.review_clean (clean_stage_key, reviewer_bucket);
CREATE INDEX review_clean_asin_idx ON task8.review_clean (clean_stage_key, asin_bucket);

-- QualityIssue：helpfulness 异常与不完整记录（review_clean 阶段键）、哈希碰撞（review_dedup 阶段键）。
CREATE TABLE task8.review_issue (
    stage_key        text NOT NULL,
    issue_key        text NOT NULL,              -- code || ':' || record_no，重试不重复
    record_no        bigint NOT NULL,
    source_review_id text NOT NULL,
    code             text NOT NULL CHECK (code IN ('numerator_exceeds_denominator', 'negative_votes',
                                                   'unparseable_helpfulness', 'missing_helpfulness',
                                                   'incomplete_review', 'hash_collision')),
    evidence         text,
    detail           jsonb NOT NULL DEFAULT '{}',
    rule_version     text NOT NULL,
    PRIMARY KEY (stage_key, issue_key)
);

CREATE INDEX review_issue_code_idx ON task8.review_issue (stage_key, code);

-- ------------------------------------------------------------------ 集成层：评论家族（R12、R13）

CREATE TABLE task8.asin_signature (
    family_stage_key text NOT NULL,
    asin             text NOT NULL,
    signature        bytea NOT NULL,             -- 键完整记录的去重内容键集合的 SHA-256
    review_count     integer NOT NULL CHECK (review_count >= 1),    -- 评论集合大小（不同内容键数）
    partition_no     smallint NOT NULL,
    PRIMARY KEY (family_stage_key, asin)
);

CREATE INDEX asin_signature_sig_idx ON task8.asin_signature (family_stage_key, signature);

-- 用户分区内的签名组对共享内容键计数（内容键包含用户，同一内容键只在一个分区）；按分区写入以便恢复。
CREATE TABLE task8.signature_overlap (
    family_stage_key text NOT NULL,
    partition_no     smallint NOT NULL,
    sig_a            bytea NOT NULL,
    sig_b            bytea NOT NULL,
    shared           integer NOT NULL CHECK (shared >= 1),
    PRIMARY KEY (family_stage_key, partition_no, sig_a, sig_b),
    CHECK (sig_a < sig_b)
);

-- 家族与成员在 review_family 最后一个事务中一次写入（无半写入状态）。
CREATE TABLE task8.review_family (
    family_stage_key text NOT NULL,
    family_id        text NOT NULL CHECK (family_id ~ '^rf_[0-9a-f]{16}$'),
    min_asin         text NOT NULL,
    asin_count       integer NOT NULL CHECK (asin_count >= 1),
    signature_count  integer NOT NULL CHECK (signature_count >= 1),
    max_review_count integer NOT NULL CHECK (max_review_count >= 1),
    weak_support     boolean NOT NULL,
    basis            jsonb NOT NULL DEFAULT '[]',   -- 相连的签名组对：签名前 12 位、共享数与 Jaccard
    PRIMARY KEY (family_stage_key, family_id),
    CHECK (signature_count <= asin_count)
);

CREATE TABLE task8.family_member (
    family_stage_key text NOT NULL,
    asin             text NOT NULL,
    family_id        text NOT NULL,
    signature        bytea NOT NULL,
    review_count     integer NOT NULL,
    PRIMARY KEY (family_stage_key, asin)
);

CREATE INDEX family_member_family_idx ON task8.family_member (family_stage_key, family_id);

-- ------------------------------------------------------------------ 目标层：逻辑评论、ReviewMapping、Reviewer、候选（R10、R14–R25）

CREATE TABLE task8.logical_review (
    dedup_stage_key          text NOT NULL,
    logical_review_id        text NOT NULL CHECK (logical_review_id ~ '^lr_[0-9a-f]{20}$'),
    reviewer_id              text NOT NULL,
    review_day               date NOT NULL,
    score                    numeric NOT NULL,
    content_hash             bytea NOT NULL,
    text_md5                 bytea NOT NULL,
    family_id                text NOT NULL,
    collision_variant        smallint NOT NULL DEFAULT 0,   -- 哈希碰撞拆出的子组序号（0 = 无碰撞或第一个子组）
    representative_record_no bigint NOT NULL,
    copy_count               integer NOT NULL CHECK (copy_count >= 1),
    asin_count               integer NOT NULL CHECK (asin_count >= 1),
    helpful_votes            bigint,
    total_votes              bigint,
    helpfulness_record_no    bigint,
    partition_no             smallint NOT NULL,
    PRIMARY KEY (dedup_stage_key, logical_review_id),
    CHECK (asin_count <= copy_count),
    CHECK ((helpfulness_record_no IS NULL) = (total_votes IS NULL)),
    CHECK ((helpful_votes IS NULL) = (total_votes IS NULL))
);

CREATE INDEX logical_review_reviewer_idx ON task8.logical_review (dedup_stage_key, reviewer_id);

-- 每条原始记录一行（R03）：mapped 的记录有逻辑评论与层次，quarantined、incomplete 两者为 NULL（R05、R08、R19）。
CREATE TABLE task8.review_mapping (
    dedup_stage_key   text NOT NULL,
    record_no         bigint NOT NULL,
    source_review_id  text NOT NULL,
    asin              text,                       -- 隔离记录可能没有可识别的产品 ID
    logical_review_id text,
    mapping_status    text NOT NULL CHECK (mapping_status IN ('mapped', 'quarantined', 'incomplete')),
    match_level       text CHECK (match_level IN ('representative', 'D1_exact_same_asin', 'D2_same_asin_variant',
                                                  'D3_family_copy')),
    diff_fields       text[] NOT NULL DEFAULT '{}',
    partition_no      smallint NOT NULL,          -- 写入它的批次单元（-1 = 未映射记录的单元）
    rule_version      text NOT NULL,
    PRIMARY KEY (dedup_stage_key, record_no),
    CHECK ((mapping_status = 'mapped') = (logical_review_id IS NOT NULL)),
    CHECK ((mapping_status = 'mapped') = (match_level IS NOT NULL)),
    CHECK (diff_fields <@ ARRAY['helpfulness', 'profile_name']::text[]),
    CHECK (match_level NOT IN ('representative', 'D1_exact_same_asin') OR cardinality(diff_fields) = 0),
    CHECK (match_level <> 'D2_same_asin_variant' OR cardinality(diff_fields) >= 1)
);

CREATE INDEX review_mapping_logical_idx ON task8.review_mapping (dedup_stage_key, logical_review_id);
CREATE INDEX review_mapping_asin_idx ON task8.review_mapping (dedup_stage_key, asin);

CREATE TABLE task8.reviewer (
    dedup_stage_key  text NOT NULL,
    reviewer_id      text NOT NULL,
    profile_names    jsonb NOT NULL DEFAULT '{}',   -- 显示名原值 → 有效记录数（显示名缺失的记录不进此表，只计入 record_count）
    profile_variant_count integer NOT NULL,
    record_count     integer NOT NULL CHECK (record_count >= 1),    -- 有效记录数（含不完整记录）
    logical_count    integer NOT NULL CHECK (logical_count >= 0),
    first_day        date,
    last_day         date,
    partition_no     smallint NOT NULL,
    PRIMARY KEY (dedup_stage_key, reviewer_id)
);

-- 不合并的候选（R20–R22）：review_dedup 写 C1 与家族内 C2；review_attribution 写统计组内（跨家族）的 C2。
CREATE TABLE task8.dup_candidate (
    stage_key          text NOT NULL,
    candidate_id       text NOT NULL CHECK (candidate_id ~ '^dc_[0-9a-f]{16}$'),
    stage              text NOT NULL CHECK (stage IN ('review_dedup', 'review_attribution')),
    candidate_type     text NOT NULL CHECK (candidate_type IN ('cross_family_same_content', 'near_duplicate')),
    reviewer_id        text NOT NULL,
    logical_review_ids text[] NOT NULL CHECK (cardinality(logical_review_ids) >= 2),
    similarity         numeric(5, 4),
    basis              jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (stage_key, candidate_id),
    CHECK ((candidate_type = 'near_duplicate') = (similarity IS NOT NULL))
);

-- ------------------------------------------------------------------ 目标层：归属与统计（R26–R32，T8-04 使用）

CREATE TABLE task8.logical_review_work (
    attribution_stage_key text NOT NULL,
    logical_review_id     text NOT NULL,
    movie_id              text NOT NULL,
    via_asins             text[] NOT NULL,       -- 归属依据：该逻辑评论所在的电影单品 ASIN
    also_on_collection    boolean NOT NULL,      -- 同一逻辑评论也有副本在合集上
    PRIMARY KEY (attribution_stage_key, logical_review_id, movie_id)
);

CREATE TABLE task8.stat_group (
    attribution_stage_key text NOT NULL,
    stat_group_id         text NOT NULL CHECK (stat_group_id ~ '^sg_[0-9a-f]{16}$'),
    movie_count           integer NOT NULL CHECK (movie_count >= 1),
    basis                 jsonb NOT NULL DEFAULT '[]',   -- 连边：likely_duplicate_movie 或 shared_logical_review 及共享数
    PRIMARY KEY (attribution_stage_key, stat_group_id)
);

CREATE TABLE task8.stat_group_member (
    attribution_stage_key text NOT NULL,
    movie_id              text NOT NULL,
    stat_group_id         text NOT NULL,
    PRIMARY KEY (attribution_stage_key, movie_id)
);

-- ------------------------------------------------------------------ 去重版本（R33、R38）

CREATE TABLE task8.dedup_version (
    review_dedup_version_id text PRIMARY KEY CHECK (review_dedup_version_id ~ '^rd[0-9]{4}$'),
    extraction_id         text NOT NULL REFERENCES public.extraction,
    parent_version_id     text REFERENCES task8.dedup_version,
    run_id                text NOT NULL REFERENCES task8.run,
    input_key             text NOT NULL,
    t3_rule_version       text NOT NULL,
    conflict_version_id   text NOT NULL REFERENCES task7.conflict_version,
    rule_version          text NOT NULL,
    clean_stage_key       text NOT NULL REFERENCES task8.stage_key,
    family_stage_key      text NOT NULL REFERENCES task8.stage_key,
    dedup_stage_key       text NOT NULL REFERENCES task8.stage_key,
    attribution_stage_key text NOT NULL REFERENCES task8.stage_key,
    status                text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified', 'verified')),
    counts                jsonb NOT NULL DEFAULT '{}',
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    verified_at           timestamptz,
    UNIQUE (extraction_id, input_key),
    CHECK ((status = 'verified') = (verified_at IS NOT NULL))
);

ALTER TABLE task8.run ADD FOREIGN KEY (review_dedup_version_id) REFERENCES task8.dedup_version;

-- 统计表按去重版本物化（R26、R30–R32）。平均分的分母为有评分的记录或逻辑评论数。
CREATE TABLE task8.product_review_stats (
    review_dedup_version_id text NOT NULL REFERENCES task8.dedup_version,
    asin                 text NOT NULL,
    product_category     text,                   -- t3r2 分类与 cv0003 商品类别（未采集等）
    has_movie            boolean NOT NULL,
    raw_valid            integer NOT NULL,
    raw_quarantined      integer NOT NULL,
    raw_rated            integer NOT NULL,
    raw_avg_score        numeric,
    logical_reviews      integer NOT NULL,
    logical_rated        integer NOT NULL,
    logical_avg_score    numeric,
    PRIMARY KEY (review_dedup_version_id, asin)
);

CREATE TABLE task8.movie_review_stats (
    review_dedup_version_id text NOT NULL REFERENCES task8.dedup_version,
    movie_id             text NOT NULL,
    stat_group_id        text NOT NULL,
    raw_records          integer NOT NULL,       -- 去重前：电影单品上的原始有效记录
    raw_rated            integer NOT NULL,
    raw_avg_score        numeric,
    logical_reviews      integer NOT NULL,
    logical_rated        integer NOT NULL,
    logical_avg_score    numeric,
    shared_with_group    integer NOT NULL DEFAULT 0,
    near_dup_candidates  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (review_dedup_version_id, movie_id)
);

CREATE TABLE task8.stat_group_review_stats (
    review_dedup_version_id text NOT NULL REFERENCES task8.dedup_version,
    stat_group_id        text NOT NULL,
    movie_count          integer NOT NULL,
    raw_records          integer NOT NULL,
    raw_rated            integer NOT NULL,
    raw_avg_score        numeric,
    logical_reviews      integer NOT NULL,
    logical_rated        integer NOT NULL,
    logical_avg_score    numeric,
    near_dup_pairs       integer NOT NULL DEFAULT 0,
    sensitivity_rated    integer,                -- R31：每组近似评论只计一次时的分母与平均分
    sensitivity_avg_score numeric,
    PRIMARY KEY (review_dedup_version_id, stat_group_id)
);

-- ------------------------------------------------------------------ 评价（R45–R49，T8-06 使用）

-- 开发池：按 md5(reviewer_id) 排序前 20% 的用户，首次全量运行前冻结；评价样本排除开发池用户。
CREATE TABLE task8.dev_pool (
    extraction_id text NOT NULL REFERENCES public.extraction,
    reviewer_id   text NOT NULL,
    frozen_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (extraction_id, reviewer_id)
);

CREATE TABLE task8.eval_set (
    eval_set_id             text PRIMARY KEY,
    extraction_id           text NOT NULL REFERENCES public.extraction,
    review_dedup_version_id text NOT NULL REFERENCES task8.dedup_version,
    rule_version            text NOT NULL,
    strata                  jsonb NOT NULL,       -- 层 → {target, actual, filled}
    exported_file           text,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- 系统层次存在库里用于算指标，不导出（R47）。
CREATE TABLE task8.eval_item (
    eval_set_id     text NOT NULL REFERENCES task8.eval_set,
    item_id         text NOT NULL,
    stratum         text NOT NULL CHECK (stratum IN ('V1', 'V2', 'V3', 'V4', 'V5', 'V6')),
    item_key        text NOT NULL,
    left_ref        text NOT NULL,            -- 原始记录号、逻辑评论 ID 或 movie_id
    right_ref       text NOT NULL,
    system_decision text NOT NULL,            -- merged、candidate、separate、grouped
    PRIMARY KEY (eval_set_id, item_id)
);

CREATE TABLE task8.eval_label (
    eval_set_id        text NOT NULL,
    item_id            text NOT NULL,
    label              text NOT NULL CHECK (label IN ('same', 'different', 'unsure')),
    evidence           text,
    ai_doubt           boolean NOT NULL DEFAULT false,
    note               text,
    annotator          text NOT NULL,
    labeled_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, item_id),
    FOREIGN KEY (eval_set_id, item_id) REFERENCES task8.eval_item
);

INSERT INTO public.schema_migration (version) VALUES ('701');
