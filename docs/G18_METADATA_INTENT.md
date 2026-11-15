# G18: versioned metadata intent and compatibility

This contract separates stored legacy defaults from a deliberate request to change YouTube metadata. Saving configuration has no provider side effects. Feature support and upload eligibility are evaluated separately by the shared resolver/capability registry; confirming a choice does not enable an unsupported feature.

## Storage and request shapes

The additive migration introduces two columns and the `metadata_intent=1` schema stamp:

| Table | Column | Initial value |
|---|---|---|
| `upload_option_groups` | `metadata_intent_json` | `'{}'` |
| `youtube_channels` | `studio_category_mode` | `'legacy_unconfirmed'` |

Both columns are non-null text. No existing category, language, remix, queue, history or Monitor value is backfilled or rewritten by this migration. `{}` means legacy intent has not been confirmed. It does not mean “apply language vi” or “allow remix.” A channel mode must be `legacy_unconfirmed`, `keep` or `apply`.

The options API accepts the new field as a JSON object or JSON object string and stores it in canonical JSON form. A confirmed object must contain **all** four fields:

```json
{
  "version": 1,
  "studio_category_mode": "inherit",
  "video_language": {"mode": "keep"},
  "remix": "keep"
}
```

- `version`: integer `1`, never a boolean/string.
- `studio_category_mode`: `inherit`, `keep` or `apply`. The numeric category remains in existing `category_id`; the channel fallback remains `default_category_id`. Value precedence and the decision to apply it are separate.
- `video_language`: exactly `{"mode":"keep"}` or `{"mode":"apply","value":"en-US"}`. `keep` does not accept a value. A language tag has 2–8 initial letters followed by optional 1–8 character alphanumeric subtags, at most 64 characters overall. The tag is preserved, including regional subtags; this syntax check does not claim backend support for every tag.
- `remix`: `keep` or `disable`. `keep` requests no change to YouTube's setting; it does not request permission to allow remix.

Unknown keys, incomplete objects, duplicate JSON keys, unsupported versions, null/empty values and malformed language tags are rejected. JSON strings are limited to 4096 characters. Errors describe the invalid field/contract without echoing supplied private values. Existing top-level display fields still follow the older API's ignore-extra contract; strict rejection applies inside metadata intent.

## Atomic writes and legacy clients

Create requests preserve the set of fields actually supplied by the caller. Defaults inserted by validation are not treated as explicit conflicting requests: creating an option with canonical language `en-US` and no legacy `language` field mirrors `en-US`, instead of conflicting with an automatically inserted `vi` default.

Update requests load the current row inside the same `BEGIN IMMEDIATE` transaction that writes the final result. Canonical JSON and mirrored values are written together:

- Canonical language `apply` mirrors its value to legacy `language`. If a request also explicitly supplies a different `language`, it receives **400** and no fields change.
- Canonical language `keep` leaves the stored legacy language unchanged when omitted; the stored value remains available for inspection/export. It is not used as a request to set language.
- Canonical remix `disable` mirrors `disable_shorts_remix=1`; explicit `keep` mirrors `0`. A conflicting explicitly supplied legacy flag receives **400** atomically.
- Category has no duplicated numeric value in JSON. A request changing `category_id` with a complete canonical object deliberately reaffirms its category mode.
- An unconfirmed legacy option may still update old fields; its JSON stays `{}`. Saving its name or other unrelated fields never confirms metadata.
- After confirmation, a legacy client changing `category_id`, `language` or `disable_shorts_remix` without supplying compatible intent receives **409** before any write. Sending the same old field values, renaming the option or changing unrelated fields remains valid and preserves intent.
- A confirmed option cannot be reset to `{}`. To release a restrictive request, explicitly submit the complete object with `keep`. Attempts to reset confirmation receive **409**.
- Once a channel mode is confirmed, changing `default_category_id` requires explicitly supplying `studio_category_mode`; otherwise **409**. Changing only channel notes/name, or sending its unchanged category, remains valid. Confirmed channel mode cannot revert to `legacy_unconfirmed`.

