#!/usr/bin/env bash
set -euo pipefail

detect_host_from_process_tree() {
    local pid="${PPID:-}" command name

    while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
        command="$(ps -p "$pid" -o comm= 2>/dev/null | awk '{print $1}' || true)"
        name="$(basename "$command" 2>/dev/null || true)"

        case "$name" in
            codex*) echo "codex"; return 0 ;;
            claude*) echo "claude"; return 0 ;;
        esac

        pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    done

    return 1
}

path_points_to_host() {
    local host="$1" marker path resolved

    marker="/.${host}/"
    shift

    for path in "$@"; do
        [ -n "$path" ] || continue
        if [ -e "$path" ]; then
            if [ -d "$path" ]; then
                resolved="$(cd "$path" 2>/dev/null && pwd -P)" || resolved="$path"
            else
                resolved="$(cd "$(dirname "$path")" 2>/dev/null && pwd -P)/$(basename "$path")" || resolved="$path"
            fi
        else
            resolved="$path"
        fi
        case "$resolved" in
            *"$marker"*) echo "$host"; return 0 ;;
        esac
    done

    return 1
}

detect_host() {
    # The executing plugin copy's path ($0 and the plugin roots) is the most
    # reliable signal: it identifies which host installed the copy that is
    # actually running. Session env vars (CODEX_*/CLAUDE_*) are inherited by
    # child processes, so a claude reviewer spawned from codex (or vice versa)
    # carries the parent's env and must not override its own installation path.
    if path_points_to_host "codex" "$0" "${PLUGIN_ROOT:-}" "${PLUGIN_DATA:-}" \
       "${CLAUDE_PLUGIN_ROOT:-}" "${CLAUDE_PLUGIN_DATA:-}"; then
        return 0
    fi

    if path_points_to_host "claude" "$0" "${PLUGIN_ROOT:-}" "${PLUGIN_DATA:-}" \
       "${CLAUDE_PLUGIN_ROOT:-}" "${CLAUDE_PLUGIN_DATA:-}"; then
        return 0
    fi

    if [ -n "${CODEX_THREAD_ID:-}" ] || [ -n "${CODEX_SESSION_JSONL:-}" ] ||
       [ -n "${CODEX_CI:-}" ]; then
        echo "codex"
        return 0
    fi

    if [ -n "${CLAUDECODE:-}" ] || [ -n "${CLAUDE_CODE_ENTRYPOINT:-}" ] ||
       [ -n "${CLAUDE_CODE_SESSION_ID:-}" ] ||
       [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
        echo "claude"
        return 0
    fi

    case "${HOST:-}" in
        codex|claude) echo "$HOST"; return 0 ;;
    esac

    detect_host_from_process_tree
}

detect_claude_profile() {
    # When the host is Codex, a layered profile config
    # ($CODEX_HOME/<name>.config.toml) can point Codex at a Claude model. If
    # one exists, the reviewer team can run its Claude-role reviewers through
    # `codex -p <name> exec`. Emit the profile name so the skill can use it.
    local codex_home="${CODEX_HOME:-${HOME:-}/.codex}"
    local profile name

    # Prefer a profile literally named "claude".
    if [ -f "$codex_home/claude.config.toml" ]; then
        echo "claude"
        return 0
    fi

    for profile in "$codex_home"/*.config.toml; do
        [ -e "$profile" ] || continue
        if grep -qiE '^[[:space:]]*model_provider[[:space:]]*=[[:space:]]*"anthropic"' "$profile" ||
           grep -qiE '^[[:space:]]*model[[:space:]]*=[[:space:]]*"claude' "$profile"; then
            name="$(basename "$profile")"
            echo "${name%.config.toml}"
            return 0
        fi
    done

    return 1
}

toml_value() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 1

    awk -v key="$key" '
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
            sub(/^[^=]*=[[:space:]]*/, "", $0)
            sub(/[[:space:]]*#.*/, "", $0)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
            gsub(/^"|"$/, "", $0)
            print $0
            exit
        }
    ' "$file"
}

infer_provider_from_model() {
    local model
    model="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"

    case "$model" in
        claude*|anthropic*) echo "anthropic"; return 0 ;;
        gpt-*|o[0-9]*|chatgpt*) echo "openai"; return 0 ;;
    esac

    return 1
}

