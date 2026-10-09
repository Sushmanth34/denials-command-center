import { api, loadSession } from "../api";
import { Loading, useLoad } from "../components";
import { money, pct } from "../format";
import type { PreventionRule } from "../types";

type Prebill = { prebill_reviewed: boolean; claims: number; denied_claims: number; preventable_denied_claims: number };

export function PreventionPage() {
  const { data, error } = useLoad(() => api<PreventionRule[]>("/prevention/rules"));
  const prebill = useLoad(() => (loadSession()?.role === "Manager" ? api<Prebill[]>("/analytics/prebill") : Promise.resolve([] as Prebill[])));
  const backtested = (data ?? []).filter((r) => !r.definition.backtest.estimated);
  const total = backtested.reduce((s, r) => s + Number(r.expected_allowed), 0);
  const caught = backtested.reduce((s, r) => s + r.denials_caught, 0);

  async function download() {
    const s = loadSession();
    const res = await fetch("/api/prevention/rules.json", { headers: { Authorization: `Bearer ${s?.token}` } });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "prebill_rules.json";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Prevention rules</h1>
          <p>
            Checks the pre-bill review team can run before a claim leaves the building. Each one was replayed against every claim billed
            Jan to Aug 2026 using the exported rule itself, so the numbers below are what the rule would really have caught.
          </p>
        </div>
        <button className="primary" onClick={download}>Download rules (JSON)</button>
      </div>

      {data && (
        <div className="panel">
          <div className="tally">
            <div><b>{caught}</b><span>denials these checks would have stopped</span></div>
            <div><b>{money(total)}</b><span>collectible dollars protected</span></div>
            <div><b>{backtested.reduce((s, r) => s + r.false_positives, 0)}</b><span>false alarms on paid claims</span></div>
          </div>
          {prebill.data && prebill.data.length > 0 && (
            <p className="small" style={{ marginTop: 14, color: "var(--ink-2)", maxWidth: "80ch" }}>
              Today's pre-bill review is not catching these:{" "}
              {prebill.data.map((p) => `${p.prebill_reviewed ? "reviewed" : "not reviewed"} claims were denied ${pct(p.denied_claims / p.claims)} of the time (${p.preventable_denied_claims} preventable)`).join("; ")}.
              The review needs these specific checks, not more of the same.
            </p>
          )}
        </div>
      )}

      <div className="panel">
        {!data ? <Loading error={error} /> : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Check</th><th>What it does</th><th>Owner</th><th className="num">Denials stopped</th><th className="num">Collectible $</th><th className="num">Claims flagged</th><th className="num">False alarms</th></tr>
              </thead>
              <tbody>
                {data.map((r) => (
                  <tr key={r.rule_id}>
                    <td style={{ minWidth: 200 }}>
                      <b>{r.name}</b>
                      <div className="muted">{r.rule_id}, {r.definition.action.toLowerCase()}</div>
                    </td>
                    <td style={{ maxWidth: 440 }}>
                      {r.definition.message}
                      {r.definition.policy_refs.length > 0 && <div className="muted">Policy: {r.definition.policy_refs.join(", ")}</div>}
                      {r.definition.note && <div className="muted">{r.definition.note}</div>}
                    </td>
                    <td>{r.definition.owner}</td>
                    <td className="num">{r.denials_caught}{r.definition.backtest.estimated ? "*" : ""}<div className="muted">of {r.definition.backtest.denials_targeted_total}</div></td>
                    <td className="num">{money(Number(r.expected_allowed))}</td>
                    <td className="num">{r.definition.backtest.estimated ? "–" : r.claims_flagged}</td>
                    <td className="num">{r.definition.backtest.estimated ? "–" : r.false_positives}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small muted" style={{ marginTop: 10 }}>
              * Needs an eligibility feed we don't have; the count is the denials it targets, not a replay. "Of N" is every denial with that reason;
              the gap on the SNF authorization check is the 7 pre-April denials, which were payer errors, not missing authorizations.
            </p>
          </div>
        )}
      </div>
    </>
  );
}
