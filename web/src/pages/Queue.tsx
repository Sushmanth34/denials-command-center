import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Bucket, Due, Loading, useLoad } from "../components";
import { useAuth } from "../main";
import { ACTION_LABEL, PAYERS, money } from "../format";
import type { User, WorkItem } from "../types";

const STATUSES = ["Open", "In Progress", "Pending Payer", "Resolved", "Written Off"];

export function QueuePage() {
  const { session } = useAuth();
  const isManager = session!.role === "Manager";
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const [assignee, setAssignee] = useState(search.get("assignee") ?? "");
  const [status, setStatus] = useState("");
  const [payer, setPayer] = useState("");
  const [showClosed, setShowClosed] = useState(false);
  const params = new URLSearchParams();
  if (assignee) params.set("assignee", assignee);
  if (status) params.set("status", status);
  if (showClosed) params.set("includeClosed", "true");
  const { data, error } = useLoad(() => api<WorkItem[]>(`/worklist?${params}`), [assignee, status, showClosed]);
  const users = useLoad(() => (isManager ? api<User[]>("/users") : Promise.resolve([] as User[])), [isManager]);

  const rows = useMemo(() => (data ?? []).filter((r) => !payer || r.payer_id === payer), [data, payer]);
  const live = rows.filter((r) => !["Resolved", "Written Off"].includes(r.status));
  const expected = live.reduce((s, r) => s + r.expected_recovery, 0);
  const urgent = live.filter((r) => r.days_left != null && r.days_left >= 0 && r.days_left <= 14).length;
  const closeOut = live.filter((r) => r.expected_recovery === 0).length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{isManager ? "Team queue" : "My queue"}</h1>
          <p>
            Ordered by money you can still collect, weighted by how soon the payer's deadline closes. Work from the top.
            Items with nothing left to collect sit at the bottom so they can be closed out.
          </p>
        </div>
        <div className="tally">
          <div><b>{live.length}</b><span>open claims</span></div>
          <div><b>{money(expected)}</b><span>expected to recover</span></div>
          <div><b>{urgent}</b><span>due within 14 days</span></div>
          <div><b>{closeOut}</b><span>to close out</span></div>
        </div>
      </div>

      <div className="panel">
        <div className="filters">
          {isManager && (
            <div className="field">
              <label htmlFor="f-a">Specialist</label>
              <select id="f-a" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
                <option value="">Everyone</option>
                {(users.data ?? []).filter((u) => u.role === "Specialist").map((u) => (
                  <option key={u.username} value={u.username}>{u.display_name}</option>
                ))}
              </select>
            </div>
          )}
          <div className="field">
            <label htmlFor="f-s">Status</label>
            <select id="f-s" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Any open status</option>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-p">Payer</label>
            <select id="f-p" value={payer} onChange={(e) => setPayer(e.target.value)}>
              <option value="">All payers</option>
              {Object.entries(PAYERS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </div>
          <label className="row" style={{ fontWeight: 500, marginBottom: 8 }}>
            <input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} /> Show resolved and written off
          </label>
        </div>

        {!data ? (
          <Loading error={error} />
        ) : rows.length === 0 ? (
          <p className="empty">Nothing in this queue. Change the filters to see other work.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Due</th>
                  <th>Claim</th>
                  <th>Payer</th>
                  <th>Why it was denied</th>
                  <th>Next step</th>
                  <th className="num">Expected</th>
                  <th>Status</th>
                  {isManager && <th>Specialist</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.claim_id}
                    className={`clickable ${["Resolved", "Written Off"].includes(r.status) ? "closed" : ""}`}
                    onClick={() => navigate(`/claims/${r.claim_id}`)}
                  >
                    <td><Due days={r.days_left} /></td>
                    <td>
                      <a href={`/claims/${r.claim_id}`} onClick={(e) => { e.preventDefault(); navigate(`/claims/${r.claim_id}`); }}>
                        {r.claim_id.replace("GPP-2026-", "")}
                      </a>
                      <div className="muted">{r.patient}</div>
                    </td>
                    <td>{PAYERS[r.payer_id] ?? r.payer_id}</td>
                    <td className="reason-cell">
                      {r.kind === "NO_RESPONSE" ? (
                        <><strong>No payer response</strong><span>Submitted, never adjudicated</span></>
                      ) : (
                        <>
                          <strong>{r.root_cause}</strong>
                          <span>CARC {r.carc}{r.open_denials > 1 ? ` and ${r.open_denials - 1} more line` : ""}, owner: {r.owning_team}</span>
                        </>
                      )}
                    </td>
                    <td className="next-cell">
                      {r.action_type && <b>{ACTION_LABEL[r.action_type] ?? r.action_type}. </b>}
                      <span className="clamp">{r.kind === "NO_RESPONSE" ? r.priority_reason : r.next_action}</span>
                      <div className="row" style={{ marginTop: 4 }}>
                        <Bucket value={r.recoverability} />
                        {r.needs_review && <span className="chip review">Needs review</span>}
                      </div>
                    </td>
                    <td className="num">
                      <b>{money(r.expected_recovery)}</b>
                      <div className="muted">of {money(r.at_stake)}</div>
                    </td>
                    <td>{r.status}</td>
                    {isManager && <td>{r.assignee ?? "Unassigned"}</td>}
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
