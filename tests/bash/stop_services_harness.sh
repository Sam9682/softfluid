#!/usr/bin/env bash
# Test harness for the "stop-command-not-stopping" bugfix spec.
#
# It extracts the REAL `stop_services` and `backup_database` function bodies
# straight out of deployControlPlan.sh (so the actual code under test is
# exercised, including the bare `backup_database` call inside stop_services)
# and runs `stop_services` in a controlled sandbox with `set -e` active,
# exactly as the real script runs (`set -e` on line 6).
#
# Only these two functions are extracted; every external command and every
# other service function is stubbed, so the real system is never touched and
# we can record which stop commands were actually invoked. Extracting by
# function name (via brace counting) keeps the harness robust if surrounding
# code shifts. Neither extracted function contains a heredoc, so brace
# counting is unambiguous.
#
# Parameters:
#   MODE       = locally | docker | default      -> sets LOCAL_MODE
#   FAIL_MODE  = pg_isready | pg_dump | none      -> how backup_database fails
#   KEEP_GITEA = true | false                     -> sets KEEP_GITEA_RUNNING
#   LOGS_FAIL  = true | false (default false)     -> exercise the REAL backup_logs
#                with a real ./logs dir present and an `aws` stub that FAILS
#                specifically on the logs `s3 sync ./logs ...` path (facet 2 of
#                the bug: the bare `aws s3 sync ./logs` inside backup_logs).
#                When false (default), backup_logs is stubbed to a no-op that
#                records the pre-stop logs step ran (preserves original harness
#                behaviour for the facet-1 / preservation tests).
#
# Output: a single PROBE line summarising the outcome:
#   PROBE result=<COMPLETED|ABORTED_EARLY> flask=<0|1> nginx=<0|1> \
#         docker=<0|1> gitea=<0|1> warning=<0|1> backup=<0|1> seq=<comma-list>
#
# The `backup=` flag records whether the pre-stop backup step ran to completion
# (backup_database returned and the logs backup afterwards ran); `seq=` records
# the ordered sequence of recorded steps (backup marker followed by the
# mode-specific stop commands) so the successful-path ordering can be asserted.
#
# On the UNFIXED script with FAIL_MODE != none, the bare `backup_database`
# call returns non-zero and `set -e` aborts stop_services before any stop
# function runs -> result=ABORTED_EARLY, all flags 0. That is the counterexample.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_SCRIPT="$REPO_ROOT/deployControlPlan.sh"

MODE="${MODE:-default}"
FAIL_MODE="${FAIL_MODE:-none}"
KEEP_GITEA="${KEEP_GITEA:-false}"
LOGS_FAIL="${LOGS_FAIL:-false}"

SANDBOX="$(mktemp -d)"
RECORD_FILE="$SANDBOX/record"
: > "$RECORD_FILE"

cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Extract a single shell function definition by name, using brace counting
# from the `name() {` header line to its matching closing brace.
# ---------------------------------------------------------------------------
extract_func() {
    local fname="$1" file="$2"
    awk -v fn="$fname" '
        $0 ~ "^"fn"\\(\\) \\{" { capture=1; depth=0 }
        capture {
            print
            n = gsub(/\{/, "{"); depth += n
            m = gsub(/\}/, "}"); depth -= m
            if (depth <= 0) { exit }
        }
    ' "$file"
}

FUNCS_FILE="$SANDBOX/functions.sh"
{
    extract_func "backup_database" "$TARGET_SCRIPT"
    echo ""
    extract_func "backup_logs" "$TARGET_SCRIPT"
    echo ""
    extract_func "stop_services" "$TARGET_SCRIPT"
} > "$FUNCS_FILE"

# Sanity check: all three functions must have been extracted.
if ! grep -q '^backup_database() {' "$FUNCS_FILE" \
   || ! grep -q '^backup_logs() {' "$FUNCS_FILE" \
   || ! grep -q '^stop_services() {' "$FUNCS_FILE"; then
    echo "HARNESS-ERROR: failed to extract required functions from $TARGET_SCRIPT" >&2
    exit 3
fi

# ---------------------------------------------------------------------------
# Run the scenario in a child bash that mirrors the real script's `set -e`.
# ---------------------------------------------------------------------------
MODE="$MODE" FAIL_MODE="$FAIL_MODE" KEEP_GITEA="$KEEP_GITEA" LOGS_FAIL="$LOGS_FAIL" \
RECORD_FILE="$RECORD_FILE" \
FUNCS_FILE="$FUNCS_FILE" SANDBOX="$SANDBOX" \
bash <<'CHILD'
set -e
cd "$SANDBOX"

# --- Stub external commands used by backup_database / backup_logs ---
pg_isready() { [ "$FAIL_MODE" = "pg_isready" ] && return 1; return 0; }
pg_dump()    { [ "$FAIL_MODE" = "pg_dump" ] && return 1; return 0; }
get_server_ip() { echo "127.0.0.1"; }

# The `aws` stub distinguishes the logs sync path from the DB backup sync path.
# When LOGS_FAIL=true, `aws s3 sync ./logs ...` (facet 2) returns non-zero
# (simulating SignatureDoesNotMatch), while the DB backup sync still succeeds.
aws() {
    if [ "$LOGS_FAIL" = "true" ] && [ "${1:-}" = "s3" ] && [ "${2:-}" = "sync" ] \
       && [ "${3:-}" = "./logs" ]; then
        echo "fatal error: An error occurred (SignatureDoesNotMatch)" >&2
        return 1
    fi
    return 0
}

