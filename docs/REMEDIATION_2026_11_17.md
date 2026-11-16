# Review remediation — 17 November 2026

Completed: candidate `c03946a` passed new acceptance and was deployed locally. See the [rollout record](REMEDIATION_LOCAL_ROLLOUT_2026_11_17.md) for test, soak, data-preservation evidence and remaining limits. This documentation update does not change the pinned runtime revision.

This release addresses the ten reproduced findings from the review of `80f595d`. The historical G22 acceptance remains documented separately in [G22_LOCAL_METADATA_ROLLOUT.md](G22_LOCAL_METADATA_ROLLOUT.md). The deployment record for this release must identify a clean candidate SHA and new evidence; the historical soak is not acceptance of these changes.

| Finding | Resulting behavior | Regression coverage |
|---|---|---|
| F1–F3: AI writer lock, stale results, unchecked batch metadata | Provider calls run after closing the read snapshot. Each item rechecks guards and semantic state in a short transaction, validates final metadata and commits independently. UI reports partial failures. | `test_ai_edit_concurrency.py`, existing guard, approval and fallback suites |
| F4: FFmpeg PATH fallback | Default PATH lookup is left to yt-dlp; explicit executable overrides are validated and shared by worker/CLI. | `test_download_ffmpeg_location.py`; offline synthetic audio/video merge |
| F5: GROUP history with NULL channel | The upload completion transaction owns history writes; Monitor only observes its latest completed attempt and actual channel. | `test_monitor_completion_history.py`, monitor and upload evidence suites |
| F6: environment proxy override | Channel proxy/direct mode and OAuth bearer remain authoritative; explicit custom CA bundles retain TLS verification. | `test_api_session_policy.py`, API evidence/security suites |
| F7: stream token in access log | Launch/recovery disable Uvicorn access logging and use exact supported process argument matching. | Real local stream sentinel and argv tests in `test_release_safety.py` |
| F8: startup retention conflicts with rehearsal | Startup preserves all logs; periodic housekeeping owns retention. Recovery rehearses old code before shutdown and refuses destructive startup. | More-than-5,000-log, retention and recovery fault tests |
| F9: missing revision after restart | Git queries use the resolved source checkout with scoped trust; soak cannot take the caller's Git revision. | Git ownership/cwd/environment tests and clean startup |
| F10: silently skipped rollback tests | The release dependency file pins psutil; Windows CI fails if it is missing. | Rollback tests run in a clean release environment and Windows CI |

AI failures use stable item codes: `item_guarded`, `stale_snapshot`, `item_missing`, `invalid_metadata`, `source_links_too_long`, `provider_failed`, and `db_write_failed`. Invalid requests return 400; an initially busy selection returns 409 before any provider or write. A processed batch returns 200 with `ok=false` and `failed` when only some items commit. It never retries stale results automatically. Default and Monitor project selection remain supported; nontext selectors cannot silently select Default.

The AI comparison is a semantic snapshot, including the latest attempt row. It does not create a lifecycle epoch capable of detecting a claim/release cycle that returns every protected value to its previous state. Current busy/NeedsReview/reservation guards are checked atomically with writes. Existing upload previews become stale when metadata changes through the normal approval contract.

Existing NULL-channel history is retained; this release prevents new accidental GROUP entries without guessing provenance. A manual reupload may retain the first history URL while the latest URL belongs to its completed attempt. Monitor checks the queue against that attempt and the history's actual channel/content.

Use [G22_RELEASE.md](G22_RELEASE.md) for pinned dependencies, acceptance, recovery and local rollout. Keep the current DB on post-start failure; do not restore an old backup over newer evidence. Old code that would prune logs blocks automatic recovery before shutdown.

Capability gates and live-validation scope are unchanged. Studio category, language, remix, age restriction, premiere and pin comment are not enabled by these fixes. Offline media, HTTP fixtures and a bounded soak do not certify live YouTube Studio or 24/7 operation. No existing NeedsReview job, missing source file or missing Channel ID is automatically repaired or retried.
