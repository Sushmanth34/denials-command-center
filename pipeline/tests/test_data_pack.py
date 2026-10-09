"""Regression tests on the real data pack: the numbers the manager sees must not drift silently."""
import csv
from decimal import Decimal
from pathlib import Path

import pytest

from dcc.llm import CachedAI
from dcc.run import build, fingerprint

DATA = Path(__file__).resolve().parents[2] / "data"
pytestmark = pytest.mark.skipif(not (DATA / "claims_export.csv").exists(), reason="data pack not present")


@pytest.fixture(scope="module")
def result():
    return build(DATA, CachedAI(None, {}))


def test_rebuild_is_deterministic(result):
    again = build(DATA, CachedAI(None, {}))
    assert fingerprint(result["data"], result["plans"]) == fingerprint(again["data"], again["plans"])


def test_every_payer_reconciles_to_the_cent(result):
    for r in result["data"]["recon_payer"]:
        assert r["difference"] == Decimal("0.00"), r


def test_resent_file_is_not_double_counted(result):
    files = {f["file_name"]: f for f in result["data"]["source_file"]}
    assert files["remits/era_2026Q2_resent_0719.835"]["status"] == "duplicate_content"
    assert len(result["data"]["remit_payment"]) == 25
    total_dup = sum(r["duplicate_payment_total"] for r in result["data"]["recon_payer"])
    assert total_dup == Decimal("47461.62")


def test_nothing_is_silently_dropped(result):
    kinds = {e["kind"] for e in result["data"]["exception_item"]}
    assert {"UNMATCHED_REMIT_CLAIM", "DUPLICATE_PAYMENT", "WORKLOG_SUSPICIOUS_TEXT", "WORKLOG_DUPLICATE_ROW",
            "NO_REMIT_RECEIVED", "POSSIBLE_OVERPAYMENT"} <= kinds
    # every remit claim is either applied to a claim or has an exception
    unmatched = {e["reference"] for e in result["data"]["exception_item"] if e["kind"] == "UNMATCHED_REMIT_CLAIM"}
    for rc in result["data"]["remit_claim"]:
        assert rc["claim_id"] or rc["remit_claim_key"] in unmatched


def test_open_denial_totals(result):
    open_d = [d for d in result["data"]["denial"] if d["is_open"]]
    assert len(open_d) == 161
    assert sum(d["denied_amount"] for d in open_d) == Decimal("31145.00")


def test_smp_recoupments_are_payer_errors(result):
    a = {x["denial_id"].split(":")[0]: x for x in result["data"]["denial_analysis"]}
    for cid in ["GPP-2026-000230", "GPP-2026-000655", "GPP-2026-001665", "GPP-2026-001893"]:
        assert a[cid]["root_cause"] == "Payer error"
        assert "SMP_SNF-AUTH-2026#3" in a[cid]["policy_refs"]


def test_injected_worklog_note_did_not_change_anything(result):
    wl = [w for w in result["data"]["worklog_entry"] if w["note_flagged"]]
    assert len(wl) == 1
    cid = wl[0]["claim_id"]
    if cid:
        assert result["initial"].get(cid, {}).get("status") != "Resolved"
        payer = next(c["payer_id"] for c in result["data"]["claim"] if c["claim_id"] == cid)
        roots = {a["root_cause"] for a, d in zip(result["data"]["denial_analysis"], result["data"]["denial"])
                 if d["is_open"] and next(c["payer_id"] for c in result["data"]["claim"] if c["claim_id"] == d["claim_id"]) == payer}
        assert roots != {"Payer error"}


def test_labeled_sample_accuracy_floor(result):
    """Guardrail, not a target: fail loudly if a change makes the rules engine regress."""
    a = {}
    for x in result["data"]["denial_analysis"]:
        a.setdefault(x["denial_id"].split(":")[0], x)
    rows = list(csv.DictReader((DATA / "labeled_denials_sample.csv").open()))
    hits = sum(a[r["claim_id"]]["root_cause"] == r["root_cause_category"] for r in rows if r["claim_id"] in a)
    assert hits / len(rows) >= 0.9
