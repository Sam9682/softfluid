# Bash tests — `stop-command-not-stopping-fix`

Tests for the bugfix spec `stop-command-not-stopping-fix`. They exercise the
real `stop_services` / `backup_database` functions from `deployControlPlan.sh`.

## Files

- `stop_services_harness.sh` — extracts the real `stop_services` and
  `backup_database` function bodies from `deployControlPlan.sh` (by name, via
  brace counting) and runs `stop_services` under `set -e` in a sandbox. All
  external commands (`pg_isready`, `pg_dump`, `aws`, ...) and the
  service-stopping functions are stubbed. It records which stop commands ran
  and prints a `PROBE` line.

  Parameters (env vars):
  - `MODE` = `locally` | `docker` | `default` → sets `LOCAL_MODE`
  - `FAIL_MODE` = `pg_isready` | `pg_dump` | `none` → how `backup_database` fails
  - `LOGS_FAIL` = `true` | `false` (default `false`) → when `true`, exercises the
    REAL `backup_logs` against a real `./logs` dir with an `aws` stub that fails
    specifically on the logs `s3 sync ./logs …` path (facet 2 of the bug — the
    bare `aws s3 sync ./logs` inside `backup_logs`, e.g. `SignatureDoesNotMatch`
    from invalid/expired S3 credentials), while the DB backup sync succeeds.

- `test_stop_backup_failure.sh` — **Property 1 (Bug Condition), facet 1
  (database backup)** test. Scoped property-based enumeration over
  `MODE × FAIL_MODE` for the bug-condition input space (`backup_database` exit
  code ≠ 0). Asserts the Expected Behavior: the script does not abort early, all
  applicable stop commands run, and a warning is logged.

- `test_stop_logs_backup_failure.sh` — **Property 1 (Bug Condition), facet 2
  (logs backup)** test. Scoped property-based enumeration over `MODE` with
  `LOGS_FAIL=true` (DB backup succeeds, the bare `aws s3 sync ./logs` fails).
  Asserts the same Expected Behavior for the logs-backup facet: the script does
  not abort early, all applicable stop commands run, and a warning is logged.
  On the pre-fix `backup_logs` (bare sync) it reports `ABORTED_EARLY`; after the
  fix (`if/else`-guarded sync + best-effort call site) it PASSES.

- `test_stop_preservation.sh` — **Property 2 (Preservation)** test. Part A
  asserts the successful-backup stop sequence (now `backup,logs,<stop cmds>`),
  Part B the standalone `backup_db` exit code (3.4), and Part C the successful
  logs-backup path (3.5): `backup_logs` prints "Logs backup completed" on a
  succeeding sync and skips cleanly with no `logs` dir.

## Running

```bash
bash tests/bash/test_stop_backup_failure.sh
bash tests/bash/test_stop_logs_backup_failure.sh
bash tests/bash/test_stop_preservation.sh
```

## Bug condition exploration — result (Task 1)

Run on the **UNFIXED** `deployControlPlan.sh`: the test **FAILS**, which is the
expected/success outcome for an exploration test — it proves the bug exists.

### Counterexamples (6/6 bug-condition cases fail)

For every `MODE ∈ {locally, docker, default}` and every backup-failure path
`FAIL_MODE ∈ {pg_isready, pg_dump}`:

```
PROBE result=ABORTED_EARLY flask=0 nginx=0 docker=0 gitea=0 warning=0 (child_exit=1)
```

- **Root cause confirmed:** inside `stop_services()`, `backup_database` is
  called as a bare statement. With `set -e` active (line 6), a non-zero return
  from `backup_database` (failed `pg_isready` connection test, or failed
  complete `pg_dump`) aborts the whole script **before** any
  `stop_flask_service` / `stop_nginx_service` / `stop_docker_services` /
  `confirm_gitea_stop` runs. No stop command is recorded and no warning is
  logged.

### Control case (sanity, not part of the bug condition)

With `FAIL_MODE=none` (backup succeeds) the harness reports `COMPLETED` and the
correct mode-specific stop commands run (`locally` → flask/nginx/gitea,
`docker` → docker, `default` → all four). This proves the harness can produce
passing probes and is genuinely discriminating.

The fix (Task 3) makes the pre-stop backup best-effort
(`backup_database || echo -e "  ⚠️ ..."`); after that this same test is expected
to PASS.

## Logs-backup facet exploration — result (facet 2)

`test_stop_logs_backup_failure.sh` drives the second facet. Run against the
**pre-fix** `backup_logs` (bare `aws s3 sync ./logs`) with the DB backup
best-effort: the test **FAILS**, confirming the bug.

### Counterexamples (3/3 mode cases)

For every `MODE ∈ {locally, docker, default}` with `LOGS_FAIL=true`:

```
PROBE result=ABORTED_EARLY flask=0 nginx=0 docker=0 gitea=0 warning=0 backup=1 logs=1 seq=backup,logs (child_exit=1)
```

- **Root cause confirmed (facet 2):** inside `backup_logs()`, `aws s3 sync ./logs …`
  was a bare command. With `set -e` active, an S3 failure (`SignatureDoesNotMatch`)
  makes `backup_logs` return non-zero, and the bare `backup_logs` call inside
  `stop_services` then aborts the whole script — *after* the (now best-effort) DB
  backup (`seq=backup,logs`) but *before* any stop command runs.

### After the fix

`backup_logs` guards the sync with `if/else` (warning + return 0) and the
`stop_services` call site is `backup_logs || echo -e "  ⚠️ …"`. The test then
**PASSES**: `result=COMPLETED`, all applicable stop commands run, `warning=1`.

## Full suite

All three tests PASS on the fixed `deployControlPlan.sh`:

- `test_stop_backup_failure.sh` — 6/6 (facet 1 fix checking)
- `test_stop_logs_backup_failure.sh` — 3/3 (facet 2 fix checking)
- `test_stop_preservation.sh` — 11/11 (Property 2 preservation)
