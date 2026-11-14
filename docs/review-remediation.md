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

## Release status

G1–G6 are complete for the user's selected scope: offline acceptance and local rollout. See `RELEASE_2026_11_15.md` for evidence, the deployed revision and pre-existing data gaps. No live YouTube upload/comment was performed.

## G3: queue preparation

Bulk destination edits now retain canonical group JSON, remove request aliases before SQL and reject conflicting destinations/aliases or missing queue IDs atomically. Excel export converts SQLite rows at the boundary and preserves formula-like text. Tests cover Unicode multi-group and schedule round trips, preview destinations, project filtering and rollback. Chromium exercises actual form submission, workbook download and preview/apply import against the isolated API/DB. Full suite: 330 tests / 139 subtests including both browser tests; Ruff critical checks and diff validation passed.

## G4: media and upload results

Foreground/background timestamps now use the same speed and start point. Shorts and square share the compositing/audio path, including selected background music; encoded output is bounded to the computed duration. Shorts auto-fit rounds speed upward so the cap is not exceeded. Existing finalizer tolerances remain unchanged.

The API uploader returns `{url, warnings}` after main-video verification. Thumbnail HTTP failures, malformed responses and transport errors preserve the verified video result. Warnings persist on the queue and attempt and remain visible after restart. No automatic video or comment retry is added. API and Studio both reject unsupported pin intent before upload; existing option text is preserved, and `/api/upload-capabilities` reports the limitation. The former API path that silently created an ordinary comment is removed.

Validation: 336 tests / 146 subtests including Chromium passed (9 opt-in media methods skipped in that run). The explicit real-media run passed all 28 cases: 18 core, 3 controls, 6 edges and 1080p, including final-frame motion, music signal, duration, audio presence and stop cleanup. CI now runs the media matrix separately. HTTP 403/500, timeout, malformed/success responses and durable Completed-with-warning behavior are covered offline.

PRs are committed and pushed per completed phase, stacked on the previous branch. Merge and live publishing are separate actions.
