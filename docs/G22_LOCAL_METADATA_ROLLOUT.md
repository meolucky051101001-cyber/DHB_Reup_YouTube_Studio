# G22 local metadata rollout — historical record

Recorded on 17 November 2026 from the existing private acceptance and rollout artifacts. This closes the missing documentation for the rollout performed on 16 November; it is not a new deployment or acceptance of later fixes.

- Runtime revision: `80f595d40761bd3c0df556a8d244d4b4a3143b7c`; build `2026.11.16-metadata`; local branch `codex/local-metadata-20261116`.
- Previous runtime revision: `74005a4d6cfa898797e0f78c502d60bd6f8d8d26`.
- Full suite: 581 tests, 572 passed and 9 media opt-in skipped; Chromium regressions ran. [CI](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/actions/runs/43802706101) passed unit/browser and media gates on this revision.
- Soak: 1,000 jobs over ten rounds with 1/4/8 workers; 3,604.562 seconds; measured simulated-upload gap 1,800.469 seconds; two attempts, one published history and one NeedsReview reservation.
- Source hashes stayed unchanged; claims drained; no owned renderer process or synthetic soak directory remained.
- Migration rehearsal/restore compared all 19 tables. Read-only Chromium opened six tabs without writes or errors. Runtime was idle and SQLite quick_check passed.
- Backup SHA256: `d8d5d037f95df45b818b1e5e1375359af2280189695dbaad80d2b931fdc365af`.
- Soak report SHA256: `f005fbb1105d128c2ae8b5da192b8a699f2dca26b46ea4acbdab1e4d33d1acfa`.

Private evidence remains in the local `review_2026-11-15/implementation/g22-local-rollout-20261116/` and adjacent `g22b-soak-accepted.jsonl`. Databases, credentials and operational logs are not included here.

Studio category, video language, remix, age restriction, premiere and pin comment remained disabled; API category and thumbnail capability retained their previous settings. Offline fixtures do not establish live YouTube Studio compatibility. Queue #212 remained NeedsReview, missing source files #193–196 and missing Channel IDs #1–7 were not repaired or retried.

The subsequent full review found ten reproducible issues. The remediation release requires its own tests, CI, soak, backup and rollout record; this historical evidence cannot be reused to accept a different revision. Recovery preserves the current DB and evidence, using a compatible tested code revision rather than overwriting it with the pre-upgrade backup.
