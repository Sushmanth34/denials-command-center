# AI evaluation results (generated 2026-10-06 08:33)

Labeled sample: 40 denials. Matched to a denial in the remits: 40.

| System | n | Root cause | Owning team | Preventable | All three correct |
|---|---|---|---|---|---|
| baseline_carc_lookup | 40 | 90% | 90% | 90% | 90% |
| rules | 40 | 100% | 100% | 100% | 100% |

_AI mode not run (no ANTHROPIC_API_KEY or --ai off). Only the rules engine was scored._

## baseline_carc_lookup: misses (4)
- GPP-2026-000230 CARC 197: expected Payer error / Denials (appeal) / No; got Authorization / Front desk / Authorization / Yes
- GPP-2026-000655 CARC 197: expected Payer error / Denials (appeal) / No; got Authorization / Front desk / Authorization / Yes
- GPP-2026-001665 CARC 197: expected Payer error / Denials (appeal) / No; got Authorization / Front desk / Authorization / Yes
- GPP-2026-001893 CARC 197: expected Payer error / Denials (appeal) / No; got Authorization / Front desk / Authorization / Yes

## rules: misses (0)
None on this sample.

## Sample composition

| Expert category | n | CARCs seen |
|---|---|---|
| Authorization | 4 | 197 x4 |
| Billing - timely filing | 4 | 29 x4 |
| Coding - diagnosis | 4 | 11 x4 |
| Coding - frequency | 4 | 151 x4 |
| Coding - modifier | 4 | 97 x4 |
| Credentialing | 4 | B7 x4 |
| Eligibility | 4 | 27 x4 |
| Medical necessity | 8 | 50 x8 |
| Payer error | 4 | 197 x4 |
