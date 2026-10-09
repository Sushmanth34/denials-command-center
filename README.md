# Denials Command Center

Answers the practice manager's three Monday questions for Gulfview Physician Partners:

1. **How much money is stuck in denials, and how much can we still recover?** Monday report: the money ribbon, recoverable / unlikely / lost / no loss, all reconciled to the 835 files.
2. **Why are we being denied, and which should never have left the building?** Root cause, owning team and preventability for every denial, plus pre-bill rules backtested on all Jan–Aug claims and exported as JSON.
3. **What should each person work on today?** A per-specialist queue ranked by expected dollars and deadline, a claim page with the full timeline, AI-drafted appeals that cite the payer's own policy, and an audit trail for every change.

Read [`docs/memo.md`](docs/memo.md) first (one page). The AI evaluation is in [`docs/ai_evaluation.md`](docs/ai_evaluation.md).

## Run it

```bash
cp -r /path/to/AQSoft_data_pack/* data/       # claims_export.csv, remits/, payer_policies/, ... (kept out of git: PHI)
cp .env.example .env                           # optional: add ANTHROPIC_API_KEY
docker compose up --build
```

Open http://localhost:8080 and sign in as **manager** (Manager) or **anjali / karan / priya / rahul** (Denials Specialists). The password is `DCC_DEMO_PASSWORD`, `demo123` by default.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Enables the AI second opinion and drafting. Without it the system runs on the rules engine (degraded mode) and says so in the UI. |
| `DCC_AI_MODE` | `auto` | `off` forces rules only. |
| `DCC_AI_MODEL` | `claude-sonnet-5-5` | Model id. |
| `DCC_DEMO_PASSWORD` | `demo123` | Password for the seeded users (first run only). |
| `POSTGRES_PASSWORD` | `dcc` | Database password. |

Re-load the data at any time (idempotent): `docker compose run --rm pipeline`.

### Without Docker

```bash
# Postgres 16 running locally, database dcc / user dcc
export DATABASE_URL=postgresql://dcc:dcc@localhost:5432/dcc
cd pipeline && pip install -r requirements.txt && python -m dcc.run          # load + analyse
cd api/Dcc.Api && dotnet run --urls http://127.0.0.1:5080                    # .NET 8 API
cd web && npm ci && npm run dev                                              # http://localhost:5173
```

### Tests

```bash
cd pipeline && python -m pytest                    # 48 tests: 835 parsing, reconciliation money, deadlines, rules, AI safety, idempotency
API_URL=http://127.0.0.1:5080 DATABASE_URL=... python -m pytest tests/api    # 13 black-box API tests (mutates data: use a scratch DB)
cd pipeline && python -m dcc.evaluate [--ai auto]  # AI evaluation against labeled_denials_sample.csv
```

## Architecture

```
 data/ (CSV, XLSX, 835, policy .md)          untrusted input
        │
        ▼
 pipeline/  Python ─ parse ─ reconcile ─ evidence ─ rules ─(LLM, optional)─ prioritise ─ backtest rules
        │   one transaction; derived tables rebuilt, app tables upserted, output fingerprint recorded
        ▼
 PostgreSQL  schema dcc: claims, remits, timeline, denials, analyses, exceptions, recon, rules
             + app tables: work_item, work_note, analysis_review, app_user, audit_log (append-only trigger)
        ▲
        │ parameterised SQL, JSON built by Postgres
 api/  .NET 8 minimal API ─ bearer-token auth, Manager / Specialist policies, audit in the same transaction
        ▲
 web/  React + TypeScript (Vite) ─ queue, claim timeline, Monday report, review queue, prevention, reconciliation
```

**Why this split.** The money logic (835 semantics, reversals, deadlines, rules) lives in one Python package with 48 tests and runs as a batch. The API stays thin: it serves the read models and owns the human workflow (status, notes, reassignment, reviews), with every write audited inside the same transaction. The brief allows Python for data and AI components.

## Key decisions

**Reconciliation (B)**
- **Payment identity is the 835 TRN trace number**, not the file. `era_2026Q2_resent_0719.835` repeats nine payments ($47,461.62) under a new ISA envelope; they load once and are listed as `DUPLICATE_PAYMENT`. If the same TRN arrived with different content, it would raise `CONFLICTING_PAYMENT` (HIGH).
- **Claim ids are normalised** across `GPP-2026-000123`, `GPP2026000123` and `000123`. `BHC-…` claims are another client's; their $260.40 is reported as unapplied cash.
- **Line state is the last non-reversal adjudication.** Money is the sum of all adjudications, with reversals (status 22) netting out. That handles recoupments, then re-denials, then corrected claims (frequency 7) where the modifier changed, so lines match on CPT rather than CPT+modifier.
- **Every claim balances to the cent per payer.** Files total, minus re-sent duplicates, equals unique payments; that equals cash applied to our claims plus cash for claims that aren't ours. The Reconciliation page shows the difference: $0.00 for every payer.
- **Nothing is dropped silently.** 84 exceptions are recorded with reasons, including:
  - unmatched remit claims
  - line and claim balance failures
  - double payments with no reversal
  - claims with no response after 45 days
  - worklog rows that are duplicated, have ambiguous dates, reference unknown claims, or contradict the remits (e.g. marked resolved but still unpaid)
  - the prompt-injection note
