# Auto Compaction

`auto-compaction` is a Claude Code plugin that protects long-running
conversations from being compacted at an arbitrary and unsafe point. It exists
to preserve the decisions, evidence, and next actions an agent needs before
its context is summarized.

## What it provides

- A SessionStart checkpoint contract that tells the agent how to leave a useful
  handoff before compaction.
- A pre-compaction gate that defers automatic compaction until the model reaches
  a safe checkpoint.
- A prompt-time nudge when the conversation enters the configured deferral
  band.
- A setup skill that selects a 200K, 1M, or custom context-window profile and
  verifies the required Claude Code settings and gate environment values.
- Fail-open hooks: a configuration or hook failure does not block Claude Code
  from continuing its work.

## Why it exists

Context compaction is valuable, but compacting midway through an investigation
or implementation can lose the constraints that make the next action safe.
The gate makes compaction intentional: the agent records a checkpoint first,
then permits the summary to happen.

## When to use it

Use the setup skill when enabling the compaction gate, changing model context
windows, or diagnosing an `ok-to-compact` or checkpoint prompt. The plugin is
Claude Code only; it is intentionally a no-op in Codex.

Install it through the [marketplace README](../../README.md#install), then ask
Claude Code to use `auto-compact-setup`.
