"""Pre-bill prevention rules.

Rules are declared as JSON (the same document we export for the pre-bill review team) and evaluated
by a small interpreter. The backtest runs the exported JSON itself against every claim we billed, so
the numbers shown in the prevention view are exactly what the exported rules would have done.

Condition language (deliberately tiny):
  {"all": [cond, ...]} | {"any": [cond, ...]} | {"not": cond}
  {"field": <claim field>, "op": <op>, "value": <v>}            ops: eq, in, contains, any_startswith,
                                                                    is_empty, not_empty, gte, lte, gt,
                                                                    startswith, not_startswith
  {"lines_any": cond}                                            some claim line satisfies cond
  {"other_claims_exist": {"match": [fields...], "where": cond}}  another earlier-submitted claim with the
                                                                    same values for `match` satisfying `where`
  {"reference_contains": {"reference": name, "keys": [fields...]}}  tuple of claim fields is in a reference list
Claim fields: payer_id, pos, dos, submitted_date, dx_codes, auth_number, rendering_npi, member_id,
  days_dos_to_submission, payer_timely_filing_days, tf_buffer_days (= payer_timely_filing_days - 30)
Line fields: cpt, modifier
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .evidence import EM_CODES, SNF_INITIAL, SUBSEQUENT_HOSPITAL
from .reconcile import Model, ZERO, money

PROCEDURE_CODES = ["31500", "36556", "93010", "94002"]


def rule_definitions(model: Model) -> list[dict]:
    # reference list: provider/payer pairs we have evidence are NOT enrolled (B7 denials, never paid).
    paid_pairs = set()
    b7_pairs = set()
    denied_claims = defaultdict(set)
    for d in model.denials:
        denied_claims[d.claim_id].add(d.carc)
    for c in model.claims.values():
        st = {model.lines[(c.claim_id, l.line_no)].status for l in c.lines}
        if st == {"PAID"}:
            paid_pairs.add((c.rendering_npi, c.payer_id))
        if "B7" in denied_claims[c.claim_id]:
            b7_pairs.add((c.rendering_npi, c.payer_id))
    not_enrolled = sorted(p for p in b7_pairs if p not in paid_pairs)
    names = {c.rendering_npi: c.rendering_provider for c in model.claims.values()}

    return [
        {
            "rule_id": "PB-DX-EXCL1-I10-I11", "name": "I10 reported with I11.x (Excludes1)", "action": "BLOCK",
            "applies_to": {"payer_ids": ["*"]},
            "condition": {"all": [{"field": "dx_codes", "op": "contains", "value": "I10"},
                                  {"field": "dx_codes", "op": "any_startswith", "value": "I11"}]},
            "message": "I10 and I11.x cannot be reported together; remove I10 and keep the I11.x code.",
            "policy_refs": ["MPPO_DX-EXCL-03#2", "MPPO_DX-EXCL-03#3"], "targets_carc": ["11"], "owner": "Coding",
        },
        {
            "rule_id": "PB-MOD25-EM-WITH-PROCEDURE", "name": "E/M with same-day procedure needs modifier 25",
            "action": "HOLD", "applies_to": {"payer_ids": ["*"]},
            "condition": {"all": [
                {"lines_any": {"field": "cpt", "op": "in", "value": PROCEDURE_CODES}},
                {"lines_any": {"all": [{"field": "cpt", "op": "in", "value": sorted(EM_CODES)},
                                       {"not": {"field": "modifier", "op": "contains", "value": "25"}}]}}]},
            "message": "E/M billed with a procedure on the same claim without modifier 25 will be bundled. "
                       "Add 25 if the note documents a significant, separately identifiable E/M.",
            "policy_refs": ["ALL_PAYERS_MOD25-2026#1", "ALL_PAYERS_MOD25-2026#2"], "targets_carc": ["97"],
            "owner": "Coding",
        },
        {
            "rule_id": "PB-SMP-SNF-AUTH", "name": "Sunshine Medicaid initial NF visit requires SNF admission auth",
            "action": "HOLD", "applies_to": {"payer_ids": ["SMP12"]},
            "condition": {"all": [{"field": "payer_id", "op": "eq", "value": "SMP12"},
                                  {"field": "dos", "op": "gte", "value": "2026-04-01"},
                                  {"lines_any": {"field": "cpt", "op": "in", "value": sorted(SNF_INITIAL)}},
                                  {"field": "auth_number", "op": "is_empty"}]},
            "message": "Initial nursing facility care (99304-99306) for DOS on/after 2026-04-01 must carry the SNF "
                       "admission authorization number. Get it from the facility before billing.",
            "policy_refs": ["SMP_SNF-AUTH-2026#2", "SMP_SNF-AUTH-2026#4"], "targets_carc": ["197"],
            "owner": "Front desk / Authorization",
        },
        {
            "rule_id": "PB-ENROLLMENT", "name": "Rendering provider not enrolled with payer",
            "action": "HOLD", "applies_to": {"payer_ids": ["*"]},
            "condition": {"reference_contains": {"reference": "providers_not_enrolled",
                                                 "keys": ["rendering_npi", "payer_id"]}},
            "message": "Rendering provider is not enrolled with this payer. Hold the claim until credentialing "
                       "confirms an enrollment effective date on or before the DOS (enrollment is not retroactive).",
            "policy_refs": ["CSA_PROVIDER-ENROLLMENT#1", "CSA_PROVIDER-ENROLLMENT#3"], "targets_carc": ["B7"],
            "owner": "Credentialing",
            "reference_data": {"providers_not_enrolled": [
                {"rendering_npi": n, "payer_id": p, "provider": names.get(n, "")} for n, p in not_enrolled]},
            "note": "Reference list derived from B7 history with no paid claims. In production feed it from the "
                    "credentialing roster (active enrollments by NPI x payer with effective dates).",
        },
        {
            "rule_id": "PB-NSHP-SAME-DAY-SUBSEQUENT", "name": "Second subsequent hospital visit same patient same day",
            "action": "HOLD", "applies_to": {"payer_ids": ["NS401"]},
            "condition": {"all": [
                {"field": "payer_id", "op": "eq", "value": "NS401"},
                {"lines_any": {"field": "cpt", "op": "in", "value": sorted(SUBSEQUENT_HOSPITAL)}},
                {"other_claims_exist": {"match": ["member_id", "dos"],
                                        "where": {"lines_any": {"field": "cpt", "op": "in",
                                                                "value": sorted(SUBSEQUENT_HOSPITAL)}}}}]},
            "message": "Northstar pays one subsequent hospital visit per patient per day per specialty group. Bill a "
                       "second visit only with modifier 25, a distinct diagnosis and a note showing a significant change.",
            "policy_refs": ["NSHP_HOSP-FREQ-07#1", "NSHP_HOSP-FREQ-07#3"], "targets_carc": ["151"], "owner": "Coding",
        },
        {
            "rule_id": "PB-DUPLICATE", "name": "Exact duplicate of an already billed service", "action": "BLOCK",
            "applies_to": {"payer_ids": ["*"]},
            "condition": {"other_claims_exist": {"match": ["member_id", "dos", "rendering_npi", "cpt_set"],
                                                 "where": {"field": "payer_id", "op": "not_empty"}}},
            "message": "Same patient, DOS, provider and CPT already billed. Do not resubmit as a new claim; use a "
                       "corrected claim (frequency 7) if something changed.",
            "policy_refs": [], "targets_carc": ["18"], "owner": "Billing",
        },
        {
            "rule_id": "PB-TIMELY-FILING", "name": "Approaching / past timely filing limit", "action": "ESCALATE",
            "applies_to": {"payer_ids": ["*"]},
            "condition": {"field": "days_dos_to_submission", "op": "gt", "value_from": "tf_buffer_days"},
            "message": "Claim is within 30 days of (or past) the payer's timely filing limit. Escalate same day; if "
                       "already past, it will deny CARC 29 and cannot be appealed.",
            "policy_refs": [], "targets_carc": ["29"], "owner": "Billing",
            "note": "Most effective as a daily aging alert on unbilled encounters (DOS age > limit - 30), not only at "
                    "submission. Limits per payer from payer_rules.csv.",
        },
        {
            "rule_id": "PB-AUTH-FORMAT", "name": "Authorization number belongs to a different payer", "action": "WARN",
            "applies_to": {"payer_ids": ["*"]},
            "condition": {"all": [{"field": "auth_number", "op": "not_empty"},
                                  {"field": "auth_prefix_matches_payer", "op": "eq", "value": False}]},
            "message": "Authorization number format does not match the payer (e.g. a Sunshine 'SMPA' auth on a "
                       "Coastal claim). Verify the patient's payer and the authorization.",
            "policy_refs": [], "targets_carc": ["197"], "owner": "Front desk / Authorization",
        },
        {
            "rule_id": "PB-ELIGIBILITY", "name": "Real-time eligibility check (270/271) for the DOS",
            "action": "HOLD", "applies_to": {"payer_ids": ["*"]},
            "condition": {"field": "eligibility_verified", "op": "eq", "value": False},
            "message": "Verify active coverage on the date of service before billing; if terminated, find the "
                       "active payer.",
            "policy_refs": [], "targets_carc": ["27"], "owner": "Front desk / Eligibility",
            "note": "Needs an eligibility feed we do not have; backtest counts the CARC 27 denials it targets.",
            "requires_external_data": True,
        },
    ]


# ------------------------------------------------------------------ interpreter
AUTH_PREFIX = {"SMP12": "SMPA"}


def claim_context(c, payer) -> dict:
    return {
        "claim_id": c.claim_id, "payer_id": c.payer_id, "pos": c.pos, "dos": c.dos.isoformat(),
        "submitted_date": c.submitted_date.isoformat(), "dx_codes": [d.upper() for d in c.dx_codes],
        "auth_number": c.auth_number or "", "rendering_npi": c.rendering_npi, "member_id": c.member_id,
        "days_dos_to_submission": (c.submitted_date - c.dos).days,
        "payer_timely_filing_days": payer.timely_filing_days, "tf_buffer_days": payer.timely_filing_days - 30,
        "cpt_set": "|".join(sorted(l.cpt for l in c.lines)),
        "auth_prefix_matches_payer": (not c.auth_number) or c.auth_number.upper().startswith(
            {"SMP12": "SMPA", "CSA77": "CSAA", "NS401": "NSA", "MRD55": "MRDA"}.get(c.payer_id, "")),
        "eligibility_verified": None,  # unknown without an eligibility feed
        "lines": [{"cpt": l.cpt, "modifier": l.modifier or ""} for l in c.lines],
    }


def evaluate(cond: dict, ctx: dict, env: dict) -> bool:
    if "all" in cond:
        return all(evaluate(x, ctx, env) for x in cond["all"])
    if "any" in cond:
        return any(evaluate(x, ctx, env) for x in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], ctx, env)
    if "lines_any" in cond:
        return any(evaluate(cond["lines_any"], l, env) for l in ctx.get("lines", []))
    if "other_claims_exist" in cond:
        spec = cond["other_claims_exist"]
        key = tuple(ctx[f] for f in spec["match"])
        for other in env["by_key"](tuple(spec["match"])).get(key, []):
            if other["claim_id"] == ctx["claim_id"]:
                continue
            # only claims already submitted when this one was (what a pre-bill check could see)
            if (other["submitted_date"], other["claim_id"]) >= (ctx["submitted_date"], ctx["claim_id"]):
                continue
            if evaluate(spec["where"], other, env):
                return True
        return False
    if "reference_contains" in cond:
        spec = cond["reference_contains"]
        ref = env["references"].get(spec["reference"], [])
        key = tuple(ctx[k] for k in spec["keys"])
        return any(tuple(r[k] for k in spec["keys"]) == key for r in ref)
    field, op = cond["field"], cond["op"]
    v = ctx.get(field)
    target = ctx.get(cond["value_from"]) if "value_from" in cond else cond.get("value")
    if op == "eq":
        return v == target
    if op == "in":
        return v in target
    if op == "contains":
        return target in (v or [] if isinstance(v, list) else (v or ""))
    if op == "any_startswith":
        return any(str(x).startswith(target) for x in (v or []))
    if op == "startswith":
        return str(v or "").startswith(target)
    if op == "not_startswith":
        return not str(v or "").startswith(target)
    if op == "is_empty":
        return v in (None, "", [])
    if op == "not_empty":
        return v not in (None, "", [])
    if op in ("gte", "lte", "gt"):
        if v is None or target is None:
            return False
        return {"gte": v >= target, "lte": v <= target, "gt": v > target}[op]
    raise ValueError(f"unknown op {op}")


def backtest(model: Model, rules: list[dict], analyses_by_denial: dict) -> list[dict]:
    ctxs = {cid: claim_context(c, model.payers[c.payer_id]) for cid, c in model.claims.items()}
    index_cache: dict[tuple, dict] = {}

    def by_key(fields: tuple) -> dict:
        if fields not in index_cache:
            m = defaultdict(list)
            for ctx in ctxs.values():
                m[tuple(ctx[f] for f in fields)].append(ctx)
            index_cache[fields] = m
        return index_cache[fields]

    denials_by_claim = defaultdict(list)
    for d in model.denials:
        denials_by_claim[d.claim_id].append(d)
    responded = {cid for cid, c in model.claims.items()
                 if any(model.lines[(cid, l.line_no)].adjudications for l in c.lines)}

    out = []
    for order, rule in enumerate(rules):
        env = {"by_key": by_key, "references": rule.get("reference_data", {})}
        targets = set(rule["targets_carc"])
        if rule.get("requires_external_data"):
            caught = [d for d in model.denials if d.carc in targets]
            flagged = {d.claim_id for d in caught}
            fp = 0
        else:
            flagged = {cid for cid, ctx in ctxs.items() if evaluate(rule["condition"], ctx, env)}
            caught = [d for cid in flagged for d in denials_by_claim[cid] if d.carc in targets]
            fp = sum(1 for cid in flagged if cid in responded
                     and not any(d.carc in targets for d in denials_by_claim[cid]))
        targeted_total = [d for d in model.denials if d.carc in targets]
        rule_out = dict(rule)
        rule_out["backtest"] = {
            "claims_flagged": len(flagged),
            "denials_caught": len(caught),
            "denials_targeted_total": len(targeted_total),
            "denied_amount_caught": str(money(sum((d.denied_amount for d in caught), ZERO))),
            "expected_allowed_caught": str(money(sum((d.expected_allowed for d in caught), ZERO))),
            "false_positives": fp,
            # share of flagged claims (with a payer response) that really were denied for this reason
            "precision": (round(len({d.claim_id for d in caught}) / (len({d.claim_id for d in caught}) + fp), 2)
                          if caught or fp else None),
            "claims_awaiting_payer_response": len([cid for cid in flagged if cid not in responded]),
            "estimated": bool(rule.get("requires_external_data")),
            "period": "claims with DOS Jan-Aug 2026",
        }
        out.append((order, rule_out, len(flagged), len(caught), fp))
    return [dict(sort_order=o, **r) for o, r, *_ in out]
