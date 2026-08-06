---
name: auto-compact-setup
description: Verify and set up the compaction-gate (plugins/auto-compaction/hooks) in ~/.claude/settings.json. Asks whether the active model is a 200K or 1M context window (or custom) and applies the matching profile for autoCompactWindow + the gate env knobs (COMPACT_NUDGE_TOKENS, COMPACT_GATE_HIGHWATER_TOKENS, COMPACT_GATE_SENTINEL), checks CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT, reports the effective configuration, and fixes anything missing or inconsistent. Claude Code only — does nothing on other hosts (e.g. Codex). Trigger on mentions of compaction gate, compact gate, ok-to-compact, checkpoint protocol, 200k/1M context window setup, or auto-compaction setup.
---

# Compaction Gate Config Check

Validates and configures the compaction-gate hooks in
`plugins/auto-compaction/hooks/` (see its `README.md` for background). The right
thresholds depend on the active
model's context window, so this skill asks which window applies and writes the
matching profile.

## Profiles (remembered defaults)

The gate has three tunables plus `autoCompactWindow`. Each profile keeps the
invariant `nudge ≤ eligible ≤ highwater`, where `eligible = autoCompactWindow −
33000` (the ~33K system/tools overhead measured in the hooks; see
`sessionstart-checkpoint-contract.py`). `highwater` is the failsafe ceiling and
must stay safely below the model's hard context limit so a forced compaction
never lands inside a failed request.

| Setting | **200K** window | **1M** window |
|---|---|---|
| `autoCompactWindow` | `170000` | `318000` |
| `env.COMPACT_NUDGE_TOKENS` | `130000` | `280000` |
| `env.COMPACT_GATE_HIGHWATER_TOKENS` | `195000` | `400000` |
| derived `eligible` (window − 33K) | ~137000 | ~285000 |
| order check | 130K ≤ 137K ≤ 195K ✓ | 280K ≤ 285K ≤ 400K ✓ |

Both profiles also require:

| Setting | Value | Why |
|---|---|---|
| `autoCompactEnabled` | `true` | Gate only mediates fires Claude Code initiates |
| `env.CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT` | `"1"` | Model needs the `Token usage:` line to judge checkpoints |
| `env.COMPACT_GATE_SENTINEL` | optional; tilde fine (scripts expand it) | Sentinel path; default `~/.claude/ok-to-compact` |

**Custom**: any window W is valid as long as the caller supplies
`nudge ≤ (W − 33000) ≤ highwater` and `highwater` sits below the model's hard
limit. Suggest `nudge = round(W − 33000 − 5000)` and `highwater = round(W ×
1.05)` capped a safe margin under the model limit, then confirm.

## Step 0 — Host check (gate)

This skill is for Claude Code only. If the host is not Claude Code — e.g. any
`CODEX_*` environment variable is present (`env | grep -c '^CODEX_'` > 0) or
you are otherwise not running as Claude Code — reply "auto-compact-setup:
not running in Claude Code, nothing to do." and STOP. Do not run any further
steps.

## Step 1 — Gather state

Run a single check and capture the output:

```bash
python3 - <<'EOF'
import json, os

settings_path = os.path.expanduser("~/.claude/settings.json")
try:
    s = json.load(open(settings_path))
except Exception as e:
    print(f"SETTINGS_UNREADABLE: {e!r}")
    s = {}

env = s.get("env", {})

def env_int(name, default):
    try:
        return int(os.environ.get(name) or env.get(name) or default)
    except (TypeError, ValueError):
        return f"INVALID ({os.environ.get(name) or env.get(name)!r}, falls back to {default})"

window = s.get("autoCompactWindow")
hw = env_int("COMPACT_GATE_HIGHWATER_TOKENS", 400000)
nu = env_int("COMPACT_NUDGE_TOKENS", 280000)
checks = {
    "autoCompactEnabled": s.get("autoCompactEnabled"),
    "autoCompactWindow": window,
    "env.CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT": env.get("CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT"),
    "COMPACT_GATE_SENTINEL (effective)": os.path.expanduser(
        os.environ.get("COMPACT_GATE_SENTINEL") or env.get("COMPACT_GATE_SENTINEL") or "~/.claude/ok-to-compact"),
    "COMPACT_GATE_HIGHWATER_TOKENS (effective)": hw,
    "COMPACT_NUDGE_TOKENS (effective)": nu,
    "stale sentinel present": os.path.exists(os.path.expanduser(
        os.environ.get("COMPACT_GATE_SENTINEL") or env.get("COMPACT_GATE_SENTINEL") or "~/.claude/ok-to-compact")),
}
for k, v in checks.items():
    print(f"{k}: {v}")

# which remembered profile (if any) do the current values match?
PROFILES = {"200K": (170000, 130000, 195000), "1M": (318000, 280000, 400000)}
matched = next((name for name, (w, n, h) in PROFILES.items()
                if (window, nu, hw) == (w, n, h)), "custom/none")
print(f"matches profile: {matched}")

# consistency: nudge <= eligible(window-33K) <= highwater
if isinstance(window, int) and isinstance(hw, int) and isinstance(nu, int):
    elig = window - 33000
    if not (nu <= elig <= hw):
        print(f"ORDER_WARNING: expected nudge({nu}) <= eligible({elig}) <= highwater({hw})")
EOF
```

## Step 2 — Ask which context window applies

The active model's window cannot be auto-detected reliably (the same model name
runs at both 200K and 1M tiers, and Claude Code exposes no window field to
hooks). So **ask the user** with `AskUserQuestion`:

- **200K context window** → apply the 200K profile from the table above.
- **1M context window** → apply the 1M profile from the table above.
- **Custom** → ask for the window W (free text via "Other"), then derive and
  confirm `nudge`/`highwater` per the Custom rule above.
- **Keep current** → only fix the non-profile requirements (TOKEN_USAGE
  attachment, autoCompactEnabled, stale sentinel, any ORDER_WARNING).

Pre-select the profile reported by "matches profile" if it is `200K` or `1M`.
If `Token usage:` is visible this turn and the live `used` number already
exceeds 200K, note that the model is necessarily a >200K (1M) window and
recommend the 1M profile.

## Step 3 — Report and confirm

Present a pass/fail table: the chosen profile's three values vs. current, plus
`autoCompactEnabled`, `CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT`, and stale
sentinel. `INVALID` markers and `ORDER_WARNING` count as failures. List exactly
what will change. Offer "fix all" as the first option when multiple items fail.

## Step 4 — Apply fixes

- settings.json changes: Read `~/.claude/settings.json` first, then Edit —
  merge, never replace; preserve all existing keys (e.g. `GEMINI_API_KEY`).
  Write `autoCompactWindow` at top level and the gate knobs under `env`.
  Validate with `python3 -c "import json; json.load(open(...))"` afterwards.
- Stale sentinel: `rm` it.
- After settings/env changes, tell the user: **restart required** — env vars
  and `autoCompactWindow` are read at session start.

If all checks pass for the chosen profile, report "Compaction gate config OK"
with the effective values and end.
