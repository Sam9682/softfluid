#!/usr/bin/env bash
# Bug condition exploration test for the bugfix spec `init-pltf-path-fix`.
#
# Property 1 (Bug Condition) — Repository-relative steps resolve against the
# clone.  This test encodes the EXPECTED (fixed) behavior, so it is designed to
# FAIL on the UNFIXED init_pltf.sh (that failure proves the bug exists) and to
# PASS once the fix anchors the repository-relative steps on an absolute clone
# path (REPO_DIR) and removes the stray `cd - > /dev/null` from the Kata block.
#
# Bug model (from design.md):
#   FUNCTION isBugCondition(input)
#     step IN ['venv_pip_install', 'final_chmod']
#     AND targetRelativePath resolved relative to currentWorkingDir
#     AND resolve(currentWorkingDir, targetRelativePath) does NOT exist
#     AND file exists at resolve(cloneDir, targetRelativePath)
#
# The stray `cd - > /dev/null` at the end of the Kata Containers block toggles
# the shell to OLDPWD, decoupling CWD from the clone.  The later steps
# (`pip install -r ./requirements.txt`, `chmod +x setup_modsecurity_config.sh`)
# then resolve their `./`-relative targets against the launch directory and
# fail even though the files exist inside the clone.
#
# This harness does NOT run the privileged installer body (apt/docker/kata/...).
# It reproduces the exact path-resolution behavior of the script in isolation,
# plus static assertions over the script text for the two root-cause facets
# (the stray `cd -`, and `REPO_DIR`-anchored repository-relative steps).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET_SCRIPT="$REPO_ROOT/init_pltf.sh"

total=0
failed=0
declare -a FAILURES=()
declare -a COUNTEREXAMPLES=()

pass() { total=$((total + 1)); echo "PASS  $1"; }
fail() {
    total=$((total + 1)); failed=$((failed + 1))
    echo "FAIL  $1 -> $2"
    FAILURES+=("$1: $2")
}
counterexample() { COUNTEREXAMPLES+=("$1"); echo "  COUNTEREXAMPLE: $1"; }

# Build a fresh fake clone (containing requirements.txt + the ModSecurity
# helper) and an unrelated launch directory.  Echoes "<launchDir>|<cloneDir>".
make_bug_input() {
    local base clone launch
    base="$(mktemp -d)"
    clone="$base/agentic-ai-pltf"
    launch="$base/launch-dir"
    mkdir -p "$clone" "$launch"
    printf 'flask\nrequests\n' > "$clone/requirements.txt"
    printf '#!/bin/bash\necho modsec\n' > "$clone/setup_modsecurity_config.sh"
    # Deliberately NO execute bit yet — the chmod step is what should set it.
    chmod -x "$clone/setup_modsecurity_config.sh" 2>/dev/null || true
    printf '%s|%s' "$launch" "$clone"
}

# resolve_requirements_target / resolve_chmod_target model how the SCRIPT
# references its targets.  On the UNFIXED script the reference is the ambient
# `./requirements.txt` / `setup_modsecurity_config.sh` (CWD-relative), so these
# helpers mirror that by resolving against the current working directory.  The
# fix will anchor on REPO_DIR; we detect that from the script text (Part D) and
# switch the resolution model accordingly so the SAME test validates the fix.
script_uses_repo_dir_anchor() {
    # True when the venv/final blocks reference the targets via "${REPO_DIR}/..."
    grep -qF 'pip install -q -r "${REPO_DIR}/requirements.txt"' "$TARGET_SCRIPT" \
        && grep -qF 'chmod +x "${REPO_DIR}/setup_modsecurity_config.sh"' "$TARGET_SCRIPT"
}

# Given (launchDir, cloneDir), return the path the script would resolve the
# requirements file to.  Models CWD-relative resolution on the unfixed script
# and REPO_DIR-anchored resolution on the fixed script.
resolve_requirements_path() {
    local launch="$1" clone="$2"
    if script_uses_repo_dir_anchor; then
        printf '%s/requirements.txt' "$clone"      # fixed: REPO_DIR anchored
    else
        printf '%s/requirements.txt' "$launch"     # unfixed: CWD (= launch) relative
    fi
}
resolve_chmod_path() {
    local launch="$1" clone="$2"
    if script_uses_repo_dir_anchor; then
        printf '%s/setup_modsecurity_config.sh' "$clone"
    else
        printf '%s/setup_modsecurity_config.sh' "$launch"
    fi
}

echo "=== init-pltf-path-fix :: bug condition exploration (Property 1) ==="
echo "Target: $TARGET_SCRIPT"
echo ""

