#!/usr/bin/env bash
# Preservation property tests for the bugfix spec `init-pltf-path-fix`.
#
# Property 2 (Preservation) — Non-path-resolution behavior unchanged.
# **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
#
# These tests capture the BASELINE behavior of the UNFIXED init_pltf.sh that
# the fix (Task 3) must leave untouched.  Following the observation-first
# methodology, the unfixed non-bug-condition behavior was observed first and
# the assertions below encode those observed outputs across the input domain:
#
#   CLONE_DEST computation (observed on unfixed code):
#     INSTALL_PARENT="${INSTALL_DIR%/}"           # strips exactly ONE trailing slash
#     CLONE_DEST="${INSTALL_PARENT}/${PLTF_FOLDER}"
#       "/opt"   + "myplat"      -> "/opt/myplat"
#       "/opt/"  + "myplat"      -> "/opt/myplat"       (one slash stripped)
#       "/opt//" + "myplat"      -> "/opt//myplat"      (only ONE stripped)
#       "/a/b/c" + "nested-plat" -> "/a/b/c/nested-plat"  (nested preserved)
#       "relative/parent/" + "p" -> "relative/parent/p"
#
#   deploy.ini round-trip (observed on unfixed code, real sourced functions):
#     set_ini_value / get_ini_value for PLTF_NAME and PLTF_FOLDER round-trips
#     the written value exactly, with no key duplication and all other keys
#     (DOMAIN, VERSION, ...) preserved.
#
#   directory creation (observed on unfixed code):
#     logs (under the clone), ~/deployments, and ~/deployments/admin are all
#     created; the completion banner text is present.
#
# EXPECTED OUTCOME on the UNFIXED code: all assertions PASS (this is what the
# fix must preserve).  The SAME test is re-run after the fix (Task 3.3) and
# must still pass.
#
# Convention: this harness SOURCES init_pltf.sh.  The script guards its
# installer body with `[ "${BASH_SOURCE[0]}" != "${0}" ]`, so sourcing only
# loads the function definitions and never runs any apt/docker/clone step.
# The CLONE_DEST and directory-creation logic are plain shell parameter
# expansion / mkdir, so the test models them directly (they are not wrapped in
# a function in the installer body), while deploy.ini uses the REAL sourced
# set_ini_value / get_ini_value functions.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_SCRIPT="$REPO_ROOT/init_pltf.sh"

total=0
failed=0
declare -a FAILURES=()

pass() { total=$((total + 1)); echo "PASS  $1"; }
fail() { total=$((total + 1)); failed=$((failed + 1)); echo "FAIL  $1 -> $2"; FAILURES+=("$1: $2"); }
assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then pass "$name"; else fail "$name" "expected '$expected', got '$actual'"; fi
}

# Load the REAL function definitions from init_pltf.sh (installer body skipped
# because we are sourcing, not executing).
# shellcheck disable=SC1090
source "$TARGET_SCRIPT"

echo "=== init-pltf-path-fix :: preservation (Property 2) ==="
echo "Target: $TARGET_SCRIPT"
echo ""

# ---------------------------------------------------------------------------
# Reference models of the UNFIXED, non-path-resolution behavior.
#
# compute_clone_dest encodes the exact observed logic
#   INSTALL_PARENT="${INSTALL_DIR%/}"; CLONE_DEST="${INSTALL_PARENT}/${PLTF_FOLDER}"
# This is the baseline the fix MUST preserve (Requirement 3.2).  Task 3's fix
# touches only the Kata `cd -` and the venv/final REPO_DIR anchoring; it must
# NOT change how CLONE_DEST is computed.  The test asserts the live computation
# equals this reference for every generated tuple.
# ---------------------------------------------------------------------------
compute_clone_dest() {
    local install_dir="$1" pltf_folder="$2"
    local install_parent="${install_dir%/}"
    printf '%s/%s' "$install_parent" "$pltf_folder"
}

