#!/usr/bin/env bash
# Install script for the e47 marketplace.
#
# One-line install:
#   curl -fsSL https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.sh | bash

set -euo pipefail

REPO_SLUG="apohl79/e47-agent-lab"
MARKETPLACE_NAME="e47"
LEGACY_MARKETPLACE_NAMES=("e47-marketplace" "my-coding")
STANDALONE_MARKETPLACE_NAMES=("plan-executor" "plan-executor-plugin" "inline-discussion" "project-context-curator")
ALL_MARKETPLACE_NAMES=("$MARKETPLACE_NAME" "${LEGACY_MARKETPLACE_NAMES[@]}" "${STANDALONE_MARKETPLACE_NAMES[@]}")
CODEX_PLUGIN_CLEANUP_MARKETPLACE_NAMES=("personal" "${ALL_MARKETPLACE_NAMES[@]}")
CODEX_PERSONAL_PLUGIN_CLEANUP_NAMES=("plan-executor" "inline-discussion" "project-context-curator" "my")
TARGET_MODE="auto"
ACTION="install"
SOURCE_OVERRIDE="${E47_MARKETPLACE_SOURCE:-}"
WITH_CONTEXT_RUNTIME=false

CODEX_PLUGIN_REGISTRY=(
    "reviewers|PR finalization and reviewer-team workflows"
    "inline-discussion|Inline-discussion browser UI"
    "project-context-curator|Durable repository domain context"
)

XEDOC_PLUGIN_REGISTRY=(
    "inline-discussion|Inline-discussion browser UI"
)

CLAUDE_PLUGIN_REGISTRY=(
    "reviewers|PR finalization and reviewer-team workflows"
    "auto-compaction|Claude Code auto-compaction gate"
    "inline-discussion|Inline-discussion browser UI"
    "project-context-curator|Durable repository domain context"
)

CLEANUP_PLUGIN_REGISTRY=(
    "plan-executor|Legacy standalone plan-executor marketplace"
    "reviewers|PR finalization and reviewer-team workflows"
    "auto-compaction|Claude Code auto-compaction gate"
    "inline-discussion|Inline-discussion browser UI"
    "project-context-curator|Durable repository domain context"
    "my|Legacy my-coding plugin"
)

# Standalone CLI tools shipped from tools/. Each entry is
# "name|relative-launcher-path|description". Installed as symlinks into the
# user's bin dir (~/bin if it exists, otherwise ~/.local/bin) and removed on
# uninstall. They are host-agnostic — no Claude/Codex/Xedoc dependency.
CLI_TOOL_REGISTRY=(
    "inline-discussion|tools/inline-discussion/bin/inline-discussion|Standalone inline-discussion server (markdown preview + AI threads + Apply/Finish)"
)

# Cache directory for the installer-managed checkout used by CLI tool symlinks
# when install.sh runs via the curl one-liner (no local checkout in $PWD).
# Fully managed by this installer — safe to wipe to force a refresh. Defer
# expansion of $HOME so an unset $HOME (e.g. `env -u HOME bash install.sh
# --help`) doesn't fail under `set -u`; the actual users (install/remove)
# already guard against an empty HOME.
CLI_TOOLS_CHECKOUT_DIR="${HOME:-}/.local/share/e47-marketplace"

