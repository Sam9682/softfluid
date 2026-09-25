#!/usr/bin/env bash
# Bug condition exploration / Property 1 test for the
# "stop-command-not-stopping-fix" bugfix spec.
#
#   Property 1 (Bug Condition -> Expected Behavior):
#     FOR ALL X WHERE isBugCondition(X)         # command="stop" AND backup exit != 0
#       stop_services(X) SHALL log a warning AND execute all applicable
#       service-stopping commands to completion (script does not abort early).
#
# This is a SCOPED property-based test: the bug is deterministic, so we
# enumerate the concrete failing configurations that make up the bug condition
# input space:
#
#     FAIL_MODE in { pg_isready, pg_dump }        # both backup_database return-1 paths
#     MODE      in { locally, docker, default }   # every LOCAL_MODE branch
#
# For each (MODE, FAIL_MODE) pair the property asserts the Expected Behavior.
#
# EXPECTED OUTCOME ON UNFIXED CODE: this test FAILS. The failures ARE the
# counterexamples that prove the bug: with the backup forced to fail, the bare
# `backup_database` call under `set -e` aborts stop_services before any stop
# command runs, so the recorded stop commands are absent and the harness
# reports result=ABORTED_EARLY.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR/stop_services_harness.sh"

MODES=(locally docker default)
FAIL_MODES=(pg_isready pg_dump)

# Which stop commands are expected to run for each mode (Expected Behavior /
# mode-specific stop logic per Requirements 2.3, 3.2).
expected_for_mode() {
    case "$1" in
        locally) echo "flask nginx gitea" ;;
        docker)  echo "docker" ;;
        default) echo "flask nginx gitea docker" ;;
    esac
}

total=0
failed=0
declare -a COUNTEREXAMPLES=()

for MODE in "${MODES[@]}"; do
    for FAIL_MODE in "${FAIL_MODES[@]}"; do
        total=$((total + 1))
        probe="$(MODE="$MODE" FAIL_MODE="$FAIL_MODE" bash "$HARNESS" | grep '^PROBE ' | tail -n1)"

        # Parse probe fields.
        result="$(sed -n 's/.*result=\([A-Z_]*\).*/\1/p' <<<"$probe")"
        get() { sed -n "s/.* $1=\([0-9]\).*/\1/p" <<<"$probe"; }
        flask="$(get flask)"; nginx="$(get nginx)"
        docker="$(get docker)"; gitea="$(get gitea)"; warning="$(get warning)"

        # Build the actual set of recorded stop commands.
        actual=""
        [ "${flask:-0}"  = "1" ] && actual="$actual flask"
        [ "${nginx:-0}"  = "1" ] && actual="$actual nginx"
        [ "${docker:-0}" = "1" ] && actual="$actual docker"
        [ "${gitea:-0}"  = "1" ] && actual="$actual gitea"
        actual="$(echo $actual)"

        want="$(expected_for_mode "$MODE")"
        want="$(echo $want)"

        # Property 1 (Requirement 2.3) is a SET-membership property: all
        # applicable stop commands must run, order is irrelevant. Normalise both
        # lists by sorting their tokens so the comparison is order-insensitive
        # while still requiring exactly the expected set (no more, no fewer).
        actual_sorted="$(printf '%s\n' $actual | sort | tr '\n' ' ' | sed 's/ *$//')"
        want_sorted="$(printf '%s\n' $want | sort | tr '\n' ' ' | sed 's/ *$//')"

        ok=1
        reason=""

        # Property assertion 1: script did not abort early.
        if [ "$result" != "COMPLETED" ]; then
            ok=0
            reason="script aborted early (result=$result); no stop command ran"
        fi

        # Property assertion 2: exactly the applicable stop commands executed
        # (order-insensitive set comparison).
        if [ "$ok" = "1" ] && [ "$actual_sorted" != "$want_sorted" ]; then
            ok=0
            reason="expected stop commands [$want] but recorded [$actual]"
        fi

        # Property assertion 3: a warning was logged.
        if [ "$ok" = "1" ] && [ "${warning:-0}" != "1" ]; then
            ok=0
            reason="no warning logged for failed pre-stop backup"
        fi

        if [ "$ok" = "1" ]; then
            echo "PASS  MODE=$MODE FAIL_MODE=$FAIL_MODE  -> $probe"
        else
            failed=$((failed + 1))
            echo "FAIL  MODE=$MODE FAIL_MODE=$FAIL_MODE  -> $reason"
            echo "        probe: $probe"
            COUNTEREXAMPLES+=("MODE=$MODE, FAIL_MODE=$FAIL_MODE: $reason")
        fi
    done
done

echo ""
echo "=============================================================="
echo "Property 1 (Bug Condition -> Stop Always Proceeds When Backup Fails)"
echo "Ran $total cases, $failed failed."
if [ "$failed" -ne 0 ]; then
    echo ""
    echo "COUNTEREXAMPLES (these prove the bug on unfixed code):"
    for ce in "${COUNTEREXAMPLES[@]}"; do
        echo "  - $ce"
    done
    echo "=============================================================="
    exit 1
fi
echo "All cases satisfied the property."
echo "=============================================================="
exit 0
