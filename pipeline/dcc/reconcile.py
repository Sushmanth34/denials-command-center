"""Reconciliation: turn raw inputs into one trustworthy record per claim.

Key decisions (each one is covered by a test):
  * A payment is identified by its TRN02 trace number (+ payer). The same payment arriving in a
    second file (e.g. a clearinghouse re-send) is loaded once; the duplicate is reported.
  * Remit claim ids are normalised (GPP-2026-000123 / GPP2026000123 / 000123).
  * Remit service lines are matched to billed lines by CPT (modifiers can legitimately change on a
    corrected claim), then by order.
  * The state of a line is the LAST non-reversal adjudication, ordered by payment date then file
    position. Money is the SUM over all adjudications (reversals carry negative amounts).
  * A denial is an adjustment in group CO/OA/PI other than CARC 45 (contractual write-down).
    PR adjustments are patient responsibility, not denials.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .ids import normalize_claim_id
from .loaders import Claim, Payer, TODAY
from .x12_835 import Interchange, Payment, RemitClaim, ServiceLine, parse_835

ZERO = Decimal("0")
CENT = Decimal("0.01")
CONTRACTUAL_CARC = "45"
DENIAL_GROUPS = ("CO", "OA", "PI")
NO_RESPONSE_FOLLOWUP_DAYS = 45


def money(x) -> Decimal:
    return Decimal(x).quantize(CENT)


def exc_id(kind: str, ref: str) -> str:
    return hashlib.sha1(f"{kind}|{ref}".encode()).hexdigest()[:16]


@dataclass
class ExceptionItem:
    kind: str
    severity: str
    source: str
    reference: str
    message: str
    claim_id: str | None = None
    amount: Decimal | None = None
    details: dict = field(default_factory=dict)

    @property
    def exception_id(self) -> str:
        return exc_id(self.kind, self.reference)


@dataclass
class LoadedPayment:
    payment: Payment
    file_name: str
    file_order: int
    also_seen_in: list[str] = field(default_factory=list)
    balanced: bool = True
    claim_paid_total: Decimal = ZERO


@dataclass
class Adjudication:
    """One remit service line applied to one billed claim line."""
    payment_date: date
    order_key: tuple
    trn: str
    remit_claim_key: str
    status_code: str
    freq_code: str
    svc: ServiceLine
    received_date: date | None

    @property
    def is_reversal(self) -> bool:
        return self.status_code == "22"

    def denial_adjustments(self):
        return [a for a in self.svc.adjustments
                if a.group in DENIAL_GROUPS and a.carc != CONTRACTUAL_CARC and a.amount > 0]


@dataclass
class LineState:
    claim_id: str
    line_no: int
    cpt: str
    charge: Decimal
    adjudications: list[Adjudication] = field(default_factory=list)

    @property
    def payer_paid(self) -> Decimal:
        return sum((a.svc.paid for a in self.adjudications), ZERO)

    @property
    def patient_resp(self) -> Decimal:
        return sum((adj.amount for a in self.adjudications for adj in a.svc.adjustments if adj.group == "PR"), ZERO)

    @property
    def contractual(self) -> Decimal:
        return sum((adj.amount for a in self.adjudications for adj in a.svc.adjustments
                    if adj.group == "CO" and adj.carc == CONTRACTUAL_CARC), ZERO)

    @property
    def current(self) -> Adjudication | None:
        """Latest non-reversal adjudication, unless the very last event is an unanswered reversal."""
        if not self.adjudications:
            return None
        last = self.adjudications[-1]
        if last.is_reversal:
            return None
        return last

    @property
    def status(self) -> str:
        if not self.adjudications:
            return "NO_RESPONSE"
        if self.adjudications[-1].is_reversal:
            return "RECOUPED_PENDING"
        return "DENIED" if self.current.denial_adjustments() else "PAID"


@dataclass
class Denial:
    denial_id: str
    claim_id: str
    line_no: int
    cpt: str
    carc: str
    group_code: str
    rarcs: list[str]
    denied_amount: Decimal
    denial_date: date
    trn: str
    is_open: bool
    resolution: str | None
    appeal_deadline: date
    corrected_deadline: date
    prior_paid_then_reversed: bool
    expected_allowed: Decimal = ZERO


@dataclass
class TimelineEvent:
    claim_id: str
    event_date: date
    event_type: str
    summary: str
    amount: Decimal | None
    source: str
    details: dict = field(default_factory=dict)
    order: tuple = ()


@dataclass
class Model:
    claims: dict[str, Claim]
    payers: dict[str, Payer]
    interchanges: list[Interchange]
    payments: dict[str, LoadedPayment]
    remit_claims: list[dict]
    remit_lines: list[dict]
    lines: dict[tuple[str, int], LineState]
    denials: list[Denial]
    events: list[TimelineEvent]
    exceptions: list[ExceptionItem]
    file_status: dict[str, dict]
    allowed_table: dict[tuple[str, str], Decimal]
    recon: list[dict]


# ------------------------------------------------------------------ remit loading
def load_remit_files(paths: list[Path]) -> tuple[list[Interchange], dict[str, LoadedPayment], dict[str, dict],
                                                 list[ExceptionItem]]:
    exceptions: list[ExceptionItem] = []
    file_status: dict[str, dict] = {}
    parsed: list[tuple[Interchange, Path, str]] = []
    for p in paths:
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        try:
            ic = parse_835(data.decode("utf-8", errors="replace"), p.name)
        except ValueError as e:
            exceptions.append(ExceptionItem("REMIT_FILE_UNREADABLE", "HIGH", p.name, p.name, f"Could not parse 835: {e}"))
            file_status[p.name] = dict(kind="835", sha256=sha, size=len(data), status="unreadable", note=str(e))
            continue
        for issue in ic.issues:
            exceptions.append(ExceptionItem("REMIT_PARSE_ISSUE", "MEDIUM", p.name, issue, issue))
        parsed.append((ic, p, sha))
        file_status[p.name] = dict(kind="835", sha256=sha, size=len(data), status="loaded", note=None)

    # deterministic processing order: interchange date, then control number, then name
    parsed.sort(key=lambda t: (t[0].isa_date, t[0].isa_control, t[1].name))
    payments: dict[str, LoadedPayment] = {}
    seen_sha: dict[str, str] = {}
    for file_order, (ic, p, sha) in enumerate(parsed):
        if sha in seen_sha:
            file_status[p.name].update(status="duplicate_content", note=f"byte-identical to {seen_sha[sha]}")
        seen_sha.setdefault(sha, p.name)
        dup_count = 0
        for pay in ic.payments:
            key = f"{pay.payer_id}|{pay.trn}"
            if key in payments:
                first = payments[key]
                same = _payment_fingerprint(first.payment) == _payment_fingerprint(pay)
                first.also_seen_in.append(p.name)
                dup_count += 1
                if same:
                    exceptions.append(ExceptionItem(
                        "DUPLICATE_PAYMENT", "LOW", p.name, f"{p.name}:{pay.trn}",
                        f"Payment {pay.trn} ({pay.payer_id}, ${pay.payment_amount}) already loaded from "
                        f"{first.file_name}; identical content, not loaded again.",
                        amount=pay.payment_amount, details=dict(first_file=first.file_name, isa=ic.isa_control)))
                else:
                    exceptions.append(ExceptionItem(
                        "CONFLICTING_PAYMENT", "HIGH", p.name, f"{p.name}:{pay.trn}",
                        f"Payment {pay.trn} appears in {first.file_name} and {p.name} with DIFFERENT content; "
                        f"first version kept, needs manual review.",
                        amount=pay.payment_amount, details=dict(first_file=first.file_name)))
                continue
            payments[key] = LoadedPayment(pay, p.name, file_order)
        if dup_count and dup_count == len(ic.payments) and file_status[p.name]["status"] == "loaded":
            file_status[p.name].update(status="duplicate_content",
                                       note=f"all {dup_count} payments already loaded (re-sent file, new ISA {ic.isa_control})")
        elif dup_count:
            file_status[p.name].update(status="partially_duplicate", note=f"{dup_count} payments already loaded")
    return [t[0] for t in parsed], payments, file_status, exceptions


def _payment_fingerprint(p: Payment) -> str:
    body = [(c.raw_claim_id, c.status_code, str(c.charge), str(c.paid),
             [(l.cpt, str(l.charge), str(l.paid), [(a.group, a.carc, str(a.amount)) for a in l.adjustments])
              for l in c.lines]) for c in p.claims]
    return hashlib.sha256(json.dumps([str(p.payment_amount), str(p.payment_date), body]).encode()).hexdigest()


# ------------------------------------------------------------------ main reconciliation
def reconcile(claims: dict[str, Claim], payers: dict[str, Payer], remit_paths: list[Path]) -> Model:
    interchanges, payments, file_status, exceptions = load_remit_files(remit_paths)

    lines: dict[tuple[str, int], LineState] = {}
    for c in claims.values():
        for l in c.lines:
            lines[(c.claim_id, l.line_no)] = LineState(c.claim_id, l.line_no, l.cpt, l.charge)

    remit_claims: list[dict] = []
    remit_lines: list[dict] = []
    recon_acc = defaultdict(lambda: defaultdict(lambda: ZERO))

    for ic in interchanges:
        for pay in ic.payments:
            recon_acc[pay.payer_id]["files_payment_total"] += pay.payment_amount
    for key, lp in payments.items():
        pay = lp.payment
        if lp.also_seen_in:
            recon_acc[pay.payer_id]["duplicate_payment_total"] += pay.payment_amount * len(lp.also_seen_in)

    for key in sorted(payments, key=lambda k: (payments[k].payment.payment_date or date.min, payments[k].file_order, k)):
        lp = payments[key]
        pay = lp.payment
        claim_paid_total = sum((c.paid for c in pay.claims), ZERO)
        balanced = money(claim_paid_total - pay.plb_total) == money(pay.payment_amount)
        if not balanced:
            exceptions.append(ExceptionItem(
                "PAYMENT_OUT_OF_BALANCE", "HIGH", lp.file_name, pay.trn,
                f"BPR amount ${pay.payment_amount} != sum of claim payments ${claim_paid_total} - PLB ${pay.plb_total}",
                amount=pay.payment_amount - claim_paid_total))
        recon_acc[pay.payer_id]["unique_payment_total"] += pay.payment_amount
        lp.balanced = balanced
        lp.claim_paid_total = claim_paid_total

        for clp in pay.claims:
            rck = f"{pay.trn}|{clp.seq}"
            cid, how = normalize_claim_id(clp.raw_claim_id)
            claim = claims.get(cid) if cid else None
            attach = claim is not None
            if claim is None:
                exceptions.append(ExceptionItem(
                    "UNMATCHED_REMIT_CLAIM", "HIGH" if clp.paid != 0 else "MEDIUM", lp.file_name, rck,
                    f"Remit claim {clp.raw_claim_id!r} ({clp.patient_name}, paid ${clp.paid}) does not match any "
                    f"claim in the billing export" + (" - foreign prefix, likely another client's claim paid into "
                                                      "our account" if how == "foreign_prefix" else ""),
                    amount=clp.paid, details=dict(payer=pay.payer_id, trn=pay.trn, how=how)))
                recon_acc[pay.payer_id]["unmatched_claim_paid"] += clp.paid
            elif claim.payer_id != pay.payer_id:
                attach = False
                exceptions.append(ExceptionItem(
                    "REMIT_PAYER_MISMATCH", "HIGH", lp.file_name, rck,
                    f"Remit from {pay.payer_id} for claim billed to {claim.payer_id}; not applied",
                    claim_id=cid, amount=clp.paid))
                recon_acc[pay.payer_id]["unmatched_claim_paid"] += clp.paid
            else:
                if clp.patient_member_id and clp.patient_member_id != claim.member_id:
                    exceptions.append(ExceptionItem(
                        "REMIT_MEMBER_MISMATCH", "MEDIUM", lp.file_name, rck,
                        f"Member id on remit {clp.patient_member_id} differs from claim {claim.member_id}; applied by claim number",
                        claim_id=cid))
                recon_acc[pay.payer_id]["matched_claim_paid"] += clp.paid
            if attach and how != "exact":
                pass  # normalisation is routine; recorded on the remit_claim row (raw_claim_id)
            line_sum_charge = sum((l.charge for l in clp.lines), ZERO)
            line_sum_paid = sum((l.paid for l in clp.lines), ZERO)
            if money(line_sum_charge) != money(clp.charge) or money(line_sum_paid) != money(clp.paid):
                exceptions.append(ExceptionItem(
                    "REMIT_CLAIM_OUT_OF_BALANCE", "MEDIUM", lp.file_name, rck,
                    f"CLP totals (charge ${clp.charge}, paid ${clp.paid}) != service lines "
                    f"(charge ${line_sum_charge}, paid ${line_sum_paid})", claim_id=cid if attach else None))
            remit_claims.append(dict(
                remit_claim_key=rck, trn=pay.trn, clp_seq=clp.seq, raw_claim_id=clp.raw_claim_id,
                claim_id=cid if attach else None, status_code=clp.status_code, charge=clp.charge, paid=clp.paid,
                patient_resp=clp.patient_resp, payer_icn=clp.payer_icn, freq_code=clp.freq_code,
                patient_name=clp.patient_name, rendering_npi=clp.rendering_npi, received_date=clp.received_date,
                payment_date=pay.payment_date))

            used_lines: set[int] = set()
            for svc in clp.lines:
                adj_sum = sum((a.amount for a in svc.adjustments), ZERO)
                if money(svc.charge - svc.paid) != money(adj_sum):
                    exceptions.append(ExceptionItem(
                        "REMIT_LINE_OUT_OF_BALANCE", "MEDIUM", lp.file_name, f"{rck}|{svc.seq}",
                        f"Line {svc.cpt}: charge ${svc.charge} - paid ${svc.paid} != adjustments ${adj_sum}",
                        claim_id=cid if attach else None))
                matched_no = None
                if attach:
                    candidates = [l for l in claim.lines if l.cpt == svc.cpt and l.line_no not in used_lines]
                    if candidates:
                        matched_no = candidates[0].line_no
                        used_lines.add(matched_no)
                        if svc.service_date and svc.service_date != claim.dos:
                            exceptions.append(ExceptionItem(
                                "REMIT_DOS_MISMATCH", "MEDIUM", lp.file_name, f"{rck}|{svc.seq}",
                                f"Service date {svc.service_date} on remit differs from claim DOS {claim.dos}",
                                claim_id=cid))
                        lines[(cid, matched_no)].adjudications.append(Adjudication(
                            payment_date=pay.payment_date, order_key=(pay.payment_date, lp.file_order, pay.trn, clp.seq, svc.seq),
                            trn=pay.trn, remit_claim_key=rck, status_code=clp.status_code, freq_code=clp.freq_code,
                            svc=svc, received_date=clp.received_date))
                    else:
                        exceptions.append(ExceptionItem(
                            "UNMATCHED_REMIT_LINE", "HIGH", lp.file_name, f"{rck}|{svc.seq}",
                            f"Remit line {svc.cpt} has no matching billed line on {cid}", claim_id=cid, amount=svc.paid))
                remit_lines.append(dict(
                    remit_claim_key=rck, svc_seq=svc.seq, cpt=svc.cpt, modifiers=svc.modifiers, charge=svc.charge,
                    paid=svc.paid, service_date=svc.service_date, allowed_amount=svc.allowed,
                    adjustments=[dict(group=a.group, carc=a.carc, amount=str(a.amount)) for a in svc.adjustments],
                    rarcs=list(svc.rarcs), matched_line_no=matched_no))

    for ls in lines.values():
        ls.adjudications.sort(key=lambda a: a.order_key)
    exceptions.extend(overpayment_exceptions(claims, lines))

    allowed_table = build_allowed_table(claims, lines)
    denials = build_denials(claims, payers, lines, allowed_table)
    events = build_timeline(claims, lines, payments)
    exceptions.extend(no_response_exceptions(claims, lines))

    recon = []
    system_paid = defaultdict(lambda: ZERO)
    for (cid, _), ls in lines.items():
        system_paid[claims[cid].payer_id] += ls.payer_paid
    for payer_id in sorted(set(recon_acc) | set(system_paid)):
        a = recon_acc[payer_id]
        sys_paid = system_paid[payer_id]
        recon.append(dict(
            payer_id=payer_id,
            files_payment_total=money(a["files_payment_total"]),
            duplicate_payment_total=money(a["duplicate_payment_total"]),
            unique_payment_total=money(a["unique_payment_total"]),
            unmatched_claim_paid=money(a["unmatched_claim_paid"]),
            matched_claim_paid=money(a["matched_claim_paid"]),
            system_claim_paid=money(sys_paid),
            # unique money received must equal what we applied to claims + what we could not apply
            difference=money(a["unique_payment_total"] - a["unmatched_claim_paid"] - sys_paid),
        ))
    return Model(claims, payers, interchanges, payments, remit_claims, remit_lines, lines, denials, events,
                 exceptions, file_status, allowed_table, recon)


# ------------------------------------------------------------------ expected reimbursement
def build_allowed_table(claims: dict[str, Claim], lines: dict) -> dict[tuple[str, str], Decimal]:
    """Median allowed amount (payer paid + patient responsibility) per (payer, CPT) from paid lines.

    Denied dollars are billed charges; what we would actually collect if a denial is overturned is
    the allowed amount. About 38% of billed charges would never be paid even if every denial were overturned.
    """
    samples = defaultdict(list)
    ratios = defaultdict(list)
    for (cid, _), ls in lines.items():
        cur = ls.current
        if cur is None or cur.denial_adjustments() or cur.svc.paid <= 0:
            continue
        pr = sum((a.amount for a in cur.svc.adjustments if a.group == "PR"), ZERO)
        allowed = cur.svc.allowed if cur.svc.allowed is not None else cur.svc.paid + pr
        payer_id = claims[cid].payer_id
        samples[(payer_id, ls.cpt)].append(allowed)
        if ls.charge > 0:
            ratios[payer_id].append(allowed / ls.charge)
    table = {k: money(statistics.median(v)) for k, v in samples.items()}
    for payer_id, r in ratios.items():
        table[(payer_id, "*ratio*")] = Decimal(statistics.median(r)).quantize(Decimal("0.0001"))
    return table


def expected_allowed(table, payer_id: str, cpt: str, charge: Decimal) -> Decimal:
    if (payer_id, cpt) in table:
        return table[(payer_id, cpt)]
    ratio = table.get((payer_id, "*ratio*"), Decimal("0.6"))
    return money(charge * ratio)


# ------------------------------------------------------------------ denials
def build_denials(claims, payers, lines, allowed_table) -> list[Denial]:
    out = []
    for (cid, line_no), ls in sorted(lines.items()):
        claim = claims[cid]
        payer = payers[claim.payer_id]
        denied_events = [a for a in ls.adjudications if not a.is_reversal and a.denial_adjustments()]
        if not denied_events:
            continue
        cur = ls.current
        last_denial = denied_events[-1]
        is_open = cur is not None and cur is last_denial
        resolution = None
        if not is_open:
            if cur is None:
                resolution = None
                is_open = True  # reversed after denial with nothing after: treat as still open
            else:
                resolution = "PAID_ON_CORRECTED_CLAIM" if cur.freq_code == "7" else "PAID_ON_REPROCESSING"
        adj = last_denial.denial_adjustments()
        primary = max(adj, key=lambda a: (a.amount, a.carc))
        prior_paid = any(a.svc.paid > 0 and not a.is_reversal and a.order_key < last_denial.order_key
                         for a in ls.adjudications)
        d = Denial(
            denial_id=f"{cid}:{line_no}", claim_id=cid, line_no=line_no, cpt=ls.cpt, carc=primary.carc,
            group_code=primary.group, rarcs=list(last_denial.svc.rarcs),
            denied_amount=money(sum((a.amount for a in adj), ZERO)), denial_date=last_denial.payment_date,
            trn=last_denial.trn, is_open=is_open, resolution=resolution,
            appeal_deadline=last_denial.payment_date + timedelta(days=payer.appeal_window_days),
            corrected_deadline=last_denial.payment_date + timedelta(days=payer.corrected_window_days),
            prior_paid_then_reversed=prior_paid and any(a.is_reversal for a in ls.adjudications),
        )
        d.expected_allowed = expected_allowed(allowed_table, claim.payer_id, ls.cpt, ls.charge)
        out.append(d)
    return out


def overpayment_exceptions(claims, lines) -> list[ExceptionItem]:
    """A line paid by two adjudications with no reversal in between was paid twice (e.g. a replacement
    claim paid in full without the payer reversing the original). That is a credit balance the practice
    may have to refund, and a likely future recoupment."""
    out = []
    for (cid, line_no), ls in sorted(lines.items()):
        live_payments = []
        for a in ls.adjudications:
            if a.is_reversal:
                live_payments = []
            elif a.svc.paid > 0:
                live_payments.append(a)
        if len(live_payments) > 1:
            extra = sum((a.svc.paid for a in live_payments[1:]), ZERO)
            out.append(ExceptionItem(
                "POSSIBLE_OVERPAYMENT", "HIGH", "835", f"{cid}:{line_no}",
                f"Line {ls.cpt} paid {len(live_payments)} times without a reversal "
                f"({', '.join(str(a.svc.paid) for a in live_payments)}); ${extra} likely overpaid "
                f"(credit balance / future recoupment).",
                claim_id=cid, amount=extra, details=dict(trns=[a.trn for a in live_payments])))
    return out


def no_response_exceptions(claims, lines) -> list[ExceptionItem]:
    out = []
    for c in sorted(claims.values(), key=lambda c: c.claim_id):
        if all(not lines[(c.claim_id, l.line_no)].adjudications for l in c.lines):
            age = (TODAY - c.submitted_date).days
            if age > NO_RESPONSE_FOLLOWUP_DAYS:
                out.append(ExceptionItem(
                    "NO_REMIT_RECEIVED", "HIGH", "claims_export.csv", c.claim_id,
                    f"Submitted {c.submitted_date} ({age} days ago) to {c.payer_id}; no remittance on file. "
                    f"Check claim status / resubmit before timely filing.",
                    claim_id=c.claim_id, amount=c.total_charge))
    return out


# ------------------------------------------------------------------ claim summary + timeline
def claim_summary(claim: Claim, lines: dict) -> dict:
    states = [lines[(claim.claim_id, l.line_no)] for l in claim.lines]
    statuses = {s.status for s in states}
    if statuses == {"NO_RESPONSE"}:
        status = "NO_RESPONSE"
    elif "RECOUPED_PENDING" in statuses:
        status = "RECOUPED_PENDING"
    elif statuses == {"DENIED"}:
        status = "DENIED"
    elif "DENIED" in statuses:
        status = "PARTIALLY_DENIED"
    elif statuses == {"PAID"}:
        status = "PAID"
    else:
        status = "PARTIALLY_ADJUDICATED"
    adjs = [a for s in states for a in s.adjudications]
    return dict(
        payer_paid=money(sum((s.payer_paid for s in states), ZERO)),
        patient_resp=money(sum((s.patient_resp for s in states), ZERO)),
        contractual_adj=money(sum((s.contractual for s in states), ZERO)),
        denied_open=money(sum((sum((a.amount for a in s.current.denial_adjustments()), ZERO)
                               for s in states if s.current is not None), ZERO)),
        claim_status=status,
        last_remit_date=max((a.payment_date for a in adjs), default=None),
        payer_received_date=min((a.received_date for a in adjs if a.received_date), default=None),
    )


def build_timeline(claims, lines, payments) -> list[TimelineEvent]:
    events = []
    for c in claims.values():
        cid = c.claim_id
        events.append(TimelineEvent(cid, c.dos, "SERVICE", f"Service rendered by {c.rendering_provider} at {c.facility}",
                                    None, "claims_export.csv", order=(c.dos, 0)))
        mods = ", ".join(f"{l.cpt}{'-' + l.modifier if l.modifier else ''}" for l in c.lines)
        events.append(TimelineEvent(cid, c.submitted_date, "BILLED", f"Billed to {c.payer_id}: {mods}",
                                    c.total_charge, "claims_export.csv",
                                    dict(coder=c.coder_id, prebill_reviewed=c.prebill_reviewed), order=(c.submitted_date, 1)))
        by_remit = defaultdict(list)
        for l in c.lines:
            for a in lines[(cid, l.line_no)].adjudications:
                by_remit[a.remit_claim_key].append((l, a))
        received = sorted({a.received_date for v in by_remit.values() for _, a in v if a.received_date})
        if received:
            events.append(TimelineEvent(cid, received[0], "RECEIVED_BY_PAYER", "Payer received claim (835 DTM*050)",
                                        None, "835", order=(received[0], 2)))
        for rck, items in by_remit.items():
            a0 = items[0][1]
            paid = sum((a.svc.paid for _, a in items), ZERO)
            parts = []
            for l, a in items:
                den = a.denial_adjustments()
                if a.is_reversal:
                    parts.append(f"{l.cpt} reversed ({a.svc.paid})")
                elif den:
                    parts.append(f"{l.cpt} denied " + ",".join(f"{d.group}-{d.carc}" for d in den)
                                 + (f" [{','.join(a.svc.rarcs)}]" if a.svc.rarcs else ""))
                else:
                    parts.append(f"{l.cpt} paid {a.svc.paid}")
            if a0.is_reversal:
                etype = "REVERSED"
            elif all(a.denial_adjustments() for _, a in items):
                etype = "DENIED"
            elif any(a.denial_adjustments() for _, a in items):
                etype = "PARTIAL"
            else:
                etype = "CORRECTED_PAID" if a0.freq_code == "7" else "PAID"
            lp = next(p for p in payments.values() if p.payment.trn == a0.trn)
            summary = {"REVERSED": "Payer reversed earlier payment (recoupment)",
                       "DENIED": "Denied", "PARTIAL": "Partially paid",
                       "PAID": "Paid", "CORRECTED_PAID": "Corrected claim (freq 7) paid"}[etype]
            events.append(TimelineEvent(cid, a0.payment_date, etype, f"{summary}: " + "; ".join(parts), money(paid),
                                        f"{lp.file_name} TRN {a0.trn}",
                                        dict(trn=a0.trn, status_code=a0.status_code, freq=a0.freq_code),
                                        order=(a0.payment_date, 3) + a0.order_key[1:]))
    return events


def output_fingerprint(rows: list) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
