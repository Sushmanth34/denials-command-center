-- Denials Command Center schema (PostgreSQL 16)
-- Two kinds of tables:
--   * DERIVED tables are rebuilt from the input files on every pipeline run (deterministically).
--   * APP tables (work_item, work_note, app_user, audit_log, analysis_review) hold human work and
--     are never truncated by the pipeline.
-- Re-running is safe: CREATE ... IF NOT EXISTS everywhere.

CREATE SCHEMA IF NOT EXISTS dcc;
SET search_path TO dcc;

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS payer (
    payer_id               text PRIMARY KEY,
    name                   text NOT NULL,
    timely_filing_days     int  NOT NULL,
    appeal_window_days     int  NOT NULL,
    corrected_window_days  int  NOT NULL
);

CREATE TABLE IF NOT EXISTS code_reference (
    code_type   text NOT NULL,          -- CARC | RARC | GROUP
    code        text NOT NULL,
    description text NOT NULL,
    PRIMARY KEY (code_type, code)
);

CREATE TABLE IF NOT EXISTS policy_section (
    section_id   text PRIMARY KEY,      -- e.g. SMP_SNF-AUTH-2026#3
    document     text NOT NULL,         -- file name
    title        text NOT NULL,
    payer_ids    text[] NOT NULL,
    effective    date,
    number       text NOT NULL,
    text         text NOT NULL
);

CREATE TABLE IF NOT EXISTS source_file (
    file_name   text PRIMARY KEY,
    kind        text NOT NULL,
    sha256      text NOT NULL,
    size_bytes  bigint NOT NULL,
    status      text NOT NULL,          -- loaded | duplicate_content | partially_duplicate
    note        text
);

-- ---------------------------------------------------------------- claims (billing export)
CREATE TABLE IF NOT EXISTS claim (
    claim_id            text PRIMARY KEY,
    patient_first       text NOT NULL,
    patient_last        text NOT NULL,
    patient_dob         date NOT NULL,
    member_id           text NOT NULL,
    payer_id            text NOT NULL REFERENCES payer(payer_id),
    dos                 date NOT NULL,
    submitted_date      date NOT NULL,
    rendering_npi       text NOT NULL,
    rendering_provider  text NOT NULL,
    facility            text NOT NULL,
    pos                 text NOT NULL,
    coder_id            text NOT NULL,
    prebill_reviewed    boolean NOT NULL,
    auth_number         text,
    dx_codes            text[] NOT NULL,
    total_charge        numeric(12,2) NOT NULL,
    -- computed from remits
    payer_paid          numeric(12,2) NOT NULL DEFAULT 0,
    patient_resp        numeric(12,2) NOT NULL DEFAULT 0,
    contractual_adj     numeric(12,2) NOT NULL DEFAULT 0,
    denied_open         numeric(12,2) NOT NULL DEFAULT 0,
    claim_status        text NOT NULL,   -- PAID | PARTIALLY_DENIED | DENIED | NO_RESPONSE | RECOUPED_PENDING
    last_remit_date     date,
    payer_received_date date
);

CREATE TABLE IF NOT EXISTS claim_line (
    claim_id    text NOT NULL REFERENCES claim(claim_id),
    line_no     int  NOT NULL,
    cpt         text NOT NULL,
    modifier    text,
    units       int  NOT NULL,
    charge      numeric(12,2) NOT NULL,
    payer_paid  numeric(12,2) NOT NULL DEFAULT 0,
    patient_resp numeric(12,2) NOT NULL DEFAULT 0,
    line_status text NOT NULL,           -- PAID | DENIED | NO_RESPONSE | RECOUPED_PENDING
    PRIMARY KEY (claim_id, line_no)
);

