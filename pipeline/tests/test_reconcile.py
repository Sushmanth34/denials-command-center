"""Money-critical reconciliation behaviour on small synthetic files."""
from datetime import date
from decimal import Decimal

from dcc.reconcile import claim_summary, reconcile
from helpers import PAYERS, claim, interchange, payment

PAID = ("99233", "205.00", "127.10", [("CO", "45", "77.90")], [])


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_same_payment_in_two_files_is_counted_once(tmp_path):
    pay = payment("TRN1", "NS401", "127.10", "20260307", [("GPP-2026-000001", "1", "205.00", "127.10", "1", [PAID])])
    f1 = write(tmp_path, "a.835", interchange([pay], control="000000001", d="260401"))
    f2 = write(tmp_path, "a_resent.835", interchange([pay], control="000000009", d="260402"))
    m = reconcile({"GPP-2026-000001": claim()}, PAYERS, [f1, f2])
    assert len(m.payments) == 1
    assert claim_summary(m.claims["GPP-2026-000001"], m.lines)["payer_paid"] == Decimal("127.10")
    assert [e.kind for e in m.exceptions] == ["DUPLICATE_PAYMENT"]
    assert m.file_status["a_resent.835"]["status"] == "duplicate_content"
    r = m.recon[0]
    assert r["files_payment_total"] == Decimal("254.20")
    assert r["duplicate_payment_total"] == Decimal("127.10")
    assert r["difference"] == Decimal("0.00")


def test_same_trn_with_different_content_is_flagged_high(tmp_path):
    p1 = payment("TRN1", "NS401", "127.10", "20260307", [("GPP-2026-000001", "1", "205.00", "127.10", "1", [PAID])])
    p2 = p1.replace("127.10", "100.00").replace("77.90", "105.00")
    f1 = write(tmp_path, "a.835", interchange([p1], d="260401"))
    f2 = write(tmp_path, "b.835", interchange([p2], d="260402"))
    m = reconcile({"GPP-2026-000001": claim()}, PAYERS, [f1, f2])
    assert any(e.kind == "CONFLICTING_PAYMENT" and e.severity == "HIGH" for e in m.exceptions)


def test_recoupment_then_denial_is_an_open_denial_with_zero_net_paid(tmp_path):
    c = claim("GPP-2026-000230", payer="SMP12", lines=[("99305", "", "240.00")])
    paid = payment("P1", "SMP12", "148.80", "20260329", [
        ("000230", "1", "240.00", "148.80", "1", [("99305", "240.00", "148.80", [("CO", "45", "91.20")], [])])])
    rev = payment("P2", "SMP12", "-148.80", "20260826", [
        ("000230", "22", "-240.00", "-148.80", "1", [("99305", "-240.00", "-148.80", [("CO", "45", "-91.20")], [])]),
        ("000230", "4", "240.00", "0.00", "1", [("99305", "240.00", "0.00", [("CO", "197", "240.00")], ["N54"])]),
    ])
    m = reconcile({c.claim_id: c}, PAYERS, [write(tmp_path, "q1.835", interchange([paid])),
                                            write(tmp_path, "q3.835", interchange([rev], d="260918"))])
    s = claim_summary(c, m.lines)
    assert s["payer_paid"] == Decimal("0.00")
    assert s["claim_status"] == "DENIED"
    (d,) = m.denials
    assert d.is_open and d.carc == "197" and d.prior_paid_then_reversed
    assert d.denial_date == date(2026, 8, 26)
    assert d.appeal_deadline == date(2026, 10, 25)  # SMP12: 60 days from denial


