# G21a: spoken video language contract and gates

Date: 2026-11-16, Asia/Bangkok. Studio's synthetic language contract is **offline_ready_gated**. Production language capability is **unsupported_with_reason** on both backends. The production flags `enabled`, `can_set`, `offline_verified` and `live_verified` are false, and production `readback` is `none`. Explicit language requests return `UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED` before provider operations.

The intended field describes the language spoken in the video. Subtitle language, caption certification and the language of a title/description are different settings. Neither their labels nor their values may substitute for a video-language control. The UI explains that distinction, and the synthetic fixtures deliberately place those controls alongside video language to test scope isolation.

## Evidence and provenance

The existing Studio uploader contains no supported video-language setter or saved-language readback. G20 established the shared contract shape and a synthetic category fixture, not real Studio selector provenance. G21a extends that local contract with authored language observations, English/Vietnamese fixtures and explicit semantic mappings. No real Profile was opened and no authenticated Studio DOM, operational screenshot or provider payload was captured for this work.

The language fixtures and their adapter are test-only. Their mapping entries for `vi`, `en` and `en-US` describe authored option values. They do not claim those strings are YouTube Studio's internal values, and translated display labels are not lookup keys. The fixture manifest marks synthetic provenance and records hashes after LF normalization. Hash verification proves the tested fixture's identity, not its fidelity to YouTube.

The API limitation remains the G19 finding from public documentation checked on 2026-11-15: the fetched `videos.insert` writable-field list did not include `snippet.defaultAudioLanguage`. `snippet.defaultLanguage` describes the title/description language and must not be used instead. The verifier can compare an explicitly expected `defaultAudioLanguage` in offline fixtures, but that does not enable language writes. See [G19 API metadata](G19_API_METADATA.md) for the official URLs and scope of that review. G21a does not make a new live API-support claim.

## Exact intent and compatibility

The G18 canonical intent remains unchanged:

```json
{
  "version": 1,
  "studio_category_mode": "keep",
  "video_language": {"mode": "apply", "value": "en-US"},
  "remix": "keep"
}
```

`vi`, `en` and `en-US` are distinct exact values. Neither resolver nor gate converts a regional tag to its base language or selects a nearby supported tag. Syntax validation still accepts the documented tag format; syntactic validity alone does not establish adapter support. A future adapter must reject a tag absent from its reviewed mapping, rather than defaulting to Vietnamese or English.

`{"mode":"keep"}` requests no language change. It carries no selected language and does not send an empty string to clear a provider field. Legacy `language="vi"` with unconfirmed intent remains stored and visible as legacy data; renaming an option does not convert it into an apply request. Canonical keep likewise preserves the stored legacy value while setting effective `video_language` to `None`. The existing G18 compatibility, stale-editor and atomic mirror rules continue to apply.

Saving an explicit language choice remains a local configuration operation. It preserves the requested tag and its intent even while upload is blocked. This makes the pending request reviewable without pretending it was applied to a channel or video.

## Contract and evidence boundaries

The language helper follows the scoped selection and guard pattern in [G20](G20_STUDIO_CATEGORY.md): typed observations for options, current control and saved value; an exact per-locale mapping; one scope/control; enabled semantic option selection; readback after selection; channel-binding and approval checks immediately before the caller's save/publish action.

The adapter boundary separates `reveal_video_language()`, `read_video_language()`, `select_video_language(value)` and `read_saved_video_language(video_id)`. The required section identifies video language specifically. A subtitle/caption/title control with a matching label or value is insufficient. Missing, duplicated, malformed, disabled, unsupported or unchanged controls fail with sanitized codes.

`StudioLanguageContract.prepare(tag, mode="apply")` prepares the exact tag. `mode="keep"` returns no preparation and performs no adapter IO. The corresponding no-preparation recheck performs no adapter work. A prepared value must be rechecked before the caller performs a send; these helpers never publish, delete or recreate a draft.

Saved verification is coordinated across the requested category/language fields. `verify_saved_metadata(...)` emits one `received` event before exact-video readback and one aggregate terminal event. It cannot emit category `verified` and later replace that terminal state after a language failure. A wrong video ID, mismatching tag, unknown locale/value or failed readback is uncertain after a send and must retain the observed ID and reservation for review.

G19 evidence now permits a strict, nonempty subset of `category_id` and `video_language` in expected metadata. This supports language-only verification without inventing a category expectation or claiming category was checked while it was kept unchanged. Combined requests require all expected fields to match before aggregate `verified`. If both fields are kept, the coordinator performs no adapter/sink work and does not manufacture metadata evidence. Empty expected objects, unknown fields and invalid values remain rejected; the worker's trusted expected set is copied and every callback must match it exactly.

The application is not wired to this helper in G21a. Existing API category verification keeps its G19 behavior. Any future Studio integration must supply a provenance-reviewed adapter, bind the expected field set to the approved request, preserve `NeedsReview` and evidence after uncertain sends, and enter G19 atomic completion only after all required checks succeed. Contract tests cannot enable the production registry.

## Gate coverage and offline acceptance

`tests/test_studio_language_gates.py` covers the missing language-specific paths:

- Each of `vi`, `en` and `en-US` survives resolver and metadata construction unchanged, while preflight, dispatcher, direct Studio and both API entry points reject before Profile, file-probe, OAuth or HTTP operations.
- Canonical keep and legacy Vietnamese reach only the selected mocked existing adapter with `video_language=None`; no title-language, audio-language, subtitle or caption-certification write field is introduced.
- Real temporary SQLite and local HTTP configuration preserve exact tags. Queue preview/start cannot authorize them; worker preflight precedes download/render/upload and attempt/reservation creation. Source bytes and paths remain unchanged.
- A Monitor containing API and Studio candidates evaluates both, retains the exact tag for each, rejects confirmation/start even with a current revision, and creates no provider worker. A rejected start may update its configuration-error status; it does not rewrite the stored option.

The existing `test_metadata_intent_config.py` exercises legacy rename preservation, explicit keep and stale clients; `test_metadata_resolution.py` covers unconfirmed/keep provenance and exact regional tags. G19 `test_api_metadata_evidence.py` confirms that keep emits neither `defaultAudioLanguage` nor `defaultLanguage` in the upload payload and that default-language readback cannot substitute for audio language. These tests are reused rather than duplicated.

The focused gate/configuration/resolution/API regression run passed **65 tests and 265 subtests**. Python compilation passed. The only warning was the existing Starlette/AnyIO deprecation.

Separate synthetic DOM tests cover scope separation from subtitle/caption/title controls, supported exact tags, unmapped values, delayed/missing/duplicate controls, drift, stale approval and saved-language mismatch. External browser requests are blocked. No real upload, OAuth exchange, Profile operation or runtime deployment is part of this acceptance. Separate contract/Chromium results are recorded for the G21a PR; full integrated acceptance remains part of G22.
