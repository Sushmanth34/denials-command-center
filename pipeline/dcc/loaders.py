"""Loaders for the non-835 inputs. Every loader returns clean records plus a list of issues."""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .ids import normalize_claim_id

TODAY = date(2026, 9, 30)  # per the data pack README


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ reference files
@dataclass
class Payer:
    payer_id: str
    name: str
    timely_filing_days: int
    appeal_window_days: int
    corrected_window_days: int


def load_payers(path: Path) -> dict[str, Payer]:
    out = {}
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            p = Payer(
                payer_id=r["payer_id"].strip(),
                name=r["payer"].strip(),
                timely_filing_days=int(r["timely_filing_days_from_dos"]),
                appeal_window_days=int(r["appeal_window_days_from_denial"]),
                corrected_window_days=int(r["corrected_claim_window_days_from_denial"]),
            )
            out[p.payer_id] = p
    return out


def load_codes(carc_path: Path, group_path: Path) -> list[tuple[str, str, str]]:
    rows = []
    with carc_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((r["type"].strip().upper(), r["code"].strip(), r["description"].strip()))
    with group_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(("GROUP", r["group"].strip(), r["meaning"].strip()))
    return rows


# ------------------------------------------------------------------ payer policies
@dataclass
class PolicySection:
    section_id: str
    document: str
    title: str
    payer_ids: list[str]
    effective: date | None
    number: str
    text: str


_POLICY_PAYER_PREFIX = {"NSHP": ["NS401"], "CSA": ["CSA77"], "SMP": ["SMP12"], "MPPO": ["MRD55"]}
ALL_PAYERS = ["NS401", "CSA77", "SMP12", "MRD55"]