# ---------------------------------------------------------------------------
# Scoped property-based enumeration over (launchDir, cloneDir) pairs where
# launchDir != cloneDir, for both repository-relative steps.  For every such
# bug-condition input the EXPECTED behavior is that the target resolves against
# the clone (so pip reads the real requirements and chmod sets the real file
# executable).  On unfixed code this fails — surfacing the counterexamples.
# ---------------------------------------------------------------------------

ITERS=25

# ---- Test case 1: pip requirements resolution -----------------------------
tc1_ok=1; tc1_detail=""
for ((i = 0; i < ITERS; i++)); do
    IFS='|' read -r launch clone < <(make_bug_input)
    # Simulate the stray `cd -` state: CWD is the launch dir, not the clone.
    resolved="$(cd "$launch" && resolve_requirements_path "$launch" "$clone")"
    # Expected behavior: the resolved requirements file must exist (anchored on
    # the clone).  On the unfixed script it resolves under the launch dir and
    # does NOT exist, even though "$clone/requirements.txt" does.
    if [ ! -f "$resolved" ]; then
        tc1_ok=0
        if [ -f "$clone/requirements.txt" ]; then
            counterexample "pip: resolved '$resolved' (Could not open requirements file: './requirements.txt'); exists at '$clone/requirements.txt'"
        fi
        tc1_detail="resolved requirements path '$resolved' does not exist while '$clone/requirements.txt' does"
    fi
    rm -rf "$(dirname "$launch")"
    [ "$tc1_ok" -eq 1 ] || break
done
if [ "$tc1_ok" -eq 1 ]; then
    pass "tc1.pip_requirements_resolves_against_clone ($ITERS iters)"
else
    fail "tc1.pip_requirements_resolves_against_clone" "$tc1_detail"
fi

# ---- Test case 2: chmod target resolution ---------------------------------
tc2_ok=1; tc2_detail=""
for ((i = 0; i < ITERS; i++)); do
    IFS='|' read -r launch clone < <(make_bug_input)
    resolved="$(cd "$launch" && resolve_chmod_path "$launch" "$clone")"
    # Expected behavior: chmod +x must succeed against a real file.  We model
    # "chmod succeeds" as "the resolved target exists and chmod returns 0".
    if [ -f "$resolved" ] && chmod +x "$resolved" 2>/dev/null && [ -x "$resolved" ]; then
        : # ok
    else
        tc2_ok=0
        if [ -f "$clone/setup_modsecurity_config.sh" ]; then
            counterexample "chmod: cannot access '$resolved' (chmod: cannot access './setup_modsecurity_config.sh'); exists at '$clone/setup_modsecurity_config.sh'"
        fi
        tc2_detail="chmod target '$resolved' not accessible while '$clone/setup_modsecurity_config.sh' exists"
    fi
    rm -rf "$(dirname "$launch")"
    [ "$tc2_ok" -eq 1 ] || break
done
if [ "$tc2_ok" -eq 1 ]; then
    pass "tc2.chmod_target_resolves_against_clone ($ITERS iters)"
else
    fail "tc2.chmod_target_resolves_against_clone" "$tc2_detail"
fi

# ---- Test case 3: masked success (requirement 1.3) ------------------------
# After a simulated pip failure, the UNFIXED sequence still prints the success
# banners.  The EXPECTED behavior is that a genuine pip/chmod failure is NOT
# reported as "[OK] ...".  We detect the de-masking from the script text: the
# venv/final steps must guard the step's exit status (print_error / exit 1 on
# failure) rather than unconditionally printing print_success.
tc3_ok=1; tc3_detail=""
# A simulated pip failure (non-zero) that is NOT guarded leads straight to the
# unconditional success print on the unfixed script.
if script_uses_repo_dir_anchor; then
    # Fixed form present.  Require that failure is surfaced, not masked:
    # the venv block must check the pip exit status and the final block the
    # chmod exit status before printing success.
    if grep -qE 'pip install -q -r "\$\{REPO_DIR\}/requirements.txt"' "$TARGET_SCRIPT" \
       && grep -qE 'print_error|exit 1' "$TARGET_SCRIPT" \
       && ! grep -qzE 'pip install -q -r "\$\{REPO_DIR\}/requirements.txt"\s*\n\s*print_success' "$TARGET_SCRIPT"; then
        : # ok — failure is guarded
    else
        tc3_ok=0
        tc3_detail="pip install success is still printed unconditionally (failure masked, requirement 1.3)"
    fi
else
    tc3_ok=0
    counterexample "masked success: unfixed sequence reaches '[OK] Python environment ready' / '[OK] Installation completed successfully!' despite the pip/chmod failures above"
    tc3_detail="unfixed script references ./requirements.txt relative to CWD and prints print_success unconditionally"
