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
# Part E - get_ini_value(): read conventions and round-trip with set_ini_value.
#   Covers Requirements 3.1 (missing file), 3.2 (missing key), 3.3 (empty value),
#   plus comment/blank-line handling and value-with-'=' integrity.
# ---------------------------------------------------------------------------

# 3.1 - absent file returns empty (and exits 0, never errors).
MISSING="$(mktemp -u)/does-not-exist.ini"
assert_eq "get.missing_file_empty" "" "$(get_ini_value "$MISSING" "PLTF_NAME")"
get_ini_value "$MISSING" "PLTF_NAME" >/dev/null 2>&1
assert_eq "get.missing_file_exit0" "0" "$?"

GWORK="$(mktemp -d)"
GINI="$GWORK/deploy.ini"
cat > "$GINI" <<'EOF'
# This is a comment line.

DOMAIN=opcp-psmc.com
# PLTF_NAME=Commented Out Name
PLTF_NAME=OPCP-Explorer_AI_SharedGPU_Docker_Serverless
EMPTY_KEY=
REPO_URL=https://github.com/Sam9682/opcp-explorer.git?ref=main&x=1

VERSION=0.0.1
EOF

# 3.2 - key not present in an existing file returns empty.
assert_eq "get.missing_key_empty" "" "$(get_ini_value "$GINI" "NO_SUCH_KEY")"

# 3.3 - a present key with an empty value (KEY=) returns empty.
assert_eq "get.empty_value_empty" "" "$(get_ini_value "$GINI" "EMPTY_KEY")"

# A plain present key reads back its value.
assert_eq "get.present_value" "opcp-psmc.com" "$(get_ini_value "$GINI" "DOMAIN")"

# Commented key (# PLTF_NAME=...) is ignored; the uncommented line is read.
assert_eq "get.comment_ignored" "OPCP-Explorer_AI_SharedGPU_Docker_Serverless" "$(get_ini_value "$GINI" "PLTF_NAME")"

# Value containing '=' (and '&') survives intact (cut -f2- keeps the remainder).
assert_eq "get.value_with_equals" "https://github.com/Sam9682/opcp-explorer.git?ref=main&x=1" "$(get_ini_value "$GINI" "REPO_URL")"

# Round-trip: a value written by set_ini_value is read back identically.
RT="$GWORK/roundtrip.ini"
: > "$RT"
set_ini_value "$RT" "SUBMODULE_URL" "git@github.com:Sam9682/ai-swautomorph--shared.git"
assert_eq "get.roundtrip_new" "git@github.com:Sam9682/ai-swautomorph--shared.git" "$(get_ini_value "$RT" "SUBMODULE_URL")"
set_ini_value "$RT" "SUBMODULE_URL" "git@example.com:acme/shared.git?token=abc=def"
assert_eq "get.roundtrip_rewrite" "git@example.com:acme/shared.git?token=abc=def" "$(get_ini_value "$RT" "SUBMODULE_URL")"

rm -rf "$GWORK"

# ---------------------------------------------------------------------------
# Part F - Property 1: config value wins when env is unset/empty.
#   Validates: Requirements 1.1, 1.2, 1.3, 1.4, 4.2
#
# For each of the four platform-identity keys we point CONFIG_FILE at a temp
# INI holding a randomized non-empty value, ensure the matching env var is
# unset, resolve the defaults, and assert the DEFAULT_* equals the config
# value. We loop over randomized values so that at least 100 iterations run
# in total across the four keys (4 keys x 30 iters = 120).
# ---------------------------------------------------------------------------

# Generate a non-empty random config value. Values deliberately include
# characters that appear in real folder slugs / names / URLs, including '='
# and '&' and spaces, to exercise the cut -f2- value-integrity behavior.
rand_value() {
    local alphabet='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./:=&? '
    local len=$(( (RANDOM % 24) + 1 ))  # 1..24 chars, always non-empty
    local out="" i ch
    for ((i = 0; i < len; i++)); do
        ch="${alphabet:$((RANDOM % ${#alphabet})):1}"
        out="$out$ch"
    done
    # Guarantee non-empty and no leading/trailing newline; a single trailing
    # space would be preserved by get_ini_value, which is intended.
    printf '%s' "${out:-x}"
}

