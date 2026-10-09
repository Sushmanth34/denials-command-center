"""Builds the evidence packet for one denial: verified facts computed from our own data.

The rules engine decides from these facts, the LLM is shown the same facts, and the UI displays
them, so every conclusion can be traced back to a checkable fact.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .loaders import Claim, PolicySection, TODAY, WorklogEntry
from .reconcile import Denial, Model

EM_CODES = {f"992{n}" for n in range(21, 40)} | {f"993{n:02d}" for n in range(4, 11)}
SUBSEQUENT_HOSPITAL = {"99231", "99232", "99233"}
SNF_INITIAL = {"99304", "99305", "99306"}
SMP_AUTH_EFFECTIVE = date(2026, 4, 1)
AUTH_PREFIX = {"SMP12": "SMPA", "CSA77": "CSAA", "NS401": "NSA", "MRD55": "MRDA"}

# Which policy documents are relevant to which CARC (retrieval, not decision).
CARC_POLICY_DOCS = {
    "197": ["SMP_SNF-AUTH-2026"],
    "97": ["ALL_PAYERS_MOD25-2026"],
    "11": ["MPPO_DX-EXCL-03"],
    "151": ["NSHP_HOSP-FREQ-07"],
    "B7": ["CSA_PROVIDER-ENROLLMENT"],
}


class Indexes:
    """Cross-claim lookups computed once."""

    def __init__(self, model: Model):
        self.by_member_dos = defaultdict(list)
        self.by_provider_payer = defaultdict(list)
        self.by_member_payer = defaultdict(list)
        for c in model.claims.values():
            self.by_member_dos[(c.member_id, c.dos)].append(c)
            self.by_provider_payer[(c.rendering_npi, c.payer_id)].append(c)
            self.by_member_payer[(c.member_id, c.payer_id)].append(c)
        self.model = model
        self.icn = {rc["remit_claim_key"]: rc["payer_icn"] for rc in model.remit_claims}

    def claim_paid(self, cid: str) -> bool:
        c = self.model.claims[cid]
        return all(self.model.lines[(cid, l.line_no)].status == "PAID" for l in c.lines)

    def claim_state(self, cid: str) -> str:
        c = self.model.claims[cid]
        st = {self.model.lines[(cid, l.line_no)].status for l in c.lines}
        return "PAID" if st == {"PAID"} else ("NO_RESPONSE" if st == {"NO_RESPONSE"} else "/".join(sorted(st)))


def has_i10_with_i11(dx: list[str]) -> bool:
    codes = [d.upper().strip() for d in dx]
    return "I10" in codes and any(c.startswith("I11") for c in codes)


def em_without_25_with_procedure(claim: Claim) -> bool:
    ems = [l for l in claim.lines if l.cpt in EM_CODES]
    procs = [l for l in claim.lines if l.cpt not in EM_CODES]
    return bool(ems and procs and any("25" not in (l.modifier or "") for l in ems))


def build_evidence(d: Denial, model: Model, idx: Indexes, codes: dict, policies: list[PolicySection],
                   worklog: list[WorklogEntry]) -> dict:
    c = model.claims[d.claim_id]
    payer = model.payers[c.payer_id]
    line = next(l for l in c.lines if l.line_no == d.line_no)
    ls = model.lines[(d.claim_id, d.line_no)]
    received = min((a.received_date for a in ls.adjudications if a.received_date), default=None)
    tf_deadline = c.dos + timedelta(days=payer.timely_filing_days)

    ev: dict = {
        "denial": {
            "carc": d.carc, "carc_description": codes.get(("CARC", d.carc), "unknown code"),
            "group": d.group_code, "group_meaning": codes.get(("GROUP", d.group_code), ""),
            "rarcs": [{"code": r, "description": codes.get(("RARC", r), "unknown code")} for r in d.rarcs],
            "denied_amount": str(d.denied_amount), "expected_allowed_if_overturned": str(d.expected_allowed),
            "denial_date": d.denial_date.isoformat(), "is_open": d.is_open, "resolution": d.resolution,
            "previously_paid_then_recouped": d.prior_paid_then_reversed,
            "payer_icn": next((idx.icn.get(a.remit_claim_key) for a in reversed(ls.adjudications)
                               if a.trn == d.trn), None),
        },
        "claim": {
            "claim_id": c.claim_id, "payer_id": c.payer_id, "payer_name": payer.name, "dos": c.dos.isoformat(),
            "pos": c.pos, "facility": c.facility, "rendering_provider": c.rendering_provider,
            "rendering_npi": c.rendering_npi, "coder_id": c.coder_id, "prebill_reviewed": c.prebill_reviewed,
            "auth_number_present": bool(c.auth_number), "dx_codes": c.dx_codes,
            "lines": [{"line_no": l.line_no, "cpt": l.cpt, "modifier": l.modifier or None, "charge": str(l.charge)}
                      for l in c.lines],
            "denied_line": {"line_no": line.line_no, "cpt": line.cpt, "modifier": line.modifier or None},
        },
        "deadlines": {
            "today": TODAY.isoformat(),
            "appeal_deadline": d.appeal_deadline.isoformat(),
            "corrected_claim_deadline": d.corrected_deadline.isoformat(),
            "days_to_appeal_deadline": (d.appeal_deadline - TODAY).days,
            "days_to_corrected_deadline": (d.corrected_deadline - TODAY).days,
        },
        "checks": {},
    }
    chk = ev["checks"]

    # timely filing
    chk["submitted_date"] = c.submitted_date.isoformat()
    chk["payer_received_date"] = received.isoformat() if received else None
    chk["timely_filing_limit_days"] = payer.timely_filing_days
    chk["days_dos_to_submission"] = (c.submitted_date - c.dos).days
    chk["submitted_within_timely_filing"] = c.submitted_date <= tf_deadline
    chk["received_within_timely_filing"] = (received <= tf_deadline) if received else None

    # diagnosis / modifier
    chk["dx_has_I10_and_I11"] = has_i10_with_i11(c.dx_codes)
    chk["em_billed_with_procedure_without_mod25"] = em_without_25_with_procedure(c)
    chk["denied_line_is_em"] = line.cpt in EM_CODES
    chk["denied_line_has_mod25"] = "25" in (line.modifier or "")

    # authorization
    if c.payer_id == "SMP12" and line.cpt in SNF_INITIAL:
        chk["smp_snf_auth_required_for_dos"] = c.dos >= SMP_AUTH_EFFECTIVE
        chk["smp_snf_auth_policy_effective"] = SMP_AUTH_EFFECTIVE.isoformat()
        chk["retro_auth_window_open"] = TODAY <= c.dos + timedelta(days=14)
    if c.auth_number:
        exp = AUTH_PREFIX.get(c.payer_id)
        chk["auth_number_matches_payer_format"] = bool(exp and c.auth_number.upper().startswith(exp))

    # same patient, same day
    siblings = [o for o in idx.by_member_dos[(c.member_id, c.dos)] if o.claim_id != c.claim_id]
    sib = []
    for o in siblings:
        o_rcv = min((a.received_date for l in o.lines for a in model.lines[(o.claim_id, l.line_no)].adjudications
                     if a.received_date), default=None)
        sib.append({
            "claim_id": o.claim_id, "cpts": [l.cpt for l in o.lines], "rendering_npi": o.rendering_npi,
            "same_provider": o.rendering_npi == c.rendering_npi,
            "same_cpt_as_denied_line": any(l.cpt == line.cpt for l in o.lines),
            "state": idx.claim_state(o.claim_id), "submitted_date": o.submitted_date.isoformat(),
            "received_date": o_rcv.isoformat() if o_rcv else None,
        })
    chk["same_patient_same_day_claims"] = sib
    chk["exact_duplicate_of_paid_claim"] = any(s["same_cpt_as_denied_line"] and s["same_provider"] and s["state"] == "PAID"
                                               for s in sib)
    chk["same_day_subsequent_care_by_other_claim"] = (line.cpt in SUBSEQUENT_HOSPITAL and any(
        set(s["cpts"]) & SUBSEQUENT_HOSPITAL for s in sib))

    # enrollment evidence: has this provider ever been paid by this payer, and when?
    pp = idx.by_provider_payer[(c.rendering_npi, c.payer_id)]
    paid_dos = sorted(o.dos for o in pp if idx.claim_paid(o.claim_id))
    b7 = [o for o in pp if any(dd.carc == "B7" for dd in model.denials if dd.claim_id == o.claim_id)]
    chk["provider_claims_with_payer"] = len(pp)
    chk["provider_paid_claims_with_payer"] = len(paid_dos)
    chk["provider_first_paid_dos_with_payer"] = paid_dos[0].isoformat() if paid_dos else None
    chk["provider_b7_denials_with_payer"] = len(b7)
    chk["provider_first_dos_any_payer"] = min(o.dos for k, v in idx.by_provider_payer.items()
                                              if k[0] == c.rendering_npi for o in v).isoformat()

    # eligibility evidence: is the same member paid by this payer for a LATER date of service?
    later_paid = sorted(o.dos for o in idx.by_member_payer[(c.member_id, c.payer_id)]
                        if o.dos > c.dos and idx.claim_paid(o.claim_id))
    chk["member_paid_by_payer_for_later_dos"] = later_paid[0].isoformat() if later_paid else None

    # policies relevant to this payer + reason (retrieval only)
    docs = CARC_POLICY_DOCS.get(d.carc, [])
    ev["policy_sections"] = [
        {"section_id": p.section_id, "document": p.document, "title": p.title, "text": p.text}
        for p in policies if p.section_id.split("#")[0] in docs and c.payer_id in p.payer_ids]

    # worklog history (UNTRUSTED free text, kept separate)
    wl = [w for w in worklog if w.claim_id == c.claim_id and w.duplicate_of is None]
    ev["worklog"] = [{"row": w.row_no, "logged": w.logged_date.isoformat() if w.logged_date else None,
                      "owner": w.owner, "status": w.status, "note": w.note, "note_flagged": w.note_flagged}
                     for w in wl]
    return ev
