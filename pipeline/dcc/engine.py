"""Combine rules + AI into one analysis per denial, then derive money buckets and the work queue."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from . import appeals
from .evidence import Indexes, build_evidence
from .llm import CachedAI, sanitize_packet, validate
from .loaders import TODAY
from .reconcile import Model, ZERO, money
from .rules import RECOVERY_PROBABILITY, classify, deadline_for

HIGH, MEDIUM = 0.85, 0.65
REVIEW_THRESHOLD = 0.70
SPECIALISTS = ["anjali", "karan", "priya", "rahul"]


def label(conf: float) -> str:
    return "HIGH" if conf >= HIGH else ("MEDIUM" if conf >= MEDIUM else "LOW")


@dataclass
class Analysis:
    denial_id: str
    engine: str
    root_cause: str
    owning_team: str
    preventable: bool
    action_type: str
    next_action: str
    policy_refs: list[str]
    appeal_draft: str | None
    confidence: float
    needs_review: bool
    review_reasons: list[str]
    rationale: str
    evidence: dict
    rules_result: dict
    ai_result: dict | None
    ai_model: str | None
    input_hash: str
    # money
    action_deadline: date | None
    days_left: int | None
    recoverability: str
    recoverability_reason: str
    recovery_probability: float
    expected_recovery: Decimal

    @property
    def confidence_label(self) -> str:
        return label(self.confidence)


def analyze_denials(model: Model, codes: dict, policies, worklog, ai: CachedAI) -> list[Analysis]:
    idx = Indexes(model)
    out = []
    for d in model.denials:
        ev = build_evidence(d, model, idx, codes, policies, worklog)
        rules = classify(ev).as_dict()
        reasons: list[str] = []
        ai_raw, ai_clean, h, err = None, None, "", None
        packet = sanitize_packet(ev)
        if d.is_open and ai.client is not None:
            ai_raw, h, err = ai.analyze(packet)
            if ai_raw is not None:
                ai_clean, problems = validate(ai_raw, packet)
                if problems:
                    reasons.append("AI output rejected: " + "; ".join(problems))
        if not h:
            from .llm import input_hash
            h = input_hash("rules", packet)

        final = dict(rules)
        conf = float(rules["confidence"])
        engine = "rules"
        if ai.client is None:
            engine = "rules (ai_disabled)"
        elif d.is_open and ai_clean is None:
            # AI is a second opinion. If it is down, the rules engine's own confidence decides what needs review
            # (the engine label shows it); rejected AI output is still surfaced because it may signal an attack.
            engine = "rules (ai_unavailable)" if err else "rules (ai_rejected)"
        elif ai_clean is not None:
            engine = "rules+ai"
            agree = (ai_clean["root_cause"] == rules["root_cause"]
                     and ai_clean["owning_team"] == rules["owning_team"]
                     and ai_clean["preventable"] == rules["preventable"])
            if agree:
                conf = min(0.99, max(conf, ai_clean["confidence"]) + 0.03)
                if ai_clean["action_type"] == rules["action_type"]:
                    final["next_action"] = ai_clean["next_action"] or rules["next_action"]
            elif rules["confidence"] >= 0.85:
                # grounded rule wins, but a disagreement always gets human eyes
                conf = min(conf, 0.6)
                reasons.append(f"AI disagrees: {ai_clean['root_cause']} / {ai_clean['owning_team']} / "
                               f"preventable={ai_clean['preventable']}")
            else:
                for f in ("root_cause", "owning_team", "preventable", "action_type", "next_action", "policy_refs"):
                    final[f] = ai_clean[f]
                conf = min(0.75, ai_clean["confidence"])
                reasons.append("Rules had no strong pattern; AI answer used")

        if any(w["note_flagged"] for w in ev["worklog"]):
            reasons.append("Worklog note on this claim looks like an injected instruction (ignored)")
        for flag in rules.get("flags", []):
            reasons.append(f"Rule flag: {flag}")
        if conf < REVIEW_THRESHOLD:
            reasons.append(f"Low confidence ({conf:.2f})")

        template = appeals.draft(ev, final)
        draft = template
        if ai_clean and ai_clean.get("appeal_draft") and final["action_type"] in appeals.DRAFTABLE \
                and set(ai_clean["policy_refs"]) >= set(final["policy_refs"]):
            draft = ai_clean["appeal_draft"]

        # ---- money / recoverability
        prob = RECOVERY_PROBABILITY.get(rules["probability_key"], 0.3)
        dl = deadline_for(rules["deadline_kind"], ev)
        days_left = (dl - TODAY).days if dl else None
        if not d.is_open:
            rec, why, prob = "RESOLVED", f"Paid after denial ({d.resolution})", 0.0
        elif rules["probability_key"] == "no_loss":
            rec, why, prob = "NO_LOSS", rules["rationale"], 0.0
        elif prob == 0:
            rec, why = "LOST", rules["rationale"]
        elif days_left is not None and days_left < 0:
            rec, why, prob = "LOST", f"{rules['deadline_kind'].title()} window closed on {dl.isoformat()}", 0.0
        elif prob < 0.25:
            rec, why = "UNLIKELY", rules.get("recoverability_note") or rules["rationale"]
        else:
            rec, why = "RECOVERABLE", f"{final['action_type'].replace('_', ' ').lower()} by {dl.isoformat() if dl else 'n/a'}"
        expected = money(d.expected_allowed * Decimal(str(prob))) if rec in ("RECOVERABLE", "UNLIKELY") else ZERO

        out.append(Analysis(
            denial_id=d.denial_id, engine=engine, root_cause=final["root_cause"], owning_team=final["owning_team"],
            preventable=bool(final["preventable"]), action_type=final["action_type"], next_action=final["next_action"],
            policy_refs=list(final["policy_refs"]), appeal_draft=draft, confidence=round(conf, 2),
            needs_review=bool(d.is_open and reasons), review_reasons=reasons if d.is_open else [],
            rationale=(ai_clean["rationale"] if ai_clean and final["root_cause"] == ai_clean["root_cause"]
                       and final["root_cause"] != rules["root_cause"] else rules["rationale"]),
            evidence=ev, rules_result=rules, ai_result={"raw": ai_raw, "valid": ai_clean is not None} if ai_raw else None,
            ai_model=ai.model if ai_raw else None, input_hash=h, action_deadline=dl, days_left=days_left,
            recoverability=rec, recoverability_reason=why, recovery_probability=prob, expected_recovery=expected))
    return out


# ------------------------------------------------------------------ work queue
def urgency(days_left: int | None) -> float:
    if days_left is None:
        return 1.0
    if days_left <= 7:
        return 3.0
    if days_left <= 14:
        return 2.5
    if days_left <= 30:
        return 1.75
    if days_left <= 60:
        return 1.25
    return 1.0


@dataclass
class WorkItemPlan:
    claim_id: str
    kind: str
    priority_score: Decimal
    priority_reason: str
    expected_recovery: Decimal
    at_stake: Decimal
    deadline: date | None
    days_left: int | None
    open_count: int


def plan_work_items(model: Model, analyses: list[Analysis]) -> list[WorkItemPlan]:
    """One work item per claim with an open denial, plus claims with no payer response past follow-up age.

    Priority = expected recovery ($ allowed x probability) x urgency(days to the action deadline).
    Lost / no-loss denials stay in the queue with score 0 so they still get closed out (write-off or
    duplicate confirmation), but they sink below anything that can still bring money in.
    """
    by_claim = defaultdict(list)
    dmap = {d.denial_id: d for d in model.denials}
    for a in analyses:
        if dmap[a.denial_id].is_open:
            by_claim[dmap[a.denial_id].claim_id].append(a)
    plans = []
    for cid, items in by_claim.items():
        exp = money(sum((a.expected_recovery for a in items), ZERO))
        stake = money(sum((dmap[a.denial_id].expected_allowed for a in items), ZERO))
        live = [a for a in items if a.recoverability in ("RECOVERABLE", "UNLIKELY") and a.action_deadline]
        deadline = min((a.action_deadline for a in live), default=None)
        days = (deadline - TODAY).days if deadline else None
        score = money(exp * Decimal(str(urgency(days))))
        lead = max(items, key=lambda a: (a.expected_recovery, a.denial_id))
        if exp > 0:
            reason = (f"${exp} expected (${stake} allowed x {lead.recovery_probability:.0%}); "
                      f"{lead.action_type.replace('_', ' ').lower()} due {deadline} ({days} days)")
        else:
            reason = f"Close out: {lead.recoverability.replace('_', ' ').lower()} - {lead.recoverability_reason}"
        plans.append(WorkItemPlan(cid, "DENIAL", score, reason[:400], exp, stake, deadline, days, len(items)))

    for c in model.claims.values():
        states = {model.lines[(c.claim_id, l.line_no)].status for l in c.lines}
        if states != {"NO_RESPONSE"}:
            continue
        age = (TODAY - c.submitted_date).days
        if age <= 45:
            continue
        payer = model.payers[c.payer_id]
        tf = c.dos + timedelta(days=payer.timely_filing_days)
        days = (tf - TODAY).days
        stake = money(sum((model.allowed_table.get((c.payer_id, l.cpt), l.charge * Decimal("0.6")) for l in c.lines), ZERO))
        if days >= 0:
            prob, why = Decimal("0.9"), f"resubmit/check status before timely filing {tf} ({days} days)"
        else:
            prob, why = Decimal("0.6"), (f"timely filing passed {tf}; submitted on time ({c.submitted_date}) - "
                                         f"resubmit with proof of timely filing")
        exp = money(stake * prob)
        score = money(exp * Decimal(str(urgency(days if days >= 0 else 30))))
        plans.append(WorkItemPlan(c.claim_id, "NO_RESPONSE", score,
                                  f"No remit {age} days after submission; ${exp} expected; {why}", exp, stake,
                                  tf if days >= 0 else None, days if days >= 0 else None, 0))
    plans.sort(key=lambda p: (-p.priority_score, p.days_left if p.days_left is not None else 10_000, p.claim_id))
    return plans


def initial_assignments(plans: list[WorkItemPlan], worklog_owner: dict[str, str]) -> dict[str, str]:
    """Owner from the team's own worklog when known; otherwise balance expected $ across specialists.
    Deterministic, so a re-run assigns the same way."""
    load = {s: Decimal("0") for s in SPECIALISTS}
    out = {}
    for p in plans:
        o = worklog_owner.get(p.claim_id)
        if o in load:
            out[p.claim_id] = o
            load[o] += p.priority_score
    for p in plans:
        if p.claim_id in out:
            continue
        s = min(SPECIALISTS, key=lambda s: (load[s], s))
        out[p.claim_id] = s
        load[s] += p.priority_score + Decimal("1")  # +1 spreads zero-value close-outs too
    return out