- **Idempotent.** Derived tables are rebuilt deterministically in one transaction. Work items are upserted, so human fields (status, assignee, notes) are only set on insert, and a no-op run writes nothing. `pipeline_run` stores input and output fingerprints: the same files give the same fingerprint, which is tested.
- **Ambiguous worklog dates** (`06/08/2026`) are resolved only when exactly one reading falls between the claim's first denial and today. Otherwise they're left empty and reported, never guessed.

**Money definitions** (shown on every screen)
- *Billed* is the charge the payer denied.
- *Allowed* is what the payer would actually pay if the denial is overturned: the median allowed amount for that payer and CPT from paid lines. Using billed charges would overstate the money: about 38% of them would never be paid even if every denial were overturned.
- *Expected* is allowed × recovery probability, counted only while the action window is open. The probabilities are explicit, conservative assumptions in `pipeline/dcc/rules.py` (`RECOVERY_PROBABILITY`), used to rank work, never to hide it.
- **Recoverable**: the window is open and there's a concrete fix.
- **Unlikely**: the window is open but a policy blocks recovery (enrollment isn't retroactive).
- **Lost**: the window has closed, or the claim was filed past the timely filing limit.
- **No loss**: a duplicate of a claim that was already paid.

**Queue priority (D)**
- Priority = expected dollars × urgency, where urgency is 3× when the deadline is ≤7 days away, 2.5× at ≤14, 1.75× at ≤30, 1.25× at ≤60, and 1× beyond that.
- The deadline is the one that applies to the recommended action: the appeal window or the corrected-claim window from `payer_rules.csv`, counted from the denial date.
- Items with nothing left to collect still appear, with score 0, so someone closes them out.
- Claims with no payer response after 45 days are queued too, timed against the timely filing date.
- Initial owner comes from the worklog when it names one; otherwise items are balanced by expected dollars across specialists, deterministically. Managers reassign.

**AI analysis (C)**
- **Rules first, AI second.** A deterministic rules engine classifies every denial from verified facts: CARC/RARC, DOS against the policy effective dates, submission lag against timely filing, I10 with I11, modifier 25 with a procedure, a same-day sibling claim, enrollment history, and so on. It is the baseline and the degraded mode.
- **The LLM sees the same evidence packet** and must answer through one enum-constrained tool. Its output is validated: categories must come from the taxonomy, and cited policy sections must be ones we supplied, including any cited inside the draft. Invalid output is rejected.
- **When the two disagree,** the grounded rule answer stands and the item goes to human review. When the rules have no strong pattern, the AI's answer is used, with confidence capped at 0.75.
- **Untrusted text.** Worklog notes are wrapped as data and truncated. Notes that look like instructions are withheld from the model, flagged as exceptions, and their claims sent to review. The model can't act on anything: it has no tools that write.
- **Drafts** quote the cited policy section word for word. A template draft always exists; the AI may only rewrite it if it keeps the same citations.
- **Reproducible.** Results are cached by a hash of (model, prompt version, packet), so re-runs never call the model again and never change results.

**Security and PHI**
- Opaque bearer tokens (ASP.NET Core 8), with role policies enforced on the server.
- Specialists can only read and change claims in their own queue. Write-offs need a manager.
- Optimistic concurrency (`version`, 409 on conflict). Rate-limited login. PBKDF2 password hashes.
- `audit_log` is append-only: a trigger blocks UPDATE, DELETE and TRUNCATE.
- `no-store` and anti-framing headers; CSP on the web tier.
- Fonts are bundled, so there are no third-party requests. Patient data stays out of git and out of logs.

## What I did not finish, and why

- **The LLM path has not been run against the real model in my build environment** (no API key there). Its plumbing is tested with a fake client: validation, rejection, disagreement, caching and outage fallback. Run `python -m dcc.evaluate --ai auto` with a key to score the raw AI answers and the adversarial check.
- **Enrollment and eligibility rules use proxies.** "Not enrolled" comes from B7 history, and the eligibility rule can't be replayed without a 270/271 feed. Both are labelled as such in the export.
- **No SSO or user admin UI.** Users are seeded, and roles are fixed to Manager and Specialist.
- **One database role for the API and the pipeline.** Production should split them, with no UPDATE on `audit_log` granted at all, rather than relying only on the trigger.
- **No outbound appeal submission or fax,** and no 276/277 claim-status integration for the no-response claims.
- **Recovery probabilities are assumptions,** not learned from outcomes. Once appeal outcomes are captured they should be calibrated.