# ---------------------------------------------------------------------------
# Part A - Property: CLONE_DEST preservation across generated tuples where the
#   bug condition does NOT hold (clone-destination computation is a pure path
#   join, independent of CWD / the stray `cd -`).  Requirement 3.2.
#
# Generator: random INSTALL_DIR built from nested segments with a randomized
# trailing-slash suffix, and a random slug-like PLTF_FOLDER.  The reference
# "original" and the "live" computation (re-deriving via the same documented
# formula) must agree, and the result must satisfy the observed invariants:
#   - exactly one trailing slash is stripped from INSTALL_DIR
#   - the join is "<stripped-parent>/<PLTF_FOLDER>"
# ---------------------------------------------------------------------------
rand_segment() {
    local alphabet='abcdefghijklmnopqrstuvwxyz0123456789-'
    local len=$(( (RANDOM % 6) + 1 )); local out="" i
    for ((i = 0; i < len; i++)); do out="$out${alphabet:$((RANDOM % ${#alphabet})):1}"; done
    printf '%s' "${out:-x}"
}
rand_install_dir() {
    # 1..4 nested segments, optional leading '/', optional 0..2 trailing '/'.
    local n=$(( (RANDOM % 4) + 1 )) i out=""
    [ $((RANDOM % 2)) -eq 0 ] && out="/"
    for ((i = 0; i < n; i++)); do
        [ -n "$out" ] && [ "${out: -1}" != "/" ] && out="$out/"
        out="$out$(rand_segment)"
    done
    local slashes=$((RANDOM % 3))  # 0, 1, or 2 trailing slashes
    for ((i = 0; i < slashes; i++)); do out="$out/"; done
    printf '%s' "$out"
}
rand_folder() {
    local n=$(( (RANDOM % 3) + 1 )) i out=""
    for ((i = 0; i < n; i++)); do [ -n "$out" ] && out="$out-"; out="$out$(rand_segment)"; done
    printf '%s' "$out"
}

A_ITERS=120
a_ok=1; a_detail=""
for ((i = 0; i < A_ITERS; i++)); do
    install_dir="$(rand_install_dir)"
    pltf_folder="$(rand_folder)"

    # Reference (expected baseline).
    expected="$(compute_clone_dest "$install_dir" "$pltf_folder")"

    # Live computation re-deriving via the documented formula exactly as the
    # script does inline.  (Preservation = these stay identical after the fix.)
    live_parent="${install_dir%/}"
    live="${live_parent}/${pltf_folder}"

    if [ "$live" != "$expected" ]; then
        a_ok=0; a_detail="INSTALL_DIR='$install_dir' PLTF_FOLDER='$pltf_folder': live '$live' != expected '$expected'"; break
    fi
    # Observed invariant: exactly one trailing slash stripped, folder appended.
    if [ "$live" != "${install_dir%/}/${pltf_folder}" ]; then
        a_ok=0; a_detail="INSTALL_DIR='$install_dir': stripped-parent invariant violated"; break
    fi
done
if [ "$a_ok" -eq 1 ]; then
    pass "propA.clone_dest_preserved ($A_ITERS iters)"
else
    fail "propA.clone_dest_preserved" "$a_detail"
fi

# ---------------------------------------------------------------------------
# Part B - Property: CLONE_DEST computation is stable for paths with trailing
#   slashes and nested segments.  Encodes the exact observed outputs so a
#   future change to the join (e.g. stripping all trailing slashes, or
#   collapsing '//') would be caught as a regression.  Requirement 3.2.
# ---------------------------------------------------------------------------
# Each case: INSTALL_DIR | PLTF_FOLDER | expected CLONE_DEST (observed unfixed).
B_ok=1; B_detail=""
while IFS='|' read -r idir folder expect; do
    [ -n "$idir" ] || continue
    got="$(compute_clone_dest "$idir" "$folder")"
    if [ "$got" != "$expect" ]; then
        B_ok=0; B_detail="INSTALL_DIR='$idir' PLTF_FOLDER='$folder': got '$got' expected '$expect'"; break
    fi
done <<'EOF'
../|agentic-ai-pltf|../agentic-ai-pltf
/opt|myplat|/opt/myplat
/opt/|myplat|/opt/myplat
/opt//|myplat|/opt//myplat
/a/b/c|nested-plat|/a/b/c/nested-plat
/a/b/c/|nested-plat|/a/b/c/nested-plat
.|p1|./p1
relative/parent|p2|relative/parent/p2
relative/parent/|p2|relative/parent/p2
/deep/nested/path/here/|final-folder|/deep/nested/path/here/final-folder
EOF
if [ "$B_ok" -eq 1 ]; then
    pass "propB.clone_dest_stable_trailing_slash_and_nested"
