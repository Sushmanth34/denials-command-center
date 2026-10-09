"""Black-box tests for the .NET API (roles, audit trail, concurrency, validation, human work survives re-runs).

    API_URL=http://localhost:5080 DATABASE_URL=postgresql://dcc:dcc@localhost:5432/dcc pytest tests/api

They mutate data, so run them against a disposable database (docker compose run creates one).
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

API = os.environ.get("API_URL")
DB = os.environ.get("DATABASE_URL")
PW = os.environ.get("DCC_DEMO_PASSWORD", "demo123")
pytestmark = pytest.mark.skipif(not API, reason="API_URL not set")
ROOT = Path(__file__).resolve().parents[2]


def call(method, path, token=None, body=None, raw=False):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read()
            return r.status, (data if raw else (json.loads(data) if data else None))
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data) if data else None
        except ValueError:
            return e.code, data


def login(user, pw=PW):
    s, b = call("POST", "/api/auth/login", body={"username": user, "password": pw})
    assert s == 200, b
    return b["access_token"]


@pytest.fixture(scope="module")
def mgr():
    return login("manager")


@pytest.fixture(scope="module")
def tokens():
    return {u: login(u) for u in ["anjali", "karan", "priya", "rahul"]}


def audit_for(mgr, entity_id):
    s, claim = call("GET", f"/api/claims/{entity_id}", mgr)
    assert s == 200
    return claim["audit"]


def test_bad_password_and_anonymous_are_rejected():
    assert call("POST", "/api/auth/login", body={"username": "manager", "password": "nope"})[0] == 401
    assert call("GET", "/api/worklist")[0] == 401


def test_specialist_sees_only_own_queue(tokens):
    s, items = call("GET", "/api/worklist?assignee=karan", tokens["priya"])
    assert s == 200 and items and all(i["assignee"] == "priya" for i in items)


def test_specialist_cannot_open_someone_elses_claim(mgr, tokens):
    _, all_items = call("GET", "/api/worklist", mgr)
    other = next(i for i in all_items if i["assignee"] != "priya")
    own = next(i for i in all_items if i["assignee"] == "priya")
    assert call("GET", f"/api/claims/{other['claim_id']}", tokens["priya"])[0] == 403
    s, detail = call("GET", f"/api/claims/{own['claim_id']}", tokens["priya"])
    assert s == 200 and detail["events"] and detail["claim"]["claim_id"] == own["claim_id"]


def test_manager_only_endpoints(tokens):
    for path in ["/api/analytics/summary", "/api/exceptions", "/api/reconciliation", "/api/users", "/api/audit"]:
        assert call("GET", path, tokens["rahul"])[0] == 403, path


def test_status_change_is_audited_with_before_and_after_and_versioned(mgr, tokens):
    _, items = call("GET", "/api/worklist", tokens["anjali"])
    item = next(i for i in items if i["status"] == "Open")
    cid, v = item["claim_id"], item["version"]
    s, r = call("PATCH", f"/api/work-items/{cid}", tokens["anjali"], {"status": "In Progress", "version": v, "reason": "called payer"})
    assert s == 200 and r["version"] == v + 1
    # stale version -> conflict, nothing written
    s, _ = call("PATCH", f"/api/work-items/{cid}", tokens["anjali"], {"status": "Pending Payer", "version": v})
    assert s == 409
    a = [x for x in audit_for(mgr, cid) if x["action"] == "STATUS_CHANGED"][-1]
    assert a["actor"] == "anjali" and a["before"] == {"status": "Open"} and a["after"] == {"status": "In Progress"}
    assert a["reason"] == "called payer" and a["at"]


def test_invalid_status_and_specialist_write_off_are_refused(tokens):
    _, items = call("GET", "/api/worklist", tokens["karan"])
    i = items[0]
    assert call("PATCH", f"/api/work-items/{i['claim_id']}", tokens["karan"], {"status": "Deleted", "version": i["version"]})[0] == 400
    assert call("PATCH", f"/api/work-items/{i['claim_id']}", tokens["karan"], {"status": "Written Off", "version": i["version"]})[0] == 403


def test_specialist_cannot_touch_other_queue(mgr, tokens):
    _, all_items = call("GET", "/api/worklist", mgr)
    other = next(i for i in all_items if i["assignee"] != "rahul")
    assert call("PATCH", f"/api/work-items/{other['claim_id']}", tokens["rahul"],
                {"status": "In Progress", "version": other["version"]})[0] == 403
    assert call("POST", f"/api/work-items/{other['claim_id']}/notes", tokens["rahul"], {"text": "hi"})[0] == 403
    assert call("POST", f"/api/work-items/{other['claim_id']}/assign", tokens["rahul"],
                {"assignee": "rahul", "version": other["version"]})[0] == 403


def test_manager_reassigns_and_note_is_audited(mgr, tokens):
    _, items = call("GET", "/api/worklist?assignee=priya", mgr)
    i = items[-1]
    s, r = call("POST", f"/api/work-items/{i['claim_id']}/assign", mgr, {"assignee": "rahul", "version": i["version"], "reason": "balance"})
    assert s == 200
    assert call("POST", f"/api/work-items/{i['claim_id']}/assign", mgr, {"assignee": "nobody", "version": r["version"]})[0] == 400
    s, _ = call("POST", f"/api/work-items/{i['claim_id']}/notes", tokens["rahul"], {"text": "Requested records from facility"})
    assert s == 201
    acts = [(a["action"], a["actor"]) for a in audit_for(mgr, i["claim_id"])]
    assert ("REASSIGNED", "manager") in acts and ("NOTE_ADDED", "rahul") in acts
    assert call("POST", f"/api/work-items/{i['claim_id']}/notes", tokens["rahul"], {"text": "  "})[0] == 400


def test_review_override_validates_taxonomy_and_changes_reporting(mgr):
    s, queue = call("GET", "/api/review-queue", mgr)
    assert s == 200 and queue, "expected at least one item in review"
    d = queue[0]
    assert call("POST", f"/api/analyses/{d['denial_id']}/review", mgr,
                {"decision": "Overridden", "root_cause": "Made up", "owning_team": "Coding", "preventable": True})[0] == 400
    s, _ = call("POST", f"/api/analyses/{d['denial_id']}/review", mgr,
                {"decision": "Approved", "comment": "Checked remit; note was an injection attempt"})
    assert s == 200
    _, queue2 = call("GET", "/api/review-queue", mgr)
    assert d["denial_id"] not in {x["denial_id"] for x in queue2}
    assert any(a["action"] == "ANALYSIS_APPROVED" for a in audit_for(mgr, d["claim_id"]))


def test_breakdown_dimension_is_whitelisted(mgr):
    assert call("GET", "/api/analytics/breakdown?by=payer", mgr)[0] == 200
    assert call("GET", "/api/analytics/breakdown?by=payer_id;drop%20table%20dcc.claim", mgr)[0] == 400


def test_rules_export_is_machine_readable(tokens):
    s, raw = call("GET", "/api/prevention/rules.json", tokens["karan"], raw=True)
    doc = json.loads(raw)
    assert s == 200 and doc["rules"] and all({"rule_id", "condition", "action", "backtest"} <= set(r) for r in doc["rules"])


@pytest.mark.skipif(not DB, reason="DATABASE_URL not set")
def test_audit_log_is_append_only():
    import psycopg
    with psycopg.connect(DB) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE dcc.audit_log SET actor='x'")
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM dcc.audit_log")


@pytest.mark.skipif(not DB, reason="DATABASE_URL not set")
def test_pipeline_rerun_preserves_human_work(mgr, tokens):
    _, items = call("GET", "/api/worklist", tokens["karan"])
    i = items[0]
    s, r = call("PATCH", f"/api/work-items/{i['claim_id']}", tokens["karan"],
                {"status": "Pending Payer" if i["status"] != "Pending Payer" else "In Progress", "version": i["version"]})
    assert s == 200
    _, before = call("GET", f"/api/claims/{i['claim_id']}", mgr)
    n_audit = len(call("GET", "/api/audit?limit=1000", mgr)[1])
    env = dict(os.environ, DCC_AI_MODE="off")
    subprocess.run([sys.executable, "-m", "dcc.run", "--ai", "off"], cwd=ROOT / "pipeline", env=env, check=True,
                   capture_output=True)
    _, after = call("GET", f"/api/claims/{i['claim_id']}", mgr)
    assert after["work_item"]["status"] == before["work_item"]["status"]
    assert after["work_item"]["assignee"] == before["work_item"]["assignee"]
    assert len(call("GET", "/api/audit?limit=1000", mgr)[1]) == n_audit   # re-run wrote no audit rows
