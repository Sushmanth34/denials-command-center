import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { BUCKET_COLOR, Loading, useLoad, useTooltip } from "../components";
import { BUCKET_LABEL, PAYERS, money, pct } from "../format";
import type { BreakdownRow, Summary, User } from "../types";

const BUCKET_COPY: Record<string, string> = {
  RECOVERABLE: "Inside the payer's window with a clear fix",
  UNLIKELY: "Window open, but enrollment is not retroactive",
  LOST: "Window closed or filed too late",
  NO_LOSS: "Duplicates of claims already paid",
};

const DIMENSIONS: [string, string][] = [
  ["root_cause", "Reason"],
  ["payer", "Payer"],
  ["provider", "Provider"],
  ["coder", "Coder"],
  ["facility", "Facility"],
  ["team", "Owning team"],
];

export function MondayPage() {
  const { data: s, error } = useLoad(() => api<Summary>("/analytics/summary"));
  if (!s) return <Loading error={error} />;
  const buckets = s.buckets ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Monday report</h1>
          <p>
            Data as of {s.as_of}: {s.claims_billed.toLocaleString()} claims billed Jan to Aug, {s.claims_ever_denied} denied at least once.
            Dollars are what the payer would actually pay if the denial is overturned (allowed amount), not billed charges.
          </p>
        </div>
      </div>

      <section className="panel" aria-labelledby="q1">
        <h2 id="q1">How much is stuck in denials, and how much can we still recover?</h2>
        <MoneyRibbon s={s} buckets={buckets} />
      </section>

      <section className="panel" aria-labelledby="dq">
        <h2 id="dq">Problems the spreadsheet was hiding</h2>
        <p className="lede">Found while reconciling the worklog against the payer files. Each needs an owner this week.</p>
        <div className="findings">
          <Finding n={String(s.worklog_resolved_but_unpaid)} text="denials marked done, resolved or closed in the worklog that the payer never paid. Reopened." to="/reconciliation?kind=WORKLOG_RESOLVED_BUT_UNPAID" />
          <Finding n={String(s.open_claims_not_in_worklog)} text="claims with an open denial that were never logged in the worklog. Now in the queue." to="/queue" />
          <Finding n={money(s.possible_overpayments)} text="paid twice on corrected claims without the original being reversed. Likely refund or recoupment." to="/reconciliation?kind=POSSIBLE_OVERPAYMENT" />
          <Finding n={money(s.unapplied_cash)} text="received for another client's claims (BHC-) inside our remittances. Route back." to="/reconciliation?kind=UNMATCHED_REMIT_CLAIM" />
          <Finding n={String(s.claims_no_response)} text={`claims with no payer response yet; 5 are 150+ days old and close to or past timely filing.`} to="/reconciliation?kind=NO_REMIT_RECEIVED" />
        </div>
      </section>

      <section className="panel" aria-labelledby="q2">
        <h2 id="q2">Why are we being denied, and which should never have left the building?</h2>
        <p className="lede">
          {s.preventable_open_count} of {s.open_denials} open denials ({money(s.preventable_open_allowed)}) would have been stopped by a check before
          billing. <Link to="/prevention">See which checks</Link>.
        </p>
        <Breakdown />
        <Trend />
      </section>

      <section className="panel" aria-labelledby="q3">
        <h2 id="q3">What should each person work on today?</h2>
        <p className="lede">
          Each specialist's queue is ranked by expected recovery and deadline. {s.due_14_days} recoverable denials ({money(s.due_14_days_allowed)})
          hit their deadline in the next 14 days. {s.needs_review} analyses are waiting for a human check.
        </p>
        <Team />
      </section>

      {s.last_run && (
        <p className="small muted" style={{ marginTop: 14 }}>
          Last data load #{s.last_run.run_id} at {new Date(s.last_run.finished_at).toLocaleString()}; AI {s.last_run.ai_mode}.
          Output fingerprint {s.last_run.output_fingerprint.slice(0, 12)} (identical on re-run of the same files).
        </p>
      )}
    </>
  );
}

