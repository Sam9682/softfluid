"""Bug condition exploration test for the "nginx locations not synced on
startup" bug.

Spec: .kiro/specs/nginx-locations-sync-on-startup

Bug summary
-----------
``deployControlPlan.sh`` starts the control plane but never populates nginx's
location configuration from the database. The repository already ships a CLI,
``scripts/sync_nginx_locations.py``, that calls
``src.nginx_manager.sync_all_locations(db_manager)`` (exit ``0`` on success /
``1`` on failure), but the deploy script only ever runs it by hand. So after a
deploy nginx can serve a stale or unpopulated location config until an operator
remembers to run the sync.

The two startup paths are:

* ``start_local_deployment`` - runs ``install_python_dependencies`` ->
  ``setup_gitea`` -> ``start_flask_application`` -> ``configure_nginx`` (ends
  with ``test_and_reload_nginx``) -> ``configure_firewall``.
* ``start_docker_deployment`` - runs ``cleanup_docker`` ->
  ``docker-compose up -d --build`` -> optional ``migrate_database``.

Neither path invokes ``scripts/sync_nginx_locations.py``.

Bug Condition C(X) (from design ``isBugCondition``):
    command == "start"
    AND localMode IN ["locally", "docker"]
    AND startupPathExecuted == true
    AND syncCliInvoked == false

Why parse-the-script
--------------------
The change is to a bash script, not importable Python, so - following the
established convention of ``tests/test_extended_ports_migration_wiring.py``
(read the script text, extract named bodies, assert on an ordered sequence of
steps) - these tests read ``deployControlPlan.sh``, extract the body of each
startup function, and assert the sync CLI is wired in as the final startup
step.

EXPECTED OUTCOME on UNFIXED code
--------------------------------
These tests are EXPECTED TO FAIL on the unfixed script (failure confirms the
bug exists): ``sync_nginx_locations`` / ``sync_nginx_locations.py`` never
appears in either startup function body, and never appears anywhere in the
script, even though ``scripts/sync_nginx_locations.py`` exists on disk. When
the sync is wired into both startup paths (the fix), these tests pass.

_Requirements: 1.1, 1.2, 1.3_
"""

import os
import re

import pytest


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_SCRIPT_PATH = os.path.join(REPO_ROOT, "deployControlPlan.sh")
SYNC_CLI_PATH = os.path.join(REPO_ROOT, "scripts", "sync_nginx_locations.py")

# The CLI referenced by name inside the deploy script.
SYNC_CLI_BASENAME = "sync_nginx_locations.py"
# The helper function the fix introduces to invoke the CLI.
SYNC_HELPER_NAME = "sync_nginx_locations"

# The two startup functions that must each end with the sync step.
STARTUP_FUNCTIONS = ("start_local_deployment", "start_docker_deployment")


# --------------------------------------------------------------------------
# Script-parsing helpers (parse-the-script wiring convention)
# --------------------------------------------------------------------------

