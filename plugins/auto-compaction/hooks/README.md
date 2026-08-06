# Context compaction gate

Hooks that defer Claude Code's **automatic context compaction** until the model
reaches a safe checkpoint, instead of letting it reset context mid-task. A cost
lever for long agentic sessions.

> Ported from an internal workflow plugin (historical provenance intentionally omitted for public distribution).

**Claude Code only — always active there.** Unlike the upstream PR, these hooks
have no opt-in switch: installing the plugin activates the gate. The Codex
marketplace does not list this plugin. If these hooks ever run under Codex from
a stale cache or manual installation path, they are guaranteed no-ops — but NOT
via env sniffing or the manifest:

- Codex can run Claude-style `hooks/hooks.json` hooks from stale plugin caches
  or manual installation paths even when a Codex marketplace no longer lists
  the plugin.
- Codex's hook runner **emulates Claude Code**: the hook subprocess gets
  `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PLUGIN_ROOT`, ... and has all
  `CODEX_*` variables stripped. Checking for `CODEX_*` therefore detects
  nothing.
- What is reliable is **structural**: Codex copies the plugin into
  `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/` and runs hooks
  from that copy. `under_codex()` checks whether the script's own resolved
  path (or `CLAUDE_PLUGIN_ROOT`) contains a `/.codex/` component, with the
  `CODEX_*` env check kept only as a fallback.

## Why

Long agentic sessions re-read their full context every turn, so a session's
cost grows **super-linearly** with its length — the back half is where the money
goes. Compacting (summarizing context in place) turns that back to ~linear. The
naive alternatives are worse:

| Approach | Problem |
|---|---|
| Fixed low-threshold auto-compaction | Resets mid-task; turns a 1M model into a 200K one; reintroduces babysitting |
| Manual `/clear` / scoping to fewer files | Fragments sessions; shifts cost onto engineer time |
| **This gate** | Compaction happens, but only at a model-chosen safe point (task done, tests green) — or a failsafe ceiling |

## ⚠️ Required settings (the gate has nothing to mediate without these)

The hooks are always active, but a plugin cannot write your `~/.claude/settings.json`.
For the system to actually do anything, set:

```jsonc
// ~/.claude/settings.json
{
  "autoCompactEnabled": true,
  "autoCompactWindow": 350000,        // window the gate measures against
  "env": {
    "CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT": "1"   // so the model sees its context size
  }
}
```

| Setting | Why required |
|---|---|
| `autoCompactEnabled: true` | The gate only mediates fires that Claude Code **initiates**. With auto-compaction off, nothing fires and the gate never runs. |
| `autoCompactWindow` | Sets when Claude Code's auto-compaction becomes eligible; the contract's thresholds are derived from it. |
| `env.CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT=1` | Surfaces the live `Token usage:` line so the model can judge when to checkpoint. |

> If you previously ran these scripts from your **personal** `~/.claude/settings.json`
> hooks, remove those entries when enabling the plugin version — otherwise both
> copies fire on every event (double-logging, double contract injection).

## How it works

Three hooks (always active in Claude Code, no-ops under Codex):

| Hook | Event | Role |
|---|---|---|
| `precompact-gate.py` | `PreCompact` | Decides whether a compaction fire proceeds now or is deferred |
| `sessionstart-checkpoint-contract.py` | `SessionStart` | Injects the checkpoint protocol each session (survives compaction/resume) |
| `userpromptsubmit-checkpoint-nudge.py` | `UserPromptSubmit` | One-line reminder to checkpoint when context is in the deferral band |

**Gate decision (fail-open — only BLOCKs when confident):**

| Condition | Decision |
|---|---|
| running under Codex (script path/`CLAUDE_PLUGIN_ROOT` under `~/.codex/`, or `CODEX_*` env) | ALLOW (inert) |
| `trigger=manual` (`/compact`) | ALLOW — you asked for it |
| context size unreadable | ALLOW — fail-open, never block into a failed request |
| `tokens ≥ COMPACT_GATE_HIGHWATER_TOKENS` (~500K) | ALLOW — failsafe ceiling |
| sentinel `~/.claude/ok-to-compact` present | ALLOW + consume sentinel — safe-point compaction |
| else (auto, mid-band, no sentinel) | BLOCK — defer to next checkpoint |

**The contract the model is given:** when it reaches a clean stopping point in a
long session, it runs `touch ~/.claude/ok-to-compact`, which authorizes exactly
**one** compaction at that boundary. If it never signals, the failsafe compacts
automatically near the high-water mark.

## Env knobs

| Var | Default | Effect |
|---|---|---|
| `COMPACT_GATE_SENTINEL` | `~/.claude/ok-to-compact` | Sentinel file path |
| `COMPACT_GATE_HIGHWATER_TOKENS` | `500000` | Failsafe ceiling (always ALLOW at/above) |
| `COMPACT_NUDGE_TOKENS` | `300000` | Nudge fires above this context size |

State/observability: every gate fire and nudge is appended to
`~/.claude/compaction-gate.log` (trigger + token count + decision).

## Status / known limitations

| Path | Status |
|---|---|
| `manual` `/compact` → ALLOW | ✓ verified live (takes the `manual` branch — returns before the sentinel logic, so it does **not** consume a sentinel) |
| sentinel-consume / BLOCK / fail-open ALLOW | ✓ verified via synthetic-transcript smoke test — **not yet observed on a live auto-fire** |
| failsafe ceiling (`tokens ≥ ~500K` → ALLOW) | logic verified by smoke test; ceiling not yet hit in practice |
| **eligible auto-fire at `autoCompactWindow − overhead` (~317K for a 350K window)** | **configured/derived, NOT yet confirmed on an auto-trigger.** On a long *resumed* 1M-context session, Claude Code's auto-compaction was observed **not** to fire at the configured window. Treat the sentinel (your explicit checkpoint) and the failsafe as the dependable paths; confirm the eligible auto-fire in a fresh session before relying on it. |

The `~33K` overhead constant in the SessionStart contract was measured on a 120K
test window; it is an estimate for display only and does not affect the gate's
decisions.

## Disable / uninstall

Set `"autoCompactEnabled": false` in `~/.claude/settings.json` (nothing fires,
the gate never runs), or uninstall the plugin to remove the hooks entirely.
