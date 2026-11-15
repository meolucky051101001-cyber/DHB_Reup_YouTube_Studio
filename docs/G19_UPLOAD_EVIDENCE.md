# G19 upload evidence contract

`upload_evidence.make_evidence_sink(connect, queue_id, claim_token, attempt_id, channel_id, *, backend, expected)` returns a synchronous callback for the narrow window after an uploader returns a YouTube video ID and before metadata readback finishes. The worker must supply the approved backend and expected metadata. Construction validates and copies that expectation; every event, including the first receipt, must match it before a connection is opened. The callback opens a supplied SQLite connection, enables `PRAGMA synchronous=FULL`, starts `BEGIN IMMEDIATE`, verifies queue claim/attempt/reservation ownership, writes one bounded JSON object, commits, and closes the connection. It never imports the application, contacts a provider, or stores credentials.

The first event for an attempt must be `verification_state="received"`; this records the ID before readback. Later events may transition to exactly one terminal state: `verified`, `mismatch`, or `unknown`. Terminal events are immutable and idempotent only when backend, ID, expected metadata, observed metadata and code match. A different ID, backend, expectation, code or terminal state is rejected. A stale claim, wrong attempt, wrong channel/content, missing/changed reservation or non-`InProgress` attempt is rejected without a write.

Event input is strict and sanitized:

```json
{
  "backend": "youtube_api",
  "video_id": "abc123XYZ_0",
  "verification_state": "received",
  "expected": {"category_id": "22", "video_language": "en-US"}
}
```

Only `youtube_api`/`playwright`, 11-character YouTube IDs, category IDs, language tags, `observed` metadata and an uppercase bounded error code are accepted. The stored object derives `https://youtu.be/<id>` and UTC timestamps from the trusted callback; caller-supplied URLs, raw responses, cookies, OAuth, proxies and arbitrary fields never enter evidence. Malformed input, corrupt existing evidence, connection/write failures and oversized JSON return stable `EvidenceError` codes without echoing values.

The additive `upload_evidence=1` migration adds `upload_attempts.evidence_json TEXT NOT NULL DEFAULT '{}'`. It runs in a savepoint, stamps last, is idempotent and respects an outer transaction. A future/invalid stamp, stamped-but-incomplete column or wrong column contract stops without repair. Existing attempt rows/evidence are preserved.

Read APIs use `read_evidence(raw, queue_id, attempt_id, channel_id)` with trusted row IDs. It returns validated evidence or `None` for the empty `{}` sentinel. It rejects malformed/oversized JSON, duplicate keys, unknown fields, foreign row IDs, invalid URLs/timestamps and non-integer IDs (including booleans) with sanitized `EvidenceError` codes. It never returns raw stored JSON on a validation failure and performs no IO.

The sink is durable evidence, not proof that a video is published. `received` means only that the uploader returned an ID; the pipeline must separately read back required metadata and choose `verified`, `mismatch` or `unknown`, retaining `NeedsReview`/reservation semantics when verification is uncertain.

The worker supplies the selected backend and expected category/language from validated metadata when constructing the sink. A callback cannot replace that expectation. Completion checks the evidence against those same values and the normalized result URL. API completion requires `verified` evidence; a returned URL alone is insufficient. Existing Studio results retain their prior verification path while new Studio metadata capabilities remain disabled.

Successful completion commits queue, attempt, published history and reservation release in one `synchronous=FULL` transaction, after checking claim/attempt/channel/content ownership. A failure at any write rolls back all completion writes and retains the earlier evidence for review. Logging or optional cleanup after a committed completion cannot downgrade the result. A late `UploadNotSent`/Antidetect exception cannot clear an attempt that already contains evidence.

`GET /api/queue/{id}/upload-evidence` returns ordered attempts with validated evidence or a sanitized `evidence_error` flag. It excludes raw errors, reason text and invalid JSON. Historical attempts with no channel and empty evidence remain readable. The queue's **Bằng chứng** dialog makes no mutations and explicitly distinguishes an observed ID from a verified outcome.

`tests/test_upload_evidence.py` covers receipt before readback and fresh-process persistence, monotonic transitions/idempotency, mismatch observations, identity/ownership/reservation races, secret omission, corrupt/oversized records, full durability ordering, write/lock failure, connection failures and migration rollback/future-schema behavior.

Final local acceptance: **533 unittest methods, 524 passed and 9 opt-in media tests skipped**, with Chromium enabled, in 82.6 seconds. API fixtures are exercised through the real worker/SQLite sink as well as directly. Fault tests cover each completion write, restart, late NotSent and logging failures. Browser tests verify the read-only evidence dialog and updated capability matrix. Compilation, JavaScript syntax and diff checks pass. All provider boundaries are mocked; this is not live YouTube acceptance or runtime rollout. CI results are recorded on the PR.
