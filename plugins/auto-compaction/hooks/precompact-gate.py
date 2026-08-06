#!/usr/bin/env python3
"""
compaction-gate: defer auto-compaction until a safe checkpoint, without ever
risking a failed request.

Wired as a PreCompact hook. Works together with:
  - a model-chosen safe point (the SessionStart contract tells the model when),
  - a sentinel file (~/.claude/ok-to-compact) the model touches at that point.

ALWAYS ACTIVE in Claude Code (no opt-in switch). Under Codex it is a hard
no-op. See hooks/README.md.

Decision logic on each PreCompact fire (FAIL-OPEN — only blocks when confident):
  manual trigger ............................ ALLOW (user asked for it)
  can't read context size ................... ALLOW (fail-open)
  tokens >= HIGH_WATER ...................... ALLOW (failsafe: covers the
                                              near-limit recovery fire and any
                                              runaway; never block into a
                                              request failure)
  sentinel present .......................... ALLOW + consume sentinel
                                              (safe-point compaction)
  else (auto, mid-band, no sentinel) ........ BLOCK (wait for a safe point)

Every fire is logged to ~/.claude/compaction-gate.log with trigger + token
count + decision — this log is also how you observe whether auto-compaction is
level-triggered (re-fires each turn while blocked) or one-shot.

Env knobs:
  COMPACT_GATE_SENTINEL          path to sentinel file (default ~/.claude/ok-to-compact)
  COMPACT_GATE_HIGHWATER_TOKENS  failsafe ceiling in tokens (default 500000)
"""
import sys, os, json, datetime

LOG = os.path.expanduser("~/.claude/compaction-gate.log")
SENTINEL = os.path.expanduser(os.environ.get("COMPACT_GATE_SENTINEL", "~/.claude/ok-to-compact"))


def env_int(name, default):
    """Parse an int env knob; fall back to default on garbage (a bad value must
    never crash the hook — fail-open by design)."""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


HIGH_WATER = env_int("COMPACT_GATE_HIGHWATER_TOKENS", 500000)


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


def log(msg: str) -> None:
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def allow(reason: str) -> "None":
    log(f"ALLOW  {reason}")
    sys.exit(0)  # exit 0, no output -> compaction proceeds


def block(reason: str) -> "None":
    log(f"BLOCK  {reason}")
    print(json.dumps({"decision": "block", "reason": f"compaction-gate: {reason}"}))
    sys.exit(0)


def read_context_tokens(tpath):
    """Best-effort current context size = input-side tokens of the latest
    usage-bearing record in the transcript. Returns int, or None if unreadable."""
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


def main() -> None:
    # No-op under Codex: these hook events and the gate semantics are
    # Claude Code-specific.
    if under_codex():
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception as e:
        log(f"parse-error {e!r} -> fail-open")
        allow("stdin parse error (fail-open)")
        return

    trigger = data.get("trigger", "?")

    if trigger == "manual":
        allow("trigger=manual")
        return

    tokens = read_context_tokens(data.get("transcript_path"))
    if tokens is None:
        allow("trigger=auto tokens=? (fail-open: could not read transcript)")
        return

    if tokens >= HIGH_WATER:
        # Consume any pending sentinel too — this compaction satisfies it;
        # leaving it would silently authorize the NEXT fire.
        try:
            os.remove(SENTINEL)
        except OSError:
            pass
        allow(f"trigger=auto tokens={tokens} >= high_water={HIGH_WATER} (failsafe)")
        return

    if os.path.exists(SENTINEL):
        try:
            os.remove(SENTINEL)  # one-shot: a sentinel authorizes ONE compaction
        except OSError as e:
            # Could not consume: still allow this authorized compaction, but
            # say so loudly — a stale sentinel would re-authorize every fire.
            allow(f"trigger=auto tokens={tokens} sentinel present but UNREMOVABLE ({e!r}) -> "
                  f"allowing; one-shot NOT enforced, remove {SENTINEL} manually")
            return
        allow(f"trigger=auto tokens={tokens} sentinel present -> safe-point compaction (sentinel consumed)")
        return

    block(f"trigger=auto tokens={tokens} no sentinel -> deferring to next safe checkpoint")


if __name__ == "__main__":
    main()