# All status output goes to stderr. Stdout is reserved for function return
# values captured via `$(...)` (e.g. ensure_cli_tools_checkout returns a path
# on stdout; if info/warn went to stdout the caller would capture log noise
# along with the path).
info() { printf "  %s\n" "$1" >&2; }
ok() { printf "  [ok] %s\n" "$1" >&2; }
warn() { printf "  [warn] %s\n" "$1" >&2; }
error() { printf "Error: %s\n" "$1" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: ./install.sh [install|uninstall] [--all|--claude|--codex|--xedoc] [--source SOURCE] [--with-context-runtime]

Without a target flag, installs host-appropriate plugins into each available CLI:
Claude Code, Codex, and/or Xedoc.

Options:
  --all            Install into Claude Code, Codex, and Xedoc; fail if any CLI is missing.
  --claude         Install into Claude Code only.
  --codex          Install into Codex only.
  --xedoc          Install into Xedoc only.
  --source SOURCE  Marketplace source path or GitHub slug. Defaults to the local
                   checkout when run from this repo, otherwise ${REPO_SLUG}.
  --with-context-runtime
                   Provision the pinned optional Qdrant/FastEmbed runtime. This
                   downloads about 277 MiB, plus a managed Python when needed.
EOF
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            install|uninstall)
                ACTION="$1"
                ;;
            --all)
                TARGET_MODE="all"
                ;;
            --claude)
                TARGET_MODE="claude"
                ;;
            --codex)
                TARGET_MODE="codex"
                ;;
            --xedoc)
                TARGET_MODE="xedoc"
                ;;
            --source)
                shift
                [ "$#" -gt 0 ] || error "--source requires a value"
                SOURCE_OVERRIDE="$1"
                ;;
            --with-context-runtime)
                WITH_CONTEXT_RUNTIME=true
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                error "unknown option: $1"
                ;;
        esac
        shift
    done
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

require_claude() {
    command_exists claude || error "claude CLI not found. Install Claude Code first."
}

require_codex() {
    command_exists codex || error "codex CLI not found. Install Codex first."
}

require_xedoc() {
    command_exists xedoc || error "xedoc CLI not found. Install Xedoc first."
}

should_install_claude() {
    case "$TARGET_MODE" in
        all|claude) return 0 ;;
        codex|xedoc) return 1 ;;
        auto) command_exists claude ;;
    esac
}

should_install_codex() {
    case "$TARGET_MODE" in
        all|codex) return 0 ;;
        claude|xedoc) return 1 ;;
        auto) command_exists codex ;;
    esac
}

should_install_xedoc() {
    case "$TARGET_MODE" in
        all|xedoc) return 0 ;;
        claude|codex) return 1 ;;
        auto) command_exists xedoc ;;
    esac
}

is_marketplace_dir() {
    local dir="$1"
    [ -f "$dir/.agents/plugins/marketplace.json" ] &&
        [ -f "$dir/.claude-plugin/marketplace.json" ] &&
        [ -d "$dir/plugins" ]
}

script_dir() {
    local source_path="${BASH_SOURCE[0]:-}"
    if [ -n "$source_path" ] && [ -f "$source_path" ]; then
        cd "$(dirname "$source_path")" && pwd
    fi
}

get_marketplace_source() {
    if [ -n "$SOURCE_OVERRIDE" ]; then
        printf "%s\n" "$SOURCE_OVERRIDE"
        return
    fi

    local dir
    dir="$(script_dir || true)"
    if [ -n "$dir" ] && is_marketplace_dir "$dir"; then
        printf "%s\n" "$dir"
        return
    fi

    if is_marketplace_dir "$PWD"; then
        pwd
        return
    fi

    printf "%s\n" "$REPO_SLUG"
}

validate_local_versions() {
    local source="$1"
    local version_script="${source}/scripts/plugin-versioning.py"

    [ -d "$source" ] || return 0
    [ -f "$version_script" ] || return 0

    if ! command_exists python3; then
        warn "python3 not found; skipping local plugin version check."
        return 0
    fi

    info "Checking local plugin versions..."
    python3 "$version_script" check >/dev/null
    ok "Plugin versions consistent."
}

clear_claude_plugin_cache_for() {
    local marketplace_name="$1"
    local base="$HOME/.claude/plugins"
    rm -rf "${base}/cache/${marketplace_name}" 2>/dev/null || true
    rm -rf "${base}/marketplaces/${marketplace_name}" 2>/dev/null || true
}

clear_claude_plugin_cache() {
    local marketplace_name
    for marketplace_name in "${ALL_MARKETPLACE_NAMES[@]}"; do
        clear_claude_plugin_cache_for "$marketplace_name"
    done
}

