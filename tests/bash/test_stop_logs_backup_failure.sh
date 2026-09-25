#!/usr/bin/env bash
# Bug condition exploration / Property 1 (facet 2) test for the
# "stop-command-not-stopping-fix" bugfix spec.
#
#   Property 1 (Bug Condition -> Expected Behavior), facet 2 (logs backup):
#     FOR ALL X WHERE isBugCondition(X)      # command="stop" AND backup_logs exit != 0
#       stop_services(X) SHALL log a warning AND execute all applicable
#       service-stopping commands to completion (script does not abort early).
#
# This drives the SECOND facet of the bug: the bare `aws s3 sync ./logs ...`
# inside backup_logs. The harness (LOGS_FAIL=true) exercises the REAL
# backup_logs against a real ./logs directory with an `aws` stub that fails
# specifically on the logs sync path (simulating SignatureDoesNotMatch from
# invalid/expired S3 credentials) while the database backup succeeds. This is a
# SCOPED property-based enumeration over every LOCAL_MODE branch:
#
#     MODE in { locally, docker, default }   # every LOCAL_MODE branch
#     FAIL_MODE = none                        # DB backup succeeds; logs sync fails
#     LOGS_FAIL = true                        # bare `aws s3 sync ./logs` fails
#
# EXPECTED OUTCOME ON THE PRE-FIX backup_logs (bare `aws s3 sync ./logs`): this
# test FAILS. With the logs sync forced to fail, the bare call under `set -e`
# aborts stop_services AFTER the (best-effort) DB backup but BEFORE any stop
# command runs -> result=ABORTED_EARLY, no stop command recorded.
#
# EXPECTED OUTCOME AFTER THE FIX (if/else-guarded logs sync + best-effort call
# site): this test PASSES. A warning is logged and all applicable stop commands
# run to completion.
#
# Validates: Requirements 2.4 (and 2.3), plus mode-specific stop logic (3.2).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR/stop_services_harness.sh"

MODES=(locally docker default)

# Which stop commands are expected to run for each mode (Expected Behavior /
# mode-specific stop logic per Requirements 2.3, 2.4, 3.2).
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
    total=$((total + 1))
    probe="$(MODE="$MODE" FAIL_MODE=none LOGS_FAIL=true bash "$HARNESS" \
             | grep '^PROBE ' | tail -n1)"

    result="$(sed -n 's/.*result=\([A-Z_]*\).*/\1/p' <<<"$probe")"
    get() { sed -n "s/.* $1=\([0-9]\).*/\1/p" <<<"$probe"; }
    flask="$(get flask)"; nginx="$(get nginx)"
    docker="$(get docker)"; gitea="$(get gitea)"; warning="$(get warning)"

    actual=""
    [ "${flask:-0}"  = "1" ] && actual="$actual flask"
    [ "${nginx:-0}"  = "1" ] && actual="$actual nginx"
    [ "${docker:-0}" = "1" ] && actual="$actual docker"
    [ "${gitea:-0}"  = "1" ] && actual="$actual gitea"
    actual="$(echo $actual)"

    want="$(expected_for_mode "$MODE")"
    want="$(echo $want)"

    # Order-insensitive set comparison (all applicable stop commands must run).
    actual_sorted="$(printf '%s\n' $actual | sort | tr '\n' ' ' | sed 's/ *$//')"
    want_sorted="$(printf '%s\n' $want | sort | tr '\n' ' ' | sed 's/ *$//')"

    ok=1
    reason=""

    # Property assertion 1: script did not abort early.
    if [ "$result" != "COMPLETED" ]; then
        ok=0
        reason="script aborted early (result=$result); no stop command ran"
    fi

    # Property assertion 2: exactly the applicable stop commands executed.
    if [ "$ok" = "1" ] && [ "$actual_sorted" != "$want_sorted" ]; then
        ok=0
        reason="expected stop commands [$want] but recorded [$actual]"
    fi

    # Property assertion 3: a warning was logged for the failed logs backup.
    if [ "$ok" = "1" ] && [ "${warning:-0}" != "1" ]; then
        ok=0
        reason="no warning logged for failed pre-stop logs backup"
    fi

    if [ "$ok" = "1" ]; then
        echo "PASS  MODE=$MODE LOGS_FAIL=true  -> $probe"
    else
        failed=$((failed + 1))
        echo "FAIL  MODE=$MODE LOGS_FAIL=true  -> $reason"
        echo "        probe: $probe"
        COUNTEREXAMPLES+=("MODE=$MODE, LOGS_FAIL=true: $reason")
    fi
done

echo ""
echo "=============================================================="
echo "Property 1 facet 2 (Bug Condition -> Stop Always Proceeds When Logs Backup Fails)"
echo "Ran $total cases, $failed failed."
if [ "$failed" -ne 0 ]; then
    echo ""
    echo "COUNTEREXAMPLES (these prove the logs-backup facet of the bug):"
    for ce in "${COUNTEREXAMPLES[@]}"; do
        echo "  - $ce"
    done
    echo "=============================================================="
    exit 1
fi
echo "All cases satisfied the property."
echo "=============================================================="
exit 0
