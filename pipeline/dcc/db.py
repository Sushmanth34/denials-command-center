"""Persistence. Derived tables are rebuilt atomically; app tables are upserted without touching human work."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

DERIVED_TABLES = [
    "prevention_rule", "recon_payer", "exception_item", "worklog_entry", "denial_analysis", "denial",
    "claim_event", "remit_line", "remit_claim", "remit_payment", "claim_line", "claim", "policy_section",
    "code_reference", "payer", "source_file",
]
SYSTEM_ACTOR = "system:pipeline"
JSONB_COLS = {
    "remit_line": {"adjustments"}, "claim_event": {"details"}, "worklog_entry": {"raw"},
    "denial_analysis": {"evidence", "rules_result", "ai_result"}, "exception_item": {"details"},
    "prevention_rule": {"definition"},
}


def dsn() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://dcc:dcc@localhost:5432/dcc")


def connect() -> psycopg.Connection:
    return psycopg.connect(dsn(), autocommit=False)


def apply_schema(conn: psycopg.Connection, schema_path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(424242)")
        cur.execute(schema_path.read_text())
    conn.commit()


def _j(v):
    return Jsonb(json.loads(json.dumps(v, default=str)))


def hash_password(password: str, iterations: int = 100_000) -> str:
    """PBKDF2-SHA256, same format verified by the .NET API."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, 32)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def seed_users(cur, users: list[tuple[str, str, str]], password: str) -> None:
    for username, display, role in users:
        cur.execute("SELECT 1 FROM dcc.app_user WHERE username=%s", (username,))
        if cur.fetchone():
            continue
        cur.execute("INSERT INTO dcc.app_user(username, display_name, role, password_hash) VALUES (%s,%s,%s,%s)",
                    (username, display, role, hash_password(password)))
        audit(cur, SYSTEM_ACTOR, "USER_CREATED", "app_user", username, None, {"role": role, "display_name": display})


def audit(cur, actor, action, entity_type, entity_id, before, after, reason=None):
    cur.execute(
        "INSERT INTO dcc.audit_log(actor, action, entity_type, entity_id, before, after, reason) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (actor, action, entity_type, entity_id, _j(before) if before is not None else None,
         _j(after) if after is not None else None, reason))


def rebuild_derived(cur, data: dict) -> None:
    cur.execute("TRUNCATE " + ", ".join(f"dcc.{t}" for t in DERIVED_TABLES))

    def ins(table: str, rows: list[dict]):
        if not rows:
            return
        cols = list(rows[0].keys())
        sql = f"INSERT INTO dcc.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        jcols = JSONB_COLS.get(table, set())
        cur.executemany(sql, [[(_j(r[c]) if r[c] is not None else None) if c in jcols else r[c] for c in cols]
                              for r in rows])

    for t in ["source_file", "payer", "code_reference", "policy_section", "claim", "claim_line", "remit_payment",
              "remit_claim", "remit_line", "claim_event", "denial", "denial_analysis", "worklog_entry",
              "exception_item", "recon_payer", "prevention_rule"]:
        ins(t, data.get(t, []))


WORK_SYSTEM_FIELDS = ["kind", "priority_score", "priority_rank", "priority_reason", "expected_recovery", "at_stake",
                      "deadline", "days_left", "system_state"]


def _norm(v):
    if isinstance(v, Decimal):
        return str(v.quantize(Decimal("0.01")))
    if isinstance(v, date):
        return v.isoformat()
    return v


def sync_work_items(cur, plans: list[dict], initial: dict[str, dict], claim_paid: dict[str, bool]) -> dict:
    """Insert new items, refresh system fields on existing ones, close items whose denial went away.
    Human-owned fields (status, assignee, notes) are only set on insert. No-op updates write nothing,
    so a re-run on the same files leaves work items and the audit log unchanged."""
    stats = {"created": 0, "refreshed": 0, "closed": 0, "unchanged": 0}
    cur.execute("SELECT claim_id, status, assignee, " + ", ".join(WORK_SYSTEM_FIELDS) + " FROM dcc.work_item")
    cols = [d.name for d in cur.description]
    existing = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    planned = set()
    for p in plans:
        cid = p["claim_id"]
        planned.add(cid)
        sys_vals = {k: p[k] for k in WORK_SYSTEM_FIELDS}
        if cid not in existing:
            init = initial.get(cid, {})
            row = dict(claim_id=cid, status=init.get("status", "Open"), assignee=init.get("assignee"), **sys_vals)
            cur.execute(f"INSERT INTO dcc.work_item ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))})",
                        list(row.values()))
            audit(cur, SYSTEM_ACTOR, "WORK_ITEM_CREATED", "work_item", cid, None,
                  {k: _norm(v) for k, v in row.items()}, init.get("reason"))
            stats["created"] += 1
            continue
        cur_row = existing[cid]
        changed = {k: v for k, v in sys_vals.items() if _norm(cur_row[k]) != _norm(v)}
        if not changed:
            stats["unchanged"] += 1
            continue
        sets = ", ".join(f"{k}=%s" for k in changed) + ", version=version+1, updated_at=now()"
        cur.execute(f"UPDATE dcc.work_item SET {sets} WHERE claim_id=%s", [*changed.values(), cid])
        audit(cur, SYSTEM_ACTOR, "WORK_ITEM_REFRESHED", "work_item", cid,
              {k: _norm(cur_row[k]) for k in changed}, {k: _norm(v) for k, v in changed.items()},
              "Recomputed from new input files")
        stats["refreshed"] += 1

    for cid, row in existing.items():
        if cid in planned or row["system_state"] != "ACTIVE":
            continue
        new_state = "CLOSED_BY_PAYMENT" if claim_paid.get(cid) else "NO_LONGER_DENIED"
        new_status = row["status"] if row["status"] in ("Resolved", "Written Off") else "Resolved"
        cur.execute("UPDATE dcc.work_item SET system_state=%s, status=%s, priority_score=0, expected_recovery=0, "
                    "version=version+1, updated_at=now() WHERE claim_id=%s", (new_state, new_status, cid))
        audit(cur, SYSTEM_ACTOR, "WORK_ITEM_AUTO_CLOSED", "work_item", cid,
              {"system_state": row["system_state"], "status": row["status"]},
              {"system_state": new_state, "status": new_status}, "Latest remittance no longer shows an open denial")
        stats["closed"] += 1
    return stats


def import_worklog_notes(cur, notes: list[dict]) -> int:
    n = 0
    for note in notes:
        cur.execute("SELECT 1 FROM dcc.work_item WHERE claim_id=%s", (note["claim_id"],))
        if not cur.fetchone():
            continue
        cur.execute("INSERT INTO dcc.work_note(claim_id, author, text, source, source_ref, created_at) "
                    "VALUES (%s,%s,%s,'worklog_import',%s,%s) ON CONFLICT (source_ref) DO NOTHING",
                    (note["claim_id"], note["author"], note["text"], note["source_ref"], note["created_at"]))
        n += cur.rowcount
    return n


def load_ai_cache(cur) -> dict[str, dict]:
    cur.execute("SELECT input_hash, response FROM dcc.ai_cache")
    return {h: r for h, r in cur.fetchall()}


def save_ai_cache(cur, entries: dict[str, dict], model: str, prompt_version: str) -> None:
    for h, resp in entries.items():
        cur.execute("INSERT INTO dcc.ai_cache(input_hash, model, prompt_version, response) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (input_hash) DO NOTHING", (h, model, prompt_version, _j(resp)))
