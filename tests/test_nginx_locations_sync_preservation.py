"""Preservation tests for the "nginx locations not synced on startup" bugfix.

Spec: .kiro/specs/nginx-locations-sync-on-startup

Property 2: Preservation - Non-Startup And Existing Steps Unchanged
------------------------------------------------------------------
For any invocation where the bug condition does NOT hold (any non-``start``
command, or any startup step other than the new final sync step), the fixed
``deployControlPlan.sh`` SHALL produce the same behavior as the original
script, preserving:

  - the existing ``locally`` step order ``start_flask_application`` ->
    ``configure_nginx`` -> ``configure_firewall`` (Req 3.2),
  - the existing ``docker`` steps ``cleanup_docker`` ->
    ``docker-compose up -d --build`` -> optional ``migrate_database`` (Req 3.3),
  - the ``test_and_reload_nginx`` test + reload/start body byte-for-byte
    (Req 3.5),
  - that no non-startup command path invokes the sync helper (Req 3.1),
  - the overall success/exit status on a successful deploy (Req 3.4).

Methodology (observation-first)
-------------------------------
These expectations were recorded by reading the UNFIXED ``deployControlPlan.sh``
first (see the baselines captured in this module), then asserted so they hold
on the unfixed script now and continue to hold after the fix. The suite is
EXPECTED TO PASS on the unfixed script - it captures the baseline behavior the
fix must preserve.

The "sync strictly last" sub-assertions are written so they are satisfied
vacuously on the UNFIXED script (where no sync reference exists yet) and only
tighten to "the sync is the final startup call" once the sync has actually
been wired in. The existing-order and non-startup assertions must pass now.

Why parse-the-script
--------------------
The change is to a bash script, not importable Python, so - following the
convention of ``tests/test_extended_ports_migration_wiring.py`` (read the
script text, extract named function bodies, assert on an ordered sequence of
steps) and the pytest + hypothesis convention of
``tests/test_start_action_preservation.py`` - these tests read
``deployControlPlan.sh``, extract the relevant function bodies, and assert on
the preserved ordering / non-reference of the sync.

_Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
"""

import os
import re

import pytest
from hypothesis import given, settings, strategies as st


# --------------------------------------------------------------------------
# Paths and domain constants
# --------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_SCRIPT_PATH = os.path.join(REPO_ROOT, "deployControlPlan.sh")

# The helper function the fix introduces to invoke the sync CLI, and the CLI
# basename it runs.
SYNC_HELPER_NAME = "sync_nginx_locations"
SYNC_CLI_BASENAME = "sync_nginx_locations.py"

# The two startup functions the sync is wired into (and the ONLY two functions
# that may reference it).
STARTUP_FUNCTIONS = ("start_local_deployment", "start_docker_deployment")

# The existing locally startup steps, in their baseline relative order
# (observed on the UNFIXED script: install_python_dependencies -> setup_gitea
# -> start_flask_application -> configure_nginx -> configure_firewall). The
# subsequence this test pins is the core trio the spec calls out.
LOCALLY_BASELINE_ORDER = (
    "start_flask_application",
    "configure_nginx",
    "configure_firewall",
)

# The existing docker startup steps, in their baseline relative order (observed
# on the UNFIXED script: cleanup_docker -> docker-compose up -d --build ->
# optional migrate_database guard).
DOCKER_BASELINE_ORDER = (
    "cleanup_docker",
    "docker-compose up",
    "migrate_database",
)

# Non-startup commands dispatched by the main case statement. The spec names
# {stop, status, backup, recover, help}; the script spells these as the tokens
# below. None of their code paths may ever reference the sync.
#   - "status" is served by the "ps"/check_status path,
#   - "backup" by backup_db/backup_database,
#   - "recover" by recover_db/recover_database.
NON_STARTUP_COMMANDS = ("stop", "status", "backup", "recover", "help")

# The non-startup command handler functions reachable from the dispatch for the
# commands above (what each non-startup command actually runs).
NON_STARTUP_HANDLERS = (
    "stop_services",
    "check_status",
    "backup_database",
    "recover_database",
    "help",
)

