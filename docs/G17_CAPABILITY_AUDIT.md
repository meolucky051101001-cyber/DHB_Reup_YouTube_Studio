# G17 read-only capability impact audit

`audit_upload_capabilities.py` reports aggregate configuration and guard impact. It opens the explicitly supplied SQLite database with `mode=ro`, `query_only=ON`, and a consistent read transaction. It does not import the application/database initializer, run migrations, repair data, launch browsers or contact providers. WAL commits are included without checkpointing; SQLite may still use its normal read-lock/shared-memory coordination.

```powershell
python audit_upload_capabilities.py --db 'C:\path\clone.sqlite' --source-root . --output 'C:\path\g17-audit.json'
```

The optional `--browser-version`, `--ffmpeg-version`, and `--ffprobe-version` arguments record versions observed separately by the operator. They are explicitly marked `operator_supplied`; omitted values remain `not_observed`. The tool never launches a browser to discover its version. The environment section describes the Python process running the audit, installed direct dependencies versus requirements pins, source revision/dirty state/build and SQLite version; it is not proof of the live application's environment. Source/schema fingerprints and migration stamps provide a baseline without exporting schema SQL or credentials.

## Report semantics

- Options: stored language buckets (`legacy_vi`, other syntactically valid tags, empty, invalid), category buckets, unconfirmed intent, restrictive remix and pin requests. A legacy `vi` value is never treated as opt-in; zero remix never means explicit permission to allow remix.
- Channels: count by known backend and category bucket; unknown backend values become `invalid` without echoing them.
- Queue: current deterministic/assigned destination uses the production group resolver. Invalid/stale destinations are counted separately. Guard decisions use the shared capability registry. Historical rows remain included; the explicitly named nonterminal metric excludes `Completed` and `NeedsReview`, without suggesting either should be retried.
- Monitors: inspect every candidate channel. A mixed group counts once under each backend and each blocking code, rather than multiplying the number of affected monitors. `candidate_evaluations` and category counts describe the candidate level.
- `guard_affected` counts all existing and new capability rejections. `new_g17_guard_affected` covers restrictive remix, Studio premiere and earlier Studio age rejection; it does not claim that Studio age was previously supported. Studio premiere previously clicked a control if present without confirming the result or persisted value, so it now fails before upload. Missing options and unresolved destinations are separate problems, not counted as metadata support.

Outputs omit IDs, names, source URLs, local paths, token/cookie/proxy fields, raw error messages and arbitrary schema-version keys. Only fixed buckets, numeric counts, known schema stamps, fingerprints and source/environment metadata are emitted. The CLI refuses to write reports over the source DB or its WAL/SHM/journal paths, including existing hardlink aliases. Missing/corrupt/unsupported/locked databases fail with stable sanitized error codes and exit status 2; missing databases are never created.

This report is an impact audit, not a validation of approval freshness, account eligibility, source-file existence or live YouTube behavior. Keep the full baseline/environment report alongside private acceptance artifacts. A summary of aggregates may be included in a PR.

## Verification

`tests/test_upload_capability_audit.py` covers aggregate counts, source byte/schema preservation, mixed Monitor destinations, frozen/stale queue assignments, secret omission, malformed configuration, missing/corrupt/unsupported/locked databases, current WAL data, protected report paths and environment inspection without importing application code.
