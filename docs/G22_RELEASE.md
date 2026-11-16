# G22 — Offline release validation

Run `validate_review_release.py --db <sqlite> --output <new-json-path>` on an initialized G16 or later database. The tool reads the source through SQLite's backup API, migrates a disposable clone twice, then verifies a separate restore. It reports table counts, schema additions and the backup SHA-256. Keep the DB, private logs and operational screenshots out of Git.

The comparison covers the 15 operational tables plus `app_settings`, `upload_slot`, `app_schema_versions` and `system_logs`. It checks every old field, including credentials and evidence. It permits the three additive metadata/evidence columns with their original defaults, the corresponding version stamps, the existing idle Monitor confirmation guard and new log entries. Rollout validation also permits the startup guard to turn a previously enabled auto-start flag off. Cooldown, reservations and claims must survive intact. The group migration now retains its stamp timestamp when it performs no data or version change.

## Local rollout

Run the Windows rollout helper with `psutil` installed in its interpreter. Pass the local runtime root, pinned source revision, a new runtime branch name, private evidence directory outside both checkouts, and the runtime Python executable. The executable needs the project dependencies and Playwright Chromium. Default port is 8686.

```powershell
python rollout_review_release.py --runtime $runtime --revision $revision --branch $releaseBranch --evidence $evidence --python $runtimePython
```

The default dry run validates the checkout and any running loopback listener, checks runtime/DB idle and rehearses the DB. Once the exact revision has passed acceptance, run the same command with `--apply --acceptance $acceptance`. The acceptance JSON records `revision`, `full_suite`, `chromium`, `clean_start`, `deterministic`, `wall_clock_soak`, `repo` and `ci_run_id`. The helper checks the GitHub run's full revision, conclusion and required jobs before taking any deployment action.

Apply mode checks the PID and working directory again before shutdown, then takes the application lock, backs up the database and repeats the rehearsal. It switches to the pinned revision, starts the loopback server and verifies revision, fingerprint, idle state and DB transition. The private manifest records the revision, previous branch, build, Python/dependencies, Chromium, external tool versions, schema, backup checksum and acceptance reference. Use read-only Chromium for the runtime UI smoke and record the outcome separately.

On pre-start failure the tool returns to the previous branch and restarts the previous process when one had been running. A post-start verification failure stops the candidate and retains the current DB and logs for investigation. To recover, use a tested revision with the same metadata intent and evidence contracts. The pre-upgrade DB backup is for rehearsal and disaster recovery. Preserve a separate backup of the current DB before any recovery attempt.
