import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Loading, useLoad } from "../components";
import { PAYERS, date, moneyCents } from "../format";
import type { ExceptionRow } from "../types";

type Recon = {
  payers: { payer_id: string; files_payment_total: number; duplicate_payment_total: number; unique_payment_total: number; unmatched_claim_paid: number; matched_claim_paid: number; system_claim_paid: number; difference: number }[];
  payments: { trn: string; payer_id: string; payment_amount: number; payment_date: string; source_file: string; claim_paid_total: number; balanced: boolean; also_seen_in: string[] }[];
  files: { file_name: string; kind: string; status: string; note: string | null; sha256: string }[];
  claims: { total: number; by_status: Record<string, number> };
  exceptions: Record<string, number>;
  runs: { run_id: number; finished_at: string; output_fingerprint: string; input_fingerprint: string; ai_mode: string }[];
};

export function ReconciliationPage() {
  const [params, setParams] = useSearchParams();
  const kind = params.get("kind") ?? "";
  const { data, error } = useLoad(() => api<Recon>("/reconciliation"));
  const ex = useLoad(() => api<ExceptionRow[]>(`/exceptions${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`), [kind]);
  const [showPayments, setShowPayments] = useState(false);
  if (!data) return <Loading error={error} />;
  const sum = (k: keyof Recon["payers"][number]) => data.payers.reduce((s, p) => s + Number(p[k]), 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Reconciliation</h1>
          <p>
            Proof that the numbers hold: every dollar in the payer files is either applied to one of our claims or explained below.
            Anything we could not match or understand is listed as an exception, never dropped.
          </p>
        </div>
      </div>

      <div className="panel">
        <h2>Payer files vs this system</h2>
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead>
              <tr><th>Payer</th><th className="num">In the 835 files</th><th className="num">Less re-sent duplicates</th><th className="num">Unique payments</th><th className="num">Not our claims</th><th className="num">Applied to our claims</th><th className="num">Paid per claim records</th><th className="num">Difference</th></tr>
            </thead>
            <tbody>
              {data.payers.map((p) => (
                <tr key={p.payer_id}>
                  <td>{PAYERS[p.payer_id] ?? p.payer_id}</td>
                  <td className="num">{moneyCents(p.files_payment_total)}</td>
                  <td className="num">−{moneyCents(p.duplicate_payment_total)}</td>
                  <td className="num">{moneyCents(p.unique_payment_total)}</td>
                  <td className="num">{moneyCents(p.unmatched_claim_paid)}</td>
                  <td className="num">{moneyCents(p.matched_claim_paid)}</td>
                  <td className="num">{moneyCents(p.system_claim_paid)}</td>
                  <td className="num"><b style={{ color: Number(p.difference) === 0 ? "var(--recoverable-ink)" : "var(--lost-ink)" }}>{moneyCents(p.difference)}</b></td>
                </tr>
              ))}
              <tr>
                <td><b>Total</b></td>
                <td className="num"><b>{moneyCents(sum("files_payment_total"))}</b></td>
                <td className="num"><b>−{moneyCents(sum("duplicate_payment_total"))}</b></td>
                <td className="num"><b>{moneyCents(sum("unique_payment_total"))}</b></td>
                <td className="num"><b>{moneyCents(sum("unmatched_claim_paid"))}</b></td>
                <td className="num"><b>{moneyCents(sum("matched_claim_paid"))}</b></td>
                <td className="num"><b>{moneyCents(sum("system_claim_paid"))}</b></td>
                <td className="num"><b>{moneyCents(sum("difference"))}</b></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="small muted" style={{ marginTop: 10 }}>
          Payments are identified by their EFT trace number (TRN), so a file the clearinghouse sends twice is loaded once. Difference = unique payments − not ours − paid per claim records.
        </p>
      </div>

      <div className="grid-2" style={{ marginTop: 18 }}>
        <div className="panel">
          <h2>Files loaded</h2>
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table>
              <thead><tr><th>File</th><th>Result</th></tr></thead>
              <tbody>
                {data.files.map((f) => (
                  <tr key={f.file_name}><td>{f.file_name}<div className="muted small">sha256 {f.sha256.slice(0, 12)}</div></td><td>{f.status.replace(/_/g, " ")}{f.note ? <div className="muted">{f.note}</div> : null}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div className="panel">
          <h2>Claims and runs</h2>
          <dl className="kv" style={{ marginTop: 10 }}>
            <dt>Claims in billing export</dt><dd>{data.claims.total}</dd>
            {Object.entries(data.claims.by_status).map(([k, v]) => (<><dt key={k}>{k.replace(/_/g, " ").toLowerCase()}</dt><dd key={k + "v"}>{v}</dd></>))}
          </dl>
          <h3 style={{ marginTop: 16 }}>Recent loads</h3>
          <p className="small muted">Same input fingerprint must give the same output fingerprint.</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Run</th><th>Finished</th><th>Input</th><th>Output</th><th>AI</th></tr></thead>
              <tbody>
                {data.runs.map((r) => (
                  <tr key={r.run_id}><td>{r.run_id}</td><td>{new Date(r.finished_at).toLocaleString()}</td><td>{r.input_fingerprint.slice(0, 10)}</td><td>{r.output_fingerprint.slice(0, 10)}</td><td>{r.ai_mode}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <button style={{ marginTop: 12 }} onClick={() => setShowPayments(!showPayments)}>{showPayments ? "Hide" : "Show"} {data.payments.length} payments</button>
        </div>
      </div>

      {showPayments && (
        <div className="panel" style={{ marginTop: 18 }}>
          <h2>Payments (unique by trace number)</h2>
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table>
              <thead><tr><th>Date</th><th>Payer</th><th>TRN</th><th className="num">Amount</th><th className="num">Claims total</th><th>Balanced</th><th>Source</th></tr></thead>
              <tbody>
                {data.payments.map((p) => (
                  <tr key={p.trn}><td>{date(p.payment_date)}</td><td>{PAYERS[p.payer_id]}</td><td>{p.trn}</td><td className="num">{moneyCents(p.payment_amount)}</td><td className="num">{moneyCents(p.claim_paid_total)}</td><td>{p.balanced ? "yes" : "NO"}</td><td>{p.source_file}{p.also_seen_in.length ? <div className="muted">also in {p.also_seen_in.join(", ")}</div> : null}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="panel" style={{ marginTop: 18 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>Exceptions</h2>
          <select aria-label="Exception type" value={kind} onChange={(e) => setParams(e.target.value ? { kind: e.target.value } : {})} style={{ width: "auto" }}>
            <option value="">All types ({Object.values(data.exceptions).reduce((a, b) => a + b, 0)})</option>
            {Object.entries(data.exceptions).sort().map(([k, n]) => <option key={k} value={k}>{k.replace(/_/g, " ").toLowerCase()} ({n})</option>)}
          </select>
        </div>
        {!ex.data ? <Loading error={ex.error} /> : (
          <div className="table-wrap" style={{ marginTop: 10 }}>
            <table>
              <thead><tr><th>Severity</th><th>Type</th><th>Where</th><th>Claim</th><th className="num">Amount</th><th>What happened</th></tr></thead>
              <tbody>
                {ex.data.map((e) => (
                  <tr key={e.exception_id}>
                    <td><span className={`chip ${e.severity === "HIGH" ? "lost" : e.severity === "MEDIUM" ? "unlikely" : ""}`}>{e.severity.toLowerCase()}</span></td>
                    <td>{e.kind.replace(/_/g, " ").toLowerCase()}</td>
                    <td className="small">{e.reference.length > 40 ? e.reference.slice(0, 40) + "…" : e.reference}</td>
                    <td>{e.claim_id ? <Link to={`/claims/${e.claim_id}`}>{e.claim_id.replace("GPP-2026-", "")}</Link> : "–"}</td>
                    <td className="num">{e.amount != null ? moneyCents(e.amount) : "–"}</td>
                    <td style={{ maxWidth: 520 }}>{e.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
