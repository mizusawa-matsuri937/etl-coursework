-- 任务 9：字段级数据血缘（schema task9）。
-- 幂等：由 etl_task9.run.apply_task9_schema 通过 public.schema_migration 判断是否已应用（须先有 001–701）。
-- 只引用 public.extraction，不在任务 1–8 的表上加列、约束或触发器（spec R03、R41）；上游表一律只读。
-- 四层结构（design“整体思路与分层”）：source_record（原始层）→ assertion（暂存层 extract、集成层 candidate/derived）
-- → derivation（派生边）→ field_selection 与 field_impact（目标层）。评论层不复制，由视图拼接任务 1、8（R10）。
-- 阶段键（R38）：sources → assertions → derivations → selections → impact，下游包含上游；大表里存短键
-- （阶段首字母 + 完整串 SHA-256 前 15 位），完整串与所属抽取登记在 task9.stage_key，大表不对它建外键（同任务 8）。
-- source_record_id 与 assertion_id 由上游定位确定（R08、R12），同一定位在各阶段键与血缘版本中相同。
-- 视图（v_review_lineage、v_product_lineage，T9-03）放在同目录的 801_task9_views.sql，每次应用都可重建
-- （etl_task9.run.apply_task9_schema 在 801 之后执行它，若文件存在）。

CREATE SCHEMA task9;

-- ------------------------------------------------------------------ 阶段键、运行、批次与质量问题（R36–R40）

CREATE TABLE task9.stage_key (
    stage_key        text PRIMARY KEY CHECK (stage_key ~ '^[sadli][0-9a-f]{15}$'),
    extraction_id    text NOT NULL REFERENCES public.extraction,
    stage            text NOT NULL CHECK (stage IN ('sources', 'assertions', 'derivations', 'selections', 'impact')),
    full_key         text NOT NULL,          -- 输入版本与规则签名的完整串（RunInputs.full_stage_key）
    parent_stage_key text REFERENCES task9.stage_key,
    rule_version     text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (stage, full_key),
    CHECK ((stage = 'sources') = (parent_stage_key IS NULL))
);

CREATE TABLE task9.run (
    run_id             text PRIMARY KEY,
    kind               text NOT NULL CHECK (kind IN ('lineage', 'stage', 'evaluate')),  -- stage：只跑若干阶段（演练与调试）
    extraction_id      text NOT NULL REFERENCES public.extraction,
    rule_version       text NOT NULL,
    input_key          text NOT NULL,
    inputs             jsonb NOT NULL,       -- R01 各上游版本、解析与规范化版本、IMDb 快照、上游阶段键
    lineage_version_id text,                 -- 本次运行产生的血缘版本；reused 或未到物化步骤时为 NULL
    status             text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'succeeded', 'failed', 'reused', 'interrupted')),
    params             jsonb NOT NULL DEFAULT '{}',   -- batch_size、snapshot_root、snapshot_workers
    counts             jsonb NOT NULL DEFAULT '{}',   -- 各阶段新写入的行数与统计
    dag_id             text,
    airflow_run_id     text,
    started_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at        timestamptz,
    failure_reason     text,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE INDEX run_input_idx ON task9.run (extraction_id, input_key, status);

-- 与该批数据在同一事务提交的批次审计（R38）。
CREATE TABLE task9.run_batch (
    run_id       text NOT NULL REFERENCES task9.run,
    stage        text NOT NULL,
    batch_no     integer NOT NULL CHECK (batch_no >= 1),
    first_entity text,
    last_entity  text,
    rows_written bigint NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage, batch_no)
);

-- 某阶段键下已提交的实体：entity_id 为“单元种类:实体”（如 page:<ASIN>、imdb_title:<tconst>、t3_field:<ASIN>）；
-- '__complete__' 与该阶段最后一批同事务写入，表示阶段完成。重跑只处理没有标记的实体（R38）。
CREATE TABLE task9.entity_stage (
    stage_key     text NOT NULL REFERENCES task9.stage_key,
    entity_id     text NOT NULL,
    stage         text NOT NULL,
    run_id        text NOT NULL REFERENCES task9.run,
    batch_no      integer NOT NULL,
    rows_written  bigint NOT NULL DEFAULT 0,
    committed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (stage_key, entity_id)
);

-- 质量问题（R07、R09、R21、R25）：同一阶段键下同一实体同一问题只记一次，重跑不重复。
CREATE TABLE task9.quality_issue (
    issue_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stage_key    text NOT NULL REFERENCES task9.stage_key,
    run_id       text REFERENCES task9.run,
    entity_type  text NOT NULL,             -- source_record、assertion、field_selection 等
    entity_id    text NOT NULL,
    issue_code   text NOT NULL CHECK (issue_code IN ('missing_acquired_at', 'snapshot_missing', 'snapshot_hash_mismatch',
                                                     'replay_mismatch', 'untraced_value', 'value_mismatch',
                                                     'dangling_edge', 'derivation_cycle')),
    detail       jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (stage_key, issue_code, entity_type, entity_id)
);