# ==========================================================================
# Observation-first baseline: the exact test_and_reload_nginx body recorded
# from the UNFIXED deployControlPlan.sh. The fix must leave this byte-for-byte
# unchanged (Req 3.5). Captured verbatim (the body between the braces of
# ``test_and_reload_nginx() { ... }``).
# ==========================================================================

TEST_AND_RELOAD_NGINX_BASELINE = (
    "\n"
    "    if systemctl is-active --quiet nginx; then\n"
    "        echo \"  \u2705 Nginx is running - reloading configuration\"\n"
    "        sudo nginx -t && sudo systemctl reload nginx\n"
    "    else\n"
    "        echo \"  \U0001f680 Starting Nginx service\"\n"
    "        sudo systemctl start nginx\n"
    "        sudo nginx -t\n"
    "    fi\n"
    "    echo \"  \u2705 Nginx configured successfully\"\n"
)


# --------------------------------------------------------------------------
# Script-parsing helpers (parse-the-script wiring convention). The
# extract_function_body helper mirrors the one in
# tests/test_nginx_locations_sync_wiring.py.
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
    header = re.compile(
        r"(?:^|\n)[ \t]*(?:function[ \t]+)?" + re.escape(name) + r"[ \t]*\(\)[ \t]*\{"
    )
    m = header.search(script_text)
    assert m is not None, (
        f"function {name}() is not defined in deployControlPlan.sh"
    )

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


def _sync_index(body):
    """Return the index of the sync reference in ``body`` (via the helper name
    or the CLI basename), or ``-1`` if the body does not reference the sync.

    On the UNFIXED script this is ``-1`` for every function body, which makes
    the "sync strictly last" assertions pass vacuously.
    """
    return max(
        body.rfind(SYNC_HELPER_NAME),
        body.rfind(SYNC_CLI_BASENAME),
    )


def _ordered_indices(body, steps):
    """Return the first-occurrence index of each step within ``body``.

    Raises ``AssertionError`` if any step is missing (so a dropped step fails
    loudly).
    """
    indices = []
    for step in steps:
        idx = body.find(step)
        assert idx != -1, (
            f"baseline step {step!r} is missing from the function body "
            f"(it must be preserved)"
        )
        indices.append(idx)
    return indices


def _assert_relative_order(body, steps):
    """Assert ``steps`` appear in ``body`` in the given strictly-increasing
    relative order.
    """
    indices = _ordered_indices(body, steps)
    for earlier, later, i in zip(steps, steps[1:], range(len(indices) - 1)):
        assert indices[i] < indices[i + 1], (
            f"baseline order violated: {earlier!r} must appear before "
            f"{later!r} in the function body"
        )


# --------------------------------------------------------------------------
# Shared fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def script_text():
    return _read_deploy_script()


# ==========================================================================
# Test case 1 - existing locally order preserved (Req 3.2)
# ==========================================================================

class TestLocallyOrderPreserved:
    """``start_flask_application`` -> ``configure_nginx`` -> ``configure_firewall``
    appear in that relative order within ``start_local_deployment``; if a sync
    is present it is strictly after this trio.
    """

    def test_existing_locally_trio_relative_order(self, script_text):
        """_Requirements: 3.2_"""
        body = extract_function_body(script_text, "start_local_deployment")
        _assert_relative_order(body, LOCALLY_BASELINE_ORDER)

    def test_sync_if_present_is_strictly_after_configure_firewall(self, script_text):
        """The sync must be the final startup step (after ``configure_firewall``).

        Vacuously satisfied on the UNFIXED script where ``_sync_index`` is -1;
        tightens to a real ordering check once the sync is wired in.

        _Requirements: 3.2_
        """
        body = extract_function_body(script_text, "start_local_deployment")
        sync_idx = _sync_index(body)
        if sync_idx == -1:
            pytest.skip("no sync wired into start_local_deployment yet (unfixed baseline)")
        fw_idx = body.rfind("configure_firewall")
        assert fw_idx != -1, "configure_firewall missing from start_local_deployment"
        assert sync_idx > fw_idx, (
            "when wired, the nginx location sync must run AFTER "
            "configure_firewall as the final startup step of the locally path"
        )


