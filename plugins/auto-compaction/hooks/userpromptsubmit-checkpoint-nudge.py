#!/usr/bin/env python3
"""
UserPromptSubmit hook — dynamic checkpoint nudge.

When the session is in the deferral band (context above the nudge threshold) and
no sentinel is set, inject a one-line reminder so the model can decide to
checkpoint at this user-turn boundary (a natural safe point). Stays silent
otherwise, so it adds no noise on short/early sessions.

This is the timing reinforcement for the SessionStart contract. UserPromptSubmit
is fine here (the nudge is dynamic/turn-specific); it is NOT used for the standing
contract, which lives in the SessionStart hook (UserPromptSubmit replays stale on
resume).

ALWAYS ACTIVE in Claude Code (no opt-in switch). Under Codex it is a hard
no-op. See hooks/README.md.

Env knobs:
  COMPACT_GATE_SENTINEL          sentinel path (default ~/.claude/ok-to-compact)
  COMPACT_NUDGE_TOKENS           nudge above this many context tokens (default 300000)
  COMPACT_GATE_HIGHWATER_TOKENS  failsafe ceiling, shown in the nudge (default 500000)
"""
import os, sys, json, datetime

SENTINEL = os.path.expanduser(os.environ.get("COMPACT_GATE_SENTINEL", "~/.claude/ok-to-compact"))
LOG = os.path.expanduser("~/.claude/compaction-gate.log")


def env_int(name, default):
    """Parse an int env knob; fall back to default on garbage (a bad value must
    never crash the hook)."""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


NUDGE_AT = env_int("COMPACT_NUDGE_TOKENS", 300000)
FAILSAFE_K = round(env_int("COMPACT_GATE_HIGHWATER_TOKENS", 500000) / 1000)


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


def read_context_tokens(tpath):
    if not tpath or not os.path.exists(tpath):
        return None
    last = None
    try:
        # Tail-read: transcripts grow to hundreds of MB; only the last records
        # matter. Read the final 256KB and skip the first (possibly partial) line.
        TAIL = 256 * 1024
        with open(tpath, "rb") as fb:
            fb.seek(max(0, os.path.getsize(tpath) - TAIL))
            chunk = fb.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        if len(chunk) >= TAIL and lines:
            lines = lines[1:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("isSidechain"):
                continue
            u = (obj.get("message") or {}).get("usage") or obj.get("usage")
            if isinstance(u, dict) and ("input_tokens" in u or "cache_read_input_tokens" in u):
                last = u
    except Exception:
        return None
    if not last:
        return None
    # Input-side only: output_tokens is the assistant's reply size, not context.
    return (int(last.get("input_tokens", 0) or 0)
            + int(last.get("cache_read_input_tokens", 0) or 0)
            + int(last.get("cache_creation_input_tokens", 0) or 0))


def main():
    # No-op under Codex: these hook events and the gate semantics are
    # Claude Code-specific.
    if under_codex():
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # silent

    # Already authorized — no need to nudge.
    if os.path.exists(SENTINEL):
        sys.exit(0)

    tokens = read_context_tokens(data.get("transcript_path"))
    if tokens is None or tokens < NUDGE_AT:
        sys.exit(0)  # below threshold or unreadable -> stay silent
    if tokens >= FAILSAFE_K * 1000:
        sys.exit(0)  # failsafe will ALLOW anyway -> "deferred" claim would be wrong

    k = round(tokens / 1000)
    nudge = (
        f"[context-checkpoint] Context is ~{k}K tokens and auto-compaction is being "
        f"deferred until you checkpoint. If you are at a clean stopping point, run "
        f'`touch "{SENTINEL}"` to allow one compaction now. If you are '
        f"mid-task, ignore this — it will be offered again, and a failsafe will "
        f"compact near {FAILSAFE_K}K if needed."
    )
    log(f"NUDGE injected tokens={tokens} (>= {NUDGE_AT}, no sentinel)")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": nudge,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
