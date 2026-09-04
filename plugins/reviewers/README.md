# Reviewers

`reviewers` gives an agent a disciplined way to review implementation
checkpoints and finish pull requests. It combines cheap feedback during
implementation with evidence-backed PR triage and optional broad review.

## What it provides

- A lightweight, single-reviewer checkpoint loop that reviews one implementation
  slice, returns valid bugs to the implementing agent, verifies the repair, and
  requires the shared `review-learning-closure` evidence-based root-cause
  closure before accepting it.
- A reviewer team that prepares a review target, runs the project’s
  verification commands, and coordinates host-native, cross-provider, Gemini,
  and security reviewers in parallel when available.
- Distinct reviewer lenses so parallel reviews add coverage instead of repeating
  the same findings.
- Structured findings with severity, source locations, and
  introduced-versus-pre-existing classification.
- An implementation preflight contract: coding rules are explicitly marked on
  context patterns and must be searched and read before source, test, or
  configuration edits.
- A lightweight OWASP and secure-coding fallback when a dedicated security
  skill is unavailable.
- PR finalization that inventories human and bot findings, learns from valid
  introduced bugs through the same closure workflow, moves a draft to ready,
  synchronizes it with the base branch, resolves review threads, monitors
  checks, and merges only with explicit approval.
- Slack review-request tools for managing approved channels and sending either
  a single-PR or stacked-PR request.

## Why it exists

Late PR review makes every defect more expensive to understand because the
implementation context has gone cold. This plugin adds a small review loop
during implementation, keeps findings reproducible, and reserves broad
multi-reviewer coverage for changes that justify it.

## When to use it

- During every source, test, or configuration implementation task, review at
  least one checkpoint before declaring the code ready.
- During larger implementation tasks, review each complete behavior slice
  before building the next slice.
- When an implementation has an open PR, finalize it and address human, bot,
  check, and quality-gate findings before declaring the task complete.
- Request the full reviewer team explicitly when broad independent coverage is
  worth its cost.
- Send a review request to Slack after the PR or stack is ready.

The plugin supports Codex and Claude Code. Install it through the
[marketplace README](../../README.md#install), then invoke the relevant
`reviewers:*` skill from your agent session.