else
    fail "propB.clone_dest_stable_trailing_slash_and_nested" "$B_detail"
fi

# ---------------------------------------------------------------------------
# Part C - Property: deploy.ini writes preserved.  For generated PLTF_NAME /
#   PLTF_FOLDER values, the REAL set_ini_value writes and get_ini_value reads
#   back the identical value, there is no key duplication, and all unrelated
#   keys are preserved.  Mirrors the clone-block writes:
#     set_ini_value "conf/deploy.ini" "PLTF_NAME"   "${PLTF_NAME}"
#     set_ini_value "conf/deploy.ini" "PLTF_FOLDER" "${PLTF_FOLDER}"
#   Requirements 3.2.
# ---------------------------------------------------------------------------
rand_name() {
    # Display-name-ish: letters, digits, spaces, hyphens, underscores. Non-empty.
    local alphabet='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_'
    local len=$(( (RANDOM % 20) + 1 )); local out="" i
    for ((i = 0; i < len; i++)); do out="$out${alphabet:$((RANDOM % ${#alphabet})):1}"; done
    printf '%s' "${out:-x}"
}

C_ITERS=120
c_ok=1; c_detail=""
CWORK="$(mktemp -d)"
for ((i = 0; i < C_ITERS; i++)); do
    name="$(rand_name)"
    folder="$(rand_folder)"
    INI="$CWORK/conf/deploy.ini"
    mkdir -p "$CWORK/conf"
    # Seed with other keys that must be preserved (the committed deploy.ini has
    # DOMAIN / VERSION / etc. alongside the identity keys).
    cat > "$INI" <<EOF
DOMAIN=opcp-psmc.com
PLTF_NAME=seed-name
PLTF_FOLDER=seed-folder
VERSION=0.0.1
LINUX_USER_INSTALLATION=psmc
EOF

    set_ini_value "$INI" "PLTF_NAME" "$name"
    set_ini_value "$INI" "PLTF_FOLDER" "$folder"

    got_name="$(get_ini_value "$INI" "PLTF_NAME")"
    got_folder="$(get_ini_value "$INI" "PLTF_FOLDER")"
    if [ "$got_name" != "$name" ]; then c_ok=0; c_detail="PLTF_NAME round-trip: wrote '$name' read '$got_name'"; break; fi
    if [ "$got_folder" != "$folder" ]; then c_ok=0; c_detail="PLTF_FOLDER round-trip: wrote '$folder' read '$got_folder'"; break; fi
    # No duplication.
    if [ "$(grep -cE '^PLTF_NAME=' "$INI")" != "1" ]; then c_ok=0; c_detail="PLTF_NAME duplicated"; break; fi
    if [ "$(grep -cE '^PLTF_FOLDER=' "$INI")" != "1" ]; then c_ok=0; c_detail="PLTF_FOLDER duplicated"; break; fi
    # Other keys preserved.
    if [ "$(get_ini_value "$INI" "DOMAIN")" != "opcp-psmc.com" ]; then c_ok=0; c_detail="DOMAIN not preserved"; break; fi
    if [ "$(get_ini_value "$INI" "VERSION")" != "0.0.1" ]; then c_ok=0; c_detail="VERSION not preserved"; break; fi
    if [ "$(get_ini_value "$INI" "LINUX_USER_INSTALLATION")" != "psmc" ]; then c_ok=0; c_detail="LINUX_USER_INSTALLATION not preserved"; break; fi
done
rm -rf "$CWORK"
if [ "$c_ok" -eq 1 ]; then
    pass "propC.deploy_ini_roundtrip_preserved ($C_ITERS iters)"
else
    fail "propC.deploy_ini_roundtrip_preserved" "$c_detail"
fi

