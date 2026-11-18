# G22 — Offline release validation

Run `validate_review_release.py --db <sqlite> --output <new-json-path>` on an initialized G16 or later database. The tool reads the source through SQLite's backup API, migrates a disposable clone twice, then verifies a separate restore. It reports table counts, schema additions and the backup SHA-256. Keep the DB, private logs and operational screenshots out of Git.

The comparison covers the 15 operational tables plus `app_settings`, `upload_slot`, `app_schema_versions` and `system_logs`. It checks every old field, including credentials and evidence. It permits the three additive metadata/evidence columns with their original defaults, the corresponding version stamps, the existing idle Monitor confirmation guard and new log entries. Rollout validation also permits the startup guard to turn a previously enabled auto-start flag off. Cooldown, reservations and claims must survive intact. The group migration now retains its stamp timestamp when it performs no data or version change.

## Local rollout

Install `python -m pip install -r requirements-release.txt` in the release/test interpreter. This pins psutil and the application dependencies; the Windows CI gate fails if the rollback dependency is missing. Pass the local runtime root, pinned source revision, a new runtime branch name, private evidence directory outside both checkouts, and the runtime Python executable. The runtime executable needs the project dependencies and Playwright Chromium. Default port is 8686.

```powershell
python rollout_review_release.py --runtime $runtime --revision $revision --branch $releaseBranch --evidence $evidence --python $runtimePython
```

The default dry run validates the checkout and any running loopback listener, checks runtime/DB idle and rehearses the DB. Once the exact revision has passed acceptance, run the same command with `--apply --acceptance $acceptance`. The acceptance JSON records `revision`, `full_suite`, `chromium`, `clean_start`, `deterministic`, `wall_clock_soak`, `soak_report`, `repo` and `ci_run_id`. Set `soak_report` to the path of the soak JSONL relative to the acceptance JSON. The helper checks that report and the GitHub run's full revision, conclusion and required jobs before taking any deployment action.

Apply mode checks the PID and working directory again before shutdown, then takes the application lock, backs up the database and repeats the rehearsal. It switches to the pinned revision, starts the loopback server and verifies revision, build, fingerprint, idle state and DB transition. Read-only Chromium opens all six dashboard tabs, checks language/remix gates and confirms that the UI did not modify the DB. The private manifest records the revision, previous branch, build, Python/dependencies, Chromium, external tool versions, schema, backup checksum and acceptance reference.

Before stopping the runtime, the helper also rehearses startup of the previous revision and interpreter on an isolated clone, blocking background/network work and checking all 19 tables. It repeats this check against the locked backup. On pre-start failure it can restart the previous process only if the current DB still matches that verified snapshot and the previous checkout remains clean and pinned. An older revision that prunes existing logs is rejected before shutdown; do not override this guard. A post-start verification failure stops the candidate and retains the current DB and logs for investigation. To recover, use a tested revision with the same metadata intent and evidence contracts. The pre-upgrade DB backup is for rehearsal and disaster recovery. Preserve a separate backup of the current DB before any recovery attempt.

Startup and migration preserve all existing logs. The six-hour housekeeping task retains the latest 5,000 log rows and can append its own cleanup entry; a temporary count above 5,000 is expected. Rehearsal continues to reject removed, changed or backdated logs.

Rollout and recovery launch Uvicorn with `--no-access-log`, keeping stream query credentials out of access logs. Process verification accepts only the exact supported launcher forms, including the previous Uvicorn form for upgrades. Git identity queries trust only the resolved checkout through command-scoped `safe.directory`; no global Git configuration change or wildcard trust is needed. Recovery also scopes the child Git environment so older code can identify its revision.

Clean-install smoke copies code without `.git`, so its expected runtime revision is null. It checks build/fingerprint and records the source revision. A separate restart test in a clean Git checkout verifies a non-null pinned revision without inherited Git configuration workarounds.

## Offline soak

Run `python soak_review_release.py --output $soakReport` with FFmpeg and FFprobe on PATH and the release dependencies installed. The 60-minute default runs on temporary SQLite and synthetic media. It processes ten rounds of one hundred render jobs across one, four and eight workers. A separate simulated upload batch exercises the production global slot and measures two real uploader timestamps around the 30-minute gap. The report includes durable receipt and readback outcomes, retained source hashes, resource limits frozen after warmup, and drain results. `--pilot` runs a short harness check. The script reads Git from its own source directory, even when invoked from another directory, and acceptance requires that checkout to be clean.

Every release needs new full-suite, browser, clean-start, deterministic, CI and soak evidence for the same full SHA. Record command, interpreter, clean status, timestamps, exit code, counts/skips and artifact hashes. Confirm process exit and temporary-directory removal after the soak's complete event. Keep post-rollout documentation on a separate branch; it does not change the accepted runtime SHA.

The deterministic release regressions cover restart after receiving a video ID, warning and uncertain upload holds, cooldown and concurrency, Monitor heartbeat and the six-hour housekeeping boundary. The wall-clock soak measures one bounded session. It cannot establish long-running housekeeping or 24/7 availability.