def test_corrected_claim_with_new_modifier_resolves_bundling_denial(tmp_path):
    c = claim("GPP-2026-001077", lines=[("99232", "", "140.00"), ("31500", "", "260.00")])
    first = payment("P1", "NS401", "161.20", "20260307", [("GPP-2026-001077", "1", "400.00", "161.20", "1", [
        ("99232", "140.00", "0.00", [("CO", "97", "140.00")], ["M15"]),
        ("31500", "260.00", "161.20", [("CO", "45", "98.80")], [])])])
    corrected = payment("P2", "NS401", "248.00", "20260414", [("GPP2026001077", "1", "400.00", "248.00", "7", [
        ("99232:25", "140.00", "86.80", [("CO", "45", "53.20")], []),
        ("31500", "260.00", "161.20", [("CO", "45", "98.80")], [])])])
    m = reconcile({c.claim_id: c}, PAYERS, [write(tmp_path, "1.835", interchange([first])),
                                            write(tmp_path, "2.835", interchange([corrected], d="260501"))])
    (d,) = m.denials
    assert not d.is_open and d.resolution == "PAID_ON_CORRECTED_CLAIM"
    # E/M paid once; procedure paid on BOTH remits (no reversal of the original) -> cash as received,
    # and the second payment is flagged as a likely overpayment.
    assert m.lines[(c.claim_id, 1)].payer_paid == Decimal("86.80")
    assert m.lines[(c.claim_id, 2)].payer_paid == Decimal("322.40")
    assert claim_summary(c, m.lines)["claim_status"] == "PAID"
    (ov,) = [e for e in m.exceptions if e.kind == "POSSIBLE_OVERPAYMENT"]
    assert ov.amount == Decimal("161.20") and ov.claim_id == c.claim_id


def test_unmatched_and_foreign_claims_go_to_exceptions_and_still_reconcile(tmp_path):
    pay = payment("P1", "NS401", "213.90", "20260307", [
        ("GPP-2026-000001", "1", "205.00", "127.10", "1", [PAID]),
        ("BHC-2026-456493", "1", "140.00", "86.80", "1", [("99232", "140.00", "86.80", [("CO", "45", "53.20")], [])]),
    ])
    m = reconcile({"GPP-2026-000001": claim()}, PAYERS, [write(tmp_path, "a.835", interchange([pay]))])
    exc = [e for e in m.exceptions if e.kind == "UNMATCHED_REMIT_CLAIM"]
    assert len(exc) == 1 and exc[0].amount == Decimal("86.80") and exc[0].severity == "HIGH"
    r = m.recon[0]
    assert r["unmatched_claim_paid"] == Decimal("86.80")
    assert r["system_claim_paid"] == Decimal("127.10")
    assert r["difference"] == Decimal("0.00")


def test_out_of_balance_payment_is_reported(tmp_path):
    pay = payment("P1", "NS401", "999.99", "20260307", [("GPP-2026-000001", "1", "205.00", "127.10", "1", [PAID])])
    m = reconcile({"GPP-2026-000001": claim()}, PAYERS, [write(tmp_path, "a.835", interchange([pay]))])
    assert any(e.kind == "PAYMENT_OUT_OF_BALANCE" for e in m.exceptions)


def test_patient_responsibility_is_not_a_denial(tmp_path):
    pay = payment("P1", "CSA77", "94.24", "20260312", [("GPP-2026-000001", "1", "190.00", "94.24", "1", [
        ("99239", "190.00", "94.24", [("CO", "45", "72.20"), ("PR", "3", "23.56")], [])])])
    c = claim(payer="CSA77", lines=[("99239", "", "190.00")])
    m = reconcile({c.claim_id: c}, PAYERS, [write(tmp_path, "a.835", interchange([pay]))])
    assert m.denials == []
    assert claim_summary(c, m.lines)["patient_resp"] == Decimal("23.56")


def test_claim_with_no_remit_is_no_response_and_old_ones_are_flagged(tmp_path):
    c = claim(sub=date(2026, 3, 1))
    m = reconcile({c.claim_id: c}, PAYERS, [])
    assert claim_summary(c, m.lines)["claim_status"] == "NO_RESPONSE"
    assert [e.kind for e in m.exceptions] == ["NO_REMIT_RECEIVED"]
