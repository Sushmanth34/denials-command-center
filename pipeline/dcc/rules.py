"""Deterministic denial classifier. This is the system's baseline AND its degraded mode.

Every decision is a function of the evidence packet (facts we computed), never of free text.
Recovery probabilities are explicit, documented assumptions (see RECOVERY_PROBABILITY); they are
used only to rank work and estimate expected recovery, never to hide a denial.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

ROOT_CAUSES = [
    "Payer error", "Authorization", "Eligibility", "Credentialing", "Medical necessity",
    "Coding - diagnosis", "Coding - modifier", "Coding - frequency", "Billing - timely filing",
    "Billing - duplicate", "Other",
]
TEAMS = [
    "Denials (appeal)", "Front desk / Authorization", "Front desk / Eligibility", "Credentialing",
    "Coding / Clinical", "Coding", "Billing",
]
ACTIONS = [
    "APPEAL", "CORRECTED_CLAIM", "SEND_RECORDS", "REBILL_OTHER_PAYER", "VERIFY_DUPLICATE",
    "ESCALATE_CREDENTIALING", "WRITE_OFF", "MANUAL_REVIEW",
]

# Assumed likelihood of collecting the allowed amount if the recommended action is taken in time.
# Conservative, explainable, and adjustable in one place.
RECOVERY_PROBABILITY = {
    "payer_error_policy": 0.90,       # payer contradicted its own published policy
    "payer_error_timely": 0.80,       # proof of timely submission exists in our data
    "coding_fix_dx": 0.90,            # deterministic edit, corrected claim
    "coding_fix_mod25": 0.75,         # needs documentation to support a separate E/M
    "auth_obtain_from_facility": 0.50,
    "medical_records": 0.50,
    "eligibility_other_coverage": 0.35,
    "frequency_second_visit": 0.25,
    "credentialing_unknown": 0.05,    # enrollment is not retroactive (CSA policy 3)
    "lost": 0.0,
    "no_loss": 0.0,
    "unknown": 0.30,
}


@dataclass
class RulesResult:
    root_cause: str
    owning_team: str
    preventable: bool
    action_type: str
    next_action: str
    policy_refs: list[str]
    confidence: float
    probability_key: str
    deadline_kind: str            # appeal | corrected | none
    rationale: str
    recoverability_note: str = ""
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "root_cause", "owning_team", "preventable", "action_type", "next_action", "policy_refs",
            "confidence", "probability_key", "deadline_kind", "rationale", "recoverability_note", "flags")}


def _refs(ev: dict, *numbers: str, doc: str) -> list[str]:
    available = {p["section_id"] for p in ev.get("policy_sections", [])}
    return [f"{doc}#{n}" for n in numbers if f"{doc}#{n}" in available]


def classify(ev: dict) -> RulesResult:
    d, c, k = ev["denial"], ev["claim"], ev["checks"]
    carc = d["carc"]
    payer = c["payer_id"]

    if carc == "197":
        if k.get("smp_snf_auth_required_for_dos") is False:
            refs = _refs(ev, "3", "5", doc="SMP_SNF-AUTH-2026")
            return RulesResult(
                "Payer error", "Denials (appeal)", False, "APPEAL",
                f"Appeal: DOS {c['dos']} is before the {k['smp_snf_auth_policy_effective']} effective date of the "
                f"SNF authorization requirement, so no authorization was required. Request reversal"
                + (" of the recoupment and reinstatement of the original payment." if d["previously_paid_then_recouped"] else "."),
                refs, 0.95, "payer_error_policy", "appeal",
                "CARC 197 on an initial NF visit whose DOS predates the payer's authorization policy.")
        if k.get("smp_snf_auth_required_for_dos"):
            note = ("Retro-authorization window (14 days from admission) has passed; recoverable only if the "
                    "facility obtained an admission authorization." if not k.get("retro_auth_window_open") else
                    "Retro-authorization is still possible (within 14 days of admission).")
            return RulesResult(
                "Authorization", "Front desk / Authorization", True, "CORRECTED_CLAIM",
                "Obtain the SNF admission authorization number from the facility and submit a corrected claim "
                "(frequency 7) with it. " + note,
                _refs(ev, "2", "4", doc="SMP_SNF-AUTH-2026"), 0.9, "auth_obtain_from_facility", "corrected",
                "Initial NF visit on/after the payer's authorization effective date billed without an auth number.",
                recoverability_note=note)
        return RulesResult("Authorization", "Front desk / Authorization", True, "APPEAL",
                           "Confirm whether an authorization exists for this service; if so, appeal/correct with "
                           "the auth number, otherwise request retro-authorization or write off.",
                           [], 0.75, "auth_obtain_from_facility", "appeal",
                           "Authorization denial (CARC 197).")

    if carc == "11":
        if k["dx_has_I10_and_I11"]:
            return RulesResult(
                "Coding - diagnosis", "Coding", True, "CORRECTED_CLAIM",
                "Submit a corrected claim (frequency 7) removing I10 and keeping the more specific I11.x code "
                "(ICD-10-CM Excludes1).",
                _refs(ev, "2", "3", "4", doc="MPPO_DX-EXCL-03"), 0.95, "coding_fix_dx", "corrected",
                "Claim reports I10 together with I11.x, an Excludes1 pair the payer edits deny.")
        return RulesResult("Coding - diagnosis", "Coding", True, "CORRECTED_CLAIM",
                           "Coder to review diagnosis-to-procedure linkage and submit a corrected claim.",
                           [], 0.6, "coding_fix_dx", "corrected", "CARC 11 without a known edit pattern.",
                           flags=["no_known_dx_edit"])

    if carc == "97":
        if k["em_billed_with_procedure_without_mod25"] and k["denied_line_is_em"]:
            return RulesResult(
                "Coding - modifier", "Coding", True, "CORRECTED_CLAIM",
                "If the note documents a significant, separately identifiable E/M, submit a corrected claim "
                "(frequency 7) with modifier 25 on the E/M line; otherwise accept the bundling.",
                _refs(ev, "1", "2", "3", doc="ALL_PAYERS_MOD25-2026"), 0.92, "coding_fix_mod25", "corrected",
                "E/M billed on the same claim/date as a procedure without modifier 25 (NCCI bundling).")
        if k["denied_line_has_mod25"]:
            return RulesResult("Payer error", "Denials (appeal)", False, "APPEAL",
                               "E/M already carries modifier 25; appeal the bundling with the progress note.",
                               _refs(ev, "1", "2", doc="ALL_PAYERS_MOD25-2026"), 0.6, "payer_error_policy",
                               "appeal", "Bundled despite modifier 25.", flags=["mod25_present"])
        return RulesResult("Coding - modifier", "Coding", True, "MANUAL_REVIEW",
                           "Review bundling: denied line is not an E/M without modifier 25.", [], 0.5,
                           "unknown", "corrected", "CARC 97 outside the modifier-25 pattern.")

    if carc == "151":
        if k["same_day_subsequent_care_by_other_claim"]:
            return RulesResult(
                "Coding - frequency", "Coding", True, "MANUAL_REVIEW",
                "Pull both same-day progress notes. If the second visit was for a significant change in condition, "
                "submit a corrected claim with modifier 25 and a distinct diagnosis and appeal with both notes; "
                "otherwise write off (only one subsequent visit per day is payable).",
                _refs(ev, "1", "2", "3", "4", doc="NSHP_HOSP-FREQ-07"), 0.9, "frequency_second_visit", "appeal",
                "Another claim for subsequent hospital care exists for the same patient on the same date.")
        return RulesResult("Coding - frequency", "Coding", True, "MANUAL_REVIEW",
                           "Frequency denial without a matching same-day claim in our data; verify with payer.",
                           _refs(ev, "1", "2", doc="NSHP_HOSP-FREQ-07"), 0.55, "unknown", "appeal",
                           "CARC 151 but no same-day sibling found.", flags=["no_sibling_found"])

    if carc == "B7":
        first_paid = k.get("provider_first_paid_dos_with_payer")
        if first_paid and first_paid <= c["dos"]:
            return RulesResult("Payer error", "Denials (appeal)", False, "APPEAL",
                               f"Provider was paid by this payer for DOS {first_paid}, before this DOS; appeal with "
                               f"proof of enrollment.", _refs(ev, "1", doc="CSA_PROVIDER-ENROLLMENT"), 0.6,
                               "payer_error_policy", "appeal", "B7 although payer paid this provider earlier.",
                               flags=["enrolled_before_dos?"])
        return RulesResult(
            "Credentialing", "Credentialing", True, "ESCALATE_CREDENTIALING",
            f"Confirm {c['rendering_provider']}'s enrollment effective date with {c['payer_name']}. Services before "
            f"that date are not payable and cannot be appealed: write them off. Hold all new claims for this "
            f"provider/payer until enrollment is active.",
            _refs(ev, "1", "2", "3", doc="CSA_PROVIDER-ENROLLMENT"), 0.93, "credentialing_unknown", "appeal",
            f"Provider has {k['provider_b7_denials_with_payer']} B7 denials and no paid claims with this payer.",
            recoverability_note="Enrollment is not retroactive; recoverable only if enrollment was effective on or before DOS.")

    if carc == "29":
        if k["submitted_within_timely_filing"]:
            return RulesResult("Payer error", "Denials (appeal)", False, "APPEAL",
                               "Appeal with proof of timely submission (clearinghouse acceptance report): claim was "
                               f"submitted {k['days_dos_to_submission']} days after DOS, within the "
                               f"{k['timely_filing_limit_days']}-day limit.", [], 0.9, "payer_error_timely", "appeal",
                               "Timely-filing denial although our submission date is inside the limit.")
        return RulesResult("Billing - timely filing", "Billing", True, "WRITE_OFF",
                           f"Write off: claim was first submitted {k['days_dos_to_submission']} days after DOS, past the "
                           f"{k['timely_filing_limit_days']}-day timely filing limit. Not appealable.",
                           [], 0.95, "lost", "none",
                           "Submission date is past the payer's timely filing limit.")

    if carc == "27":
        later = k.get("member_paid_by_payer_for_later_dos")
        if later:
            return RulesResult("Payer error", "Denials (appeal)", False, "APPEAL",
                               f"Payer paid this member for a later DOS ({later}); verify eligibility on DOS and "
                               "appeal if coverage was active.", [], 0.55, "eligibility_other_coverage", "appeal",
                               "Coverage-terminated denial contradicted by a later paid claim.",
                               flags=["eligibility_conflict"])
        return RulesResult("Eligibility", "Front desk / Eligibility", True, "REBILL_OTHER_PAYER",
                           "Run an eligibility check (270/271) for the DOS, identify the active coverage and bill that "
                           "payer within its timely filing limit; if none, bill the patient per policy.",
                           [], 0.9, "eligibility_other_coverage", "appeal",
                           "Coverage terminated before the date of service (CARC 27 / N30).")

    if carc == "50":
        return RulesResult("Medical necessity", "Coding / Clinical", False, "SEND_RECORDS",
                           "Request the progress note/medical record (RARC M127) and submit an appeal with records "
                           "demonstrating medical necessity" + (", contesting the post-payment recoupment."
                                                                  if d["previously_paid_then_recouped"] else "."),
                           [], 0.85, "medical_records", "appeal",
                           "Medical necessity denial"
                           + (" after post-payment review (payment was recouped)." if d["previously_paid_then_recouped"] else "."))

    if carc == "18":
        if k["exact_duplicate_of_paid_claim"]:
            orig = next(s["claim_id"] for s in k["same_patient_same_day_claims"]
                        if s["same_provider"] and s["same_cpt_as_denied_line"] and s["state"] == "PAID")
            return RulesResult("Billing - duplicate", "Billing", True, "VERIFY_DUPLICATE",
                               f"Duplicate of paid claim {orig}. Confirm and close with no write-off of revenue.",
                               [], 0.9, "no_loss", "none", f"Same patient, DOS, CPT and provider as paid claim {orig}.")
        return RulesResult("Billing - duplicate", "Billing", True, "VERIFY_DUPLICATE",
                           "Payer says duplicate but we cannot find a paid original; check claim status and appeal "
                           "if the original was not paid.", [], 0.6, "unknown", "appeal",
                           "Duplicate denial without a paid original in our data.", flags=["no_paid_original"])

    return RulesResult("Other", "Denials (appeal)", False, "MANUAL_REVIEW",
                       f"Unrecognised denial reason CARC {carc}; review the remit and payer portal.", [], 0.3,
                       "unknown", "appeal", "No rule for this CARC.", flags=["unknown_carc"])


def deadline_for(kind: str, ev: dict) -> date | None:
    if kind == "appeal":
        return date.fromisoformat(ev["deadlines"]["appeal_deadline"])
    if kind == "corrected":
        return date.fromisoformat(ev["deadlines"]["corrected_claim_deadline"])
    return None
