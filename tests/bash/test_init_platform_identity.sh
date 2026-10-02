#!/usr/bin/env bash
# Tests for the configurable platform-identity feature in init_pltf.sh.
#
# Covers:
#   - prompt_platform_identity(): default fallback (non-interactive),
#     environment-variable override, and slug validation.
#   - set_ini_value(): in-place key rewrite with no duplication and other
#     keys preserved.
#
# The harness SOURCES init_pltf.sh. Because the script guards its installer
# body with a `[ "${BASH_SOURCE[0]}" != "${0}" ]` return, sourcing only loads
# the function definitions and never runs any apt/docker/clone step.

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
    if [ "$expected" = "$actual" ]; then
        pass "$name"
    else
        fail "$name" "expected '$expected', got '$actual'"
    fi
}

# Load the function definitions from init_pltf.sh (installer body is skipped
# because we are sourcing, not executing).
# shellcheck disable=SC1090
source "$TARGET_SCRIPT"

# ---------------------------------------------------------------------------
# Part A - prompt_platform_identity(): non-interactive default fallback.
# No env vars set, no TTY (stdin redirected from /dev/null) -> defaults used.
# ---------------------------------------------------------------------------
(
    unset PLTF_FOLDER PLTF_NAME REPO_URL SUBMODULE_URL
    prompt_platform_identity < /dev/null
    echo "$PLTF_FOLDER|$PLTF_NAME|$REPO_URL|$SUBMODULE_URL"
) > /tmp/init_identity_defaults.$$ 2>/dev/null
IFS='|' read -r f n r s < /tmp/init_identity_defaults.$$
rm -f /tmp/init_identity_defaults.$$
assert_eq "default_fallback.folder" "opcp-explorer" "$f"
assert_eq "default_fallback.name"   "OPCP-Explorer_AI_SharedGPU_Docker_Serverless" "$n"
assert_eq "default_fallback.repo"   "https://github.com/Sam9682/opcp-explorer.git" "$r"
assert_eq "default_fallback.submodule" "git@github.com:Sam9682/ai-swautomorph--shared.git" "$s"

# ---------------------------------------------------------------------------
# Part B - environment-variable override is honored as-is.
# ---------------------------------------------------------------------------
(
    export PLTF_FOLDER="acme-cloud"
    export PLTF_NAME="Acme Cloud Platform"
    export REPO_URL="https://example.com/acme/acme-cloud.git"
    export SUBMODULE_URL="git@example.com:acme/shared.git"
    prompt_platform_identity < /dev/null
    echo "$PLTF_FOLDER|$PLTF_NAME|$REPO_URL|$SUBMODULE_URL"
) > /tmp/init_identity_env.$$ 2>/dev/null
IFS='|' read -r f n r s < /tmp/init_identity_env.$$
rm -f /tmp/init_identity_env.$$
assert_eq "env_override.folder" "acme-cloud" "$f"
assert_eq "env_override.name"   "Acme Cloud Platform" "$n"
assert_eq "env_override.repo"   "https://example.com/acme/acme-cloud.git" "$r"
assert_eq "env_override.submodule" "git@example.com:acme/shared.git" "$s"

# ---------------------------------------------------------------------------
# Part C - slug validation via is_valid_slug().
# ---------------------------------------------------------------------------
for good in "opcp-explorer" "acme-cloud" "a" "x1" "my-platform-2"; do
    if is_valid_slug "$good"; then pass "slug_valid.$good"; else fail "slug_valid.$good" "rejected a valid slug"; fi
done
for bad in "" "Upper" "with space" "under_score" "-leading" "has/slash"; do
    label="${bad:-<empty>}"
    if is_valid_slug "$bad"; then fail "slug_invalid.$label" "accepted an invalid slug"; else pass "slug_invalid.$label"; fi
done

# A pre-set invalid PLTF_FOLDER must abort with a non-zero exit (subshell).
(
    export PLTF_FOLDER="Invalid Slug"
    unset PLTF_NAME REPO_URL SUBMODULE_URL
    prompt_platform_identity < /dev/null
) > /dev/null 2>&1
if [ $? -ne 0 ]; then pass "slug_invalid.aborts"; else fail "slug_invalid.aborts" "did not abort on invalid preset slug"; fi

# ---------------------------------------------------------------------------
# Part D - set_ini_value(): in-place rewrite, no duplication, others preserved.
# ---------------------------------------------------------------------------
WORK="$(mktemp -d)"
INI="$WORK/deploy.ini"
cat > "$INI" <<'EOF'
DOMAIN=opcp-psmc.com
PLTF_NAME=OPCP-Explorer_AI_SharedGPU_Docker_Serverless
PLTF_FOLDER=opcp-explorer
VERSION=0.0.1
LINUX_USER_INSTALLATION=psmc
EOF

set_ini_value "$INI" "PLTF_NAME" "Acme Cloud Platform"
set_ini_value "$INI" "PLTF_FOLDER" "acme-cloud"

assert_eq "ini.name_rewritten"   "Acme Cloud Platform" "$(grep -E '^PLTF_NAME=' "$INI" | cut -d'=' -f2-)"
assert_eq "ini.folder_rewritten" "acme-cloud"          "$(grep -E '^PLTF_FOLDER=' "$INI" | cut -d'=' -f2-)"
assert_eq "ini.name_no_dup"      "1" "$(grep -cE '^PLTF_NAME=' "$INI")"
assert_eq "ini.folder_no_dup"    "1" "$(grep -cE '^PLTF_FOLDER=' "$INI")"
assert_eq "ini.domain_preserved" "opcp-psmc.com" "$(grep -E '^DOMAIN=' "$INI" | cut -d'=' -f2-)"
assert_eq "ini.version_preserved" "0.0.1" "$(grep -E '^VERSION=' "$INI" | cut -d'=' -f2-)"
assert_eq "ini.user_preserved"   "psmc" "$(grep -E '^LINUX_USER_INSTALLATION=' "$INI" | cut -d'=' -f2-)"

# Appends when key is missing.
set_ini_value "$INI" "NEW_KEY" "new-value"
assert_eq "ini.append_missing" "new-value" "$(grep -E '^NEW_KEY=' "$INI" | cut -d'=' -f2-)"

rm -rf "$WORK"

# ---------------------------------------------------------------------------
echo ""
echo "----------------------------------------"
echo "Total: $total   Failed: $failed"
if [ "$failed" -ne 0 ]; then
    printf 'FAILED: %s\n' "${FAILURES[@]}"
    exit 1
fi
echo "All init platform-identity tests passed."