function MoneyRibbon({ s, buckets }: { s: Summary; buckets: Summary["buckets"] }) {
  const { bind, node } = useTooltip();
  const total = buckets.reduce((a, b) => a + b.allowed, 0) || 1;
  const rec = buckets.find((b) => b.recoverability === "RECOVERABLE");
  return (
    <>
      <div className="ribbon-head" style={{ marginTop: 14 }}>
        <span className="big">{money(s.open_allowed)}</span>
        <span className="of">
          stuck in {s.open_denials} open denials on {s.open_claims} claims ({money(s.open_billed)} billed).
          About <b>{money(s.expected_recovery)}</b> is realistically collectible if the team works the queue in order.
        </span>
      </div>
      <div className="ribbon" role="img" aria-label="Open denial dollars by recoverability">
        {buckets.map((b) => (
          <div
            key={b.recoverability}
            className="seg"
            tabIndex={0}
            style={{ flexGrow: b.allowed / total, background: BUCKET_COLOR[b.recoverability] }}
            {...bind(<><b>{BUCKET_LABEL[b.recoverability]}</b>: {money(b.allowed)} allowed, {b.denials} denials, {money(b.billed)} billed{b.expected ? `; ${money(b.expected)} expected` : ""}</>)}
          />
        ))}
      </div>
      <div className="ribbon-legend">
        {buckets.map((b) => (
          <div className="item" key={b.recoverability} style={{ ["--c" as string]: BUCKET_COLOR[b.recoverability] }}>
            <span className="t">{BUCKET_LABEL[b.recoverability]}</span>
            <b>{money(b.allowed)}</b>
            <p>
              {b.denials} denials, {pct(b.allowed / total)} of the total. {BUCKET_COPY[b.recoverability]}.
              {b.recoverability === "RECOVERABLE" && rec ? ` Expected ${money(rec.expected)}.` : ""}
            </p>
          </div>
        ))}
      </div>
      <p className="small muted" style={{ marginTop: 14 }}>
        Already recovered this year: {money(s.recovered_allowed)} on {s.recovered_count} corrected claims. Payer paid {money(s.paid_total)} of {money(s.billed_total)} billed overall.
      </p>
      {node}
    </>
  );
}

function Finding({ n, text, to }: { n: string; text: string; to: string }) {
  return (
    <div className="finding">
      <b>{n}</b>
      <p>{text}</p>
      <Link to={to}>Show them</Link>
    </div>
  );
}

