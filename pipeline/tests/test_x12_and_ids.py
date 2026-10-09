from decimal import Decimal

import pytest

from dcc.ids import normalize_claim_id
from dcc.x12_835 import X12ParseError, parse_835
from helpers import interchange, isa, payment


@pytest.mark.parametrize("raw,expected,how", [
    ("GPP-2026-000123", "GPP-2026-000123", "exact"),
    (" gpp-2026-000123 ", "GPP-2026-000123", "exact"),
    ("GPP2026000123", "GPP-2026-000123", "compact"),
    ("000123", "GPP-2026-000123", "bare_number"),
    ("BHC-2026-456493", None, "foreign_prefix"),
    ("hello", None, "unrecognized"),
    (None, None, "unrecognized"),
])
def test_normalize_claim_id(raw, expected, how):
    assert normalize_claim_id(raw) == (expected, how)


def test_parse_basic_payment_and_adjustments():
    txt = interchange([payment("T1", "NS401", "86.80", "20260307", [
        ("GPP-2026-000001", "1", "140.00", "86.80", "1",
         [("99232", "140.00", "86.80", [("CO", "45", "53.20")], [])])])])
    ic = parse_835(txt, "f.835")
    assert ic.issues == []
    p = ic.payments[0]
    assert (p.trn, p.payer_id, p.payment_amount) == ("T1", "NS401", Decimal("86.80"))
    line = p.claims[0].lines[0]
    assert line.cpt == "99232" and line.adjustments[0].carc == "45" and line.adjustments[0].amount == Decimal("53.20")


def test_delimiters_come_from_isa_and_newlines_are_tolerated():
    body = payment("T9", "NS401", "10.00", "20260307",
                   [("X1", "1", "10.00", "10.00", "1", [("99231", "10.00", "10.00", [], [])])], el="|", term="\n")
    txt = isa(el="|", term="\n") + "GS|HP|A|B|20260415|1200|1|X|005010X221A1\n" + body + "IEA|1|1\n"
    ic = parse_835(txt, "pipe.835")
    assert ic.payments[0].claims[0].raw_claim_id == "X1"
    assert ic.payments[0].payment_amount == Decimal("10.00")


def test_reversal_amounts_are_negative_and_multiple_cas_triplets():
    seg = payment("T2", "SMP12", "0.00", "20260826", [
        ("000230", "22", "-240.00", "-148.80", "1", [("99305", "-240.00", "-148.80", [("CO", "45", "-91.20")], [])]),
    ])
    seg = seg.replace("CAS*CO*45*-91.20", "CAS*CO*45*-91.20**97*0")
    ic = parse_835(interchange([seg]), "r.835")
    line = ic.payments[0].claims[0].lines[0]
    assert line.paid == Decimal("-148.80")
    assert [a.carc for a in line.adjustments] == ["45", "97"]


def test_segment_count_mismatch_is_reported_not_ignored():
    seg = payment("T3", "NS401", "1.00", "20260307", [("X", "1", "1.00", "1.00", "1", [("99231", "1.00", "1.00", [], [])])])
    seg = seg.replace("SE*", "SE*9")  # corrupt the count
    ic = parse_835(interchange([seg]), "bad.835")
    assert any("SE declares" in i for i in ic.issues)


def test_rejects_non_x12():
    with pytest.raises(X12ParseError):
        parse_835("not an edi file", "x.835")
