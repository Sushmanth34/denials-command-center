"""Identifier normalisation. Claim numbers arrive in several shapes across sources."""
from __future__ import annotations

import re

_FULL = re.compile(r"^GPP-(\d{4})-(\d{6})$")
_COMPACT = re.compile(r"^GPP(\d{4})(\d{6})$")
_BARE = re.compile(r"^(\d{6})$")
_FOREIGN = re.compile(r"^[A-Z]{2,5}-\d{4}-\d{6}$")


def normalize_claim_id(raw: str | None, default_year: str = "2026") -> tuple[str | None, str]:
    """Return (normalized_id, how). normalized_id is None when the value is not one of our claims.

    how: exact | compact | bare_number | foreign_prefix | unrecognized
    A bare 6-digit number is assumed to belong to the export year; that is an inference and the
    caller records it.
    """
    if raw is None:
        return None, "unrecognized"
    s = str(raw).strip().upper().replace(" ", "")
    if (m := _FULL.match(s)):
        return f"GPP-{m[1]}-{m[2]}", "exact"
    if (m := _COMPACT.match(s)):
        return f"GPP-{m[1]}-{m[2]}", "compact"
    if (m := _BARE.match(s)):
        return f"GPP-{default_year}-{m[1]}", "bare_number"
    if _FOREIGN.match(s):
        return None, "foreign_prefix"
    return None, "unrecognized"