clear_codex_plugin_cache_for() {
    local marketplace_name="$1"
    local base="$HOME/.codex/plugins"
    rm -rf "${base}/cache/${marketplace_name}" 2>/dev/null || true
    rm -rf "${base}/marketplaces/${marketplace_name}" 2>/dev/null || true
}

clear_codex_plugin_cache() {
    local marketplace_name
    for marketplace_name in "${ALL_MARKETPLACE_NAMES[@]}"; do
        clear_codex_plugin_cache_for "$marketplace_name"
    done
}

clear_xedoc_plugin_cache_for() {
    local marketplace_name="$1"
    local base="$HOME/.xedoc/plugins"
    rm -rf "${base}/cache/${marketplace_name}" 2>/dev/null || true
    rm -rf "${base}/marketplaces/${marketplace_name}" 2>/dev/null || true
}

clear_xedoc_plugin_cache() {
    local marketplace_name
    for marketplace_name in "${ALL_MARKETPLACE_NAMES[@]}"; do
        clear_xedoc_plugin_cache_for "$marketplace_name"
    done
}

remove_codex_personal_marketplace_entries() {
    local marketplace_file="${HOME}/.agents/plugins/marketplace.json"
    [ -f "$marketplace_file" ] || return 0

    if ! command_exists python3; then
        warn "python3 not found; skipping Codex personal marketplace entry cleanup."
        return 0
    fi

    python3 - "$marketplace_file" "${CODEX_PERSONAL_PLUGIN_CLEANUP_NAMES[@]}" <<'PY'
import json
import os
import sys
import tempfile

path = sys.argv[1]
legacy_names = set(sys.argv[2:])

try:
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
except Exception as exc:
    print(f"  [warn] Could not read Codex personal marketplace {path}: {exc}", file=sys.stderr)
    sys.exit(0)

plugins = data.get("plugins")
if not isinstance(plugins, list):
    sys.exit(0)

filtered = [
    plugin
    for plugin in plugins
    if not (isinstance(plugin, dict) and plugin.get("name") in legacy_names)
]

if len(filtered) == len(plugins):
    sys.exit(0)

data["plugins"] = filtered
directory = os.path.dirname(path) or "."
fd, tmp_path = tempfile.mkstemp(prefix=".marketplace.", suffix=".json", dir=directory)

try:
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)
finally:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
PY
}

remove_claude_plugins() {
    require_claude

    info "Removing existing Claude Code plugins..."
    local entry plugin_name marketplace_name
    for marketplace_name in "${ALL_MARKETPLACE_NAMES[@]}"; do
        for entry in "${CLEANUP_PLUGIN_REGISTRY[@]}"; do
            IFS="|" read -r plugin_name _description <<< "$entry"
            claude plugin uninstall "${plugin_name}@${marketplace_name}" >/dev/null 2>&1 || true
        done
        claude plugin marketplace remove "${marketplace_name}" >/dev/null 2>&1 || true
    done
    clear_claude_plugin_cache
    ok "Claude Code marketplace clean."
}

remove_codex_plugins() {
    require_codex

    info "Removing existing Codex plugins..."
    local entry plugin_name marketplace_name
    for marketplace_name in "${CODEX_PLUGIN_CLEANUP_MARKETPLACE_NAMES[@]}"; do
        for entry in "${CLEANUP_PLUGIN_REGISTRY[@]}"; do
            IFS="|" read -r plugin_name _description <<< "$entry"
            codex plugin remove "${plugin_name}@${marketplace_name}" >/dev/null 2>&1 || true
        done
    done

    for marketplace_name in "${ALL_MARKETPLACE_NAMES[@]}"; do
        codex plugin marketplace remove "${marketplace_name}" >/dev/null 2>&1 || true
    done
    remove_codex_personal_marketplace_entries
    clear_codex_plugin_cache
    ok "Codex marketplace clean."
}

