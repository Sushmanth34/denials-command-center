"""Builders for synthetic 835s and claims used in tests."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from dcc.loaders import Claim, ClaimLine, Payer

PAYERS = {
    "NS401": Payer("NS401", "Northstar Health Plan", 180, 180, 180),
    "SMP12": Payer("SMP12", "Sunshine Medicaid Partners", 120, 60, 60),
    "MRD55": Payer("MRD55", "Meridian PPO", 90, 90, 90),
    "CSA77": Payer("CSA77", "Coastal Senior Advantage", 365, 120, 120),
}


def isa(control="000000001", d="260415", el="*", comp=":", term="~"):
    seg = (f"ISA{el}00{el}          {el}00{el}          {el}ZZ{el}CLEARHOUSE01   {el}ZZ{el}AQKODE-GPP     {el}"
           f"{d}{el}1200{el}^{el}00501{el}{control}{el}0{el}P{el}{comp}")
    assert len(seg) == 105, len(seg)
    return seg + term


def payment(trn, payer, amount, pay_date, claims, st="0001", el="*", term="~"):
    """claims: list of (claim_id, status, charge, paid, freq, [(proc, charge, paid, [(grp, carc, amt)], [rarc])])"""
    segs = [f"ST*835*{st}*005010X221A1", f"BPR*I*{amount}*C*ACH*CCP*01*1*DA*1*1{payer}**01*1*DA*1*{pay_date}",
            f"TRN*1*{trn}*1{payer}", f"DTM*405*{pay_date}", f"N1*PR*{payer} PLAN", f"REF*2U*{payer}",
            "N1*PE*GULFVIEW*XX*1932145067", "LX*1"]
    for cid, status, charge, paid, freq, lines in claims:
        segs += [f"CLP*{cid}*{status}*{charge}*{paid}**12*ICN{cid}*11*{freq}", "NM1*QC*1*DOE*JANE****MI*M1",
                 "NM1*82*1*SMITH*ANN****XX*111", "DTM*050*20260110"]
        for proc, lc, lp, cas, rarcs in lines:
            segs += [f"SVC*HC:{proc}*{lc}*{lp}**1", "DTM*472*20260105"]
            for g, c, a in cas:
                segs.append(f"CAS*{g}*{c}*{a}")
            for r in rarcs:
                segs.append(f"LQ*HE*{r}")
    segs.append(f"SE*{len(segs) + 1}*{st}")
    return term.join(s.replace("*", el) for s in segs) + term


def interchange(payments_text, control="000000001", d="260415"):
    return isa(control, d) + "GS*HP*A*B*20260415*1200*1*X*005010X221A1~" + "".join(payments_text) + "GE*1*1~IEA*1*" + control + "~"


def claim(cid="GPP-2026-000001", payer="NS401", dos=date(2026, 1, 5), sub=date(2026, 1, 8), lines=None, dx=None,
          npi="111", member="M1", auth=""):
    c = Claim(cid, "Jane", "Doe", date(1940, 1, 1), member, PAYERS[payer].name, payer, dos, sub, npi, "Ann Smith",
              "General Hospital", "21", "C01", True, auth, dx or ["I48.91"])
    for i, (cpt, mod, charge) in enumerate(lines or [("99233", "", "205.00")], start=1):
        c.lines.append(ClaimLine(i, cpt, mod, 1, Decimal(charge)))
    return c