Prefer sending only the new canonical field and fields deliberately changed by the user. Clients that also send old language/remix fields must keep them consistent with the canonical choice. GET responses retain old fields and return the new stored fields; clients must read them again after a 409.

### Stale metadata editors

GET options/channels also returns `metadata_revision`, a SHA-256 revision of the metadata fields only. Updated clients send this token as `expected_metadata_revision` on PUT. Inside the write transaction, a mismatch returns **409** before any field is written, including unrelated name/note changes in the same request. Reload the row and review its current choices before retrying.

The option revision covers canonical intent, category, legacy language and legacy remix; the channel revision covers Studio category mode and default category. Names, notes and credentials do not affect this metadata token. A note/name-only edit with a current token preserves the revision. This token is optional for compatibility with existing clients; the mandatory old-client intent protections above remain active even without it. Revision fields are response/concurrency metadata and are never sent to SQL as stored columns.

## Migration and active work

Initialization checks the metadata-intent schema stamp before application DDL or data normalization. A future/invalid version stops initialization. The dedicated migration uses a savepoint, adds only missing unstamped columns, verifies their type/default/non-null contract, installs the specific active-channel-mode trigger and writes the schema stamp last. If any step fails, both new columns and the stamp roll back. Repeating a completed migration does not change rows or its timestamp; a stamped but incomplete schema stops for investigation instead of silently repairing it.

The separate `upload_approval_guard_channel_metadata_intent` trigger rejects a channel mode change while a claimed queue item is assigned to that channel, including direct SQL writers. It supplements pre-existing binding triggers so older databases receive the new protection despite their general trigger having been installed with `CREATE TRIGGER IF NOT EXISTS`.

Migration does not rewrite approval snapshots or Monitor policy envelopes to pretend that new metadata has already been approved. Resolver, preview and approval integration determines which existing configurations need a fresh confirmation.

## Effective metadata and approvals

`metadata_resolution.resolve_metadata` separates category value (option → channel → `22`) from intent. API preserves category application; Studio requires a deliberate channel or option choice. `keep` retains the setting on YouTube, while `apply` remains blocked until that backend capability is enabled. An optionless Studio job can confirm its intent on the channel. Language and remix preserve legacy-unconfirmed provenance; confirmed values and sources appear in batch and Monitor previews.

Upload snapshots use schema 3 and include effective metadata plus capability revision, alongside the existing option/binding hashes. Changing intent, channel mode, backend, group or gate makes the affected approval stale. Existing queue rows, attempt history and policy envelopes are not rewritten. The additive default columns are omitted from the legacy Monitor hash so unchanged API Monitor semantics keep their old revision; Studio legacy semantics are still blocked until confirmation. Current capability guards are rechecked even when an old revision remains compatible.

Deleting an option referenced by any queue row or Monitor returns 409 atomically. A missing legacy option reference blocks preview/worker instead of silently dropping the metadata request and falling back to channel defaults. Unused options may still be deleted.

## Verification

`tests/test_metadata_intent_config.py` tests strict parsing, canonical create/default handling, atomic dual-field conflicts, old-client 409 behavior, preserving unrelated edits, explicit keep transitions, channel mode round trips, active bindings, injected SQL failure, future-schema startup rejection, unchanged old values, migration idempotence, outer-transaction rollback and a failure after the first ALTER.

Final local acceptance with Chromium enabled: **483 methods, 474 passed and 9 opt-in media tests skipped**, 70.0 seconds. The browser regression covers real local API form submissions, explicit confirmation, legacy rename, effective values, blocked requests, malformed saved intent and two stale-editor conflicts. Python compilation, frontend syntax and diff checks pass. Initial browser failures were traced to an incomplete test Profile binding and corrected in the fixture.

All tests use temporary or in-memory SQLite databases. This schema/API change is not a local rollout or live YouTube acceptance. G17 CI passed both push/PR unit/browser and media lanes; G18 CI is recorded on its PR.