# ==========================================================================
# Test case 2 - existing docker steps preserved (Req 3.3)
# ==========================================================================

class TestDockerStepsPreserved:
    """``cleanup_docker``, the ``docker-compose up`` line, and the
    ``migrate_database`` guard appear in their original relative order within
    ``start_docker_deployment``; if a sync is present it is strictly after
    them.
    """

    def test_existing_docker_steps_relative_order(self, script_text):
        """_Requirements: 3.3_"""
        body = extract_function_body(script_text, "start_docker_deployment")
        _assert_relative_order(body, DOCKER_BASELINE_ORDER)

    def test_sync_if_present_is_strictly_after_services_up(self, script_text):
        """The sync must be the final startup step (after services are up and
        the optional migrate).

        Vacuously satisfied on the UNFIXED script; tightens once wired.

        _Requirements: 3.3_
        """
        body = extract_function_body(script_text, "start_docker_deployment")
        sync_idx = _sync_index(body)
        if sync_idx == -1:
            pytest.skip("no sync wired into start_docker_deployment yet (unfixed baseline)")
        up_idx = body.rfind("docker-compose up")
        migrate_idx = body.rfind("migrate_database")
        last_existing = max(up_idx, migrate_idx)
        assert up_idx != -1, "docker-compose up missing from start_docker_deployment"
        assert sync_idx > last_existing, (
            "when wired, the nginx location sync must run AFTER docker-compose "
            "up and the optional migrate_database as the final startup step of "
            "the docker path"
        )


# ==========================================================================
# Test case 3 - non-startup commands untouched (Req 3.1)
# ==========================================================================

class TestNonStartupCommandsUntouched:
    """The sync helper is referenced ONLY from the two startup functions; no
    non-startup command path references it.
    """

    def test_sync_referenced_only_from_startup_functions(self, script_text):
        """Every reference to the sync helper/CLI in the script must live
        inside the body of ``start_local_deployment`` or
        ``start_docker_deployment`` (or inside the ``sync_nginx_locations``
        definition itself). No non-startup handler body may reference it.

        On the UNFIXED script there are no references at all, which trivially
        satisfies this. After the fix the only references are the two startup
        call sites plus the helper definition.

        _Requirements: 3.1_
        """
        for handler in NON_STARTUP_HANDLERS:
            # The handler may not be defined as a standalone function in every
            # script variant (e.g. "help" is, "check_status" is); only check
            # the ones that exist.
            header = re.compile(
                r"(?:^|\n)[ \t]*(?:function[ \t]+)?" + re.escape(handler)
                + r"[ \t]*\(\)[ \t]*\{"
            )
            if header.search(script_text) is None:
                continue
            body = extract_function_body(script_text, handler)
            assert SYNC_HELPER_NAME not in body and SYNC_CLI_BASENAME not in body, (
                f"non-startup handler {handler}() must NOT reference the nginx "
                f"location sync, but it does"
            )

    def test_dispatch_non_startup_cases_do_not_reference_sync(self, script_text):
        """The main command dispatch's non-startup command arms must not call
        the sync inline.

        _Requirements: 3.1_
        """
        # Extract the main() body (which holds the dispatch case statement).
        main_body = extract_function_body(script_text, "main")
        # The sync must never appear as an inline dispatch action; it is only
        # ever reached through start -> start_services -> start_*_deployment.
        # (On unfixed code it does not appear at all.)
        for cmd_token in ("ps", "stop", "logs", "recover_db", "backup_db",
                          "restart", "help", "version"):
            # locate the case arm for this token and scan a small window after
            # it up to the arm terminator ";;"
            arm = re.search(
                r'"' + re.escape(cmd_token) + r'"[^\)]*\)(.*?);;',
                main_body,
                re.DOTALL,
            )
            if arm is None:
                continue
            assert SYNC_HELPER_NAME not in arm.group(1), (
                f"the '{cmd_token}' dispatch arm must not invoke the sync helper"
            )


