"""Minimal, defensive ASC X12 835 (5010) parser.

Only the segments needed for payment/denial reconciliation are interpreted. Anything we do not
understand is reported as an issue rather than silently ignored. The file is treated as untrusted
input: no field is ever evaluated, and delimiters are read from the ISA header rather than assumed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

MAX_FILE_BYTES = 50 * 1024 * 1024


@dataclass
class Adjustment:
    group: str
    carc: str
    amount: Decimal


@dataclass
class ServiceLine:
    seq: int
    cpt: str
    modifiers: list[str]
    charge: Decimal
    paid: Decimal
    units: str = ""
    service_date: date | None = None
    allowed: Decimal | None = None
    adjustments: list[Adjustment] = field(default_factory=list)
    rarcs: list[str] = field(default_factory=list)


@dataclass
class RemitClaim:
    seq: int
    raw_claim_id: str
    status_code: str
    charge: Decimal
    paid: Decimal
    patient_resp: Decimal
    payer_icn: str
    facility_code: str
    freq_code: str
    patient_name: str = ""
    patient_member_id: str = ""
    rendering_npi: str = ""
    rendering_name: str = ""
    received_date: date | None = None
    claim_adjustments: list[Adjustment] = field(default_factory=list)
    claim_rarcs: list[str] = field(default_factory=list)
    lines: list[ServiceLine] = field(default_factory=list)


@dataclass
class Payment:
    """One ST/SE transaction = one check/EFT from one payer."""
    st_control: str
    isa_control: str
    payment_amount: Decimal = Decimal("0")
    payment_method: str = ""
    payment_date: date | None = None
    trn: str = ""
    payer_id: str = ""
    payer_name: str = ""
    payee_npi: str = ""
    plb_total: Decimal = Decimal("0")
    claims: list[RemitClaim] = field(default_factory=list)
    declared_segment_count: int | None = None
    actual_segment_count: int = 0


@dataclass
class Interchange:
    file_name: str
    isa_control: str
    isa_date: str
    sender: str
    receiver: str
    payments: list[Payment] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


class X12ParseError(ValueError):
    pass


def _dec(value: str, issues: list[str], ctx: str) -> Decimal:
    if value is None or value.strip() == "":
        return Decimal("0")
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        issues.append(f"{ctx}: non-numeric amount {value!r}")
        return Decimal("0")


def _date(value: str, issues: list[str], ctx: str) -> date | None:
    v = (value or "").strip()
    if len(v) != 8 or not v.isdigit():
        if v:
            issues.append(f"{ctx}: bad date {v!r}")
        return None
    try:
        return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    except ValueError:
        issues.append(f"{ctx}: invalid date {v!r}")
        return None


def _el(seg: list[str], i: int) -> str:
    return seg[i] if i < len(seg) else ""


def split_segments(text: str) -> tuple[list[list[str]], str]:
    """Return segments (as element lists) and the component separator, using ISA-declared delimiters."""
    text = text.lstrip("﻿\r\n ")
    if not text.startswith("ISA") or len(text) < 106:
        raise X12ParseError("file does not start with a valid ISA segment")
    element_sep = text[3]
    component_sep = text[104]
    segment_term = text[105]
    if element_sep.isalnum() or segment_term.isalnum():
        raise X12ParseError("invalid delimiters in ISA header")
    segments = []
    for raw in text.split(segment_term):
        raw = raw.strip("\r\n ")
        if raw:
            segments.append(raw.split(element_sep))
    return segments, component_sep


def parse_835(text: str, file_name: str) -> Interchange:
    if len(text) > MAX_FILE_BYTES:
        raise X12ParseError("file too large")
    segments, comp = split_segments(text)
    isa = segments[0]
    ic = Interchange(
        file_name=file_name,
        isa_control=_el(isa, 13).strip(),
        isa_date=_el(isa, 9).strip(),
        sender=_el(isa, 6).strip(),
        receiver=_el(isa, 8).strip(),
    )
    issues = ic.issues
    pay: Payment | None = None
    clp: RemitClaim | None = None
    svc: ServiceLine | None = None

    for idx, seg in enumerate(segments):
        tag = seg[0].strip()
        ctx = f"{file_name} seg#{idx + 1} {tag}"
        if pay is not None:
            pay.actual_segment_count += 1

        if tag == "ST":
            if _el(seg, 1) != "835":
                issues.append(f"{ctx}: transaction set {_el(seg, 1)!r} is not an 835; skipped")
                pay = None
                continue
            pay = Payment(st_control=_el(seg, 2), isa_control=ic.isa_control)
            pay.actual_segment_count = 1
            ic.payments.append(pay)
            clp = svc = None
        elif pay is None:
            if tag not in ("ISA", "GS", "GE", "IEA"):
                issues.append(f"{ctx}: segment outside of a transaction; skipped")
            continue
        elif tag == "BPR":
            pay.payment_amount = _dec(_el(seg, 2), issues, ctx)
            pay.payment_method = _el(seg, 4)
            pay.payment_date = _date(_el(seg, 16), issues, ctx)
        elif tag == "TRN":
            pay.trn = _el(seg, 2).strip()
        elif tag == "REF" and _el(seg, 1) == "2U":
            pay.payer_id = _el(seg, 2).strip()
        elif tag == "N1" and _el(seg, 1) == "PR":
            pay.payer_name = _el(seg, 2).strip()
        elif tag == "N1" and _el(seg, 1) == "PE":
            pay.payee_npi = _el(seg, 4).strip()
        elif tag == "CLP":
            clp = RemitClaim(
                seq=len(pay.claims) + 1,
                raw_claim_id=_el(seg, 1).strip(),
                status_code=_el(seg, 2).strip(),
                charge=_dec(_el(seg, 3), issues, ctx),
                paid=_dec(_el(seg, 4), issues, ctx),
                patient_resp=_dec(_el(seg, 5), issues, ctx),
                payer_icn=_el(seg, 7).strip(),
                facility_code=_el(seg, 8).strip(),
                freq_code=_el(seg, 9).strip(),
            )
            pay.claims.append(clp)
            svc = None
        elif tag == "NM1" and clp is not None and svc is None:
            qual = _el(seg, 1)
            name = " ".join(p for p in (_el(seg, 4), _el(seg, 3)) if p).strip()
            if qual == "QC":
                clp.patient_name = name
                clp.patient_member_id = _el(seg, 9).strip()
            elif qual == "82":
                clp.rendering_name = name
                clp.rendering_npi = _el(seg, 9).strip()
        elif tag == "SVC" and clp is not None:
            composite = _el(seg, 1).split(comp)
            svc = ServiceLine(
                seq=len(clp.lines) + 1,
                cpt=_el(composite, 1).strip(),
                modifiers=[m.strip() for m in composite[2:] if m.strip()],
                charge=_dec(_el(seg, 2), issues, ctx),
                paid=_dec(_el(seg, 3), issues, ctx),
                units=_el(seg, 5).strip(),
            )
            if _el(composite, 0) not in ("HC", "AD", "ER", "WK", "NU"):
                issues.append(f"{ctx}: unexpected procedure qualifier {_el(composite, 0)!r}")
            clp.lines.append(svc)
        elif tag == "DTM":
            qual = _el(seg, 1)
            d = _date(_el(seg, 2), issues, ctx)
            if svc is not None and qual in ("472", "150", "151"):
                if qual in ("472", "150"):
                    svc.service_date = d
            elif clp is not None and qual == "050":
                clp.received_date = d
        elif tag == "CAS":
            target = svc.adjustments if svc is not None else (clp.claim_adjustments if clp else None)
            if target is None:
                issues.append(f"{ctx}: CAS outside claim; skipped")
                continue
            group = _el(seg, 1).strip()
            # up to 6 (reason, amount, quantity) triplets
            for i in range(2, min(len(seg), 20), 3):
                code = _el(seg, i).strip()
                if not code:
                    continue
                target.append(Adjustment(group, code, _dec(_el(seg, i + 1), issues, ctx)))
        elif tag == "AMT" and svc is not None and _el(seg, 1) == "B6":
            svc.allowed = _dec(_el(seg, 2), issues, ctx)
        elif tag == "LQ" and _el(seg, 1) == "HE":
            code = _el(seg, 2).strip()
            if svc is not None:
                svc.rarcs.append(code)
            elif clp is not None:
                clp.claim_rarcs.append(code)
        elif tag == "PLB":
            # provider-level adjustments: pairs of (reason, amount) starting at element 3
            for i in range(3, len(seg), 2):
                if _el(seg, i + 1):
                    pay.plb_total += _dec(_el(seg, i + 1), issues, ctx)
        elif tag == "SE":
            try:
                pay.declared_segment_count = int(_el(seg, 1))
            except ValueError:
                issues.append(f"{ctx}: bad SE count")
            if pay.declared_segment_count != pay.actual_segment_count:
                issues.append(
                    f"{ctx}: SE declares {pay.declared_segment_count} segments, found {pay.actual_segment_count}"
                )
            pay = clp = svc = None
        elif tag in ("LX", "N3", "N4", "REF", "N1", "PER", "DTM", "TS3", "TS2", "MIA", "MOA", "QTY", "AMT", "NM1", "LQ"):
            continue
        # unknown segments are tolerated (835 has many optional loops) but not interpreted

    for p in ic.payments:
        if not p.trn:
            issues.append(f"{file_name} ST {p.st_control}: missing TRN trace number")
        if p.payment_date is None:
            issues.append(f"{file_name} ST {p.st_control}: missing BPR payment date")
    return ic