PWORK="$(mktemp -d)"
ITERS_PER_KEY=30

# Each entry: "<env/config key> <DEFAULT_* var name>".
for pair in \
    "PLTF_FOLDER DEFAULT_PLTF_FOLDER" \
    "PLTF_NAME DEFAULT_PLTF_NAME" \
    "REPO_URL DEFAULT_REPO_URL" \
    "SUBMODULE_URL DEFAULT_SUBMODULE_URL"; do
    key="${pair%% *}"
    default_var="${pair##* }"
    prop_ok=1
    prop_detail=""
    for ((iter = 0; iter < ITERS_PER_KEY; iter++)); do
        value="$(rand_value)"
        PINI="$PWORK/${key}_${iter}.ini"
        # Write only the key under test so resolution for it depends solely on
        # the config value (other keys fall back, which does not matter here).
        printf '%s=%s\n' "$key" "$value" > "$PINI"
        # Resolve in a subshell so env mutations and CONFIG_FILE do not leak.
        actual="$(
            unset PLTF_FOLDER PLTF_NAME REPO_URL SUBMODULE_URL
            CONFIG_FILE="$PINI"
            resolve_platform_defaults
            printf '%s' "${!default_var}"
        )"
        if [ "$actual" != "$value" ]; then
            prop_ok=0
            prop_detail="iter $iter: expected '$value', got '$actual'"
            break
        fi
    done
    if [ "$prop_ok" -eq 1 ]; then
        pass "property1.config_wins.$key ($ITERS_PER_KEY iters)"
    else
        fail "property1.config_wins.$key" "$prop_detail"
    fi
done

rm -rf "$PWORK"

# ---------------------------------------------------------------------------
# Part G - Property 2: Environment override wins and is never overridden.
#   Validates: Requirements 4.1
#
# For each of the four platform-identity keys we pre-set the matching env var
# to a randomized non-empty value AND point CONFIG_FILE at a temp INI holding
# an arbitrary (possibly conflicting) config value for that key. Resolving the
# defaults must yield the env value, never the config value. We loop over
# randomized env/config pairs so at least 100 iterations run in total across
# the four keys (4 keys x 30 iters = 120).
# ---------------------------------------------------------------------------

EWORK="$(mktemp -d)"
ENV_ITERS_PER_KEY=30

for pair in \
    "PLTF_FOLDER DEFAULT_PLTF_FOLDER" \
    "PLTF_NAME DEFAULT_PLTF_NAME" \
    "REPO_URL DEFAULT_REPO_URL" \
    "SUBMODULE_URL DEFAULT_SUBMODULE_URL"; do
    key="${pair%% *}"
    default_var="${pair##* }"
    prop_ok=1
    prop_detail=""
    for ((iter = 0; iter < ENV_ITERS_PER_KEY; iter++)); do
        env_value="$(rand_value)"
        cfg_value="$(rand_value)"
        EINI="$EWORK/${key}_${iter}.ini"
        # Write an arbitrary config value for the key under test. It is
        # deliberately independent of env_value, so it may or may not conflict.
        printf '%s=%s\n' "$key" "$cfg_value" > "$EINI"
        # Resolve in a subshell so env mutations and CONFIG_FILE do not leak.
        # Only the key under test is exported; the other three stay unset so
        # their resolution does not interfere with this assertion.
        actual="$(
            unset PLTF_FOLDER PLTF_NAME REPO_URL SUBMODULE_URL
            export "$key"="$env_value"
            CONFIG_FILE="$EINI"
            resolve_platform_defaults
            printf '%s' "${!default_var}"
        )"
        if [ "$actual" != "$env_value" ]; then
            prop_ok=0
            prop_detail="iter $iter: env '$env_value' (config '$cfg_value') -> got '$actual'"
            break
        fi
    done
    if [ "$prop_ok" -eq 1 ]; then
        pass "property2.env_wins.$key ($ENV_ITERS_PER_KEY iters)"
    else
        fail "property2.env_wins.$key" "$prop_detail"
    fi
done

rm -rf "$EWORK"