# --- Load the REAL function definitions under test ---
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Wrap the REAL backup_database so we can record that the pre-stop backup
#     step ran while PRESERVING its real return code (so `set -e` behaviour on
#     the bare call inside stop_services is unchanged). The marker is recorded
#     only when the real backup succeeds, mirroring "backup created, then
#     services stop". ---
_real_backup_database() { :; }
eval "$(declare -f backup_database | sed '1s/^backup_database/_real_backup_database/')"
backup_database() {
    if _real_backup_database "$@"; then
        echo "backup" >> "$RECORD_FILE"
        return 0
    else
        return 1
    fi
}

# --- backup_logs handling ---
# LOGS_FAIL=false (default): stub backup_logs to a no-op that records the
#   pre-stop logs step ran (preserves the original harness behaviour used by
#   the facet-1 / preservation tests; keeps the DB backup the sole variable).
# LOGS_FAIL=true: exercise the REAL extracted backup_logs against a real ./logs
#   dir with the failing `aws s3 sync ./logs` stub above, so facet 2 (bare
#   logs sync under set -e) is driven exactly as in production. Wrap it so the
#   "logs" marker is recorded when it returns success, while PRESERVING its real
#   return code (so `set -e` behaviour on the bare call is unchanged).
if [ "$LOGS_FAIL" = "true" ]; then
    # A real, non-empty logs dir so backup_logs takes the sync branch.
    mkdir -p ./logs
    echo "sample log line" > ./logs/app.log
    # Wrap the REAL backup_logs so it records the "logs" marker as its FIRST
    # action (before the sync attempt), then runs the real body as a BARE
    # statement. Calling the real body bare (NOT inside `if`/`&&`) is essential:
    # it preserves `set -e` semantics inside the function exactly as production,
    # so a failing bare `aws s3 sync ./logs` propagates non-zero / aborts just
    # as it does in the real script. The marker is recorded up-front so the
    # "logs step was reached" fact survives even if the body then aborts.
    _real_backup_logs() { :; }
    eval "$(declare -f backup_logs | sed '1s/^backup_logs/_real_backup_logs/')"
    backup_logs() {
        echo "logs" >> "$RECORD_FILE"
        _real_backup_logs "$@"
    }
else
    backup_logs() { echo "logs" >> "$RECORD_FILE"; return 0; }
fi

# --- Override service-stopping functions to RECORD invocations (win over any
#     definitions; these are not among the extracted two but stop_services
#     calls them) ---
remove_backup_cron()  { return 0; }
remove_nginx_config() { return 0; }
provide_cleanup_guidance() { return 0; }
stop_flask_service()  { echo "flask"  >> "$RECORD_FILE"; return 0; }
stop_nginx_service()  { echo "nginx"  >> "$RECORD_FILE"; return 0; }
stop_docker_services(){ echo "docker" >> "$RECORD_FILE"; return 0; }
confirm_gitea_stop()  { echo "gitea"  >> "$RECORD_FILE"; return 0; }

case "$MODE" in
    locally) LOCAL_MODE="locally" ;;
    docker)  LOCAL_MODE="docker" ;;
    *)       LOCAL_MODE="default" ;;
esac

# Minimal globals referenced by the extracted functions.
NAME_OF_APPLICATION="opcp-explorer"
S3_BUCKET_NAME="test-bucket"
KEEP_GITEA_RUNNING="$KEEP_GITEA"
OK="OK"
ERROR="ERR"

OUT="$SANDBOX/output.log"

# Function under test. On UNFIXED code with a failing backup, `set -e` aborts
# on the bare `backup_database` line and the PROBE below never prints.
stop_services > "$OUT" 2>&1

flask=0; nginx=0; docker=0; gitea=0; backup=0; logs=0
grep -qx "flask"  "$RECORD_FILE" && flask=1
grep -qx "nginx"  "$RECORD_FILE" && nginx=1
grep -qx "docker" "$RECORD_FILE" && docker=1
grep -qx "gitea"  "$RECORD_FILE" && gitea=1
grep -qx "backup" "$RECORD_FILE" && backup=1
grep -qx "logs"   "$RECORD_FILE" && logs=1

# Ordered sequence of recorded steps (comma-separated), preserving the order in
# which they were appended to the record file.
seq="$(paste -sd, "$RECORD_FILE" 2>/dev/null)"
[ -z "$seq" ] && seq="-"

warning=0
if grep -qi "backup failed" "$OUT" 2>/dev/null \
   || grep -qi "continuing to stop services" "$OUT" 2>/dev/null \
   || grep -qi "Logs S3 sync failed" "$OUT" 2>/dev/null; then
    warning=1
fi

echo "PROBE result=COMPLETED flask=$flask nginx=$nginx docker=$docker gitea=$gitea warning=$warning backup=$backup logs=$logs seq=$seq"
CHILD
CHILD_STATUS=$?

if [ "$CHILD_STATUS" -ne 0 ]; then
    # On early abort, recover whatever was recorded before the abort so the
    # backup marker / sequence still reflect what ran.
    b=0; grep -qx "backup" "$RECORD_FILE" 2>/dev/null && b=1
    l=0; grep -qx "logs" "$RECORD_FILE" 2>/dev/null && l=1
    s="$(paste -sd, "$RECORD_FILE" 2>/dev/null)"; [ -z "$s" ] && s="-"
    echo "PROBE result=ABORTED_EARLY flask=0 nginx=0 docker=0 gitea=0 warning=0 backup=$b logs=$l seq=$s (child_exit=$CHILD_STATUS)"
fi

exit 0