def load_policies(folder: Path) -> list[PolicySection]:
    sections: list[PolicySection] = []
    for path in sorted(folder.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        stem = path.stem
        lines = [l.rstrip() for l in text.splitlines()]
        titles = [l.lstrip("#").strip() for l in lines if l.startswith("#")]
        title = " - ".join(titles) if titles else stem
        lower = text.lower()
        prefix = stem.split("_")[0]
        if prefix == "ALL" or "all four payers" in lower or "all network payers" in lower:
            payers = list(ALL_PAYERS)
        else:
            payers = _POLICY_PAYER_PREFIX.get(prefix, [])
        eff = None
        m = re.search(r"Effective:\s*(\d{4}-\d{2}-\d{2})", text)
        if m:
            eff = date.fromisoformat(m[1])
        for l in lines:
            m = re.match(r"^(\d+)\.\s+(.*)$", l.strip())
            if m:
                sections.append(PolicySection(
                    section_id=f"{stem}#{m[1]}", document=path.name, title=title,
                    payer_ids=payers, effective=eff, number=m[1], text=m[2].strip()))
    return sections


# ------------------------------------------------------------------ claims export
@dataclass
class ClaimLine:
    line_no: int
    cpt: str
    modifier: str
    units: int
    charge: Decimal


@dataclass
class Claim:
    claim_id: str
    patient_first: str
    patient_last: str
    patient_dob: date
    member_id: str
    payer_name: str
    payer_id: str
    dos: date
    submitted_date: date
    rendering_npi: str
    rendering_provider: str
    facility: str
    pos: str
    coder_id: str
    prebill_reviewed: bool
    auth_number: str
    dx_codes: list[str]
    lines: list[ClaimLine] = field(default_factory=list)

    @property
    def total_charge(self) -> Decimal:
        return sum((l.charge for l in self.lines), Decimal("0"))


HEADER_FIELDS = ["patient_first", "patient_last", "patient_dob", "member_id", "payer", "payer_id", "dos",
                 "submitted_date", "rendering_npi", "rendering_provider", "facility", "pos", "coder_id",
                 "prebill_reviewed", "auth_number", "dx1", "dx2", "dx3", "dx4"]


def load_claims(path: Path) -> tuple[dict[str, Claim], list[dict]]:
    claims: dict[str, Claim] = {}
    issues: list[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        for rowno, r in enumerate(csv.DictReader(f), start=2):
            r = {k: (v or "").strip() for k, v in r.items()}
            cid, how = normalize_claim_id(r.get("claim_id"))
            if cid is None:
                issues.append(dict(kind="CLAIM_ROW_BAD_ID", ref=f"claims_export.csv row {rowno}",
                                   message=f"Unrecognised claim id {r.get('claim_id')!r}"))
                continue
            try:
                line = ClaimLine(int(r["line_no"]), r["cpt"], r["modifier"], int(r["units"] or 1),
                                 Decimal(r["charge"]))
                dos = date.fromisoformat(r["dos"])
                sub = date.fromisoformat(r["submitted_date"])
                dob = date.fromisoformat(r["patient_dob"])
            except (ValueError, InvalidOperation, KeyError) as e:
                issues.append(dict(kind="CLAIM_ROW_UNPARSEABLE", ref=f"claims_export.csv row {rowno}",
                                   claim_id=cid, message=f"Row could not be parsed: {e}"))
                continue
            if cid not in claims:
                claims[cid] = Claim(
                    claim_id=cid, patient_first=r["patient_first"], patient_last=r["patient_last"],
                    patient_dob=dob, member_id=r["member_id"], payer_name=r["payer"], payer_id=r["payer_id"],
                    dos=dos, submitted_date=sub, rendering_npi=r["rendering_npi"],
                    rendering_provider=r["rendering_provider"], facility=r["facility"], pos=r["pos"],
                    coder_id=r["coder_id"], prebill_reviewed=r["prebill_reviewed"].upper() == "Y",
                    auth_number=r["auth_number"],
                    dx_codes=[r[k] for k in ("dx1", "dx2", "dx3", "dx4") if r.get(k)])
            else:
                c = claims[cid]
                hdr_now = (c.member_id, c.payer_id, c.dos, c.rendering_npi, c.facility)
                hdr_row = (r["member_id"], r["payer_id"], dos, r["rendering_npi"], r["facility"])
                if hdr_now != hdr_row:
                    issues.append(dict(kind="CLAIM_HEADER_CONFLICT", ref=f"claims_export.csv row {rowno}",
                                       claim_id=cid, message="Claim lines disagree on header fields"))
            c = claims[cid]
            if any(l.line_no == line.line_no for l in c.lines):
                issues.append(dict(kind="CLAIM_LINE_DUPLICATE", ref=f"claims_export.csv row {rowno}",
                                   claim_id=cid, message=f"Duplicate line {line.line_no}; kept first"))
                continue
            c.lines.append(line)
    for c in claims.values():
        c.lines.sort(key=lambda l: l.line_no)
    return claims, issues


# ------------------------------------------------------------------ denials worklog (Excel)
STATUS_MAP = {
    "open": "Open", "in progress": "In Progress", "wip": "In Progress",
    "pending w/ payer": "Pending Payer", "pending with payer": "Pending Payer",
    "resolved": "Resolved", "done": "Resolved", "closed": "Resolved",
}
PAYER_ALIASES = {
    "northstar health plan": "NS401", "nshp": "NS401", "ns401": "NS401", "northstar": "NS401",
    "coastal senior advantage": "CSA77", "csa": "CSA77", "csa77": "CSA77",
    "sunshine medicaid partners": "SMP12", "smp": "SMP12", "smp12": "SMP12",
    "meridian ppo": "MRD55", "mppo": "MRD55", "mrd55": "MRD55", "meridian": "MRD55",
}
_INJECTION_PATTERNS = re.compile(
    r"(ignore (all )?(previous|prior) instructions|system (note|prompt)|assistant|you are now|"
    r"disregard|mark (this|all|every)|classify every|<\s*/?\s*(system|instructions?)\s*>)",
    re.IGNORECASE)


def looks_like_injection(text: str) -> bool:
    return bool(text) and bool(_INJECTION_PATTERNS.search(text))


@dataclass
class WorklogEntry:
    row_no: int
    raw: dict
    claim_id: str | None
    id_how: str
    logged_date: date | None
    date_issue: str | None
    payer_id: str | None
    amount: Decimal | None
    note: str
    note_flagged: bool
    owner: str | None
    status: str | None
    duplicate_of: int | None = None


def _parse_logged_date(value, lo: date | None, hi: date) -> tuple[date | None, str | None]:
    """Parse the free-form 'Date Logged' cell.

    Day/month order is ambiguous for values like 06/08/2026. We resolve it with evidence: a log
    date cannot be in the future and should not precede the claim's first denial (lo). If both
    readings remain plausible we keep none and report it, rather than guess.
    """
    if value is None or value == "":
        return None, "missing date"
    if isinstance(value, datetime):
        return value.date(), None
    if isinstance(value, date):
        return value, None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date(), None
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if not m:
        return None, f"unrecognised date {s!r}"
    a, b, y = int(m[1]), int(m[2]), int(m[3])
    candidates = []
    for mm, dd, label in ((a, b, "MM/DD"), (b, a, "DD/MM")):
        try:
            candidates.append((date(y, mm, dd), label))
        except ValueError:
            pass
    if not candidates:
        return None, f"invalid date {s!r}"
    if len(candidates) == 1 or candidates[0][0] == candidates[-1][0]:
        return candidates[0][0], None
    plausible = [(d, l) for d, l in candidates if d <= hi and (lo is None or d >= lo)]
    if len(plausible) == 1:
        return plausible[0][0], f"ambiguous {s!r}; resolved as {plausible[0][1]} using denial/today bounds"
    return None, f"ambiguous day/month order {s!r}; could not resolve"


def _amount(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def load_worklog(path: Path, first_denial_dates: dict[str, date] | None = None) -> list[WorklogEntry]:
    import openpyxl  # local import keeps module import light

    first_denial_dates = first_denial_dates or {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    entries: list[WorklogEntry] = []
    seen: dict[tuple, int] = {}
    for i, row in enumerate(rows[1:], start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        raw = {header[j]: ("" if v is None else (v.isoformat() if isinstance(v, (date, datetime)) else str(v)))
               for j, v in enumerate(row) if j < len(header)}
        cid, how = normalize_claim_id(raw.get("Claim #"))
        lo = first_denial_dates.get(cid) if cid else None
        d, issue = _parse_logged_date(row[header.index("Date Logged")] if "Date Logged" in header else None,
                                      lo, TODAY)
        payer_raw = raw.get("Payer", "").strip().lower()
        status_raw = raw.get("Status", "").strip().lower()
        owner = raw.get("Owner", "").strip()
        note = raw.get("Notes", "").strip()
        e = WorklogEntry(
            row_no=i, raw=raw, claim_id=cid, id_how=how, logged_date=d, date_issue=issue,
            payer_id=PAYER_ALIASES.get(payer_raw), amount=_amount(raw.get("Amt")), note=note,
            note_flagged=looks_like_injection(note), owner=owner.title() if owner else None,
            status=STATUS_MAP.get(status_raw))
        key = tuple(sorted((k, v.strip()) for k, v in raw.items()))
        if key in seen:
            e.duplicate_of = seen[key]
        else:
            seen[key] = i
        entries.append(e)
    return entries