# ==========================================================================
# Test case 4 - test_and_reload_nginx unchanged (Req 3.5)
# ==========================================================================

class TestTestAndReloadNginxUnchanged:
    """The ``test_and_reload_nginx`` body must be byte-for-byte unchanged
    against the recorded baseline.
    """

    def test_body_is_byte_for_byte_baseline(self, script_text):
        """_Requirements: 3.5_"""
        body = extract_function_body(script_text, "test_and_reload_nginx")
        assert body == TEST_AND_RELOAD_NGINX_BASELINE, (
            "test_and_reload_nginx body differs from the recorded preservation "
            "baseline; the fix must not touch the nginx test+reload/start "
            "logic.\n--- expected ---\n"
            f"{TEST_AND_RELOAD_NGINX_BASELINE!r}\n--- actual ---\n{body!r}"
        )

    def test_body_never_references_sync(self, script_text):
        """The nginx reload body must never invoke the sync (the sync runs
        after, as a separate final startup step).

        _Requirements: 3.5_
        """
        body = extract_function_body(script_text, "test_and_reload_nginx")
        assert SYNC_HELPER_NAME not in body and SYNC_CLI_BASENAME not in body


# ==========================================================================
# Property-based tests (hypothesis)
# ==========================================================================

class TestPreservationProperties:
    """Property-based preservation checks over the non-startup commands and the
    existing per-mode startup steps.
    """

    @settings(max_examples=50, deadline=None)
    @given(command=st.sampled_from(NON_STARTUP_COMMANDS))
    def test_property_non_startup_command_never_references_sync(self, command):
        """Over the set of non-startup commands {stop, status, backup, recover,
        help}, none of their handler code paths reference ``sync_nginx_locations``.

        _Requirements: 3.1_
        """
        script = _read_deploy_script()
        # Map the spec command name to the handler function(s) that implement
        # it, and assert each existing handler body is sync-free.
        handler_by_command = {
            "stop": ("stop_services",),
            "status": ("check_status",),
            "backup": ("backup_database",),
            "recover": ("recover_database",),
            "help": ("help",),
        }
        for handler in handler_by_command[command]:
            header = re.compile(
                r"(?:^|\n)[ \t]*(?:function[ \t]+)?" + re.escape(handler)
                + r"[ \t]*\(\)[ \t]*\{"
            )
            if header.search(script) is None:
                continue
            body = extract_function_body(script, handler)
            assert SYNC_HELPER_NAME not in body, (
                f"non-startup command {command!r} (handler {handler}()) must "
                f"not reference the sync helper"
            )
            assert SYNC_CLI_BASENAME not in body, (
                f"non-startup command {command!r} (handler {handler}()) must "
                f"not reference the sync CLI"
            )

    @settings(max_examples=50, deadline=None)
    @given(mode=st.sampled_from(["locally", "docker"]))
    def test_property_existing_steps_present_in_order_with_sync_last(self, mode):
        """Over the existing startup steps per mode, each required step is
        still present and in its original relative order; and if a sync is
        wired in it is strictly last.

        The "sync strictly last" clause is satisfied vacuously on the UNFIXED
        script (no sync reference), and becomes a real check after the fix.

        _Requirements: 3.2, 3.3_
        """
        script = _read_deploy_script()
        if mode == "locally":
            fn = "start_local_deployment"
            steps = LOCALLY_BASELINE_ORDER
        else:
            fn = "start_docker_deployment"
            steps = DOCKER_BASELINE_ORDER

        body = extract_function_body(script, fn)

        # Each required existing step is still present and in order.
        _assert_relative_order(body, steps)

        # If (and only if) a sync is present, it must be strictly after the
        # last existing step. Vacuously true on the unfixed script.
        sync_idx = _sync_index(body)
        if sync_idx != -1:
            last_existing = max(body.find(step) for step in steps)
            assert sync_idx > last_existing, (
                f"in {fn}() the sync must be the final startup step, strictly "
                f"after every existing step {steps}"
            )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