-- ---------------------------------------------------------------- remittances (835)
CREATE TABLE IF NOT EXISTS remit_payment (
    trn                 text PRIMARY KEY,   -- TRN02 check/EFT trace number = payment identity
    payer_id            text NOT NULL,
    payment_amount      numeric(12,2) NOT NULL,
    payment_date        date NOT NULL,
    source_file         text NOT NULL,      -- first file the payment was loaded from
    isa_control         text NOT NULL,
    st_control          text NOT NULL,
    claim_paid_total    numeric(12,2) NOT NULL,
    plb_total           numeric(12,2) NOT NULL,
    balanced            boolean NOT NULL,
    also_seen_in        text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS remit_claim (
    remit_claim_key     text PRIMARY KEY,   -- trn|clp_seq
    trn                 text NOT NULL REFERENCES remit_payment(trn),
    clp_seq             int  NOT NULL,
    raw_claim_id        text NOT NULL,
    claim_id            text,               -- normalized; NULL when unmatched (see exception)
    status_code         text NOT NULL,      -- 1,2,3 processed, 4 denied, 22 reversal
    charge              numeric(12,2) NOT NULL,
    paid                numeric(12,2) NOT NULL,
    patient_resp        numeric(12,2) NOT NULL,
    payer_icn           text,
    freq_code           text,
    patient_name        text,
    rendering_npi       text,
    received_date       date,
    payment_date        date NOT NULL
);

CREATE TABLE IF NOT EXISTS remit_line (
    remit_claim_key     text NOT NULL REFERENCES remit_claim(remit_claim_key),
    svc_seq             int  NOT NULL,
    cpt                 text NOT NULL,
    modifiers           text[] NOT NULL,
    charge              numeric(12,2) NOT NULL,
    paid                numeric(12,2) NOT NULL,
    service_date        date,
    allowed_amount      numeric(12,2),
    adjustments         jsonb NOT NULL,      -- [{group, carc, amount}]
    rarcs               text[] NOT NULL,
    matched_line_no     int,
    PRIMARY KEY (remit_claim_key, svc_seq)
);

-- ---------------------------------------------------------------- timeline
CREATE TABLE IF NOT EXISTS claim_event (
    claim_id     text NOT NULL,
    seq          int  NOT NULL,
    event_date   date NOT NULL,
    event_type   text NOT NULL,   -- BILLED | RECEIVED_BY_PAYER | PAID | DENIED | PARTIAL | REVERSED | CORRECTED_PAID | WORKLOG
    summary      text NOT NULL,
    amount       numeric(12,2),
    source       text NOT NULL,
    details      jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (claim_id, seq)
);

-- ---------------------------------------------------------------- denials (current state per denied line)
CREATE TABLE IF NOT EXISTS denial (
    denial_id          text PRIMARY KEY,   -- claim_id:line_no
    claim_id           text NOT NULL REFERENCES claim(claim_id),
    line_no            int  NOT NULL,
    cpt                text NOT NULL,
    carc               text NOT NULL,
    group_code         text NOT NULL,
    rarcs              text[] NOT NULL,
    denied_amount      numeric(12,2) NOT NULL,   -- billed charge on the line (what the payer adjusted away)
    expected_allowed   numeric(12,2) NOT NULL,   -- what we would realistically be paid (payer + patient) if overturned
    denial_date        date NOT NULL,            -- 835 payment date of the denying remit
    trn                text NOT NULL,
    is_open            boolean NOT NULL,
    resolution         text,                     -- e.g. PAID_ON_CORRECTED_CLAIM
    appeal_deadline    date NOT NULL,
    corrected_deadline date NOT NULL,
    action_deadline    date,                     -- the deadline relevant to the recommended action
    days_left          int,
    recoverability     text NOT NULL,            -- RECOVERABLE | LOST | NO_LOSS | RESOLVED
    recoverability_reason text NOT NULL,
    recovery_probability numeric(4,2) NOT NULL,
    expected_recovery  numeric(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS denial_analysis (
    denial_id          text PRIMARY KEY REFERENCES denial(denial_id),
    engine             text NOT NULL,     -- rules | rules+ai | rules (ai_unavailable)
    root_cause         text NOT NULL,
    owning_team        text NOT NULL,
    preventable        boolean NOT NULL,
    action_type        text NOT NULL,     -- APPEAL | CORRECTED_CLAIM | REBILL_OTHER_PAYER | SEND_RECORDS | VERIFY_DUPLICATE | WRITE_OFF | ESCALATE_CREDENTIALING
    next_action        text NOT NULL,
    policy_refs        text[] NOT NULL,
    appeal_draft       text,
    confidence         numeric(4,2) NOT NULL,
    confidence_label   text NOT NULL,     -- HIGH | MEDIUM | LOW
    needs_review       boolean NOT NULL,
    review_reasons     text[] NOT NULL,
    rationale          text NOT NULL,
    evidence           jsonb NOT NULL,
    rules_result       jsonb NOT NULL,
    ai_result          jsonb,
    ai_model           text,
    prompt_version     text,
    input_hash         text NOT NULL
);

-- LLM responses cached by input hash -> re-runs never re-call the model and never change results.
CREATE TABLE IF NOT EXISTS ai_cache (
    input_hash    text PRIMARY KEY,
    model         text NOT NULL,
    prompt_version text NOT NULL,
    response      jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- worklog (manual Excel)
CREATE TABLE IF NOT EXISTS worklog_entry (
    row_no        int PRIMARY KEY,          -- Excel row number
    raw           jsonb NOT NULL,
    claim_id      text,
    logged_date   date,
    date_issue    text,
    payer_id      text,
    amount        numeric(12,2),
    note          text,
    note_flagged  boolean NOT NULL,         -- looks like an instruction / injection
    owner         text,
    status        text,                     -- normalized
    duplicate_of  int                       -- row_no of the first identical row
);

-- ---------------------------------------------------------------- exceptions & reconciliation
CREATE TABLE IF NOT EXISTS exception_item (
    exception_id  text PRIMARY KEY,          -- deterministic hash of kind + reference
    kind          text NOT NULL,
    severity      text NOT NULL,             -- HIGH | MEDIUM | LOW
    source        text NOT NULL,
    reference     text NOT NULL,
    claim_id      text,
    amount        numeric(12,2),
    message       text NOT NULL,
    details       jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS recon_payer (
    payer_id                text PRIMARY KEY,
    files_payment_total     numeric(12,2) NOT NULL,   -- sum of BPR02 across all files as received
    duplicate_payment_total numeric(12,2) NOT NULL,   -- BPR02 of payments already loaded (same TRN)
    unique_payment_total    numeric(12,2) NOT NULL,
    unmatched_claim_paid    numeric(12,2) NOT NULL,   -- paid on claims that are not ours
    matched_claim_paid      numeric(12,2) NOT NULL,
    system_claim_paid       numeric(12,2) NOT NULL,   -- sum of claim.payer_paid in our system
    difference              numeric(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS prevention_rule (
    rule_id        text PRIMARY KEY,
    name           text NOT NULL,
    definition     jsonb NOT NULL,
    denials_caught int NOT NULL,
    denied_amount  numeric(12,2) NOT NULL,
    expected_allowed numeric(12,2) NOT NULL,
    claims_flagged int NOT NULL,
    false_positives int NOT NULL,
    sort_order     int NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id        bigserial PRIMARY KEY,
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    input_fingerprint text NOT NULL,
    output_fingerprint text,
    ai_mode       text,
    summary       jsonb
);

-- ---------------------------------------------------------------- APP tables (never truncated)
CREATE TABLE IF NOT EXISTS app_user (
    username      text PRIMARY KEY,
    display_name  text NOT NULL,
    role          text NOT NULL CHECK (role IN ('Specialist','Manager')),
    password_hash text NOT NULL,
    active        boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS work_item (
    claim_id          text PRIMARY KEY,
    kind              text NOT NULL,           -- DENIAL | NO_RESPONSE
    status            text NOT NULL CHECK (status IN ('Open','In Progress','Pending Payer','Resolved','Written Off')),
    assignee          text REFERENCES app_user(username),
    -- system-maintained (refreshed every pipeline run)
    priority_score    numeric(12,2) NOT NULL,
    priority_rank     int NOT NULL,
    priority_reason   text NOT NULL,
    expected_recovery numeric(12,2) NOT NULL,
    at_stake          numeric(12,2) NOT NULL,
    deadline          date,
    days_left         int,
    system_state      text NOT NULL,           -- ACTIVE | CLOSED_BY_PAYMENT | NO_LONGER_DENIED
    version           int NOT NULL DEFAULT 1,  -- optimistic concurrency
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS work_note (
    note_id     bigserial PRIMARY KEY,
    claim_id    text NOT NULL REFERENCES work_item(claim_id),
    author      text NOT NULL,
    text        text NOT NULL,
    source      text NOT NULL DEFAULT 'app',   -- app | worklog_import
    source_ref  text UNIQUE,                   -- worklog row for idempotent import
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analysis_review (
    denial_id   text PRIMARY KEY,
    reviewer    text NOT NULL,
    decision    text NOT NULL CHECK (decision IN ('Approved','Overridden')),
    root_cause  text,
    owning_team text,
    preventable boolean,
    comment     text,
    reviewed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL,
    action      text NOT NULL,
    entity_type text NOT NULL,
    entity_id   text NOT NULL,
    before      jsonb,
    after       jsonb,
    reason      text
);
CREATE INDEX IF NOT EXISTS audit_entity_idx ON audit_log(entity_type, entity_id);

-- audit_log is append-only.
CREATE OR REPLACE FUNCTION dcc.audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION dcc.audit_log_immutable();

-- ---------------------------------------------------------------- read model used by the API
-- A human review overrides the engine's classification; everything downstream reads this view.
CREATE OR REPLACE VIEW dcc.v_denial AS
SELECT d.denial_id, d.claim_id, d.line_no, d.cpt, d.carc, d.group_code, d.rarcs, d.denied_amount,
       d.expected_allowed, d.denial_date, d.trn, d.is_open, d.resolution, d.appeal_deadline, d.corrected_deadline,
       d.action_deadline, d.days_left, d.recoverability, d.recoverability_reason, d.recovery_probability,
       d.expected_recovery,
       c.payer_id, c.rendering_provider, c.rendering_npi, c.coder_id, c.facility, c.dos, c.pos, c.prebill_reviewed,
       coalesce(r.root_cause, a.root_cause)   AS root_cause,
       coalesce(r.owning_team, a.owning_team) AS owning_team,
       coalesce(r.preventable, a.preventable) AS preventable,
       a.action_type, a.next_action, a.policy_refs, a.confidence, a.confidence_label, a.engine,
       (a.needs_review AND r.denial_id IS NULL) AS needs_review, a.review_reasons,
       r.decision AS review_decision, r.reviewer
FROM dcc.denial d
JOIN dcc.claim c USING (claim_id)
JOIN dcc.denial_analysis a USING (denial_id)
LEFT JOIN dcc.analysis_review r USING (denial_id);