detect_codex_process_args() {
    local pid="${PPID:-}" command name args

    if [ -n "${CODEX_TEST_PARENT_ARGS:-}" ]; then
        echo "$CODEX_TEST_PARENT_ARGS"
        return 0
    fi

    while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
        command="$(ps -p "$pid" -o comm= 2>/dev/null | awk '{print $1}' || true)"
        name="$(basename "$command" 2>/dev/null || true)"

        case "$name" in
            codex*)
                args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
                [ -n "$args" ] && echo "$args" && return 0
                ;;
        esac

        pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    done

    return 1
}

strip_toml_scalar() {
    local value="$1"
    value="${value#model_provider=}"
    value="${value#model=}"
    value="${value#profile=}"
    value="${value#\"}"
    value="${value%\"}"
    value="${value#\'}"
    value="${value%\'}"
    printf '%s' "$value"
}

detect_codex_active_config() {
    local codex_home="${CODEX_HOME:-${HOME:-}/.codex}"
    local args profile="" model="${CODEX_ACTIVE_MODEL:-${CODEX_MODEL:-}}"
    local provider="${CODEX_ACTIVE_PROVIDER:-${CODEX_MODEL_PROVIDER:-}}"
    local config_arg key value base_config profile_config

    args="$(detect_codex_process_args || true)"
    if [ -n "$args" ]; then
        # The Codex command line is simple for the flags we need here
        # (-p/--profile, -m/--model, and -c key=value).
        # shellcheck disable=SC2086
        set -- $args
        while [ "$#" -gt 0 ]; do
            case "$1" in
                -p|--profile)
                    profile="${2:-}"
                    shift 2 || break
                    continue
                    ;;
                --profile=*)
                    profile="${1#*=}"
                    ;;
                -m|--model)
                    model="${2:-}"
                    shift 2 || break
                    continue
                    ;;
                --model=*)
                    model="${1#*=}"
                    ;;
                -c|--config)
                    config_arg="${2:-}"
                    key="${config_arg%%=*}"
                    value="$(strip_toml_scalar "$config_arg")"
                    case "$key" in
                        model) model="$value" ;;
                        model_provider) provider="$value" ;;
                    esac
                    shift 2 || break
                    continue
                    ;;
                --config=*)
                    config_arg="${1#*=}"
                    key="${config_arg%%=*}"
                    value="$(strip_toml_scalar "$config_arg")"
                    case "$key" in
                        model) model="$value" ;;
                        model_provider) provider="$value" ;;
                    esac
                    ;;
            esac
            shift
        done
    fi

    base_config="$codex_home/config.toml"
    profile_config=""
    if [ -n "$profile" ]; then
        profile_config="$codex_home/${profile}.config.toml"
    fi

    if [ -z "$model" ] && [ -n "$profile_config" ]; then
        model="$(toml_value "$profile_config" "model" || true)"
    fi
    if [ -z "$provider" ] && [ -n "$profile_config" ]; then
        provider="$(toml_value "$profile_config" "model_provider" || true)"
    fi
    if [ -z "$model" ]; then
        model="$(toml_value "$base_config" "model" || true)"
    fi
    if [ -z "$provider" ]; then
        provider="$(toml_value "$base_config" "model_provider" || true)"
    fi
    if [ -z "$provider" ] && [ -n "$model" ]; then
        provider="$(infer_provider_from_model "$model" || true)"
    fi

    [ -n "$provider" ] || [ -n "$model" ] || return 1
    printf '%s\t%s\n' "$provider" "$model"
}

host="$(detect_host || true)"
[ -n "$host" ] || exit 0

context="HARNESS=$host"
if [ "$host" = "codex" ]; then
    active_config="$(detect_codex_active_config || true)"
    if [ -n "$active_config" ]; then
        active_provider="${active_config%%	*}"
        active_model="${active_config#*	}"
        if [ -n "$active_provider" ]; then
            context="${context}\\nCODEX_ACTIVE_PROVIDER=${active_provider}"
        fi
        if [ -n "$active_model" ]; then
            context="${context}\\nCODEX_ACTIVE_MODEL=${active_model}"
        fi
    fi

    profile="$(detect_claude_profile || true)"
    if [ -n "$profile" ]; then
        context="${context}\\nCLAUDE_PROFILE=${profile}"
    fi
fi

cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "$context"
  }
}
EOF
