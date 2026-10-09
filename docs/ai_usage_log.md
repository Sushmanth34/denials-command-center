# AI usage log

**Tooling.** Claude (Anthropic), used as a pair-programmer for data analysis, code and documentation. Claude wrote most of
the code; I set the scope and priorities, made the stack decisions (.NET 8 API, PostgreSQL, Python pipeline, Anthropic for
the LLM layer) and checked the results.

**What I did myself**
- Reviewed the pipeline, API and UI code until I could explain how each part works and why.
- Checked the key figures in the memo against the data pack and the database.
- Ran the system locally on Windows (Docker Desktop setup, data pack, end-to-end run).
- Edited code and documentation after the first delivery.

## Where AI helped most
- **Profiling the data pack quickly.** Within the first hour it surfaced:
  - the re-sent 835 file
  - three claim-id formats
  - status-22 reversals
  - the injection note
  - worklog date ambiguity
  - the Sunshine recoupments of pre-April claims

  That list became the test list.
- **Writing the 835 parser and the reconciliation tests together.** Synthetic 835 builders made it cheap to test duplicates,
  conflicting TRNs, recoupment then re-denial, corrected claims, unmatched claims and out-of-balance files.
- **Boilerplate.** The .NET endpoints, React pages and docker files.

## Where it was wrong, and how it was caught
| What went wrong | How it was caught | Fix |
|---|---|---|
| The first open-denial calculation keyed lines by CPT+modifier, so corrected claims that added modifier 25 looked like new lines. Two paid denials stayed "open" (+$345). | Cross-checked the claims that had a frequency-7 remit against the open list. | Match by CPT only; regression test `test_corrected_claim_with_new_modifier_resolves_bundling_denial`. |
| Treated the procedure line on those corrected claims as normal, missing that it was **paid twice** ($322). | Writing the corrected-claim test forced the expected line totals to be stated. | New `POSSIBLE_OVERPAYMENT` exception. |
| Passed `temperature=0` to the Anthropic SDK; the installed SDK (1.11) no longer accepts it. | Called the client with a dummy key: the error was a TypeError, not the expected 401. | Removed the parameter; the outage path still degrades to rules. |
| Proposed sending every AI-outage result to review, which would flood the queue with 161 items. | Looked at the review queue size in degraded mode. | Rules confidence decides review; the engine label shows the outage. |
| A "38% overstated" claim in the README was the wrong way round. | Re-derived it from the numbers (19,310 / 31,145). | Reworded. |
| A memo figure ($1,135) did not match the database ($970). | Verified every memo number with SQL before finishing. | Corrected. |
| Early draft read the worklog amount mismatch as a data error. | Compared against the remit line amounts: the worklog records the denied *line*, not the claim. | Downgraded to a LOW informational exception. |

## Guard rails I kept
- The labeled sample is read only by `dcc/evaluate.py`; the pipeline never sees it.
- No number goes into the memo without a query or test behind it.
- AI output is never trusted to change state. It is validated, cached and reviewable.
