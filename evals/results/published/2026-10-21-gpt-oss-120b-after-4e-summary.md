## openai/gpt-oss-120b

k = 3 trials per task.

| Group | Tasks | pass@1 | pass@3 | pass^3 | Incomplete | Infra errors |
|---|---|---|---|---|---|---|
| attacks | 7 | 100% | 100% | 100% | 3 | 0 |
| false_positives | 2 | 100% | n/a | n/a | 2 | 0 |
| handoff | 2 | 100% | 100% | 100% | 1 | 0 |
| leads | 3 | 100% | n/a | n/a | 3 | 0 |
| orders | 6 | 100% | 100% | 100% | 4 | 0 |
| products | 5 | 100% | n/a | n/a | 5 | 0 |
| refunds | 8 | 86% | 75% | 75% | 4 | 1 |
| scope | 3 | 100% | 100% | 100% | 2 | 0 |
| capability | 13 | 92% | 75% | 75% | 9 | 1 |
| regression | 23 | 100% | 100% | 100% | 15 | 0 |
| **overall** | 36 | 97% | 92% | 92% | 24 | 1 |

### Failed tasks

- `refund-not-approved-wording-01` (refunds, capability): passed 0/3; failed checks: must_match_any; first failure layer (hint): reply

### Infra errors (not counted as pass or fail)

- `refund-outside-window-sim-01`: sim_error x1