remove_xedoc_plugins() {
    require_xedoc

    info "Removing existing Xedoc plugins..."
    local entry plugin_name marketplace_name
    for marketplace_name in "${ALL_MARKETPLACE_NAMES[@]}"; do
        for entry in "${XEDOC_PLUGIN_REGISTRY[@]}"; do
            IFS="|" read -r plugin_name _description <<< "$entry"
            xedoc plugin remove "${plugin_name}@${marketplace_name}" >/dev/null 2>&1 || true
        done
        xedoc plugin marketplace remove "${marketplace_name}" >/dev/null 2>&1 || true
    done
    clear_xedoc_plugin_cache
    ok "Xedoc marketplace clean."
}

install_claude_plugins() {
    local source="$1"
    require_claude
    remove_claude_plugins

    info "Adding Claude Code marketplace '${MARKETPLACE_NAME}' from ${source}..."
    claude plugin marketplace add "$source" --scope user >/dev/null
    ok "Claude Code marketplace added."

    local entry plugin_name
    for entry in "${CLAUDE_PLUGIN_REGISTRY[@]}"; do
        IFS="|" read -r plugin_name _description <<< "$entry"
        info "Installing Claude Code plugin ${plugin_name}@${MARKETPLACE_NAME}..."
        claude plugin install "${plugin_name}@${MARKETPLACE_NAME}" >/dev/null
        ok "Claude Code plugin installed: ${plugin_name}@${MARKETPLACE_NAME}"
    done
}

install_codex_plugins() {
    local source="$1"
    require_codex
    remove_codex_plugins

    info "Adding Codex marketplace '${MARKETPLACE_NAME}' from ${source}..."
    codex plugin marketplace add "$source" >/dev/null
    ok "Codex marketplace added."

    local entry plugin_name
    for entry in "${CODEX_PLUGIN_REGISTRY[@]}"; do
        IFS="|" read -r plugin_name _description <<< "$entry"
        info "Installing Codex plugin ${plugin_name}@${MARKETPLACE_NAME}..."
        codex plugin add "${plugin_name}@${MARKETPLACE_NAME}" >/dev/null
        ok "Codex plugin installed: ${plugin_name}@${MARKETPLACE_NAME}"
    done
}

install_xedoc_plugins() {
    local source="$1"
    require_xedoc
    remove_xedoc_plugins

    info "Adding Xedoc marketplace '${MARKETPLACE_NAME}' from ${source}..."
    xedoc plugin marketplace add "$source" >/dev/null
    ok "Xedoc marketplace added."

    local entry plugin_name
    for entry in "${XEDOC_PLUGIN_REGISTRY[@]}"; do
        IFS="|" read -r plugin_name _description <<< "$entry"
        info "Installing Xedoc plugin ${plugin_name}@${MARKETPLACE_NAME}..."
        xedoc plugin add "${plugin_name}@${MARKETPLACE_NAME}" >/dev/null
        ok "Xedoc plugin installed: ${plugin_name}@${MARKETPLACE_NAME}"
    done
}

# Pick the target bin dir for CLI tool symlinks. Preference: ~/bin if it
# already exists, otherwise ~/.local/bin (created if needed).
resolve_bin_dir() {
    if [ -z "${HOME:-}" ]; then
        # Empty HOME would resolve to "/bin" / "/.local/bin" — refuse rather
        # than write into system paths.
        printf "%s\n" "" 
        return
    fi
    if [ -d "${HOME}/bin" ]; then
        printf "%s\n" "${HOME}/bin"
        return
    fi
    printf "%s\n" "${HOME}/.local/bin"
}

