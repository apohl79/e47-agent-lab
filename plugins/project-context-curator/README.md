# Project Context Curator

`project-context-curator` gives agents durable, reviewable project knowledge
instead of relying on transient conversation memory. It exists to make future
sessions start from verified architecture, terminology, storage, and workflow
decisions while keeping private context private.

> [!IMPORTANT]
> **The curator learns automatically.** During normal agent work, it builds and
> maintains a durable, verified knowledge base from reusable project facts. It
> does not retain raw chat history or ordinary implementation details.

## What it provides

- Guided initialization that records non-empty, verified project context rather
  than creating an empty memory file.
- Canonical local or Git-backed storage, plus migration that preserves record
  identity and provenance.
- Domain knowledge in the main Git store or dedicated repositories linked as
  Git submodules, with guided setup and whole-domain migration.
- Typed applicability for project, domain, user, machine, and universal facts;
  user and machine facts remain private in XDG storage.
- Session-start retrieval guidance, generated human-readable views, and
  hybrid search across relevant project and configured shared context, with
  deterministic abbreviation, morphology, and identifier reranking.
- A context audit for stale, duplicated, divergent, dead-path, time-bound, and
  oversized records.
- Explicit snapshot enrollment for optional cross-project retrieval. New
  projects are never silently added to that index; a rejected current-project
  prompt can be deferred privately with `global-enroll --defer-current`.
- User-confirmed domain roots that automatically add current and future
  initialized or bound repositories while keeping local paths private.
- Read-only graph exports in text, JSON, Mermaid, DOT, and self-contained HTML
  for inspecting project relationships, domains, record shadows, and
  divergences.

## Optional shared context

Enable shared context when agents should discover durable knowledge in related
repositories. Setup previews the exact repositories and storage policy before
anything is indexed or initialized; repositories are not silently added, and
private user or machine facts remain private.

Use `project-context-curator:manage-context-domains` to choose main or dedicated
storage for each new domain, manage portable Git-remote membership and local
root rules, or migrate its knowledge and member projects between stores. New
GitHub repositories can be created through `gh` after account, owner/name,
visibility, and the migration preview are approved.

On another machine, clone and configure the main store through
`project-context-curator:configure-context-storage`, activate the registered
domain submodules you need, and bind available component checkouts by Git remote.
Checkout paths may differ. Component source need not exist locally: inspect its
remote at a pinned commit and capture/update knowledge using the stable project
ID with source-remote and source-commit provenance. A binding alone is not source
verification. Use `git-store-register` to preview and approve a new remote-only
project identity, then bootstrap verified records without creating a source
checkout. New sources for global retrieval still require enrollment approval.

New PCC versions continue to read and write older storage repositories without
requiring domain submodules. Older PCC versions are not required to understand
new multi-store layouts or PR publication.

## Git-store publication

Each store checkout selects its own write flow through native local Git config.
`project-context-curator.writeMode` defaults to `auto`: GitHub branch rules
requiring PRs, workflows, or status checks select PR publication; otherwise PCC
uses direct pushes. Set `direct` or `pull-request` explicitly when needed:

```bash
git -C <store-checkout> config --local project-context-curator.writeMode pull-request
git -C <store-checkout> config --local project-context-curator.adminMerge false
```

These settings are local to each checkout, including dedicated domain stores;
they are not tracked or transferred to another machine. `adminMerge` defaults to
`false`; enable it only with explicit approval to use the caller's existing
GitHub bypass rights. It cannot bypass rules the caller lacks permission to
bypass.
PR publication requires `gh` authentication and merge commits enabled on the
destination, preserving canonical commit identities.

Ordinary PR-backed writes commit locally and queue a bounded background worker.
It retries every 15 seconds for up to 15 minutes, releasing store locks between
attempts; it is not a recurring scheduled task. Pending writes are not reported
as synchronized. Inspect `git-store-status --repo <project-checkout>` and retry
once with `git-store-publish --repo <project-checkout>`, using the updater.
Errors and the queue remain in the local Git directory after the worker stops.
Approved domain migrations resume from their journal and retain source copies
until publication is verified. Local ↔ Git-store runtime migrations involving
PR publication are currently rejected before mutation.

## Why it exists

Important project knowledge is often rediscovered in every new agent session,
then lost again with the conversation. The curator keeps only durable facts,
preserves their source and scope, and provides a review path when that
knowledge becomes stale. Its explicit enrollment and private-scope boundaries
avoid turning a convenience index into an unreviewed data-sharing mechanism.

## Xedoc and Codex compatibility

Xedoc supports the `context.thread` extension, which inserts the curator's
durable-knowledge guidance as a fixed context block for each active thread.
Codex does not support that manifest field, so its SessionStart hook provides
the complete retrieval and capture policy. Codex and Claude Code register a
UserPromptSubmit hook that reinforces the essentials every fourth top-level
user turn. Xedoc selects a SessionStart-only hook file alongside its persistent
thread context and does not register the periodic reminder.

## Knowledge graph

The graph makes cross-project context easier to understand: explore
dependencies, domains, shared records, and duplicate or divergent definitions
before they become stale or inconsistent.

![Example multi-domain knowledge graph](assets/knowledge-graph-demo.png)

Open the [interactive HTML demo](assets/knowledge-graph-demo.html) to inspect
graph search, relationship filters, and node details.

## When to use it

Use the curator when a repository needs persistent terminology, architecture
decisions, component ownership, or cross-project context. It also fits context
hygiene reviews and graph exploration. The plugin supports Xedoc, Codex, and
Claude Code; install it through the [marketplace README](../../README.md#install), then
invoke the relevant `project-context-curator:*` skill.
