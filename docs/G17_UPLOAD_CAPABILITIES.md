# G17 — capability registry and read-only impact baseline

Date: 2026-11-15. Development base: `1f0f094` (plan PR #23). G17 does not migrate or deploy the runtime. G18–G22 remain separate milestones in [the implementation plan](PLAN_G17_G22_UPLOAD_METADATA.md).

## Changes and compatibility

`upload_capabilities.py` is the common source for API/UI evidence and capability rejection. Preview, worker preflight, both public upload adapters and the inner API adapter use the same guards before provider/file/browser side effects. A worker rechecks fresh queue metadata before reserving content. Monitor previews evaluate all destination candidates, expose blocked reasons/codes, and cannot confirm/start unsupported requests. Download/render-only processing does not consult upload credentials or capability guards.

The endpoint retains `youtube_api`/`playwright` and the legacy `pin_comment`, `thumbnail`, `pin_comment_reason` keys. Each backend adds schema version 1, a content revision and feature details: configuration storage, setter availability, readback scope, offline evidence, live evidence, enabled state, reason and source references. Unsupported requests raise `UploadCapabilityError` with stable `code` and `issues`; batch preview exposes `reason_codes`. The Options UI shows the matrix and clears stale claims when the endpoint fails or its schema is incomplete.

Legacy `language` remains observable as `legacy_language` with `language_intent=legacy_unconfirmed`; it does not populate an API language field. Legacy remix zero does not opt into allowing remix. A restrictive remix request now blocks before upload instead of being dropped. Pin intent blocks even if its text is blank. Boolean normalization preserves false/zero values and rejects malformed flags. Unknown upload modes cannot silently fall back to Studio.

## Capability evidence at G17

| Feature | API | Studio |
|---|---|---|
| Category | Existing payload enabled; no category readback yet | Configuration stored; setter/readback disabled |
| Video language | Legacy storage only; disabled | Legacy storage only; disabled |
| Remix restriction | Unsupported; explicit restriction blocks | Unsupported; explicit restriction blocks |
| 18+ | Unsupported; early block | Existing late guard moved into shared early preflight |
| Premiere | Unsupported; early block | Existing click-if-present has no post-click/saved verification; now blocked |
| Pin comment | Unsupported; early block | Unsupported; early block |
| Thumbnail | Existing optional action; validates API response | Existing optional action; no saved-image readback |

The Studio Premiere restriction is intentional: source review found that a missing checkbox or a click that did not change it could silently drop the request. The registry does not advertise that incomplete action as enabled support. API errors no longer suggest switching to an equally unsupported backend. No new feature is marked live-verified. API category readback and canonical metadata intent belong to G19/G18 respectively.

Evidence: `tests/test_upload_capabilities.py`, existing `tests/test_optional_upload_results.py` and `tests/test_p3_uploader.py`, plus the browser test `ConfigBrowserTests.test_upload_capabilities_show_limits_and_clear_stale_support_on_failure`.

## Read-only runtime baseline

Collected at 2026-11-15 12:28 UTC with [the read-only audit utility](G17_CAPABILITY_AUDIT.md). Full report remains outside Git; no operational row identifiers, credentials or DB files are included here.

- Runtime source checkout clean at `74005a4`, build `2026.11.15-crud`.
- 16 channels: 15 Studio, 1 API; all configured category `22`.
- 3 options: legacy language `vi`, unconfirmed remix zero; no restrictive remix or pin request.
- 34 queue rows, all without options: 20 Completed, 8 Rendered, 4 Error, 1 Stopped, 1 NeedsReview. One historical row has an age guard; zero nonterminal rows acquire a new metadata guard in this snapshot. This is not eligibility to upload or retry those rows.
- No Monitors. SQLite quick check passes; existing schema stamps remain version 1.
- Audit interpreter Python 3.10.11, SQLite 3.40.1; all nine direct dependencies match pins. Separately observed Chromium 151.0.7922.34 and FFmpeg/ffprobe 8.1.2. These are audit/test environment observations, not assertions about a running browser.

The audit uses read-only SQLite transactions with normal WAL coordination. The restricted shell could not read runtime WAL state; the same read-only command succeeded outside that sandbox. No runtime migration, repair, restart, Profile connection or YouTube request was performed.

## Acceptance

Focused guard and audit regressions exercise real temporary SQLite plus mocked provider boundaries. Browser acceptance routes only to the local TestClient; external requests are aborted. Local final run with `DHB_BROWSER_TESTS=1`: 432 methods, 423 passed and 9 opt-in media tests skipped, in 81.6 seconds. Frontend syntax, Python compilation and diff checks passed. CI results are recorded in the PR after checks complete. The initial sandbox full run exposed a Windows descendant-termination restriction; the unchanged six scanner lifecycle tests and the final full run passed outside the sandbox. No production process-management change was made for that environment failure.

Next: G18, additive metadata-intent migration and consistent configuration/preview/approval/Monitor behavior. No runtime rollout before the required migration rehearsal and integrated acceptance.