def _read_deploy_script():
    """Return the full text of ``deployControlPlan.sh``."""
    with open(DEPLOY_SCRIPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def extract_function_body(script_text, name):
    """Extract the body of a bash function defined as ``name() {`` up to its
    matching closing ``}`` (brace-depth balanced).

    Returns the text between the opening ``{`` and the matching ``}``
    (exclusive of the braces themselves). Raises ``AssertionError`` if the
    function definition is not found.
    """
    # Match ``name()`` optionally spaced, then ``{``. Bash also allows
    # ``function name {`` but this script uses the ``name() {`` form.
    header = re.compile(
        r"(?:^|\n)[ \t]*(?:function[ \t]+)?" + re.escape(name) + r"[ \t]*\(\)[ \t]*\{"
    )
    m = header.search(script_text)
    assert m is not None, (
        f"function {name}() is not defined in deployControlPlan.sh"
    )

    # Start scanning right after the opening brace.
    open_brace = script_text.index("{", m.start())
    depth = 0
    i = open_brace
    n = len(script_text)
    while i < n:
        c = script_text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return script_text[open_brace + 1:i]
        i += 1
    raise AssertionError(
        f"unbalanced braces while extracting body of {name}() - no matching "
        f"closing '}}' found"
    )


def _references_sync(text):
    """True if the given text invokes the sync CLI, either by calling the
    ``sync_nginx_locations`` helper or by referencing the CLI path directly.
    """
    return (
        SYNC_CLI_BASENAME in text
        or re.search(r"\b" + re.escape(SYNC_HELPER_NAME) + r"\b", text) is not None
    )


# --------------------------------------------------------------------------
# Shared fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def script_text():
    return _read_deploy_script()


# ==========================================================================
# Property 1: Bug Condition - Sync Runs As Final Startup Step
#
# EXPECTED on UNFIXED code: each assertion FAILS because the sync is never
# wired in. After the fix each assertion passes.
#
# _Requirements: 1.1, 1.2, 1.3_
# ==========================================================================

class TestSyncWiredAsFinalStartupStep:
    """The sync CLI must be invoked as the final startup step of both the
    ``locally`` and ``docker`` startup paths.
    """

    def test_locally_startup_invokes_sync(self, script_text):
        """Test case 1 (Locally wiring absent): ``start_local_deployment``
        must reference the sync. FAILS on unfixed code (confirms Req 2.1 gap).

        _Requirements: 1.1_
        """
        body = extract_function_body(script_text, "start_local_deployment")
        assert _references_sync(body), (
            "start_local_deployment() never invokes the nginx location sync: "
            f"neither '{SYNC_HELPER_NAME}' nor '{SYNC_CLI_BASENAME}' appears in "
            "its body. The locally startup path finishes without populating "
            "nginx location config from the database (bug present)."
        )

    def test_docker_startup_invokes_sync(self, script_text):
        """Test case 2 (Docker wiring absent): ``start_docker_deployment`` must
        reference the sync. FAILS on unfixed code (confirms Req 2.2 gap).

        _Requirements: 1.2_
        """
        body = extract_function_body(script_text, "start_docker_deployment")
        assert _references_sync(body), (
            "start_docker_deployment() never invokes the nginx location sync: "
            f"neither '{SYNC_HELPER_NAME}' nor '{SYNC_CLI_BASENAME}' appears in "
            "its body. The docker startup path finishes without populating "
            "nginx location config from the database (bug present)."
        )

    def test_cli_exists_but_is_referenced_by_the_deploy_script(self, script_text):
        """Test case 3 (CLI exists but unreferenced): the sync CLI exists on
        disk, yet ``sync_nginx_locations.py`` never appears in
        ``deployControlPlan.sh`` on unfixed code - the "is referenced"
        assertion FAILS, confirming the omission root cause.

        _Requirements: 1.3_
        """
        # The CLI genuinely exists - this part documents the premise and holds
        # on unfixed code too.
        assert os.path.exists(SYNC_CLI_PATH), (
            f"expected the sync CLI to exist on disk at {SYNC_CLI_PATH}"
        )
        # ...yet the deploy script never references it (this is the failing
        # assertion on unfixed code).
        assert SYNC_CLI_BASENAME in script_text, (
            f"'{SYNC_CLI_BASENAME}' exists on disk but is NEVER referenced "
            "anywhere in deployControlPlan.sh. The deploy script ships the "
            "sync CLI yet never wires it into startup - the omission root "
            "cause is confirmed (bug present)."
        )


class TestSyncIsLastStartupStep:
    """The sync must be the FINAL startup step in each path (after
    configure_firewall for locally; after services up / optional migrate for
    docker), matching the Expected Behavior Property from the design.

    EXPECTED on UNFIXED code: FAILS (no sync reference exists at all).

    _Requirements: 1.1, 1.2_
    """

    def test_locally_sync_is_after_configure_firewall(self, script_text):
        body = extract_function_body(script_text, "start_local_deployment")
        assert _references_sync(body), (
            "start_local_deployment() has no sync step, so it cannot be the "
            "final startup step (bug present)."
        )
        fw_idx = body.rfind("configure_firewall")
        sync_idx = max(
            body.rfind(SYNC_HELPER_NAME),
            body.rfind(SYNC_CLI_BASENAME),
        )
        assert fw_idx != -1, "configure_firewall missing from start_local_deployment"
        assert sync_idx > fw_idx, (
            "the nginx location sync must run AFTER configure_firewall as the "
            "final startup step of the locally path"
        )

    def test_docker_sync_is_after_services_started(self, script_text):
        body = extract_function_body(script_text, "start_docker_deployment")
        assert _references_sync(body), (
            "start_docker_deployment() has no sync step, so it cannot be the "
            "final startup step (bug present)."
        )
        up_idx = body.rfind("docker-compose up")
        sync_idx = max(
            body.rfind(SYNC_HELPER_NAME),
            body.rfind(SYNC_CLI_BASENAME),
        )
        assert up_idx != -1, "docker-compose up missing from start_docker_deployment"
        assert sync_idx > up_idx, (
            "the nginx location sync must run AFTER docker-compose up as the "
            "final startup step of the docker path"
        )


# ==========================================================================
# Counterexample documentation helper
#
# Not a correctness assertion - this test always "passes" and prints the
# counterexamples (how many times the sync appears in each place). It makes the
# exploration findings visible in the test output / captured logs.
# ==========================================================================

def test_report_counterexamples(script_text, capsys):
    local_body = extract_function_body(script_text, "start_local_deployment")
    docker_body = extract_function_body(script_text, "start_docker_deployment")

    whole_count = script_text.count(SYNC_CLI_BASENAME)
    local_has = _references_sync(local_body)
    docker_has = _references_sync(docker_body)
    helper_defined = (
        re.search(r"(?:^|\n)[ \t]*" + re.escape(SYNC_HELPER_NAME) + r"[ \t]*\(\)",
                  script_text) is not None
    )

    print("\n--- nginx-locations-sync exploration counterexamples ---")
    print(f"scripts/{SYNC_CLI_BASENAME} exists on disk: {os.path.exists(SYNC_CLI_PATH)}")
    print(f"'{SYNC_CLI_BASENAME}' appears in deployControlPlan.sh: {whole_count} time(s)")
    print(f"'{SYNC_HELPER_NAME}' helper function defined: {helper_defined}")
    print(f"start_local_deployment body invokes sync: {local_has}")
    print(f"start_docker_deployment body invokes sync: {docker_has}")
    print("--------------------------------------------------------")


# ==========================================================================
# Task 4: `set -e` guard unit assertions + optional shell-level smoke test
#
# Property 1: Expected Behavior - Sync Failure Is Non-Fatal Under set -e
#
# These assertions lock in the fix: the sync is wired as the final startup
# step of both modes, is defined as a reusable helper that references the CLI,
# runs with the venv Python, and is guarded so a non-zero exit cannot abort the
# deploy under `set -e`. An optional bash-level smoke test exercises the guard
# directly with stubbed CLIs that exit 1 and 0.
#
# _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.4_
# ==========================================================================

import base64
import shutil
import subprocess
import textwrap


class TestSetEGuardUnitAssertions:
    """Static (parse-the-script) assertions about the `sync_nginx_locations`
    helper and its wiring. These do not require bash to run.
    """

    def test_sync_invocation_is_guarded_against_set_e(self, script_text):
        """The CLI invocation inside ``sync_nginx_locations`` must be guarded -
        either wrapped in an ``if ...; then ... else ... fi`` or combined with
        ``|| ...`` - so a non-zero exit is consumed and never trips ``set -e``.

        _Requirements: 2.5_
        """
        body = extract_function_body(script_text, SYNC_HELPER_NAME)
        assert SYNC_CLI_BASENAME in body, (
            f"sync_nginx_locations() must reference ./scripts/{SYNC_CLI_BASENAME}"
        )

        # Find the line that actually invokes the CLI.
        invoke_line = next(
            (ln for ln in body.splitlines() if SYNC_CLI_BASENAME in ln and "if " not in ln.strip()[:3]),
            None,
        )
        # The CLI is invoked either as the condition of an `if`, or via `|| `.
        guarded_by_if = bool(
            re.search(
                r"if\s+.*" + re.escape(SYNC_CLI_BASENAME) + r".*;\s*then",
                body,
            )
        )
        guarded_by_or = bool(
            re.search(
                re.escape(SYNC_CLI_BASENAME) + r".*\|\|",
                body,
            )
        )
        assert guarded_by_if or guarded_by_or, (
            "the sync CLI invocation in sync_nginx_locations() is not guarded "
            "against set -e: it must appear as the condition of an "
            "`if ...; then ... else ... fi` or be combined with `|| ...` so a "
            "non-zero exit cannot abort the deploy.\n"
            f"invoke line seen: {invoke_line!r}"
        )

    def test_sync_uses_venv_python(self, script_text):
        """The sync must run with the project's ``.venv`` Python (explicitly
        selecting ``.venv/bin/python``) or run after ``.venv`` has been
        activated, so ``src.nginx_manager`` / ``src.database_postgres`` import.

        _Requirements: 2.3_
        """
        body = extract_function_body(script_text, SYNC_HELPER_NAME)
        uses_venv_python = ".venv/bin/python" in body
        # Fallback acceptance: the script activates .venv (source .venv/bin/activate)
        # before the startup functions run, so a bare `python` would resolve to
        # the venv in-process.
        activates_venv = bool(
            re.search(r"source\s+\.venv/bin/activate", script_text)
            or re.search(r"\.\s+\.venv/bin/activate", script_text)
        )
        assert uses_venv_python or activates_venv, (
            "sync_nginx_locations() must use the venv Python: it should select "
            "'.venv/bin/python' explicitly, or the script must activate .venv "
            "(source .venv/bin/activate) before the sync runs."
        )
        # The implemented fix selects it explicitly - assert that strong form.
        assert uses_venv_python, (
            "expected sync_nginx_locations() to select '.venv/bin/python' "
            "explicitly for robustness (Req 2.3)."
        )

    def test_sync_helper_defined_and_references_cli(self, script_text):
        """``sync_nginx_locations`` must be defined as a bash function and the
        CLI ``scripts/sync_nginx_locations.py`` must be referenced through it.

        _Requirements: 2.1, 2.2_
        """
        helper_defined = bool(
            re.search(
                r"(?:^|\n)[ \t]*(?:function[ \t]+)?"
                + re.escape(SYNC_HELPER_NAME)
                + r"[ \t]*\(\)[ \t]*\{",
                script_text,
            )
        )
        assert helper_defined, (
            f"{SYNC_HELPER_NAME}() is not defined as a function in "
            "deployControlPlan.sh"
        )
        body = extract_function_body(script_text, SYNC_HELPER_NAME)
        assert SYNC_CLI_BASENAME in body, (
            f"scripts/{SYNC_CLI_BASENAME} must be referenced through the "
            f"{SYNC_HELPER_NAME}() helper (it is not referenced inside its body)."
        )

    def test_sync_is_last_startup_call_in_both_paths(self, script_text):
        """``sync_nginx_locations`` must be the LAST startup call in each of
        ``start_local_deployment`` and ``start_docker_deployment``.

        "Last startup call" = the final helper/command invocation in the body;
        trailing ``echo`` status lines (and comments/blank lines) are allowed
        after it.

        _Requirements: 2.6_
        """
        for fn in STARTUP_FUNCTIONS:
            body = extract_function_body(script_text, fn)
            assert _references_sync(body), (
                f"{fn}() does not invoke the sync at all"
            )

            # Collect the meaningful (non-comment, non-blank) statement lines.
            stmt_lines = []
            for raw in body.splitlines():
                ln = raw.strip()
                if not ln or ln.startswith("#"):
                    continue
                stmt_lines.append(ln)

            # The sync call line.
            sync_line_positions = [
                i for i, ln in enumerate(stmt_lines)
                if re.search(r"\b" + re.escape(SYNC_HELPER_NAME) + r"\b", ln)
            ]
            assert sync_line_positions, (
                f"{fn}() references the sync but no explicit "
                f"'{SYNC_HELPER_NAME}' call line was found"
            )
            last_sync = sync_line_positions[-1]

            # Everything after the sync call must be a trailing echo (status
            # message) only - no other helper/command invocation.
            trailing = stmt_lines[last_sync + 1:]
            offending = [ln for ln in trailing if not ln.startswith("echo")]
            assert not offending, (
                f"{SYNC_HELPER_NAME} must be the LAST startup call in {fn}(), "
                f"but these non-echo statements follow it: {offending}"
            )


# --------------------------------------------------------------------------
# Optional shell-level integration smoke test (environment-permitting).
#
# Directly exercises the `set -e` guard: define `sync_nginx_locations` in
# isolation under `set -e`, stub ./scripts/sync_nginx_locations.py to exit 1
# (then 0), run the function, and assert the surrounding script continues
# (exit 0) in BOTH cases, with the expected WARN / OK message.
#
# Skips gracefully when bash is unavailable. The whole harness is built and run
# INSIDE a bash-created temp dir (via mktemp -d), so no Windows<->WSL path
# translation is needed when bash is provided by WSL.
# --------------------------------------------------------------------------

def _extract_sync_function_definition(script_text):
    """Return the full ``sync_nginx_locations() { ... }`` definition text so it
    can be re-defined verbatim inside an isolated harness. Carriage returns are
    stripped so the definition is clean LF-only bash regardless of how the
    script file was read on the host.
    """
    body = extract_function_body(script_text, SYNC_HELPER_NAME)
    definition = SYNC_HELPER_NAME + "() {" + body + "}"
    return definition.replace("\r\n", "\n").replace("\r", "\n")


def _run_guard_smoke(stub_exit_code, script_text):
    """Build a bash harness that re-defines the real ``sync_nginx_locations``
    under ``set -e`` with a stubbed CLI exiting ``stub_exit_code``, run it, and
    return ``(returncode, combined_output)``.

    All files are created inside a bash ``mktemp -d`` directory to avoid any
    path-translation issues across a Windows/WSL boundary. The harness is
    base64-encoded and decoded inside bash so that neither CRLF line endings
    nor the host's console encoding can corrupt the script on the way in.
    """
    sync_def = _extract_sync_function_definition(script_text)

    # The harness runs entirely inside bash. It:
    #   * creates an isolated temp dir and cd's into it
    #   * writes a stub ./scripts/sync_nginx_locations.py that exits N
    #   * defines OK / WARN markers (as the real script does)
    #   * turns on `set -e`
    #   * defines the real sync_nginx_locations function
    #   * calls it and prints a sentinel so we know it continued
    #   * cleans up the temp dir via a trap
    harness = textwrap.dedent(
        """
        OK="[OK]"
        WARN="[WARN]"
        workdir="$(mktemp -d)"
        trap 'rm -rf "$workdir"' EXIT
        cd "$workdir"
        mkdir -p scripts
        cat > scripts/sync_nginx_locations.py <<'PYEOF'
        import sys
        print("stub sync CLI running")
        sys.exit(__EXIT_CODE__)
        PYEOF
        # Turn on set -e only now, right before defining and calling the real
        # helper, so the guard is what's under test (not harness setup).
        set -e
        __SYNC_DEF__
        # Call the function; if `set -e` were tripped by a non-zero sync the
        # script would abort before reaching the sentinel below.
        sync_nginx_locations
        echo "HARNESS_CONTINUED rc=$?"
        """
    ).strip()
    harness = harness.replace("__EXIT_CODE__", str(stub_exit_code))
    harness = harness.replace("__SYNC_DEF__", sync_def)
    harness = harness.replace("\r\n", "\n").replace("\r", "\n")

    # Encode the harness so the Windows->WSL argument boundary cannot mangle it.
    encoded = base64.b64encode(harness.encode("utf-8")).decode("ascii")
    outer = (
        "export LC_ALL=C.UTF-8 2>/dev/null || true; "
        f"printf %s '{encoded}' | base64 -d | bash"
    )

    proc = subprocess.run(
        ["bash", "-c", outer],
        capture_output=True,
        timeout=60,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out + err


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestSetEGuardShellSmoke:
    """Shell-level smoke test of the `set -e` guard (skips if bash missing)."""

    def test_sync_failure_is_non_fatal_under_set_e(self, script_text):
        """Stub the CLI to exit 1 under ``set -e``; the surrounding script must
        continue (overall exit 0) and emit the WARN message - a sync failure
        must not abort the deploy.

        _Requirements: 2.5_
        """
        try:
            rc, out = _run_guard_smoke(1, script_text)
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip(f"bash harness could not run in this environment: {exc}")

        assert "HARNESS_CONTINUED" in out, (
            "the harness did not continue past a failing sync - `set -e` "
            f"appears to have aborted it.\n--- output ---\n{out}"
        )
        assert rc == 0, (
            "the harness exited non-zero after a failing sync; the guard must "
            f"keep the deploy alive (rc={rc}).\n--- output ---\n{out}"
        )
        assert "[WARN]" in out, (
            "expected a WARN message when the sync CLI exits non-zero.\n"
            f"--- output ---\n{out}"
        )

    def test_sync_success_completes_normally(self, script_text):
        """Companion stub exiting 0: the helper reports success (OK) and the
        script completes normally (overall exit 0).

        _Requirements: 2.4, 3.4_
        """
        try:
            rc, out = _run_guard_smoke(0, script_text)
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip(f"bash harness could not run in this environment: {exc}")

        assert "HARNESS_CONTINUED" in out, (
            f"the harness did not complete after a successful sync.\n"
            f"--- output ---\n{out}"
        )
        assert rc == 0, (
            f"the harness exited non-zero after a successful sync (rc={rc}).\n"
            f"--- output ---\n{out}"
        )
        assert "[OK]" in out, (
            "expected an OK success message when the sync CLI exits 0.\n"
            f"--- output ---\n{out}"
        )


# --------------------------------------------------------------------------
# Manual verification note (documented, not automated).
#
# Full end-to-end verification - that `sync_nginx_locations` actually populates
# the live nginx location config from the database - requires a host with a
# running nginx AND a reachable PostgreSQL instance so that
# `src.nginx_manager.sync_all_locations(db_manager)` can connect and write real
# config. No live nginx/PostgreSQL is reachable in this test environment, so
# that path is a MANUAL check:
#
#   1. Deploy locally:   ./deployControlPlan.sh start --locally
#      (or docker:       ./deployControlPlan.sh start --docker)
#   2. Observe the final "Syncing nginx locations from database..." step and
#      the "[OK] Nginx locations synced from database" message.
#   3. Confirm nginx now serves the location config matching the DB rows.
#
# The automated tests above cover the wiring, the venv-Python selection, and
# the `set -e` guard behavior - everything verifiable without live services.
# --------------------------------------------------------------------------
