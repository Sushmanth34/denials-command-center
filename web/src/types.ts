export type Recoverability = "RECOVERABLE" | "UNLIKELY" | "LOST" | "NO_LOSS" | "RESOLVED";

export interface WorkItem {
  claim_id: string;
  kind: "DENIAL" | "NO_RESPONSE";
  status: string;
  assignee: string | null;
  priority_score: number;
  priority_rank: number;
  priority_reason: string;
  expected_recovery: number;
  at_stake: number;
  deadline: string | null;
  days_left: number | null;
  version: number;
  payer_id: string;
  dos: string;
  rendering_provider: string;
  facility: string;
  claim_status: string;
  total_charge: number;
  patient: string;
  carc: string | null;
  root_cause: string | null;
  owning_team: string | null;
  action_type: string | null;
  next_action: string | null;
  recoverability: Recoverability | null;
  confidence_label: string | null;
  needs_review: boolean;
  open_denials: number;
  note_count: number;
}

export interface Summary {
  as_of: string;
  claims_billed: number;
  billed_total: number;
  paid_total: number;
  claims_adjudicated: number;
  claims_ever_denied: number;
  claims_no_response: number;
  open_denials: number;
  open_claims: number;
  open_billed: number;
  open_allowed: number;
  expected_recovery: number;
  due_14_days: number;
  due_14_days_allowed: number;
  recovered_allowed: number;
  recovered_count: number;
  preventable_open_allowed: number;
  preventable_open_count: number;
  needs_review: number;
  buckets: { recoverability: Recoverability; denials: number; billed: number; allowed: number; expected: number }[];
  exceptions_high: number;
  unapplied_cash: number;
  possible_overpayments: number;
  worklog_resolved_but_unpaid: number;
  open_claims_not_in_worklog: number;
  last_run: { run_id: number; finished_at: string; ai_mode: string; output_fingerprint: string } | null;
}

export interface BreakdownRow {
  key: string;
  open_denials: number;
  open_billed: number;
  open_allowed: number;
  recoverable_allowed: number;
  unlikely_allowed: number;
  lost_allowed: number;
  expected_recovery: number;
  recovered_allowed: number;
  preventable_open: number;
  denials_all_time: number;
  claims_billed?: number;
  claims_denied?: number;
}

export interface TimelineEvent {
  seq: number;
  event_date: string;
  event_type: string;
  summary: string;
  amount: number | null;
  source: string;
  details: Record<string, unknown>;
}

export interface DenialDetail {
  denial_id: string;
  line_no: number;
  cpt: string;
  carc: string;
  group_code: string;
  rarcs: string[];
  denied_amount: number;
  expected_allowed: number;
  denial_date: string;
  is_open: boolean;
  resolution: string | null;
  appeal_deadline: string;
  corrected_deadline: string;
  action_deadline: string | null;
  days_left: number | null;
  recoverability: Recoverability;
  recoverability_reason: string;
  recovery_probability: number;
  expected_recovery: number;
  root_cause: string;
  owning_team: string;
  preventable: boolean;
  action_type: string;
  next_action: string;
  policy_refs: string[];
  confidence: number;
  confidence_label: string;
  engine: string;
  needs_review: boolean;
  review_reasons: string[];
  review_decision: string | null;
  reviewer: string | null;
  review_comment: string | null;
  appeal_draft: string | null;
  rationale: string;
  evidence: {
    denial: { carc_description: string; rarcs: { code: string; description: string }[]; previously_paid_then_recouped: boolean };
    checks: Record<string, unknown>;
    worklog: { row: number; note: string; note_flagged: boolean; status: string | null; owner: string | null }[];
  };
}

export interface AuditEntry {
  audit_id: number;
  at: string;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  reason: string | null;
}

export interface ClaimDetail {
  claim: {
    claim_id: string;
    patient_first: string;
    patient_last: string;
    patient_dob: string;
    member_id: string;
    payer_id: string;
    dos: string;
    submitted_date: string;
    rendering_provider: string;
    rendering_npi: string;
    facility: string;
    pos: string;
    coder_id: string;
    prebill_reviewed: boolean;
    auth_number: string | null;
    dx_codes: string[];
    total_charge: number;
    payer_paid: number;
    patient_resp: number;
    contractual_adj: number;
    denied_open: number;
    claim_status: string;
  };
  payer: { name: string; timely_filing_days: number; appeal_window_days: number; corrected_window_days: number };
  lines: { line_no: number; cpt: string; modifier: string | null; charge: number; payer_paid: number; line_status: string }[];
  events: TimelineEvent[];
  denials: DenialDetail[];
  work_item: { status: string; assignee: string | null; version: number; priority_reason: string; deadline: string | null; days_left: number | null; kind: string } | null;
  notes: { note_id: number; author: string; text: string; source: string; created_at: string }[];
  audit: AuditEntry[];
  exceptions: { kind: string; severity: string; message: string }[];
  policies: { section_id: string; title: string; text: string }[];
}

export interface User {
  username: string;
  display_name: string;
  role: string;
  open_items: number;
  open_expected: number;
  due_14_days: number;
}

export interface ReviewItem {
  denial_id: string;
  claim_id: string;
  carc: string;
  payer_id: string;
  denied_amount: number;
  expected_allowed: number;
  root_cause: string;
  owning_team: string;
  preventable: boolean;
  action_type: string;
  confidence: number;
  confidence_label: string;
  engine: string;
  review_reasons: string[];
  days_left: number | null;
  assignee: string | null;
}

export interface PreventionRule {
  rule_id: string;
  name: string;
  denials_caught: number;
  denied_amount: number;
  expected_allowed: number;
  claims_flagged: number;
  false_positives: number;
  definition: {
    action: string;
    message: string;
    owner: string;
    policy_refs: string[];
    note?: string;
    backtest: { precision: number | null; estimated: boolean; denials_targeted_total: number; claims_awaiting_payer_response?: number };
  };
}

export interface ExceptionRow {
  exception_id: string;
  kind: string;
  severity: "HIGH" | "MEDIUM" | "LOW";
  source: string;
  reference: string;
  claim_id: string | null;
  amount: number | null;
  message: string;
}