# ---------------------------------------------------------------------------
# Part H - Property 3: Fallback applies when there is no usable config value.
#   Validates: Requirements 3.1, 3.2, 3.3, 4.3
#
# For each of the four platform-identity keys we ensure the matching env var is
# unset/empty AND the config provides NO usable value, then assert the
# DEFAULT_* equals that key's hardcoded fallback. "No usable config value" is
# exercised in all three documented forms:
#   (a) absent file          -> CONFIG_FILE points at a path that does not exist
#   (b) existing file missing -> CONFIG_FILE exists but has no line for the key
#       the key                  (an unrelated key is present instead)
#   (c) key present, empty    -> the key is written as "KEY=" (empty value)
# We loop over the three scenarios for each key (4 keys x 3 scenarios = 12
# assertions) so every (key, no-usable-config) combination is covered.
# ---------------------------------------------------------------------------

FWORK="$(mktemp -d)"

# Each entry: "<env/config key> <DEFAULT_* var name> <hardcoded fallback>".
# The fallbacks mirror the hardcoded strings inside resolve_platform_defaults.
while IFS='|' read -r key default_var fallback; do
    [ -n "$key" ] || continue
    prop_ok=1
    prop_detail=""

    for scenario in absent_file missing_key empty_value; do
        case "$scenario" in
            absent_file)
                # A path that does not exist: get_ini_value's [ -f ] guard
                # returns empty, so resolution must collapse to the fallback.
                cfg_path="$FWORK/${key}_absent/does-not-exist.ini"
                ;;
            missing_key)
                # An existing file that does NOT contain the key under test.
                cfg_path="$FWORK/${key}_missing.ini"
                printf 'UNRELATED_KEY=some-value\n' > "$cfg_path"
                ;;
            empty_value)
                # An existing file where the key is present but has an empty
                # value (KEY=). cut yields "", so the fallback must apply.
                cfg_path="$FWORK/${key}_empty.ini"
                printf '%s=\n' "$key" > "$cfg_path"
                ;;
        esac

        # Resolve in a subshell so env mutations and CONFIG_FILE do not leak.
        # All four identity env vars are unset so none interferes; we only
        # read back the key under test's DEFAULT_* variable.
        actual="$(
            unset PLTF_FOLDER PLTF_NAME REPO_URL SUBMODULE_URL
            CONFIG_FILE="$cfg_path"
            resolve_platform_defaults
            printf '%s' "${!default_var}"
        )"
        if [ "$actual" != "$fallback" ]; then
            prop_ok=0
            prop_detail="scenario '$scenario': expected fallback '$fallback', got '$actual'"
            break
        fi
    done

    if [ "$prop_ok" -eq 1 ]; then
        pass "property3.fallback_collapse.$key (absent_file, missing_key, empty_value)"
    else
        fail "property3.fallback_collapse.$key" "$prop_detail"
    fi
done <<'EOF'
PLTF_FOLDER|DEFAULT_PLTF_FOLDER|opcp-explorer
PLTF_NAME|DEFAULT_PLTF_NAME|OPCP-explorer
REPO_URL|DEFAULT_REPO_URL|https://github.com/Sam9682/opcp-explorer.git
SUBMODULE_URL|DEFAULT_SUBMODULE_URL|git@github.com:Sam9682/ai-swautomorph--shared.git
EOF

rm -rf "$FWORK"

# ---------------------------------------------------------------------------
# Part I - Example tests: config-file keys, documented fallbacks, guard.
#   Validates: Requirements 2.1, 2.2, 2.3, 3.4, 5.1, 5.3
#
# These are concrete example assertions (as opposed to the Part F-H property
# loops): the committed conf/deploy.ini carries the exact required URL values
# and retains the existing keys; the no-file/no-env resolution collapses to the
# documented literal fallbacks; and sourcing init_pltf.sh loads the new
# functions, returns 0 without running the installer body, and still carries
# the ${BASH_SOURCE[0]} != ${0} guard expression.
# ---------------------------------------------------------------------------

# --- I.1  conf/deploy.ini carries the required values and existing keys (2.1-2.3).
DEPLOY_INI="$REPO_ROOT/conf/deploy.ini"
assert_eq "deploy_ini.repo_url" \
    "https://github.com/Sam9682/opcp-explorer.git" \
    "$(get_ini_value "$DEPLOY_INI" "REPO_URL")"
