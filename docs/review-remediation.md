# Review remediation — 2026-11-15

## G1: profile actions, review holds and shared sources

- Profile actions use fixed event listeners and preserve the original profile name. Entity-encoded input never becomes JavaScript.
- A queue row or reservation in `NeedsReview` blocks preparation, preview, claiming and upload. Legacy rows with a hidden reservation expose reconciliation controls.
- Successful uploads leave user-owned source paths and files intact. Managed temporary artifacts retain their existing ownership checks.
- `python repair_review_state.py --db PATH` inspects inconsistent review holds without changes. Stop the app before `--apply`; it acquires the app lock, backs up SQLite, retains reservations and records each change. Active claims or later reconciliation decisions require manual review.

Validation: full suite 300 passed / 135 subtests, browser test run separately; focused suite after final guards 14 passed / 2 subtests. Ruff critical checks, JavaScript syntax and diff whitespace passed. No live upload or runtime data mutation.

## Remaining phases

G2: explicit monitor inheritance and approval binding. G3: group editing and workbook round trip. G4: render timelines, optional upload warnings and truthful pin capability. G5: integrated offline release validation. G6: controlled rollout and explicitly scoped live acceptance.

PRs are committed and pushed per completed phase, stacked on the previous branch. Merge and live publishing are separate actions.