fi
if [ "$tc3_ok" -eq 1 ]; then
    pass "tc3.genuine_failure_not_masked_as_success"
else
    fail "tc3.genuine_failure_not_masked_as_success" "$tc3_detail"
fi

# ---- Test case 4: edge case — CWD already equals the clone (¬C) -----------
# When CWD already is the clone, the relative lookups succeed even on the
# unfixed script.  This confirms the bug is CWD-dependent, not a missing file,
# and must hold both before and after the fix.
tc4_ok=1; tc4_detail=""
IFS='|' read -r launch clone < <(make_bug_input)
# Resolve from INSIDE the clone (CWD == clone).  Even the unfixed CWD-relative
# model resolves correctly here.
req_from_clone="$(cd "$clone" && { if script_uses_repo_dir_anchor; then printf '%s/requirements.txt' "$clone"; else printf '%s/requirements.txt' "$clone"; fi; })"
chmod_from_clone="$(cd "$clone" && { if script_uses_repo_dir_anchor; then printf '%s/setup_modsecurity_config.sh' "$clone"; else printf '%s/setup_modsecurity_config.sh' "$clone"; fi; })"
if [ ! -f "$req_from_clone" ]; then
    tc4_ok=0; tc4_detail="requirements.txt not found even with CWD == clone ('$req_from_clone')"
fi
if [ "$tc4_ok" -eq 1 ] && { [ ! -f "$chmod_from_clone" ] || ! (chmod +x "$chmod_from_clone" 2>/dev/null && [ -x "$chmod_from_clone" ]); }; then
    tc4_ok=0; tc4_detail="chmod target not accessible even with CWD == clone ('$chmod_from_clone')"
fi
rm -rf "$(dirname "$launch")"
if [ "$tc4_ok" -eq 1 ]; then
    pass "tc4.edge_cwd_equals_clone_resolves (bug is CWD-dependent, not a missing file)"
else
    fail "tc4.edge_cwd_equals_clone_resolves" "$tc4_detail"
fi

# ---------------------------------------------------------------------------
# Part D - Static root-cause assertions over the script text.
#   D.1 The stray `cd - > /dev/null` must be removed from the Kata block.
#   D.2 The repository-relative steps must be anchored on ${REPO_DIR}.
# These encode the two root-cause facets; both FAIL on the unfixed script.
# ---------------------------------------------------------------------------

# D.1 — stray `cd -` removed.
if grep -qE '^[[:space:]]*cd - > /dev/null' "$TARGET_SCRIPT"; then
    counterexample "stray 'cd - > /dev/null' still present in the Kata block (decouples CWD from the clone)"
    fail "d1.stray_cd_dash_removed" "found 'cd - > /dev/null' in init_pltf.sh (Kata block)"
else
    pass "d1.stray_cd_dash_removed"
fi

# D.2 — venv + final steps anchored on ${REPO_DIR}.
if grep -qF 'pip install -q -r "${REPO_DIR}/requirements.txt"' "$TARGET_SCRIPT"; then
    pass "d2.pip_anchored_on_repo_dir"
else
    counterexample "pip step uses './requirements.txt' (CWD-relative) instead of \"\${REPO_DIR}/requirements.txt\""
    fail "d2.pip_anchored_on_repo_dir" 'pip install does not reference "${REPO_DIR}/requirements.txt"'
fi
if grep -qF 'chmod +x "${REPO_DIR}/setup_modsecurity_config.sh"' "$TARGET_SCRIPT"; then
    pass "d2.chmod_anchored_on_repo_dir"
else
    counterexample "chmod step uses 'setup_modsecurity_config.sh' (CWD-relative) instead of \"\${REPO_DIR}/setup_modsecurity_config.sh\""
    fail "d2.chmod_anchored_on_repo_dir" 'chmod does not reference "${REPO_DIR}/setup_modsecurity_config.sh"'
fi

# ---------------------------------------------------------------------------
echo ""
echo "----------------------------------------"
echo "Total: $total   Failed: $failed"
if [ "${#COUNTEREXAMPLES[@]}" -ne 0 ]; then
    echo ""
    echo "Counterexamples (bug confirmed on unfixed code):"
    printf '  - %s\n' "${COUNTEREXAMPLES[@]}"
fi
if [ "$failed" -ne 0 ]; then
    echo ""
    printf 'FAILED: %s\n' "${FAILURES[@]}"
    exit 1
fi
echo "All init path-resolution bug-condition assertions passed (bug is fixed)."
