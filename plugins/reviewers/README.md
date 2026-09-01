# Reviewers

`reviewers` gives an agent a disciplined way to review and finish pull
requests. It exists to turn review from a single, broad opinion into
evidence-backed coverage across correctness, security, architecture,
observability, integration, and test quality.

## What it provides

- A reviewer team that prepares a review target, runs the project’s
  verification commands, and coordinates host-native, cross-provider, Gemini,
  and security reviewers in parallel when available.
- Distinct reviewer lenses so parallel reviews add coverage instead of repeating
  the same findings.
- Structured findings with severity, source locations, and
  introduced-versus-pre-existing classification.
- A lightweight OWASP and secure-coding fallback when a dedicated security
  skill is unavailable.
- PR finalization that can move a draft to ready, synchronize it with the base
  branch, resolve review threads, monitor checks, and merge only with explicit
  approval.
- Slack review-request tools for managing approved channels and sending either
  a single-PR or stacked-PR request.

## Why it exists

PRs often fail in the gaps between individual review habits: a functional
review can miss a security flaw, and a security review can miss an untested
integration boundary. This plugin makes those viewpoints explicit, keeps
findings reproducible, and helps teams bring an existing PR to a reliably
mergeable state.

## When to use it

- Request a reviewer team for a change, branch, PR, or stack.
- Ask an agent to prepare an existing PR for review or merge.
- Send a review request to Slack after the PR or stack is ready.

The plugin supports Codex and Claude Code. Install it through the
[marketplace README](../../README.md#install), then invoke the relevant
`reviewers:*` skill from your agent session.
