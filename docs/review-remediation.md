# Review remediation — 2026-11-15

## G1: profile actions, review holds and shared sources

- Profile actions use fixed event listeners and preserve the original profile name. Entity-encoded input never becomes JavaScript.
- A queue row or reservation in `NeedsReview` blocks preparation, preview, claiming and upload. Legacy rows with a hidden reservation expose reconciliation controls.
- Successful uploads leave user-owned source paths and files intact. Managed temporary artifacts retain their existing ownership checks.
- `python repair_review_state.py --db PATH` inspects inconsistent review holds without changes. Stop the app before `--apply`; it acquires the app lock, backs up SQLite, retains reservations and records each change. Active claims or later reconciliation decisions require manual review.

Validation: full suite 300 passed / 135 subtests, browser test run separately; focused suite after final guards 14 passed / 2 subtests. Ruff critical checks, JavaScript syntax and diff whitespace passed. No live upload or runtime data mutation.

## G2: monitor metadata

Monitors now require a preview and confirmation of effective privacy, audience and AI labels for every candidate channel. Inheritance is distinct from explicit false; option-group false overrides channel true. Each new queue item retains the policy and configuration revision, and approval schema 2 binds that policy. Group/binding/default changes block subsequent uploads instead of silently recalculating the destination.

The migration adds columns only; legacy monitor configuration remains blank and cannot auto-start. Existing queue values are not backfilled. Deliberate queue metadata/destination edits adopt the displayed values as manual settings and require a fresh upload preview. A changed monitor can be stopped, reviewed and confirmed again; old queued items retain their old revision until manually reviewed.

Rollback: stop all monitors before using an older binary, which does not understand these policy fields. Do not treat an additive migration as automatic rollback compatibility.

Validation: 317 tests and 139 subtests including Chromium passed; real SQLite tests cover inherited payload/snapshot, explicit false, group channel defaults, changed revisions, atomic freeze rejection, legacy migration and manual overrides. Lifecycle fixtures now carry explicit confirmed policies. No live upload performed.

## Remaining phases

G3: group editing and workbook round trip. G4: render timelines, optional upload warnings and truthful pin capability. G5: integrated offline release validation. G6: controlled rollout and explicitly scoped live acceptance.

PRs are committed and pushed per completed phase, stacked on the previous branch. Merge and live publishing are separate actions.
