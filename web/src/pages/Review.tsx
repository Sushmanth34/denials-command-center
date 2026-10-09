import { Link } from "react-router-dom";
import { api } from "../api";
import { Due, Loading, useLoad } from "../components";
import { PAYERS, money } from "../format";
import type { ReviewItem } from "../types";
import { ReviewForm } from "./Claim";

export function ReviewPage({ onChange }: { onChange: (n: number) => void }) {
  const { data, error, reload } = useLoad(async () => {
    const r = await api<ReviewItem[]>("/review-queue");
    onChange(r.length);
    return r;
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Needs review</h1>
          <p>
            Analyses the system is not sure about: low confidence, the AI and the rules disagree, the AI's answer was rejected,
            or the claim's notes contain text aimed at manipulating AI tools. Approve or correct each one; your decision is audited and
            overrides the analysis everywhere.
          </p>
        </div>
      </div>
      <div className="panel">
        {!data ? <Loading error={error} /> : data.length === 0 ? (
          <p className="empty">Nothing waiting for review.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Claim</th><th>Payer</th><th>Current analysis</th><th>Why it needs a check</th><th className="num">At stake</th><th>Due</th><th>Decision</th></tr></thead>
              <tbody>
                {data.map((r) => (
                  <tr key={r.denial_id}>
                    <td><Link to={`/claims/${r.claim_id}`}>{r.claim_id.replace("GPP-2026-", "")}</Link><div className="muted">line {r.denial_id.split(":")[1]}, CARC {r.carc}</div></td>
                    <td>{PAYERS[r.payer_id] ?? r.payer_id}</td>
                    <td>
                      <b>{r.root_cause}</b>
                      <div className="muted">{r.owning_team}, {r.preventable ? "preventable" : "not preventable"}</div>
                      <div className="muted">Confidence {Number(r.confidence).toFixed(2)} ({r.engine})</div>
                    </td>
                    <td style={{ maxWidth: 320 }}>{r.review_reasons.join(" ")}</td>
                    <td className="num">{money(r.expected_allowed)}</td>
                    <td><Due days={r.days_left} /></td>
                    <td style={{ minWidth: 260 }}><ReviewForm d={r} onDone={reload} /></td>
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