function Breakdown() {
  const [by, setBy] = useState("root_cause");
  const { data, error } = useLoad(() => api<BreakdownRow[]>(`/analytics/breakdown?by=${by}`), [by]);
  const { bind, node } = useTooltip();
  const rows = (data ?? []).filter((r) => r.open_denials > 0);
  const max = Math.max(1, ...rows.map((r) => r.open_allowed));
  const label = (k: string) => (by === "payer" ? PAYERS[k] ?? k : k);

  return (
    <div style={{ marginTop: 6 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <div className="seg-tabs" role="group" aria-label="Break down by">
          {DIMENSIONS.map(([k, v]) => (
            <button key={k} aria-pressed={by === k} onClick={() => setBy(k)}>{v}</button>
          ))}
        </div>
        <div className="row small">
          {(["RECOVERABLE", "UNLIKELY", "LOST"] as const).map((b) => (
            <span key={b} className="row" style={{ gap: 5 }}><span className="swatch" style={{ background: BUCKET_COLOR[b] }} />{BUCKET_LABEL[b]}</span>
          ))}
        </div>
      </div>
      {!data ? <Loading error={error} /> : (
        <div className="bars">
          {rows.map((r) => {
            const rate = r.claims_billed ? r.claims_denied! / r.claims_billed : null;
            const other = r.open_allowed - r.recoverable_allowed - r.unlikely_allowed - r.lost_allowed;
            const tip = (
              <>
                <b>{label(r.key)}</b><br />
                {r.open_denials} open denials, {money(r.open_allowed)} allowed ({money(r.open_billed)} billed)<br />
                Recoverable {money(r.recoverable_allowed)}, unlikely {money(r.unlikely_allowed)}, lost {money(r.lost_allowed)}
                {other > 0.5 ? `, no loss ${money(other)}` : ""}<br />
                {r.preventable_open} preventable{rate != null ? `; ${pct(rate)} of ${r.claims_billed} claims denied` : ""}
              </>
            );
            return (
              <div key={r.key} style={{ display: "contents" }}>
                <span className="label" title={label(r.key)}>{label(r.key)}</span>
                <span className="track" tabIndex={0} {...bind(tip)}>
                  {r.recoverable_allowed > 0 && <span style={{ width: `${(r.recoverable_allowed / max) * 100}%`, background: BUCKET_COLOR.RECOVERABLE }} />}
                  {r.unlikely_allowed > 0 && <span style={{ width: `${(r.unlikely_allowed / max) * 100}%`, background: BUCKET_COLOR.UNLIKELY }} />}
                  {r.lost_allowed > 0 && <span style={{ width: `${(r.lost_allowed / max) * 100}%`, background: BUCKET_COLOR.LOST }} />}
                  {other > 0.5 && <span style={{ width: `${(other / max) * 100}%`, background: BUCKET_COLOR.NO_LOSS }} />}
                </span>
                <span className="val">
                  {money(r.open_allowed)} <span className="muted">({r.open_denials}{rate != null ? `, ${pct(rate)} denial rate` : ""})</span>
                </span>
              </div>
            );
          })}
        </div>
      )}
      {node}
    </div>
  );
}

type TrendData = {
  by_denial_month: { month: string; denials: number; allowed: number; recoverable: number | null; lost_or_unlikely: number | null; recovered: number | null }[];
  by_dos_month: { month: string; claims: number; adjudicated: number; denied: number }[];
};

function Trend() {
  const { data, error } = useLoad(() => api<TrendData>("/analytics/trend"));
  const { bind, node } = useTooltip();
  if (!data) return <Loading error={error} />;
  const months = data.by_denial_month;
  const max = Math.max(1, ...months.map((m) => m.allowed));
  const H = 120;
  const monthName = (m: string) => new Date(m + "-01T00:00:00").toLocaleDateString("en-US", { month: "short" });

  return (
    <div style={{ marginTop: 26 }}>
      <h3>Denials by month received, and where that money stands now</h3>
      <p className="small muted" style={{ marginBottom: 10 }}>
        The older the denial, the more of it is already lost: windows close 60 to 180 days after the denial date.
      </p>
      <div style={{ display: "grid", gridTemplateColumns: `repeat(${months.length}, minmax(0, 1fr))`, gap: 10, alignItems: "end" }}>
        {months.map((m) => {
          const rec = m.recoverable ?? 0, lost = m.lost_or_unlikely ?? 0, done = m.recovered ?? 0;
          const other = Math.max(0, m.allowed - rec - lost - done);
          const seg = (v: number, c: string) => v > 0 && <span style={{ display: "block", height: (v / max) * H, background: c, marginTop: 2 }} />;
          const dos = data.by_dos_month.find((d) => d.month === m.month);
          return (
            <div key={m.month} tabIndex={0} {...bind(
              <><b>{monthName(m.month)} 2026</b>: {m.denials} denials, {money(m.allowed)} allowed<br />
                Recoverable {money(rec)}, lost or unlikely {money(lost)}, recovered {money(done)}{other > 0.5 ? `, no loss ${money(other)}` : ""}
                {dos ? <><br />Claims with DOS this month: {dos.claims}, {dos.denied} denied ({pct(dos.adjudicated ? dos.denied / dos.adjudicated : 0)})</> : null}</>
            )}>
              <div className="small num" style={{ textAlign: "center", color: "var(--ink-2)" }}>{money(m.allowed)}</div>
              <div style={{ display: "flex", flexDirection: "column-reverse", height: H + 10, justifyContent: "flex-start" }}>
                {seg(lost, BUCKET_COLOR.LOST)}
                {seg(other, BUCKET_COLOR.NO_LOSS)}
                {seg(done, "var(--line-strong)")}
                {seg(rec, BUCKET_COLOR.RECOVERABLE)}
              </div>
              <div className="small" style={{ textAlign: "center", borderTop: "1px solid var(--line-strong)", paddingTop: 4 }}>{monthName(m.month)}</div>
            </div>
          );
        })}
      </div>
      <div className="row small" style={{ marginTop: 10 }}>
        <span className="row" style={{ gap: 5 }}><span className="swatch" style={{ background: BUCKET_COLOR.RECOVERABLE }} />Still recoverable</span>
        <span className="row" style={{ gap: 5 }}><span className="swatch" style={{ background: BUCKET_COLOR.LOST }} />Lost or unlikely</span>
        <span className="row" style={{ gap: 5 }}><span className="swatch" style={{ background: "var(--line-strong)" }} />Recovered</span>
        <span className="row" style={{ gap: 5 }}><span className="swatch" style={{ background: BUCKET_COLOR.NO_LOSS }} />No loss</span>
      </div>
      {node}
    </div>
  );
}

function Team() {
  const { data, error } = useLoad(() => api<User[]>("/users"));
  if (!data) return <Loading error={error} />;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Specialist</th><th className="num">Open claims</th><th className="num">Due in 14 days</th><th className="num">Expected recovery</th><th></th></tr></thead>
        <tbody>
          {data.filter((u) => u.role === "Specialist").map((u) => (
            <tr key={u.username}>
              <td>{u.display_name}</td>
              <td className="num">{u.open_items}</td>
              <td className="num">{u.due_14_days}</td>
              <td className="num">{money(u.open_expected)}</td>
              <td><Link to={`/queue?assignee=${u.username}`}>Open queue</Link></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