install_cli_tools() {
    local source="$1"

    local bin_dir
    bin_dir="$(resolve_bin_dir)"
    if [ -z "$bin_dir" ]; then
        warn "\$HOME is empty; skipping CLI tool install."
        return 0
    fi

    # The CLI tool launchers depend on a real source tree on disk (they read
    # tools/<name>/{bin,src,package.json,…}). For the one-liner install the
    # marketplace "source" is the GitHub slug, not a local path — in that case
    # we clone the repo into a managed cache dir and symlink from there.
    local resolved_source
    if [ -d "$source" ]; then
        resolved_source="$source"
    else
        # Declare assignment on its own line so the command-substitution exit
        # status is not masked by `local`.
        resolved_source="$(ensure_cli_tools_checkout "$source")" || return 0
        [ -n "$resolved_source" ] || return 0
    fi

    mkdir -p "$bin_dir"

    local entry tool_name rel_path _description launcher link
    for entry in "${CLI_TOOL_REGISTRY[@]}"; do
        IFS="|" read -r tool_name rel_path _description <<< "$entry"
        launcher="${resolved_source}/${rel_path}"
        if [ ! -f "$launcher" ]; then
            warn "CLI tool launcher missing at ${launcher}; skipping ${tool_name}."
            continue
        fi
        chmod +x "$launcher" 2>/dev/null || true
        link="${bin_dir}/${tool_name}"
        if [ -e "$link" ] && [ ! -L "$link" ]; then
            warn "Refusing to replace existing non-symlink at ${link}; skipping ${tool_name}."
            warn "Remove it manually or move it out of the way and re-run install.sh."
            continue
        fi
        ln -sfn "$launcher" "$link"
        ok "CLI tool installed: ${link} -> ${launcher}"
    done

    case ":${PATH:-}:" in
        *":${bin_dir}:"*) ;;
        *) warn "${bin_dir} is not on \$PATH. Add it to your shell rc to use installed tools." ;;
    esac
}

