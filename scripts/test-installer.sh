#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

FAKE_BIN="${TEST_ROOT}/bin"
TEST_HOME="${TEST_ROOT}/home"
LOG_FILE="${TEST_ROOT}/host.log"
mkdir -p "$FAKE_BIN" "$TEST_HOME"

FAKE_HOST="${FAKE_BIN}/fake-host"
cat >"$FAKE_HOST" <<'EOF'
#!/usr/bin/env bash
printf '%s %s\n' "$(basename "$0")" "$*" >>"$E47_INSTALLER_TEST_LOG"
EOF
chmod +x "$FAKE_HOST"
ln -s "$FAKE_HOST" "${FAKE_BIN}/xedoc"
ln -s "$FAKE_HOST" "${FAKE_BIN}/claude"
ln -s "$FAKE_HOST" "${FAKE_BIN}/codex"

run_installer() {
    : >"$LOG_FILE"
    env \
        HOME="$TEST_HOME" \
        XDG_CACHE_HOME="${TEST_ROOT}/cache" \
        XDG_CONFIG_HOME="${TEST_ROOT}/config" \
        XDG_DATA_HOME="${TEST_ROOT}/data" \
        XEDOC_HOME="${TEST_ROOT}/xedoc" \
        E47_INSTALLER_TEST_LOG="$LOG_FILE" \
        PATH="${FAKE_BIN}:${PATH}" \
        bash "${REPO_ROOT}/install.sh" "$@" --source "$REPO_ROOT" \
        >"${TEST_ROOT}/stdout.log" 2>"${TEST_ROOT}/stderr.log"
}

assert_logged() {
    local expected="$1"
    if ! grep -Fq "$expected" "$LOG_FILE"; then
        printf 'Expected installer log to contain: %s\n' "$expected" >&2
        cat "$LOG_FILE" >&2
        exit 1
    fi
}

assert_host_not_logged() {
    local host="$1"
    if grep -Eq "^${host} " "$LOG_FILE"; then
        printf 'Expected installer not to invoke %s\n' "$host" >&2
        cat "$LOG_FILE" >&2
        exit 1
    fi
}

# Xedoc is the sole implicit target even when all three CLIs are available.
run_installer
assert_logged "xedoc plugin add reviewers@e47"
assert_logged "xedoc plugin add inline-discussion@e47"
assert_logged "xedoc plugin add project-context-curator@e47"
assert_host_not_logged claude
assert_host_not_logged codex

# Claude Code and Codex require their explicit command-line selectors.
run_installer --claude
assert_logged "claude plugin install reviewers@e47"
assert_logged "claude plugin install auto-compaction@e47"
assert_host_not_logged xedoc
assert_host_not_logged codex

run_installer --codex
assert_logged "codex plugin add reviewers@e47"
assert_logged "codex plugin add project-context-curator@e47"
assert_host_not_logged xedoc
assert_host_not_logged claude

printf 'Installer target tests passed.\n'
