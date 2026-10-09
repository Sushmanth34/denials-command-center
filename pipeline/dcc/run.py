"""Pipeline entry point:  python -m dcc.run [--data DIR] [--ai off|auto]

Idempotent: same input files -> same derived tables (verified by an output fingerprint), no new audit
rows, no new AI calls (cached by input hash).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import db
from .engine import SPECIALISTS, analyze_denials, initial_assignments, plan_work_items
from .llm import PROMPT_VERSION, CachedAI, make_client
from .loaders import (TODAY, load_claims, load_codes, load_payers, load_policies, load_worklog, sha256_file)
from .prevention import backtest, rule_definitions
from .reconcile import ExceptionItem, claim_summary, money, output_fingerprint, reconcile

log = logging.getLogger("dcc")
ROOT = Path(__file__).resolve().parents[2]

USERS = [
    ("manager", "Practice Manager", "Manager"),
    ("anjali", "Anjali", "Specialist"),
    ("karan", "Karan", "Specialist"),
    ("priya", "Priya", "Specialist"),
    ("rahul", "Rahul", "Specialist"),
]


def build(data_dir: Path, ai: CachedAI) -> dict:
    payers = load_payers(data_dir / "payer_rules.csv")
    claims, claim_issues = load_claims(data_dir / "claims_export.csv")
    code_rows = load_codes(data_dir / "carc_rarc_reference.csv", data_dir / "claim_adjustment_group_codes.csv")
    codes = {(t, c): d for t, c, d in code_rows}
    policies = load_policies(data_dir / "payer_policies")
    remit_paths = sorted((data_dir / "remits").glob("*.835"))
    model = reconcile(claims, payers, remit_paths)
    exceptions: list[ExceptionItem] = list(model.exceptions)
    for i in claim_issues:
        exceptions.append(ExceptionItem(i["kind"], "MEDIUM", "claims_export.csv", i["ref"], i["message"],
                                        claim_id=i.get("claim_id")))

    first_denial = {}
    for d in model.denials:
        first_denial[d.claim_id] = min(first_denial.get(d.claim_id, d.denial_date), d.denial_date)
    worklog = load_worklog(data_dir / "denials_worklog.xlsx", first_denial)

    analyses = analyze_denials(model, codes, policies, worklog, ai)
    adict = {a.denial_id: a for a in analyses}
    dmap = {d.denial_id: d for d in model.denials}
    open_by_claim = defaultdict(list)
    for d in model.denials:
        if d.is_open:
            open_by_claim[d.claim_id].append(d)

    # ---------------- worklog reconciliation -> exceptions + initial work state
    worklog_exceptions(worklog, claims, open_by_claim, model, exceptions)
    plans = plan_work_items(model, analyses)
    latest_wl = {}
    for w in sorted((w for w in worklog if w.claim_id and w.duplicate_of is None),
                    key=lambda w: (w.logged_date or datetime.min.date(), w.row_no)):
        latest_wl[w.claim_id] = w
    owners = {cid: (w.owner or "").lower() for cid, w in latest_wl.items() if w.owner}
    assign = initial_assignments(plans, owners)
    initial = {}
    for p in plans:
        w = latest_wl.get(p.claim_id)
        status, reason = "Open", "New item from remittance data"
        if w and w.status:
            status = w.status
            reason = f"Imported from worklog row {w.row_no} ({w.raw.get('Status', '').strip()!r})"
            if status == "Resolved":
                status = "Open"
                reason = (f"Worklog row {w.row_no} says {w.raw.get('Status', '').strip()!r} but the latest remittance "
                          f"still shows the denial unpaid; reopened")
        src = "worklog owner" if owners.get(p.claim_id) in SPECIALISTS else "auto-balanced"
        initial[p.claim_id] = dict(status=status, assignee=assign[p.claim_id], reason=f"{reason}; assignee: {src}")

    # ---------------- prevention
    rules = rule_definitions(model)
    prevention = backtest(model, rules, adict)

    # ---------------- rows
    files = []
    for name, st in sorted(model.file_status.items()):
        files.append(dict(file_name=f"remits/{name}", kind="835", sha256=st["sha256"], size_bytes=st["size"],
                          status=st["status"], note=st["note"]))
    for rel in ["claims_export.csv", "denials_worklog.xlsx", "payer_rules.csv", "carc_rarc_reference.csv",
                "claim_adjustment_group_codes.csv"]:
        p = data_dir / rel
        files.append(dict(file_name=rel, kind=p.suffix.lstrip("."), sha256=sha256_file(p), size_bytes=p.stat().st_size,
                          status="loaded", note=None))
    for p in sorted((data_dir / "payer_policies").glob("*.md")):
        files.append(dict(file_name=f"payer_policies/{p.name}", kind="policy", sha256=sha256_file(p),
                          size_bytes=p.stat().st_size, status="loaded", note=None))

    claim_rows, line_rows = [], []
    for cid in sorted(claims):
        c = claims[cid]
        s = claim_summary(c, model.lines)
        claim_rows.append(dict(
            claim_id=cid, patient_first=c.patient_first, patient_last=c.patient_last, patient_dob=c.patient_dob,
            member_id=c.member_id, payer_id=c.payer_id, dos=c.dos, submitted_date=c.submitted_date,
            rendering_npi=c.rendering_npi, rendering_provider=c.rendering_provider, facility=c.facility, pos=c.pos,
            coder_id=c.coder_id, prebill_reviewed=c.prebill_reviewed, auth_number=c.auth_number or None,
            dx_codes=c.dx_codes, total_charge=c.total_charge, **s))
        for l in c.lines:
            ls = model.lines[(cid, l.line_no)]
            line_rows.append(dict(claim_id=cid, line_no=l.line_no, cpt=l.cpt, modifier=l.modifier or None,
                                  units=l.units, charge=l.charge, payer_paid=money(ls.payer_paid),
                                  patient_resp=money(ls.patient_resp), line_status=ls.status))

    pay_rows = []
    for key, lp in sorted(model.payments.items()):
        p = lp.payment
        pay_rows.append(dict(trn=p.trn, payer_id=p.payer_id, payment_amount=p.payment_amount, payment_date=p.payment_date,
                             source_file=lp.file_name, isa_control=p.isa_control, st_control=p.st_control,
                             claim_paid_total=money(lp.claim_paid_total), plb_total=p.plb_total, balanced=lp.balanced,
                             also_seen_in=lp.also_seen_in))

    events = []
    by_claim = defaultdict(list)
    for e in model.events:
        by_claim[e.claim_id].append(e)
    wl_by_claim = defaultdict(list)
    for w in worklog:
        if w.claim_id and w.claim_id in claims and w.duplicate_of is None:
            wl_by_claim[w.claim_id].append(w)
    for cid in sorted(by_claim):
        evs = sorted(by_claim[cid], key=lambda e: e.order)
        for w in wl_by_claim.get(cid, []):
            if w.logged_date:
                from .reconcile import TimelineEvent
                evs.append(TimelineEvent(cid, w.logged_date, "WORKLOG",
                                         f"Logged in team worklog ({w.owner or 'no owner'}, {w.status or 'no status'})"
                                         + (" - note flagged as possible injected instruction" if w.note_flagged else ""),
                                         w.amount, f"denials_worklog.xlsx row {w.row_no}",
                                         dict(note=w.note, flagged=w.note_flagged), order=(w.logged_date, 9, w.row_no)))
        evs.sort(key=lambda e: (e.order[0], e.order[1:]))
        for i, e in enumerate(evs, start=1):
            events.append(dict(claim_id=cid, seq=i, event_date=e.event_date, event_type=e.event_type, summary=e.summary,
                               amount=e.amount, source=e.source, details=e.details))

    denial_rows, analysis_rows = [], []
    for d in model.denials:
        a = adict[d.denial_id]
        denial_rows.append(dict(
            denial_id=d.denial_id, claim_id=d.claim_id, line_no=d.line_no, cpt=d.cpt, carc=d.carc,
            group_code=d.group_code, rarcs=d.rarcs, denied_amount=d.denied_amount, expected_allowed=d.expected_allowed,
            denial_date=d.denial_date, trn=d.trn, is_open=d.is_open, resolution=d.resolution,
            appeal_deadline=d.appeal_deadline, corrected_deadline=d.corrected_deadline,
            action_deadline=a.action_deadline, days_left=a.days_left, recoverability=a.recoverability,
            recoverability_reason=a.recoverability_reason, recovery_probability=Decimal(str(a.recovery_probability)),
            expected_recovery=a.expected_recovery))
        analysis_rows.append(dict(
            denial_id=d.denial_id, engine=a.engine, root_cause=a.root_cause, owning_team=a.owning_team,
            preventable=a.preventable, action_type=a.action_type, next_action=a.next_action, policy_refs=a.policy_refs,
            appeal_draft=a.appeal_draft, confidence=Decimal(str(a.confidence)), confidence_label=a.confidence_label,
            needs_review=a.needs_review, review_reasons=a.review_reasons, rationale=a.rationale, evidence=a.evidence,
            rules_result=a.rules_result, ai_result=a.ai_result, ai_model=a.ai_model,
            prompt_version=PROMPT_VERSION if a.ai_result else None, input_hash=a.input_hash))

    wl_rows = [dict(row_no=w.row_no, raw=w.raw, claim_id=w.claim_id if w.claim_id in claims else None,
                    logged_date=w.logged_date, date_issue=w.date_issue, payer_id=w.payer_id, amount=w.amount,
                    note=w.note, note_flagged=w.note_flagged, owner=w.owner, status=w.status,
                    duplicate_of=w.duplicate_of) for w in worklog]

    exc_rows, seen = [], set()
    for e in exceptions:
        if e.exception_id in seen:
            continue
        seen.add(e.exception_id)
        exc_rows.append(dict(exception_id=e.exception_id, kind=e.kind, severity=e.severity, source=e.source,
                             reference=e.reference, claim_id=e.claim_id, amount=e.amount, message=e.message,
                             details=e.details))
    exc_rows.sort(key=lambda r: r["exception_id"])

    prev_rows = []
    for r in prevention:
        bt = r["backtest"]
        defn = {k: v for k, v in r.items() if k != "sort_order"}
        prev_rows.append(dict(rule_id=r["rule_id"], name=r["name"], definition=defn,
                              denials_caught=bt["denials_caught"], denied_amount=Decimal(bt["denied_amount_caught"]),
                              expected_allowed=Decimal(bt["expected_allowed_caught"]),
                              claims_flagged=bt["claims_flagged"], false_positives=bt["false_positives"],
                              sort_order=r["sort_order"]))

    plan_rows = [dict(claim_id=p.claim_id, kind=p.kind, priority_score=p.priority_score, priority_rank=i,
                      priority_reason=p.priority_reason, expected_recovery=p.expected_recovery, at_stake=p.at_stake,
                      deadline=p.deadline, days_left=p.days_left, system_state="ACTIVE")
                 for i, p in enumerate(plans, start=1)]

    notes = []
    for w in worklog:
        if w.claim_id and w.duplicate_of is None and w.note:
            text = w.note if not w.note_flagged else (
                "[FLAGGED - looks like an instruction to an AI system; not acted on] " + w.note)
            notes.append(dict(claim_id=w.claim_id, author=(w.owner or "unknown").lower(), text=text,
                              source_ref=f"worklog:row{w.row_no}",
                              created_at=datetime.combine(w.logged_date or TODAY, datetime.min.time(), timezone.utc)))

    data = dict(
        source_file=files,
        payer=[dict(payer_id=p.payer_id, name=p.name, timely_filing_days=p.timely_filing_days,
                    appeal_window_days=p.appeal_window_days, corrected_window_days=p.corrected_window_days)
               for p in payers.values()],
        code_reference=[dict(code_type=t, code=c, description=d) for t, c, d in code_rows],
        policy_section=[dict(section_id=p.section_id, document=p.document, title=p.title, payer_ids=p.payer_ids,
                             effective=p.effective, number=p.number, text=p.text) for p in policies],
        claim=claim_rows, claim_line=line_rows, remit_payment=pay_rows, remit_claim=model.remit_claims,
        remit_line=model.remit_lines, claim_event=events, denial=denial_rows, denial_analysis=analysis_rows,
        worklog_entry=wl_rows, exception_item=exc_rows, recon_payer=model.recon, prevention_rule=prev_rows,
    )
    claim_paid = {r["claim_id"]: r["claim_status"] == "PAID" for r in claim_rows}
    summary = summarize(model, analyses, plans, exc_rows, worklog, dmap)
    return dict(data=data, plans=plan_rows, initial=initial, claim_paid=claim_paid, notes=notes,
                summary=summary, prevention=prevention)


def worklog_exceptions(worklog, claims, open_by_claim, model, exceptions):
    denied_ever = {d.claim_id for d in model.denials}
    for w in worklog:
        ref = f"denials_worklog.xlsx row {w.row_no}"
        if w.duplicate_of:
            exceptions.append(ExceptionItem("WORKLOG_DUPLICATE_ROW", "LOW", "denials_worklog.xlsx", ref,
                                            f"Exact duplicate of row {w.duplicate_of}; ignored", claim_id=w.claim_id))
            continue
        if w.note_flagged:
            exceptions.append(ExceptionItem(
                "WORKLOG_SUSPICIOUS_TEXT", "HIGH", "denials_worklog.xlsx", ref,
                "Note contains text addressed to an AI system (possible prompt injection). Not followed; withheld from "
                "the AI; analysis sent to human review.", claim_id=w.claim_id, details=dict(note=w.note[:300])))
        if w.claim_id is None or w.claim_id not in claims:
            exceptions.append(ExceptionItem("WORKLOG_UNKNOWN_CLAIM", "MEDIUM", "denials_worklog.xlsx", ref,
                                            f"Claim # {w.raw.get('Claim #')!r} not found in billing export"))
            continue
        if w.id_how == "bare_number":
            pass  # routine normalisation (6-digit number -> GPP-2026-xxxxxx)
        if w.date_issue:
            exceptions.append(ExceptionItem("WORKLOG_DATE_AMBIGUOUS" if "ambiguous" in w.date_issue else "WORKLOG_BAD_DATE",
                                            "LOW", "denials_worklog.xlsx", ref, w.date_issue, claim_id=w.claim_id))
        c = claims[w.claim_id]
        if w.payer_id and w.payer_id != c.payer_id:
            exceptions.append(ExceptionItem("WORKLOG_PAYER_MISMATCH", "MEDIUM", "denials_worklog.xlsx", ref,
                                            f"Worklog payer {w.raw.get('Payer')!r} != claim payer {c.payer_id}",
                                            claim_id=w.claim_id))
        if w.claim_id not in denied_ever:
            st = claim_summary(c, model.lines)["claim_status"]
            exceptions.append(ExceptionItem(
                "WORKLOG_CLAIM_NOT_DENIED", "MEDIUM", "denials_worklog.xlsx", ref,
                f"Worklog tracks this claim as a denial but no remittance shows a denial (claim status {st}).",
                claim_id=w.claim_id))
        elif w.status == "Resolved" and open_by_claim.get(w.claim_id):
            amt = sum((d.denied_amount for d in open_by_claim[w.claim_id]), Decimal(0))
            exceptions.append(ExceptionItem(
                "WORKLOG_RESOLVED_BUT_UNPAID", "HIGH", "denials_worklog.xlsx", ref,
                f"Marked {w.raw.get('Status', '').strip()!r} in the worklog, but the latest remittance still shows the "
                f"denial (${amt}) unpaid. Reopened.", claim_id=w.claim_id, amount=amt))
        if w.amount is not None and open_by_claim.get(w.claim_id):
            denied = sum((d.denied_amount for d in open_by_claim[w.claim_id]), Decimal(0))
            if money(w.amount) != money(denied):
                exceptions.append(ExceptionItem(
                    "WORKLOG_AMOUNT_MISMATCH", "LOW", "denials_worklog.xlsx", ref,
                    f"Worklog amount ${w.amount} != denied amount ${denied} on remittance", claim_id=w.claim_id))


def summarize(model, analyses, plans, exc_rows, worklog, dmap) -> dict:
    open_a = [a for a in analyses if dmap[a.denial_id].is_open]
    buckets = defaultdict(lambda: {"denials": 0, "billed": Decimal(0), "allowed": Decimal(0), "expected": Decimal(0)})
    for a in open_a:
        d = dmap[a.denial_id]
        b = buckets[a.recoverability]
        b["denials"] += 1
        b["billed"] += d.denied_amount
        b["allowed"] += d.expected_allowed
        b["expected"] += a.expected_recovery
    logged = {w.claim_id for w in worklog if w.claim_id}
    open_claims = {dmap[a.denial_id].claim_id for a in open_a}
    return {
        "open_denials": len(open_a), "open_claims": len(open_claims),
        "open_billed": str(sum((dmap[a.denial_id].denied_amount for a in open_a), Decimal(0))),
        "buckets": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in sorted(buckets.items())},
        "needs_review": sum(1 for a in open_a if a.needs_review),
        "engines": dict(Counter(a.engine for a in open_a)),
        "work_items": len(plans), "exceptions": dict(Counter(r["kind"] for r in exc_rows)),
        "open_denial_claims_not_in_worklog": len(open_claims - logged),
        "recon": [{k: str(v) for k, v in r.items()} for r in model.recon],
    }


def fingerprint(data: dict, plans: list[dict]) -> str:
    return output_fingerprint([data[k] for k in sorted(data)] + [plans])


def input_fingerprint(data_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(data_dir.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(data_dir)).encode())
            h.update(sha256_file(p).encode())
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Denials Command Center pipeline")
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", str(ROOT / "data")))
    ap.add_argument("--ai", choices=["auto", "off"], default=os.environ.get("DCC_AI_MODE", "auto"))
    ap.add_argument("--schema", default=os.environ.get("SCHEMA_PATH", str(ROOT / "db" / "schema.sql")))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["DCC_AI_MODE"] = args.ai
    data_dir = Path(args.data)

    conn = db.connect()
    db.apply_schema(conn, Path(args.schema))
    with conn.cursor() as cur:
        cache = db.load_ai_cache(cur)
    client = make_client()
    ai = CachedAI(client, cache)
    log.info("AI mode: %s", f"enabled ({client.model})" if client else "disabled - rules engine only (degraded mode)")

    started = datetime.now(timezone.utc)
    result = build(data_dir, ai)
    out_fp = fingerprint(result["data"], result["plans"])
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(424243)")
        db.rebuild_derived(cur, result["data"])
        db.seed_users(cur, USERS, os.environ.get("DCC_DEMO_PASSWORD", "demo123"))
        stats = db.sync_work_items(cur, result["plans"], result["initial"], result["claim_paid"])
        notes = db.import_worklog_notes(cur, result["notes"])
        if ai.new_entries:
            db.save_ai_cache(cur, ai.new_entries, ai.model, PROMPT_VERSION)
        summary = dict(result["summary"], work_item_sync=stats, notes_imported=notes,
                       ai_calls=len(ai.new_entries), ai_cache_hits=len(cache), ai_last_error=ai.last_error)
        cur.execute("INSERT INTO dcc.pipeline_run(started_at, finished_at, input_fingerprint, output_fingerprint, "
                    "ai_mode, summary) VALUES (%s, now(), %s, %s, %s, %s)",
                    (started, input_fingerprint(data_dir), out_fp,
                     f"enabled:{ai.model}" if client else "disabled", db._j(summary)))
    conn.commit()
    print(json.dumps(dict(output_fingerprint=out_fp, **summary), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