# Ensure a local source tree for CLI tool launchers when install.sh is run
# without a local checkout. Clones (or updates) the repo into
# $CLI_TOOLS_CHECKOUT_DIR and prints its path on stdout. Falls back gracefully
# when git is unavailable: prints nothing and warns, so the caller can skip.
ensure_cli_tools_checkout() {
    local source="$1"
    if [ -z "${HOME:-}" ]; then
        warn "\$HOME is empty; skipping CLI tools checkout."
        return 1
    fi
    local slug="$source"
    local is_slug=false
    if [[ "$slug" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        is_slug=true
    fi
    local clone_url
    if [ "$is_slug" = true ]; then
        clone_url="https://github.com/${slug}.git"
    else
        clone_url="$slug"
    fi

    if ! command_exists git; then
        warn "git not found; skipping CLI tool install (need local checkout)."
        return 1
    fi

    local dir="$CLI_TOOLS_CHECKOUT_DIR"
    mkdir -p "$(dirname "$dir")"

    # Prefer `gh repo clone` for GitHub slugs when gh is already available; the
    # public curl one-liner does not require it. Fall back to `git clone` for
    # explicit URLs or when gh isn't available.
    if [ -d "$dir/.git" ]; then
        # Refuse to reuse a checkout whose origin points somewhere other than
        # the requested marketplace source. A stale or attacker-preseeded
        # .git here could otherwise smuggle a launcher onto PATH.
        local cached_origin
        cached_origin="$(git -C "$dir" config --get remote.origin.url 2>/dev/null || true)"
        if [ -n "$cached_origin" ] && [ "$cached_origin" != "$clone_url" ]; then
            warn "Existing ${dir} has origin ${cached_origin}, not ${clone_url}."
            warn "Wiping and re-cloning to honour the requested marketplace source."
            _safe_rm_managed_dir "$dir"
        fi
    fi

    if [ -d "$dir/.git" ]; then
        info "Updating CLI tools checkout at ${dir}..."
        if ! git -C "$dir" fetch --quiet origin 2>/dev/null; then
            warn "git fetch failed for ${dir}; using existing checkout."
        else
            local default_branch
            default_branch="$(git -C "$dir" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')"
            default_branch="${default_branch:-main}"
            git -C "$dir" reset --hard "origin/${default_branch}" --quiet 2>/dev/null || true
        fi
    else
        local clone_ok=false
        if [ "$is_slug" = true ] && command_exists gh; then
            info "Cloning ${slug} into ${dir} via gh (for CLI tool launchers)..."
            if gh repo clone "$slug" "$dir" -- --depth 1 --quiet >/dev/null 2>&1; then
                clone_ok=true
            fi
        fi
        if [ "$clone_ok" = false ]; then
            info "Cloning ${clone_url} into ${dir} (for CLI tool launchers)..."
            if git clone --quiet --depth 1 "$clone_url" "$dir" 2>/dev/null; then
                clone_ok=true
            fi
        fi
        if [ "$clone_ok" = false ]; then
            warn "Could not clone ${clone_url}; skipping CLI tool install."
            warn "Re-run install.sh from a local clone of the marketplace to install CLI tools."
            return 1
        fi
    fi

    printf "%s\n" "$dir"
    return 0
}

resolve_marketplace_tree() {
    local source="$1"
    if [ -d "$source" ]; then
        printf "%s\n" "$source"
        return 0
    fi
    if [ -d "$CLI_TOOLS_CHECKOUT_DIR" ]; then
        printf "%s\n" "$CLI_TOOLS_CHECKOUT_DIR"
        return 0
    fi
    ensure_cli_tools_checkout "$source"
}

install_project_context_runtime() {
    local source="$1"
    local tree
    tree="$(resolve_marketplace_tree "$source")" || return 0
    [ -n "$tree" ] || return 0
    local script="${tree}/plugins/project-context-curator/skills/maintain-project-context/scripts/project_context.py"
    if [ ! -f "$script" ]; then
        warn "Project context updater missing at ${script}; skipping optional runtime."
        return 0
    fi
    if ! command_exists python3; then
        warn "python3 not found; skipping optional project context runtime."
        return 0
    fi

    if [ "$WITH_CONTEXT_RUNTIME" = true ]; then
        info "Provisioning pinned project context retrieval runtime..."
        python3 "$script" global-upgrade
        ok "Project context retrieval runtime ready."
        return 0
    fi

    local status
    status="$(python3 "$script" global-status 2>/dev/null || true)"
    case "$status" in
        *"runtime update required"*)
            if [ -t 0 ] || [ -t 1 ] || [ -t 2 ]; then
                local reply
                printf "Project context retrieval runtime changed (~277 MiB plus Python if needed). Upgrade now? [y/N] " >/dev/tty || true
                read -r reply </dev/tty || reply=""
                case "$reply" in
                    y|Y|yes|YES)
                        python3 "$script" global-upgrade
                        ok "Project context retrieval runtime ready."
                        return 0
                        ;;
                esac
            fi
            warn "Project context runtime remains unchanged. Run install.sh --with-context-runtime after approval."
            ;;
    esac
}

remove_cli_tools() {
    if [ -z "${HOME:-}" ]; then
        warn "\$HOME is empty; skipping CLI tool removal."
        return 0
    fi
    local entry tool_name _rel _description link
    local candidates=("${HOME}/bin" "${HOME}/.local/bin")
    local removed_any=false
    for entry in "${CLI_TOOL_REGISTRY[@]}"; do
        IFS="|" read -r tool_name _rel _description <<< "$entry"
        local dir
        for dir in "${candidates[@]}"; do
            link="${dir}/${tool_name}"
            # Only remove symlinks that this installer planted — never touch a
            # real binary the user dropped at the same name.
            if [ -L "$link" ]; then
                local target
                target="$(readlink "$link" 2>/dev/null || true)"
                if [ -n "$target" ] && _is_managed_cli_launcher "$target"; then
                    rm -f "$link"
                    ok "CLI tool removed: ${link}"
                    removed_any=true
                else
                    warn "Leaving ${link} alone (not a managed e47 launcher symlink: ${target:-<unreadable>})."
                fi
            elif [ -e "$link" ]; then
                warn "Leaving ${link} alone (not a symlink)."
            fi
        done
    done
    if [ "$removed_any" = false ]; then
        info "No CLI tool launchers to remove."
    fi

    # Drop the installer-managed checkout used by the one-liner install path.
    # Defensive checks before rm -rf: must be non-empty, absolute, not root,
    # and inside $HOME.
    _safe_rm_managed_dir "$CLI_TOOLS_CHECKOUT_DIR"
}