-- ------------------------------------------------------------------ 血缘版本（R36、R40）

CREATE TABLE task9.lineage_version (
    lineage_version_id    text PRIMARY KEY CHECK (lineage_version_id ~ '^lv[0-9]{4}$'),
    extraction_id         text NOT NULL REFERENCES public.extraction,
    parent_version_id     text REFERENCES task9.lineage_version,
    run_id                text NOT NULL REFERENCES task9.run,
    input_key             text NOT NULL,
    inputs                jsonb NOT NULL,   -- R01 各版本与上游阶段键（同 run.inputs）
    rule_version          text NOT NULL,
    sources_stage_key     text NOT NULL REFERENCES task9.stage_key,
    assertions_stage_key  text NOT NULL REFERENCES task9.stage_key,
    derivations_stage_key text NOT NULL REFERENCES task9.stage_key,
    selections_stage_key  text NOT NULL REFERENCES task9.stage_key,
    impact_stage_key      text NOT NULL REFERENCES task9.stage_key,
    status                text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified', 'verified')),
    counts                jsonb NOT NULL DEFAULT '{}',
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    verified_at           timestamptz,
    UNIQUE (extraction_id, input_key),
    CHECK ((status = 'verified') = (verified_at IS NOT NULL))
);

ALTER TABLE task9.run ADD FOREIGN KEY (lineage_version_id) REFERENCES task9.lineage_version;

-- ------------------------------------------------------------------ 原始层：源记录（R06–R09）

-- 每条原始证据一行：页面快照、IMDb 文件行、评论文件记录、人工修订记录。stage_key 为 sources 阶段键。
--  · locator 按 source_kind 保存可回到原文的定位（R06），locator_key 是其规范化串，source_record_id 由它导出（R08）。
--  · acquired_at 只保存，不参与任何决策；acquired_at_basis 注明来源列，取不到时为 NULL 并写 missing_acquired_at（R07）。
--  · evidence_status：页面快照构建时重算正文 SHA-256 的结果（R09）；其余种类为 not_checked（由 verify-sources 核验）。
CREATE TABLE task9.source_record (
    stage_key         text NOT NULL,
    source_record_id  text NOT NULL CHECK (source_record_id ~ '^sr_[0-9a-f]{16}$'),
    source_kind       text NOT NULL CHECK (source_kind IN ('page_snapshot', 'imdb_row', 'review_record',
                                                           'manual_revision')),
    locator           jsonb NOT NULL,
    locator_key       text NOT NULL,
    content_hash      text,
    hash_scope        text NOT NULL CHECK (hash_scope IN ('page_body_sha256', 'imdb_file_sha256', 'review_record_sha256',
                                                          'import_file_sha256')),
    acquired_at       timestamptz,
    acquired_at_basis text CHECK (acquired_at_basis IN ('task2.product_manifest.fetched_at',
                                                        'imdb.snapshot.downloaded_at',
                                                        'public.source_file.download_completed_at',
                                                        'human_decision.decided_at')),
    upstream_table    text NOT NULL,
    upstream_key      text NOT NULL,
    evidence_status   text NOT NULL CHECK (evidence_status IN ('ok', 'snapshot_missing', 'snapshot_hash_mismatch',
                                                               'not_checked')),
    run_id            text NOT NULL REFERENCES task9.run,
    PRIMARY KEY (stage_key, source_record_id),
    UNIQUE (stage_key, locator_key),
    CHECK ((acquired_at IS NULL) = (acquired_at_basis IS NULL)),
    CHECK (source_kind = 'page_snapshot' OR evidence_status = 'not_checked')
);

CREATE INDEX source_record_upstream_idx ON task9.source_record (stage_key, upstream_table, upstream_key);
CREATE INDEX source_record_kind_idx ON task9.source_record (stage_key, source_kind);

-- ------------------------------------------------------------------ 暂存层与集成层：字段断言（R11–R16）

