# Remediation local rollout — 17 November 2026

The ten reviewed issues were fixed and deployed locally at **03:56 Asia/Bangkok**. Runtime remains pinned to `c03946a3077810bdf912d52e18fb9cbeca747bc2`, build `2026.11.17-remediation`, branch `codex/local-remediation-20261117`. The previous revision was `80f595d40761bd3c0df556a8d244d4b4a3143b7c`. This documentation commit is separate from the accepted runtime revision.

Implementation and limits are described in [REMEDIATION_2026_11_17.md](REMEDIATION_2026_11_17.md); release/recovery instructions are in [G22_RELEASE.md](G22_RELEASE.md).

## Acceptance on the deployed revision

| Gate | Result |
|---|---|
| Local full suite in clean release venv | 637 total; 628 passed; 9 media opt-in skipped; 104.402 seconds |
| Chromium in local full suite | All 11 cases ran and passed |
| Syntax | 42 top-level Python modules and frontend JavaScript passed |
| Deterministic release checks | 38 passed |
| Fresh installation | Build/fingerprint, empty DB, PATH FFmpeg and no auto-run verified, including environment proxy isolation |
| Git checkout restart | Actual app started twice with correct SHA/fingerprint; no inherited Git trust workaround; ownership mismatch simulated |
| CI | [Push run 43943672805](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/actions/runs/43943672805) passed on the exact deployed SHA; unit, separate browser 11 and media 9 gates passed; mandatory rollback tests ran |
| Wall-clock soak | 1,000 jobs, ten rounds across 1/4/8 workers, 3,610.907 seconds; source hashes unchanged |
| Simulated upload | Measured gap 1,800.375 seconds; two attempts, one published history, one retained NeedsReview reservation |
| Soak teardown | Exit 0, empty stderr, no surviving owned process or synthetic soak directory; source checkout clean |
| Migration and recovery | 19-table rehearsal, idempotence and restore passed; previous code startup rehearsed before shutdown and against the locked backup |
| Runtime Chromium | Six tabs, zero write attempts, zero errors, zero external requests |

The highest sampled SQLite lock wait was **1,938.48 ms**, below the unchanged **2,000 ms** bound but close to it. This run does not establish spare capacity beyond the tested load or 24/7 stability. RSS, handles, threads and DB/artifact growth stayed within the thresholds frozen after warmup.

CI's unit step skipped 22 tests: 11 browser cases ran in its separate browser step, nine media cases ran in the media job, and two FFmpeg-dependent render-hang cases ran locally with FFmpeg. An intermediate proxy PR push run initially failed while removing a temporary Windows venv executable after the rollback test; the original log was retained and one rerun of the same job/source passed. The deployed candidate's own push and PR runs passed without that failure. The available log does not identify the holder of the temporary file lock.

## Runtime preservation and handoff

Independent read-only verification after apply confirmed the deployed SHA, matching startup/disk fingerprint, `restart_required=false`, a clean runtime checkout and Uvicorn `--no-access-log`. Pipeline and monitors remained idle, with zero claims, zero Active reservations and zero accepted approvals. The only app child was its console host; no renderer or soak process remained.

All 19 compared tables preserved their existing data; startup appended one permitted log (756 to 757). The queue remained 34 rows: 20 Completed, eight Rendered, four Error, one NeedsReview and one Stopped. Queue #212 stayed NeedsReview. Missing sources #193–196 and missing Channel IDs #1–7 were neither repaired nor retried. SQLite quick_check returned ok.

Studio category, video language, remix, age restriction, premiere and pin comment remain disabled. API category and thumbnail retain their enabled settings. No live upload, profile session, provider call or unsupported feature enablement occurred.

Backup SHA256: `f5aaa38bde4fd488ded598966c584f3a6213c2db3946fb5c2efe708a413a6ef4`.

Soak JSONL SHA256: `a5fe667a06d559ca7e0c7944ab3d386d4de459b373c74a72615cbaaa8c0c7843`.

Private artifacts remain in `review_2026-11-15/implementation/remediation-20261117/`: candidate suite manifests/logs, CI, soak/teardown, Git restart, runtime before/after and final verification; `local-rollout/` contains the acceptance JSON, SQLite backup, dry-run, apply result and manifest. No DB, credentials or private operational log is committed here.

Recovery must preserve the current DB/evidence. The helper rehearses previous startup on a clone and refuses automatic restart if it would delete logs or if the DB no longer matches its verified snapshot. Take a new backup before any post-start recovery; do not overwrite current evidence with the pre-rollout backup.

PR stack remains open for review, with no automatic merge: [CI #32](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/pull/32), [AI #37](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/pull/37), [download/Monitor #33](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/pull/33), [proxy #34](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/pull/34), [release #36](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/pull/36), [candidate #35](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/pull/35).
