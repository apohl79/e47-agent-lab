#!/usr/bin/env python3
"""
SessionStart hook — injects the context-checkpoint contract into every session,
independent of CLAUDE.md (project or user). Intended to re-fire on
startup/resume/clear/compact so the instruction is present even right after an
auto-compaction. (Re-fire on source="compact" is the load-bearing property —
verify it in a live session via the log line this script writes.)

Pairs with precompact-gate.py: the gate defers auto-compaction until the model
drops the sentinel at a safe point. This contract tells the model how/when.

ALWAYS ACTIVE in Claude Code (no opt-in switch). Under Codex it is a hard
no-op. See hooks/README.md.

Numbers in the contract are DERIVED from config (autoCompactWindow in
~/.claude/settings.json and COMPACT_GATE_HIGHWATER_TOKENS) so they can't drift
out of sync with the gate.
"""
import json, os, sys, datetime

LOG = os.path.expanduser("~/.claude/compaction-gate.log")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
SENTINEL = os.path.expanduser(os.environ.get("COMPACT_GATE_SENTINEL", "~/.claude/ok-to-compact"))
OVERHEAD = 33000  # measured: 120K window first fired at ~87K -> ~33K system/tools overhead


def env_int(name, default):
    """Parse an int env knob; fall back to default on garbage (a bad value must
    never crash the hook)."""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def under_codex() -> bool:
    """Codex ships this plugin from the same directory but must never run these
    hooks. Codex's hook runner EMULATES Claude Code (sets CLAUDECODE,
    CLAUDE_PLUGIN_ROOT, ... and strips CODEX_* from the hook env), so env
    sniffing is unreliable. Structural check instead: Codex copies plugins into
    ~/.codex/plugins/cache/ and runs hooks from there — detect via this
    script's own resolved path (and CLAUDE_PLUGIN_ROOT), with the CODEX_* env
    check kept as a fallback."""
    codex_dir = os.sep + ".codex" + os.sep
    candidates = (os.path.realpath(__file__),
                  os.path.realpath(os.environ.get("CLAUDE_PLUGIN_ROOT") or "/"))
    if any(codex_dir in c for c in candidates):
        return True
    return any(k.startswith("CODEX_") for k in os.environ)


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def cfg_window(default=350000):
    try:
        return int(json.load(open(SETTINGS)).get("autoCompactWindow", default))
    except Exception:
        return default



# No-op under Codex: these hook events and the gate semantics are
# Claude Code-specific.
if under_codex():
    sys.exit(0)

# read payload (capture source for the log; the re-fire-on-compact check depends on it)
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
source = data.get("source", "?")

window = cfg_window()
elig_k = max(0, round((window - OVERHEAD) / 1000))
failsafe_k = round(env_int("COMPACT_GATE_HIGHWATER_TOKENS", 500000) / 1000)

CONTRACT = (
    "Context-checkpoint protocol (a PreCompact gate is active in this session):\n"
    "- Automatic compaction is DEFERRED until you signal a safe point, so your "
    "context is never reset mid-task. On this setup compaction becomes eligible "
    f"around ~{elig_k}K tokens and a failsafe forces it near {failsafe_k}K.\n"
    "- You can see your live context size each turn in the `Token usage:` line. "
    "Its `total`/`remaining` are measured against the model's hard ceiling (~1M), "
    f"NOT this gate — compare the `used` number against the ~{elig_k}K (eligible) "
    f"and {failsafe_k}K (failsafe) thresholds above, not against `remaining`.\n"
    "- When you reach a clean stopping point — a task or TODO finished, tests "
    "passing, or just before starting a large/destructive operation — AND the "
    f'session has grown long, run `touch "{SENTINEL}"`. That authorizes '
    "exactly ONE compaction at that boundary; the gate consumes the sentinel when "
    "it fires.\n"
    "- Do NOT touch the sentinel mid-task. If you never signal, the failsafe "
    f"compacts automatically near {failsafe_k}K — safe, but a checkpoint you choose "
    "is cleaner.\n"
    "- Prefer checkpointing between logical units rather than letting context ride "
    "to the failsafe; it keeps cost down and avoids a forced reset at an awkward moment."
)

log(f"SESSIONSTART fired source={source} -> inject contract (elig~{elig_k}K failsafe {failsafe_k}K)")

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": CONTRACT,
    }
}))
sys.exit(0)
