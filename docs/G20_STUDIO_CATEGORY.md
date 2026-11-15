# G20: Studio category contract and provenance

Date: 2026-11-15. G20 implements and tests a provider-free category contract. The result is **contract_tested_gated**: the application still rejects an explicit Studio category `apply` request before opening a Profile, sending a file or starting a Monitor. There is no production Studio category adapter, observed Studio selector catalog or verified saved-category readback. The public production capability remains `enabled=false`, `can_set=false`, `readback="none"`, `offline_verified=false` and `live_verified=false`.

The synthetic contract tests do not establish that the same controls, option values or timing exist on YouTube Studio. They are evidence for the required behavior of a future adapter. G20 follows the plan's gated-contract fallback when genuine DOM/readback provenance is unavailable; it does not claim a working category setter in the production uploader.

## Provenance inventory

| Source | What it establishes | What it does not establish |
|---|---|---|
| Existing `youtube_uploader.py:upload_via_playwright_studio` | Existing upload flow, identity guards and result handling | No category setter or saved-category readback |
| Existing `tests/test_p3_uploader.py` Studio fixtures | Title/video-ID, upload, visibility and failure regressions | No captured Studio category DOM or ID-to-option mapping |
| G19 API category tests and official API field references | API `categoryId`, assignability and exact-ID API readback | Studio DOM option values, localized labels or selectors |
| `tests/fixtures/studio_metadata/category_en.html` and `category_vi.html` | Authored English/Vietnamese DOM for the local contract harness | Observations from a real Studio account or Profile |
| `tests/fixtures/studio_metadata/manifest.json` | Synthetic/live flags, authored provenance and SHA-256 fixture integrity | Attestation that the fixture reflects YouTube's current UI |

All G20 DOM selectors and semantic option strings originate in the authored fixtures and their test adapter. The manifest explicitly marks them synthetic and not live-verified. Even familiar category IDs are not evidence that a corresponding Studio option has the same internal value. Fixture mappings remain test inputs; there is no production mapping table or approximate matching by translated text. Hashes establish which fixture was tested, not its authenticity as provider HTML.

No Profile was connected to obtain this material. No authenticated Studio page, cookie, OAuth token, provider response or operational screenshot is added to the repository. Public API documentation from G19 is not repurposed as DOM evidence.

## Contract boundaries

`studio_metadata_contract.py` defines immutable observations and a small adapter protocol:

- `CategoryOption` supplies a semantic option value and enabled state.
- `CategoryControl` supplies locale, category section identity, scope/control counts, visibility/enabled state, current value and options.
- `SavedCategory` supplies the exact observed video ID, locale and saved semantic value.
- `CategoryAdapter` implements `reveal_category()`, `read_category()`, `select_category(value)` and `read_saved_category(video_id)`.

`StudioCategoryContract(adapter, locale_maps, provenance=...)` requires an adapter plus a provenance-bearing mapping from category ID to semantic value per locale. Missing adapters/mappings fail closed. Mappings are copied and must be unambiguous; passing a fixture mapping cannot change the application's capability registry.

`prepare(category_id)` reveals the category control, requires one category scope and one control, checks visibility/enabled state and finds exactly one enabled option by semantic value. It selects only when needed and reads back the control. Missing, unmapped, disabled, duplicate or unchanged options produce stable errors rather than selecting the first available item. A valid but unmapped category does not become category `22`.

`recheck(prepared, verify_binding=..., verify_approval=...)` requires the caller's channel-binding and approval guards, then re-reads the control immediately before the caller's save/publish action. Changed scope, locale, option availability or selected value stops the operation. The helper does not click Save/Publish and does not delete or recreate a draft.

`verify_saved(prepared, video_id, evidence_sink=...)` accepts an already-observed video ID and synchronously records `received` before asking the adapter for that exact video's saved category. It records `verified` only for a matching ID, locale and mapped category; mismatches record `mismatch`, while missing/unreadable/unmapped observations record `unknown`. Exceptions identify the error code and whether a send has already occurred. Sink failures are propagated as uncertain outcomes; they never trigger upload retry or cleanup.