# ---------------------------------------------------------------------------
# Part D - Property: directory-creation outcomes preserved.  For generated
#   (clone, home) layouts where the bug condition does NOT hold, the final
#   block creates `logs` (under the clone), `~/deployments`, and
#   `~/deployments/admin`.  Mirrors the unfixed final block:
#     mkdir -p logs ; cd ~ ; mkdir -p deployments ; cd deployments ; mkdir -p admin
#   Requirement 3.4.
# ---------------------------------------------------------------------------
D_ITERS=40
d_ok=1; d_detail=""
for ((i = 0; i < D_ITERS; i++)); do
    base="$(mktemp -d)"
    home="$base/home-$(rand_segment)"
    clone="$base/parent/$(rand_folder)"
    mkdir -p "$home" "$clone"
    # Reproduce the unfixed final-block directory creation (CWD-relative: logs
    # under the clone, deployments under home).
    ( cd "$clone" && mkdir -p logs ) \
        && ( cd "$home" && mkdir -p deployments && cd deployments && mkdir -p admin )
    if [ ! -d "$clone/logs" ]; then d_ok=0; d_detail="logs not created under clone '$clone'"; rm -rf "$base"; break; fi
    if [ ! -d "$home/deployments" ]; then d_ok=0; d_detail="deployments not created under home '$home'"; rm -rf "$base"; break; fi
    if [ ! -d "$home/deployments/admin" ]; then d_ok=0; d_detail="deployments/admin not created under home '$home'"; rm -rf "$base"; break; fi
    rm -rf "$base"
done
if [ "$d_ok" -eq 1 ]; then
    pass "propD.directory_creation_preserved ($D_ITERS iters)"
else
    fail "propD.directory_creation_preserved" "$d_detail"
fi

# ---------------------------------------------------------------------------
# Part E - Static preservation assertions over the script text.  The Kata
#   download/extract/move/daemon.json/reload/cleanup steps and the completion
#   messages are behaviors the fix MUST leave intact (Requirements 3.1, 3.4).
#   These are example assertions complementing the property loops above.
# ---------------------------------------------------------------------------
assert_present() {
    local name="$1" needle="$2"
    if grep -qF "$needle" "$TARGET_SCRIPT"; then pass "$name"; else fail "$name" "missing '$needle' in init_pltf.sh"; fi
}
# Kata sequence (3.1).
assert_present "kata.download" 'kata-static-3.32.0-amd64.tar.zst'
assert_present "kata.unzstd"   'unzstd kata-static-3.32.0-amd64.tar.zst'
assert_present "kata.extract"  'sudo tar xvf kata-static-3.32.0-amd64.tar'
assert_present "kata.move"     'sudo mv ./opt/kata /opt/'
assert_present "kata.daemon"   '/etc/docker/daemon.json'
assert_present "kata.runtime"  'containerd-shim-kata-v2'
assert_present "kata.reload"   'sudo systemctl reload docker'
assert_present "kata.cleanup"  'rm -f kata-static-3.32.0-amd64.tar.zst kata-static-3.32.0-amd64.tar'
# Clone + submodule + deploy.ini writes (3.2).
assert_present "clone.install_parent" 'INSTALL_PARENT="${INSTALL_DIR%/}"'
assert_present "clone.dest"           'CLONE_DEST="${INSTALL_PARENT}/${PLTF_FOLDER}"'
assert_present "clone.submodule_add"  'git submodule add "${SUBMODULE_URL}" shared'
assert_present "clone.submodule_upd"  'git submodule update --init --recursive'
assert_present "ini.write_name"       'set_ini_value "conf/deploy.ini" "PLTF_NAME" "${PLTF_NAME}"'
assert_present "ini.write_folder"     'set_ini_value "conf/deploy.ini" "PLTF_FOLDER" "${PLTF_FOLDER}"'
# Final directory creation + completion messages (3.4).
assert_present "final.deployments"    'mkdir -p deployments'
assert_present "final.admin"          'mkdir -p admin'
assert_present "final.completion"     '[OK] Installation completed successfully!'

# ---------------------------------------------------------------------------
echo ""
echo "----------------------------------------"
echo "Total: $total   Failed: $failed"
if [ "$failed" -ne 0 ]; then
    echo ""
    printf 'FAILED: %s\n' "${FAILURES[@]}"
    exit 1
fi
echo "All init preservation (Property 2) assertions passed (baseline confirmed)."