-- 每条上游行一条断言（IMDb 作品行按列、类型串按成员拆开）。stage_key 为 assertions 阶段键。
--  · level：extract（直接来自源记录，source_record_id 必填）、candidate（某字段的候选，含未采用的，R15）、
--    derived（由其他断言派生，经 derivation 指向父节点，T9-02）。
--  · upstream_table、upstream_key 原样保存上游定位，assertion_id 由二者导出（R12）。
--  · upstream_status、upstream_reason：候选的上游状态与原因（adopted、rejected、conflict_pending、
--    superseded_by_specific，任务 6 的 selected、not_selected 等），原样保留（R15）。
CREATE TABLE task9.assertion (
    stage_key            text NOT NULL,
    assertion_id         text NOT NULL CHECK (assertion_id ~ '^as_[0-9a-f]{16}$'),
    level                text NOT NULL CHECK (level IN ('extract', 'candidate', 'derived')),
    entity_type          text NOT NULL CHECK (entity_type IN ('movie', 'edition', 'collection_member', 'person',
                                                              'mention', 'product_movie', 'review', 'imdb_title',
                                                              'imdb_name')),
    entity_id            text NOT NULL,
    field_name           text NOT NULL,
    member_key           text NOT NULL DEFAULT '',   -- 多值字段的成员键；标量为空串
    raw_value            text,
    normalized_value     text,
    source_record_id     text,                       -- extract 必填；指向同一血缘版本 sources 阶段键下的源记录
    field_locator        jsonb NOT NULL DEFAULT '{}',   -- 区域与序号、文件列、行号、断言 ID 等
    extract_rule_version text,                       -- 抽取或规范化规则版本（t3p1、t4n1、t5n1、t6r1、t7r3、imdb 快照等）
    rule_id              text,                       -- 上游规则编号（title.key、genre.tag 等），没有时为 NULL
    upstream_status      text,
    upstream_reason      text,
    upstream_table       text NOT NULL,
    upstream_key         text NOT NULL,
    upstream_run_id      text,
    confidence           numeric(4, 2) CHECK (confidence BETWEEN 0 AND 1),
    run_id               text NOT NULL REFERENCES task9.run,
    PRIMARY KEY (stage_key, assertion_id),
    CHECK (level <> 'extract' OR source_record_id IS NOT NULL)
);

CREATE INDEX assertion_entity_idx ON task9.assertion (stage_key, entity_type, entity_id, field_name);
CREATE INDEX assertion_source_idx ON task9.assertion (stage_key, source_record_id);
CREATE INDEX assertion_upstream_idx ON task9.assertion (stage_key, upstream_table, upstream_key);

-- ------------------------------------------------------------------ 集成层：派生边（R17–R22，T9-02 写入）

-- 子断言或字段选择 ← 父断言或父源记录。stage_key 为 derivations 阶段键。
CREATE TABLE task9.derivation (
    stage_key       text NOT NULL,
    child_kind      text NOT NULL CHECK (child_kind IN ('assertion', 'selection')),
    child_id        text NOT NULL,
    parent_kind     text NOT NULL CHECK (parent_kind IN ('assertion', 'source_record')),
    parent_id       text NOT NULL,
    operation       text NOT NULL CHECK (operation IN ('extract', 'normalize', 'vocab_map', 'split', 'align', 'link',
                                                       'merge', 'select', 'union', 'infer', 'aggregate', 'relocate')),
    ol_type         text NOT NULL CHECK (ol_type IN ('DIRECT', 'INDIRECT')),
    ol_subtype      text NOT NULL CHECK (ol_subtype IN ('IDENTITY', 'TRANSFORMATION', 'AGGREGATION', 'JOIN', 'FILTER',
                                                        'CONDITIONAL')),
    rule_id         text NOT NULL,
    rule_version    text NOT NULL,
    upstream_task   text NOT NULL,          -- task1 … task8、imdb、task9
    upstream_run_id text,
    occurred_at     timestamptz,
    confidence      numeric(4, 2) CHECK (confidence BETWEEN 0 AND 1),
    role            text,                   -- direct_dependency 等
    relocated       boolean NOT NULL DEFAULT false,
    detail          jsonb NOT NULL DEFAULT '{}',
    run_id          text NOT NULL REFERENCES task9.run,
    PRIMARY KEY (stage_key, child_kind, child_id, parent_kind, parent_id, operation),
    CHECK ((ol_type = 'DIRECT') = (ol_subtype IN ('IDENTITY', 'TRANSFORMATION', 'AGGREGATION')))
);

CREATE INDEX derivation_parent_idx ON task9.derivation (stage_key, parent_kind, parent_id);
CREATE INDEX derivation_rule_idx ON task9.derivation (stage_key, rule_id, rule_version);