The sink event format is the G19 contract: backend `playwright`, observed video ID, expected category, observed category when known, state and sanitized code. The production worker owns the trusted backend/expected metadata, timestamps, queue/attempt/channel binding and durable SQLite write. The contract alone does not mark a queue row Completed. A future production integration must retain the attempt/reservation and source file on uncertain outcomes and use G19's atomic completion rules. The existing Studio uploader is not wired to this new helper in G20.

## Production gate and compatibility

Category intent and numeric value remain separate. The resolver preserves option → channel → existing fallback precedence when values are absent, while explicit invalid values fail validation. It never substitutes a known category for an explicit unknown request. Syntax-valid `999` in a G20 fixture is kept as `999`; it is not asserted assignable by Studio or API.

Studio `apply` remains `UPLOAD_CATEGORY_UNSUPPORTED` at resolver/preflight, dispatcher, direct Studio adapter, queue preview/start, worker and Monitor boundaries. Explicit `keep` requests no category write and retains the existing Studio route, including when an option overrides a channel's `apply` intent. The helper is not called in that route. `legacy_unconfirmed` still requires the G18 confirmation flow.

A mixed API/Studio Monitor evaluates every candidate independently: the Studio apply request blocks the Monitor while the API candidate retains its own effective category. A current metadata revision cannot bypass that capability rejection. Preview/confirmation do not rewrite configuration; a rejected Monitor start records its existing configuration-error status without creating a scan thread, upload attempt or reservation. Download/render-only compatibility remains covered by the G17 regression suite.

The capability reason offers an explicit choice: keep the current YouTube category only if the user accepts it, or keep the requested job blocked. It never silently changes `apply` to `keep`. Updating the evidence/reason changes capability revision, so existing G18 snapshot checks require a fresh preview where applicable.

## Offline verification

`tests/test_studio_metadata_gates.py` adds missing category-specific route coverage:

- Channel and option `apply` requests, including unmapped `999`, retain their value and fail with the same code before provider, media-probe or Profile boundaries.
- Explicit option `keep` preserves configuration and reaches only the existing mocked Studio adapter.
- Real temporary SQLite preview/start cannot produce an upload approval for a blocked request; worker preflight precedes download/render/upload and attempt/reservation creation.
- A mixed Monitor exposes the Studio blocker, leaves the API candidate independently eligible, refuses confirmation/start, creates no provider worker, and preserves configuration.

The contract's separate local DOM tests exercise authored English/Vietnamese fixtures, scoped semantic selection, delayed/hidden controls, duplicates, disabled/no-change controls, pre-publish drift, binding/approval rejection and exact-ID saved readback. Browser networking is blocked by the fixture harness. Unit tests use temporary SQLite and mocked external boundaries; no real upload is part of acceptance.

The focused run of `test_studio_metadata_gates.py`, `test_metadata_resolution.py`, `test_metadata_approval.py` and `test_upload_capabilities.py` passed **37 tests and 193 subtests**. Python compilation passed. Separate contract/Chromium results are recorded with the G20 PR; the complete integrated suite remains mandatory for G22.

Final contract acceptance: **15 unit methods passed**; the complete Chromium regression module passed **10 methods** in 21.5 seconds, including the new synthetic category scenarios. Fixture hashes, JavaScript/Python syntax and diff checks pass. G19's preceding CI is green for both push/PR unit/browser and media lanes. No runtime rollout occurred.

Before production enablement, a future change must provide reviewed provenance for actual Studio controls and mappings, an exact-video saved readback source, bounded adapter waits, integration into the existing approval/identity/evidence lifecycle, and acceptance appropriate to that scope. Synthetic test success alone does not satisfy those requirements.
