"""Deterministic appeal / corrected-claim drafts.

Drafts quote the payer policy section verbatim from the policy files and cite it by id, so a
reviewer can check every claim the letter makes. Used directly when the AI is unavailable and
as the factual skeleton the AI may only rephrase.
"""
from __future__ import annotations

DRAFTABLE = {"APPEAL", "CORRECTED_CLAIM", "SEND_RECORDS"}


def _section_lines(ev: dict, refs: list[str]) -> list[str]:
    secs = {p["section_id"]: p for p in ev.get("policy_sections", [])}
    out = []
    for r in refs:
        p = secs.get(r)
        if p:
            doc, num = r.split("#")
            out.append(f'- {doc}, section {num} ({p["title"]}): "{p["text"]}"')
    return out


def draft(ev: dict, rules: dict) -> str | None:
    action = rules["action_type"]
    if action not in DRAFTABLE:
        return None
    d, c, k = ev["denial"], ev["claim"], ev["checks"]
    rarcs = ", ".join(f'{r["code"]} ({r["description"]})' for r in d["rarcs"]) or "none"
    cite = _section_lines(ev, rules["policy_refs"])
    header = [
        f"To: {c['payer_name']} - Provider Appeals / Claims Correction",
        f"Re: Claim {c['claim_id']} | Payer ICN {d.get('payer_icn') or 'n/a'} | DOS {c['dos']} | "
        f"CPT {c['denied_line']['cpt']}{('-' + c['denied_line']['modifier']) if c['denied_line']['modifier'] else ''} | "
        f"Rendering NPI {c['rendering_npi']}",
        f"Denied {d['denial_date']}: CARC {d['carc']} ({d['carc_description']}); RARC {rarcs}",
        "",
    ]
    basis = (["Basis (payer policy):"] + cite) if cite else [
        "Basis: no payer policy excerpt in our policy library addresses this denial; the request relies on the "
        "remittance codes and the enclosed documentation."]

    if action == "CORRECTED_CLAIM":
        if d["carc"] == "11":
            change = "Removed I10; retained the more specific I11.x hypertensive heart disease code (ICD-10-CM Excludes1)."
        elif d["carc"] == "97":
            change = (f"Appended modifier 25 to E/M {c['denied_line']['cpt']} - significant, separately identifiable "
                      "service documented in the attached note.")
        elif d["carc"] == "197":
            change = "Added the SNF admission authorization number obtained from the facility: [AUTH #]."
        else:
            change = "[Describe the correction]"
        body = [
            "CORRECTED CLAIM NOTE (submit as replacement claim, frequency code 7, referencing the original ICN)",
            *header,
            f"Correction: {change}",
            *basis,
            f"Corrected-claim deadline: {ev['deadlines']['corrected_claim_deadline']}.",
        ]
    else:
        if d["carc"] == "197" and k.get("smp_snf_auth_required_for_dos") is False:
            ask = (f"The date of service ({c['dos']}) precedes the {k['smp_snf_auth_policy_effective']} effective "
                   "date of the SNF admission authorization requirement. No authorization was required. "
                   "We request reversal of this denial"
                   + (" and reinstatement of the original payment that was recouped." if d["previously_paid_then_recouped"] else "."))
        elif d["carc"] == "29":
            ask = (f"The claim was submitted on {k['submitted_date']}, {k['days_dos_to_submission']} days after the date of "
                   f"service, within the {k['timely_filing_limit_days']}-day timely filing limit. Proof of timely "
                   "submission (clearinghouse acceptance report) is enclosed. We request reprocessing.")
        elif d["carc"] == "50":
            ask = ("Enclosed is the complete medical record for this date of service documenting the medical necessity "
                   "of the service billed. We request reconsideration"
                   + (" and reversal of the post-payment recoupment." if d["previously_paid_then_recouped"] else "."))
        elif d["carc"] == "151":
            ask = ("The second visit on this date addressed a significant change in the patient's condition. Both "
                   "progress notes are enclosed; the visit is billed with modifier 25 and a distinct diagnosis.")
        elif d["carc"] == "27":
            ask = "Our records indicate active coverage on the date of service. Please verify eligibility and reprocess."
        else:
            ask = "We request reconsideration of this denial based on the information below."
        body = [
            "APPEAL / REQUEST FOR RECONSIDERATION",
            *header,
            ask,
            *basis,
            f"Appeal deadline: {ev['deadlines']['appeal_deadline']}.",
            "Enclosures: remittance advice, [progress note / records], [proof of submission if applicable]",
        ]
    body += ["", "Prepared by: [Denials Specialist], AQKODE Healthcare Solutions on behalf of Gulfview Physician Partners",
             "DRAFT - requires specialist review before sending."]
    return "\n".join(body)