-- 只读重放补定位的结果（R21，T9-02 写入）：被补定位的对象、补出的定位、与上游是否一致。
CREATE TABLE task9.relocation (
    stage_key      text NOT NULL,
    target_kind    text NOT NULL CHECK (target_kind IN ('genre_term', 'edition_feature', 'second_review')),
    target_id      text NOT NULL,           -- 作品与词面、商品与特征值、作品 ID
    located        jsonb NOT NULL DEFAULT '{}',   -- 区域、序号、任务 4 断言 ID 或源评论 ID
    consistent     boolean NOT NULL,
    upstream_value jsonb,
    replay_value   jsonb,
    run_id         text NOT NULL REFERENCES task9.run,
    PRIMARY KEY (stage_key, target_kind, target_id)
);

-- ------------------------------------------------------------------ 目标层：字段选择与影响闭包（R23–R27、R32，T9-03/T9-04 写入）

CREATE TABLE task9.field_selection (
    stage_key                text NOT NULL,
    selection_id             text NOT NULL CHECK (selection_id ~ '^fs_[0-9a-f]{16}$'),
    entity_type              text NOT NULL,
    entity_id                text NOT NULL,
    field_name               text NOT NULL,
    member_key               text NOT NULL DEFAULT '',
    value_text               text,           -- 最终值的文本形式（多值成员为成员值）
    value_int                integer,
    value_date               date,
    status                   text NOT NULL CHECK (status IN ('resolved', 'conflict_unresolved', 'missing', 'inferred')),
    missing_reason           text,
    selected_assertion_ids   text[] NOT NULL DEFAULT '{}',
    considered_assertion_ids text[] NOT NULL DEFAULT '{}',
    decision_rule            text,
    reason                   text,
    confidence               numeric(4, 2) CHECK (confidence BETWEEN 0 AND 1),
    decision_version         text NOT NULL,  -- cv0003、rv0001、map0002、pmap0001、t3r2 等
    trace_status             text NOT NULL CHECK (trace_status IN ('traced', 'relocated', 'manual', 'partial',
                                                                   'evidence_unverified', 'untraced')),
    target_ref               jsonb NOT NULL DEFAULT '{}',   -- 目标表与主键
    run_id                   text NOT NULL REFERENCES task9.run,
    PRIMARY KEY (stage_key, selection_id),
    UNIQUE (stage_key, entity_type, entity_id, field_name, member_key),
    CHECK (status <> 'missing' OR missing_reason IS NOT NULL)
);

CREATE INDEX field_selection_trace_idx ON task9.field_selection (stage_key, trace_status);

CREATE TABLE task9.field_impact (
    stage_key        text NOT NULL,
    source_record_id text NOT NULL,
    selection_id     text NOT NULL,
    depth            integer NOT NULL CHECK (depth >= 1),   -- 最短路径长度（边数）
    PRIMARY KEY (stage_key, source_record_id, selection_id)
);

CREATE INDEX field_impact_selection_idx ON task9.field_impact (stage_key, selection_id);

-- ------------------------------------------------------------------ 评价（R46–R49，T9-06 使用）

CREATE TABLE task9.eval_set (
    eval_set_id        text PRIMARY KEY,
    extraction_id      text NOT NULL REFERENCES public.extraction,
    lineage_version_id text NOT NULL REFERENCES task9.lineage_version,
    rule_version       text NOT NULL,
    strata             jsonb NOT NULL,       -- 层 → {target, actual, filled}
    exported_file      text,
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- 系统路径与证据摘录存哈希，标注时不显示系统状态列（R47、R49）。
CREATE TABLE task9.eval_item (
    eval_set_id     text NOT NULL REFERENCES task9.eval_set,
    item_id         text NOT NULL,
    stratum         text NOT NULL CHECK (stratum IN ('L1', 'L2', 'L3', 'L4', 'L5')),
    selection_key   text NOT NULL,           -- selection_id，或评论与商品归属的键
    entity_type     text NOT NULL,
    entity_id       text NOT NULL,
    field_name      text NOT NULL,
    member_key      text NOT NULL DEFAULT '',
    path_sha256     text NOT NULL,
    evidence_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, item_id)
);

CREATE TABLE task9.eval_label (
    eval_set_id        text NOT NULL,
    item_id            text NOT NULL,
    label              text NOT NULL CHECK (label IN ('correct', 'incorrect', 'unsure')),
    error_type         text CHECK (error_type IN ('wrong_source', 'wrong_value', 'missing_step', 'wrong_reason')),
    evidence           text,
    ai_doubt           boolean NOT NULL DEFAULT false,
    note               text,
    annotator          text NOT NULL,
    labeled_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, item_id),
    FOREIGN KEY (eval_set_id, item_id) REFERENCES task9.eval_item,
    CHECK ((label = 'incorrect') = (error_type IS NOT NULL))
);

INSERT INTO public.schema_migration (version) VALUES ('801');