# Return success when $1 points at a path the installer would have planted —
# i.e. ends with /tools/<name>/bin/<name>.mjs for one of the registered tools,
# OR lives under $CLI_TOOLS_CHECKOUT_DIR.
_is_managed_cli_launcher() {
    local target="$1"
    [ -n "$target" ] || return 1

    local entry _name rel_path _desc
    for entry in "${CLI_TOOL_REGISTRY[@]}"; do
        IFS="|" read -r _name rel_path _desc <<< "$entry"
        # Match either a relative or absolute symlink whose tail is the
        # registered relative path.
        case "$target" in
            */"$rel_path") return 0 ;;
            "$rel_path") return 0 ;;
        esac
    done

    case "$target" in
        "$CLI_TOOLS_CHECKOUT_DIR"/*) return 0 ;;
    esac
    return 1
}

_safe_rm_managed_dir() {
    local dir="$1"
    [ -n "$dir" ] || return 0
    [ -d "$dir" ] || return 0
    case "$dir" in
        /|/root|/home|/Users) warn "Refusing to remove suspicious dir ${dir}"; return 0 ;;
    esac
    [ -n "${HOME:-}" ] || { warn "HOME is empty; refusing to remove ${dir}"; return 0; }
    case "$dir" in
        "$HOME"/*) ;;
        *) warn "Refusing to remove ${dir}: not inside \$HOME"; return 0 ;;
    esac
    rm -rf "$dir"
    ok "CLI tools checkout removed: ${dir}"
}

run_for_selected_targets() {
    local source="$1"
    local action="$2"
    local ran_plugin_target=false

    if should_install_claude; then
        if [ "$action" = "install" ]; then
            install_claude_plugins "$source"
        else
            remove_claude_plugins
        fi
        ran_plugin_target=true
    fi

    if should_install_codex; then
        if [ "$action" = "install" ]; then
            install_codex_plugins "$source"
        else
            remove_codex_plugins
        fi
        ran_plugin_target=true
    fi

    if should_install_xedoc; then
        if [ "$action" = "install" ]; then
            install_xedoc_plugins "$source"
        else
            remove_xedoc_plugins
        fi
        ran_plugin_target=true
    fi

    # CLI tools are host-agnostic — install/remove them regardless of which
    # host CLIs are present, but only fail loudly when the user explicitly
    # asked for a plugin target and neither CLI was available.
    if [ "$action" = "install" ]; then
        install_cli_tools "$source"
        install_project_context_runtime "$source"
    else
        remove_cli_tools
    fi

    if [ "$ran_plugin_target" = false ] && [ "$TARGET_MODE" != "auto" ]; then
        error "none of the Claude Code, Codex, or Xedoc CLIs were found. Install one CLI or pass --claude/--codex/--xedoc explicitly."
    fi
}

parse_args "$@"

marketplace_source="$(get_marketplace_source)"
validate_local_versions "$marketplace_source"
run_for_selected_targets "$marketplace_source" "$ACTION"

case "$ACTION:$TARGET_MODE" in
    install:claude)
        ok "Done. Restart Claude Code to apply changes."
        ;;
    install:codex)
        ok "Done. Start a new Codex thread to apply changes."
        ;;
    install:xedoc)
        ok "Done. Start a new Xedoc thread to apply changes."
        ;;
    install:*)
        ok "Done. Restart Claude Code or start a new Codex or Xedoc thread to apply changes."
        ;;
    uninstall:*)
        ok "Done. Restart Claude Code or start a new Codex or Xedoc thread to apply changes."
        ;;
esac
