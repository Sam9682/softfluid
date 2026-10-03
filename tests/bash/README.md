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


---

# Bash tests — `init-pltf-path-fix`

Bug condition exploration test for the bugfix spec `init-pltf-path-fix`.

## Files

- `test_init_path_resolution.sh` — **Property 1 (Bug Condition)** exploration
  test. Scoped property-based enumeration over `(launchDir, cloneDir)` pairs
  (with `launchDir != cloneDir`) modelling `isBugCondition(input)` from
  `design.md`: a repository-relative step (`venv_pip_install`, `final_chmod`)
  whose target resolves relative to the current working directory, where
  `resolve(cwd, target)` does NOT exist yet `resolve(cloneDir, target)` does.
  It builds a temporary fake clone containing `requirements.txt` and
  `setup_modsecurity_config.sh`, sets CWD to an unrelated launch directory
  (the state left by the stray `cd - > /dev/null`), and asserts the EXPECTED
  behavior (targets resolve against the clone, failures are not masked, the
  stray `cd -` is gone, and the steps are anchored on `${REPO_DIR}`).

  Test cases:
  1. pip requirements resolution — `./requirements.txt` must resolve against
     the clone (fails on unfixed logic).
  2. chmod target resolution — `setup_modsecurity_config.sh` must resolve
     against the clone and `chmod +x` succeed (fails on unfixed logic).
  3. masked success — a genuine pip/chmod failure must not be reported as
     `[OK] ...` (requirement 1.3; fails on unfixed logic).
  4. edge case — when CWD already equals the clone the relative lookups
     succeed (confirms the bug is CWD-dependent, not a missing file; passes
     both before and after the fix).
  Plus static root-cause assertions: the stray `cd - > /dev/null` is removed
  from the Kata block, and the venv/final steps reference
  `"${REPO_DIR}/requirements.txt"` / `"${REPO_DIR}/setup_modsecurity_config.sh"`.

- `test_init_preservation.sh` — **Property 2 (Preservation)** test.
  **Validates: Requirements 3.1, 3.2, 3.3, 3.4**. Captures the baseline,
  non-path-resolution behavior of the UNFIXED `init_pltf.sh` that the fix must
  leave untouched. Following observation-first methodology, the unfixed outputs
  were observed first, then encoded as property assertions:

  - **propA** — `CLONE_DEST = "${INSTALL_DIR%/}/${PLTF_FOLDER}"` preserved
    across 120 generated `(INSTALL_DIR, PLTF_FOLDER)` tuples (nested segments
    and 0–2 trailing slashes). Encodes the observed invariant that exactly ONE
    trailing slash is stripped from `INSTALL_DIR`.
  - **propB** — destination computation is stable for explicit trailing-slash
    and nested-segment cases (`/opt/` → `/opt/myplat`, `/opt//` → `/opt//myplat`,
    `/a/b/c` → `/a/b/c/nested-plat`, etc.).
  - **propC** — `set_ini_value` / `get_ini_value` round-trip for `PLTF_NAME`
    and `PLTF_FOLDER` across 120 generated values: value read back identically,
    no key duplication, and `DOMAIN` / `VERSION` / `LINUX_USER_INSTALLATION`
    preserved (uses the REAL sourced functions).
  - **propD** — directory-creation outcomes preserved across 40 generated
    `(clone, home)` layouts: `logs` (under the clone), `~/deployments`, and
    `~/deployments/admin` are all created.
  - static assertions — the Kata download/unzstd/extract/move/`daemon.json`/
    reload/cleanup steps, the clone + submodule + `deploy.ini` writes, and the
    final directory creation + completion banner text are all present.

## Running

```bash
bash tests/bash/test_init_path_resolution.sh
bash tests/bash/test_init_preservation.sh
```

## Bug condition exploration — result (Task 1)

Run on the **UNFIXED** `init_pltf.sh`: the test **FAILS** (6/7 assertions),
which is the expected/success outcome for an exploration test — it proves the
bug exists.

### Counterexamples

- `pip`: resolves `./requirements.txt` under the launch dir
  (`Could not open requirements file: './requirements.txt'`) while
  `${clone}/requirements.txt` exists.
- `chmod`: `chmod: cannot access './setup_modsecurity_config.sh'` while
  `${clone}/setup_modsecurity_config.sh` exists.
- Masked success: despite both failures, the unfixed sequence still reaches
  `[OK] Python environment ready` and `[OK] Installation completed
  successfully!` (requirement 1.3).
- Static: the stray `cd - > /dev/null` is still present in the Kata block, and
  the pip/chmod steps are CWD-relative rather than anchored on `${REPO_DIR}`.

### Control / edge case (¬C)

Test case 4 (CWD already equals the clone) **PASSES** — the relative lookups
resolve correctly, confirming the bug is working-directory-dependent rather
than a genuinely missing file. This proves the harness is discriminating.

After the fix (Task 3) removes the stray `cd -` and anchors the
repository-relative steps on `${REPO_DIR}`, this same test is expected to
**PASS**.

## Preservation — result (Task 2)

Run on the **UNFIXED** `init_pltf.sh`: `test_init_preservation.sh` **PASSES**
(21/21 assertions), confirming the baseline that the fix must preserve.

### Observed baseline (unfixed code)

- `CLONE_DEST = "${INSTALL_DIR%/}/${PLTF_FOLDER}"` — strips exactly ONE
  trailing slash from `INSTALL_DIR`; nested segments preserved:
  `/opt` + `myplat` → `/opt/myplat`, `/opt/` + `myplat` → `/opt/myplat`,
  `/opt//` + `myplat` → `/opt//myplat`, `/a/b/c` + `nested-plat` →
  `/a/b/c/nested-plat`.
- `set_ini_value` / `get_ini_value` round-trips `PLTF_NAME` and `PLTF_FOLDER`
  exactly, with no duplication and `DOMAIN` / `VERSION` / … preserved.
- `logs` (under the clone), `~/deployments`, and `~/deployments/admin` are all
  created; the `[OK] Installation completed successfully!` banner and the full
  Kata install/extract/cleanup sequence are present.

After the fix (Task 3.3) this same test is re-run and must still **PASS**
(no regressions in the Kata sequence, clone/submodule/`deploy.ini` writes, or
directory creation).