assert_eq "deploy_ini.submodule_url" \
    "git@github.com:Sam9682/ai-swautomorph--shared.git" \
    "$(get_ini_value "$DEPLOY_INI" "SUBMODULE_URL")"

# Existing keys must remain present (non-empty value where applicable; the
# presence of the line is what Requirement 2.3 guards). SECONDARY_DOMAINS is
# intentionally allowed to be empty, so we assert its line exists rather than
# a non-empty value.
for existing in DOMAIN PLTF_NAME PLTF_FOLDER VERSION RANGE_PORTS_PER_APPLICATION LINUX_USER_INSTALLATION; do
    if [ -n "$(get_ini_value "$DEPLOY_INI" "$existing")" ]; then
        pass "deploy_ini.retains.$existing"
    else
        fail "deploy_ini.retains.$existing" "key missing or empty in conf/deploy.ini"
    fi
done
assert_eq "deploy_ini.retains.SECONDARY_DOMAINS" \
    "1" "$(grep -cE '^SECONDARY_DOMAINS=' "$DEPLOY_INI")"

# --- I.2  No file + no env -> every DEFAULT_* equals its documented fallback (3.4).
# Point CONFIG_FILE at a path that does not exist and resolve in a subshell
# with all four identity env vars unset so only the fallbacks can apply.
NOFILE="$(mktemp -u)/no-such-config.ini"
fallbacks="$(
    unset PLTF_FOLDER PLTF_NAME REPO_URL SUBMODULE_URL
    CONFIG_FILE="$NOFILE"
    resolve_platform_defaults
    printf '%s|%s|%s|%s' \
        "$DEFAULT_PLTF_FOLDER" "$DEFAULT_PLTF_NAME" \
        "$DEFAULT_REPO_URL" "$DEFAULT_SUBMODULE_URL"
)"
IFS='|' read -r df dn dr ds <<< "$fallbacks"
assert_eq "fallback.literal.folder"    "opcp-explorer" "$df"
assert_eq "fallback.literal.name"      "OPCP-explorer" "$dn"
assert_eq "fallback.literal.repo"      "https://github.com/Sam9682/opcp-explorer.git" "$dr"
assert_eq "fallback.literal.submodule" "git@github.com:Sam9682/ai-swautomorph--shared.git" "$ds"

# --- I.3  Sourcing loads the new functions, returns 0, and the guard is present (5.1, 5.3).
# The two resolver/reader functions must be defined after sourcing (the harness
# itself sourced the script at the top, so they are already loaded here).
if declare -F get_ini_value >/dev/null 2>&1; then
    pass "guard.loads_get_ini_value"
else
    fail "guard.loads_get_ini_value" "get_ini_value not defined after sourcing"
fi
if declare -F resolve_platform_defaults >/dev/null 2>&1; then
    pass "guard.loads_resolve_platform_defaults"
else
    fail "guard.loads_resolve_platform_defaults" "resolve_platform_defaults not defined after sourcing"
fi

# Sourcing in a fresh subshell returns 0 without running the installer body.
# The guard returns before any apt/docker/clone step, so a clean source exits 0.
(
    unset PLTF_FOLDER PLTF_NAME REPO_URL SUBMODULE_URL
    # shellcheck disable=SC1090
    source "$TARGET_SCRIPT"
) < /dev/null > /dev/null 2>&1
assert_eq "guard.source_returns_0" "0" "$?"

# The guard expression itself must be present in the script source.
if grep -qF '"${BASH_SOURCE[0]}" != "${0}"' "$TARGET_SCRIPT"; then
    pass "guard.expression_present"
else
    fail "guard.expression_present" 'guard expression ${BASH_SOURCE[0]} != ${0} not found in init_pltf.sh'
fi

# ---------------------------------------------------------------------------
echo ""
echo "----------------------------------------"
echo "Total: $total   Failed: $failed"
if [ "$failed" -ne 0 ]; then
    printf 'FAILED: %s\n' "${FAILURES[@]}"
    exit 1
fi
echo "All init platform-identity tests passed."
