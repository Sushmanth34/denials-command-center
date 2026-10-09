import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import { Bucket, Due, Loading, useLoad } from "../components";
import { useAuth } from "../main";
import { ACTION_LABEL, date, dateTime, moneyCents, pct } from "../format";
import type { AuditEntry, ClaimDetail, DenialDetail, User } from "../types";

const STATUSES = ["Open", "In Progress", "Pending Payer", "Resolved", "Written Off"];

export function ClaimPage() {
  const { claimId } = useParams();
  const { data, error, reload } = useLoad(() => api<ClaimDetail>(`/claims/${claimId}`), [claimId]);
  if (!data) return <Loading error={error} />;
  const c = data.claim;
  const openDenials = data.denials.filter((d) => d.is_open);
  const closedDenials = data.denials.filter((d) => !d.is_open);

  return (
    <>
      <p className="small" style={{ marginBottom: 10 }}><Link to="/">Back to queue</Link></p>
      <div className="panel">
        <div className="claim-head">
          <div>
            <div className="id">{c.claim_id}</div>
            <p className="muted">
              {c.patient_last}, {c.patient_first} (DOB {date(c.patient_dob)}), member {c.member_id}
            </p>
            <p style={{ marginTop: 6 }}>
              {data.payer.name}. Seen {date(c.dos)} by {c.rendering_provider} (NPI {c.rendering_npi}) at {c.facility}, POS {c.pos}.
            </p>
            <p className="muted small" style={{ marginTop: 4 }}>
              Coder {c.coder_id}. Pre-bill review: {c.prebill_reviewed ? "yes" : "no"}. Diagnoses {c.dx_codes.join(", ")}.
              {c.auth_number ? ` Auth ${c.auth_number}.` : " No auth number."}
            </p>
          </div>
          <div className="money-strip">
            <div><span>Billed</span><b>{moneyCents(c.total_charge)}</b></div>
            <div><span>Paid by payer</span><b>{moneyCents(c.payer_paid)}</b></div>
            <div><span>Patient owes</span><b>{moneyCents(c.patient_resp)}</b></div>
            <div><span>Open denied</span><b style={{ color: c.denied_open > 0 ? "var(--lost-ink)" : undefined }}>{moneyCents(c.denied_open)}</b></div>
          </div>
        </div>
        {data.exceptions.length > 0 && (
          <div style={{ marginTop: 12 }}>
            {data.exceptions.map((e, i) => (
              <div className="flag" key={i}><b>{e.kind.replace(/_/g, " ").toLowerCase()}</b>: {e.message}</div>
            ))}
          </div>
        )}
      </div>

      <div className="grid-2" style={{ marginTop: 18 }}>
        <div className="stack">
          {openDenials.length === 0 && data.work_item?.kind === "NO_RESPONSE" && (
            <div className="panel">
              <h2>No response from the payer</h2>
              <p className="lede" style={{ marginTop: 6 }}>{data.work_item.priority_reason}</p>
              <p className="small">Call {data.payer.name} or check the portal for claim status. If they have no record, resubmit with the clearinghouse acceptance report as proof of timely filing ({data.payer.timely_filing_days} days from DOS).</p>
            </div>
          )}
          {openDenials.map((d) => <DenialCard key={d.denial_id} d={d} policies={data.policies} onChange={reload} assignee={data.work_item?.assignee ?? null} />)}
          {closedDenials.length > 0 && (
            <div className="panel">
              <h2>Earlier denials, since paid</h2>
              {closedDenials.map((d) => (
                <p key={d.denial_id} className="small" style={{ marginTop: 8 }}>
                  Line {d.line_no} ({d.cpt}) denied CARC {d.carc} on {date(d.denial_date)}: {d.root_cause}. Recovered by {d.resolution?.replace(/_/g, " ").toLowerCase()}.
                </p>
              ))}
            </div>
          )}
          <div className="panel">
            <h2>Timeline</h2>
            <p className="lede">Everything we know about this claim, from billing to the latest remittance, in date order.</p>
            <ol className="timeline">
              {data.events.map((e) => (
                <li key={e.seq} className={e.event_type}>
                  <div className="when">{date(e.event_date)}</div>
                  <div className="what">{e.summary}{e.amount != null && e.event_type !== "WORKLOG" ? `, ${moneyCents(e.amount)}` : ""}</div>
                  {e.event_type === "WORKLOG" && typeof e.details.note === "string" && (
                    <div className="small" style={{ color: "var(--ink-2)" }}>
                      Note: {e.details.flagged ? <em>withheld (flagged as an instruction aimed at AI tools)</em> : e.details.note}
                    </div>
                  )}
                  <div className="src">{e.source}</div>
                </li>
              ))}
            </ol>
          </div>
          <div className="panel">
            <h2>Lines</h2>
            <div className="table-wrap" style={{ marginTop: 8 }}>
              <table>
                <thead><tr><th>Line</th><th>CPT</th><th>Modifier</th><th className="num">Charge</th><th className="num">Paid</th><th>Status</th></tr></thead>
                <tbody>
                  {data.lines.map((l) => (
                    <tr key={l.line_no}><td>{l.line_no}</td><td>{l.cpt}</td><td>{l.modifier ?? "–"}</td><td className="num">{moneyCents(l.charge)}</td><td className="num">{moneyCents(l.payer_paid)}</td><td>{l.line_status.replace("_", " ").toLowerCase()}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className="stack">
          {data.work_item && <WorkPanel claimId={c.claim_id} data={data} onChange={reload} />}
          <Notes claimId={c.claim_id} notes={data.notes} canAdd={!!data.work_item} onChange={reload} />
          <AuditTrail entries={data.audit} />
        </div>
      </div>
    </>
  );
}

function DenialCard({ d, policies, onChange, assignee }: { d: DenialDetail; policies: ClaimDetail["policies"]; onChange: () => void; assignee: string | null }) {
  const { session } = useAuth();
  const [copied, setCopied] = useState(false);
  const checks = d.evidence.checks;
  const shownChecks: [string, unknown][] = Object.entries(checks).filter(([, v]) => v !== null && !(Array.isArray(v) && v.length === 0));
  const canReview = session!.role === "Manager" || session!.username === assignee;

  return (
    <div className="panel">
      <div className="top denial-card" style={{ border: 0, padding: 0 }}>
        <div>
          <h2>Line {d.line_no}, CPT {d.cpt}: {d.root_cause}</h2>
          <p className="small muted" style={{ marginTop: 4 }}>
            <span className="code">CARC {d.carc}</span> {d.evidence.denial.carc_description}
            {d.evidence.denial.rarcs.map((r) => <span key={r.code}> RARC <b>{r.code}</b> {r.description}</span>)}
          </p>
        </div>
        <Bucket value={d.recoverability} />
      </div>

      <div className="next">
        <b>Next step: {ACTION_LABEL[d.action_type] ?? d.action_type}{d.action_deadline ? `, by ${date(d.action_deadline)}` : ""}</b>
        {d.next_action}
      </div>

      <dl className="kv">
        <dt>Owner</dt><dd>{d.owning_team}</dd>
        <dt>Preventable before billing</dt><dd>{d.preventable ? "Yes" : "No"}</dd>
        <dt>Denied</dt><dd>{date(d.denial_date)}, {moneyCents(d.denied_amount)} billed, about {moneyCents(d.expected_allowed)} collectible</dd>
        <dt>Deadline</dt><dd><Due days={d.days_left} /> {d.recoverability_reason}</dd>
        <dt>Expected recovery</dt><dd>{moneyCents(d.expected_recovery)} ({pct(Number(d.recovery_probability))} likelihood)</dd>
        <dt>Confidence</dt><dd>{d.confidence_label.toLowerCase()} ({Number(d.confidence).toFixed(2)}), {d.engine}</dd>
        {d.review_decision && <><dt>Reviewed</dt><dd>{d.review_decision} by {d.reviewer}{d.review_comment ? `: ${d.review_comment}` : ""}</dd></>}
      </dl>
      <p className="small" style={{ marginTop: 10, color: "var(--ink-2)" }}>Why: {d.rationale}</p>

      {d.needs_review && (
        <div className="flag">
          <b>Needs a human check.</b> {d.review_reasons.join(" ")}
          {canReview && <ReviewForm d={d} onDone={onChange} />}
        </div>
      )}

      {d.policy_refs.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <h3>Payer policy this relies on</h3>
          {policies.filter((p) => d.policy_refs.includes(p.section_id)).map((p) => (
            <div className="policy" key={p.section_id}><b>{p.section_id}</b>: "{p.text}"</div>
          ))}
        </div>
      )}

      {d.appeal_draft && (
        <details style={{ marginTop: 10 }}>
          <summary>Draft {d.action_type === "CORRECTED_CLAIM" ? "corrected-claim note" : "appeal letter"}</summary>
          <pre className="draft">{d.appeal_draft}</pre>
          <button style={{ marginTop: 8 }} onClick={() => { navigator.clipboard?.writeText(d.appeal_draft!); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>
            {copied ? "Copied" : "Copy draft"}
          </button>
        </details>
      )}

      <details style={{ marginTop: 6 }}>
        <summary>Evidence checked ({shownChecks.length} facts)</summary>
        <dl className="checks">
          {shownChecks.map(([k, v]) => (
            <FragmentKV key={k} k={k} v={v} />
          ))}
        </dl>
      </details>
    </div>
  );
}

function FragmentKV({ k, v }: { k: string; v: unknown }) {
  const text = typeof v === "boolean" ? (v ? "yes" : "no") : Array.isArray(v) ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join("; ") : String(v);
  return (<><dt>{k.replace(/_/g, " ")}</dt><dd>{text}</dd></>);
}

const ROOT_CAUSES = ["Payer error", "Authorization", "Eligibility", "Credentialing", "Medical necessity", "Coding - diagnosis", "Coding - modifier", "Coding - frequency", "Billing - timely filing", "Billing - duplicate", "Other"];
const TEAMS = ["Denials (appeal)", "Front desk / Authorization", "Front desk / Eligibility", "Credentialing", "Coding / Clinical", "Coding", "Billing"];

export function ReviewForm({ d, onDone }: { d: { denial_id: string; root_cause: string; owning_team: string; preventable: boolean }; onDone: () => void }) {
  const [mode, setMode] = useState<"approve" | "override">("approve");
  const [rc, setRc] = useState(d.root_cause);
  const [team, setTeam] = useState(d.owning_team);
  const [prev, setPrev] = useState(d.preventable);
  const [comment, setComment] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await api(`/analyses/${d.denial_id}/review`, {
        method: "POST",
        json: mode === "approve" ? { decision: "Approved", comment } : { decision: "Overridden", root_cause: rc, owning_team: team, preventable: prev, comment },
      });
      onDone();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }
  return (
    <form onSubmit={submit} style={{ marginTop: 10 }}>
      <div className="row">
        <label className="row" style={{ margin: 0, fontWeight: 500 }}><input type="radio" checked={mode === "approve"} onChange={() => setMode("approve")} /> Analysis is right</label>
        <label className="row" style={{ margin: 0, fontWeight: 500 }}><input type="radio" checked={mode === "override"} onChange={() => setMode("override")} /> Correct it</label>
      </div>
      {mode === "override" && (
        <div className="row" style={{ marginTop: 8 }}>
          <select aria-label="Root cause" value={rc} onChange={(e) => setRc(e.target.value)} style={{ width: "auto" }}>{ROOT_CAUSES.map((x) => <option key={x}>{x}</option>)}</select>
          <select aria-label="Owning team" value={team} onChange={(e) => setTeam(e.target.value)} style={{ width: "auto" }}>{TEAMS.map((x) => <option key={x}>{x}</option>)}</select>
          <label className="row" style={{ margin: 0, fontWeight: 500 }}><input type="checkbox" checked={prev} onChange={(e) => setPrev(e.target.checked)} /> Preventable</label>
        </div>
      )}
      <input type="text" aria-label="Comment" placeholder="What you checked (optional)" value={comment} onChange={(e) => setComment(e.target.value)} style={{ marginTop: 8 }} />
      {err && <p className="error" style={{ marginTop: 8 }}>{err}</p>}
      <button className="primary" style={{ marginTop: 8 }} type="submit">{mode === "approve" ? "Approve analysis" : "Save correction"}</button>
    </form>
  );
}

function WorkPanel({ claimId, data, onChange }: { claimId: string; data: ClaimDetail; onChange: () => void }) {
  const { session } = useAuth();
  const w = data.work_item!;
  const isManager = session!.role === "Manager";
  const [status, setStatus] = useState(w.status);
  const [reason, setReason] = useState("");
  const [assignee, setAssignee] = useState(w.assignee ?? "");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const users = useLoad(() => (isManager ? api<User[]>("/users") : Promise.resolve([] as User[])), [isManager]);

  async function saveStatus(e: FormEvent) {
    e.preventDefault();
    try {
      await api(`/work-items/${claimId}`, { method: "PATCH", json: { status, version: w.version, reason } });
      setMsg({ ok: true, text: `Status saved as ${status}.` });
      setReason("");
      onChange();
    } catch (ex) {
      setMsg({ ok: false, text: ex instanceof ApiError ? ex.message : String(ex) });
    }
  }
  async function reassign() {
    try {
      await api(`/work-items/${claimId}/assign`, { method: "POST", json: { assignee, version: w.version } });
      setMsg({ ok: true, text: `Reassigned to ${assignee}.` });
      onChange();
    } catch (ex) {
      setMsg({ ok: false, text: ex instanceof ApiError ? ex.message : String(ex) });
    }
  }

  return (
    <div className="panel">
      <h2>Work</h2>
      <p className="small muted" style={{ marginBottom: 12 }}>{w.priority_reason}</p>
      <form onSubmit={saveStatus}>
        <div className="field">
          <label htmlFor="st">Status</label>
          <select id="st" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.filter((s) => isManager || s !== "Written Off" || w.status === s).map((s) => <option key={s}>{s}</option>)}
          </select>
          {!isManager && <p className="small muted" style={{ marginTop: 4 }}>Write-offs are approved by the manager.</p>}
        </div>
        <div className="field">
          <label htmlFor="rs">Reason (saved to the audit trail)</label>
          <input id="rs" type="text" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Appeal faxed, confirmation #" />
        </div>
        <button className="primary" style={{ marginTop: 10 }} type="submit" disabled={status === w.status}>Save status</button>
      </form>
      {isManager && (
        <div className="field" style={{ marginTop: 18 }}>
          <label htmlFor="as">Assigned to</label>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <select id="as" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
              {(users.data ?? []).filter((u) => u.role === "Specialist").map((u) => (
                <option key={u.username} value={u.username}>{u.display_name} ({u.open_items} open)</option>
              ))}
            </select>
            <button onClick={reassign} disabled={assignee === w.assignee}>Reassign</button>
          </div>
        </div>
      )}
      {!isManager && <p className="small muted" style={{ marginTop: 12 }}>Assigned to you.</p>}
      {msg && <p className={msg.ok ? "ok" : "error"} style={{ marginTop: 12 }}>{msg.text}</p>}
    </div>
  );
}

function Notes({ claimId, notes, canAdd, onChange }: { claimId: string; notes: ClaimDetail["notes"]; canAdd: boolean; onChange: () => void }) {
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  async function add(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await api(`/work-items/${claimId}/notes`, { method: "POST", json: { text } });
      setText("");
      onChange();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }
  return (
    <div className="panel">
      <h2>Notes</h2>
      {notes.length === 0 && <p className="muted small" style={{ marginTop: 6 }}>No notes yet.</p>}
      {notes.map((n) => (
        <div className="note" key={n.note_id}>
          <div className="meta">{n.author}, {dateTime(n.created_at)}{n.source === "worklog_import" ? " (from the Excel worklog)" : ""}</div>
          <div>{n.text}</div>
        </div>
      ))}
      {canAdd && (
        <form onSubmit={add} style={{ marginTop: 10 }}>
          <label htmlFor="nt">Add a note</label>
          <textarea id="nt" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} placeholder="Who you spoke to, reference numbers, what happens next" />
          {err && <p className="error">{err}</p>}
          <button type="submit" style={{ marginTop: 8 }} disabled={!text.trim()}>Add note</button>
        </form>
      )}
    </div>
  );
}

function AuditTrail({ entries }: { entries: AuditEntry[] }) {
  const show = (o: Record<string, unknown> | null) =>
    o ? Object.entries(o).filter(([k]) => !["claim_id", "priority_reason"].includes(k)).map(([k, v]) => `${k}: ${v}`).join(", ") : "–";
  return (
    <div className="panel">
      <h2>Audit trail</h2>
      <p className="lede">Every change: who, what, when, before and after. This log cannot be edited.</p>
      <div className="table-wrap">
        <table className="audit">
          <thead><tr><th>When</th><th>Who</th><th>What</th><th>Before</th><th>After</th></tr></thead>
          <tbody>
            {entries.slice().reverse().map((a) => (
              <tr key={a.audit_id}>
                <td>{dateTime(a.at)}</td>
                <td>{a.actor}</td>
                <td>{a.action.replace(/_/g, " ").toLowerCase()}{a.reason ? <div className="muted">{a.reason}</div> : null}</td>
                <td>{show(a.before)}</td>
                <td>{show(a.after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

