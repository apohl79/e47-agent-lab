---
name: writing
description: "Write inline-discussion documents with local source links, Mermaid diagrams, and persistent task-register metadata for the task sidebar. Use when writing content that will be opened in the inline-discussion browser."
---

# Inline-discussion writing conventions

## Writing style and evidence

Use simple, human-understandable language. Prefer short sentences and concrete words.
Avoid encouragement, praise, and motivational language.
Never add unverified information. If a claim cannot be checked, leave it out.
List the references used for factual claims, with links to the source files or external sources.
Keep content as brief as possible. Add extra context or prose only when asked.
Prefer a diagram over prose whenever the content can be represented visually.

## Local file links

Use ordinary Markdown links whose target ends in one of these suffixes:

- `path/to/file.md:40` — line 40.
- `path/to/file.md:40-45` — lines 40 through 45.
- `path/to/file.md:40:5-45:12` — line 40, character 5 through line 45, character 12.

Lines and characters are 1-based and inclusive. Prefer a path relative to the Markdown document because it remains portable when the project moves. Use an absolute filesystem path or a server-root path beginning with `/` only when a document-relative path is unsuitable. Do not hard-code the discussion server's host or port because it changes between launches.

A slash-prefixed target keeps server-root behavior when a matching project file
exists. Otherwise, a supported Markdown or source file at that absolute
filesystem path opens in the discussion browser.

```markdown
[Provisioning decision](./decision.md:40-45)
[Exact setting](../infra/main.tf:18:3-18:31)
```

Use angle brackets around a Markdown link target containing spaces. Do not append this range syntax to remote HTTP(S) URLs.

## In-document section links

Use a hash-only Markdown link to navigate to a heading in the same document. This form is portable and scrolls the inline-discussion viewport without reloading the page.

Heading targets are lowercased, with punctuation and spaces replaced by hyphens. Prefer the hash-only form over repeating the current document path.

## Table of contents

For a document with several major sections, add a concise table of contents after the title and introductory text. Use hash-only links, keep link labels aligned with their headings, and include only sections that help the reader navigate. Mirror one level of heading hierarchy when it materially clarifies the document.

```markdown
## Table of contents

- [Deployment](#deployment)
- [Failure handling](#failure-handling)
  - [Retry policy](#retry-policy)
```

## Diagram preference

Always prefer a fenced `mermaid` diagram over prose when expressing structure, flow, sequence, state, or relationships. Prefer Mermaid over an SVG or PNG as well. Keep prose for explanation that a diagram cannot express, and keep screenshots or other images whose exact visual appearance is the evidence.

## Persistent tasks and the sidebar

Opt in per document. Put exactly one standalone declaration after the title,
surrounded by blank lines. Resolve its JSON path relative to the Markdown file;
the existing register must stay within the server's project root, including
after symlink resolution. Do not put declarations in code fences or blockquotes.

```markdown
# Daily briefing

<!-- task-register: ../tasks/register.json -->

## Tasks

- [ ] Review gateway capacity <!-- task-id: TASK-042 -->
  Prepare a recommendation for the minimum replica count.
```

Use an ordinary Markdown task item and append exactly one `task-id` comment
on its first line. IDs must match `[A-Za-z0-9][A-Za-z0-9_-]{0,79}`, exist in
the register, and occur only once per document. Never infer identity from a
checkbox position, title hash, discussion block ID, or date. Reuse the same ID
when moving or carrying a task to another document. Leave ordinary untracked
checkboxes unchanged.

Keep human-readable titles, rationale, evidence links, and dated notes in
Markdown. Do not write executable HTML, duplicate the sidebar as a Markdown
table, or encode lifecycle state in checkbox syntax. The checkbox represents
completion only. Tracked checkboxes cannot bypass final review.

The renderer associates each task item with `data-task-id` and a
`#task-TASK-042` navigation anchor. The browser builds its sidebar from the
referenced register and shows only tasks referenced by the open document.
The sidebar and inline badges show **current** state, including when viewing
an older document; stored Markdown remains a historical snapshot unless an
authorized workflow explicitly refreshes the current document. Ordinary
Markdown viewers show the saved snapshot without the sidebar.

Register changes arrive over the existing live-event connection without
rerendering the document or interrupting discussions. Missing registers,
unknown/duplicate IDs, malformed records, and out-of-project paths produce a
visible task error; never silently create replacement tasks or reset state.
Task navigation persists across days; discussion threads remain owned by
their original document and are not automatically moved by task identity.

### Register commands

Use the shared CLI rather than directly editing JSON:

```bash
inline-discussion tasks init tasks/register.json
inline-discussion tasks read tasks/register.json
inline-discussion tasks update tasks/register.json command.json
# Use "-" instead of command.json to read JSON from stdin.
```

`init` refuses to overwrite an existing file. `read` returns version 1,
a register `revision`, and `tasks`. Every update must supply the latest
`expectedRevision`. Updates use an exclusive lock and atomic replacement.
On a stale revision or lock conflict, reload and reconsider; do not blindly
retry. Never remove another writer's lock.

Create a task, then classify it with a second command using the new revision:

```json
{
  "action": "create",
  "expectedRevision": 0,
  "id": "TASK-042",
  "task": {
    "title": "Review gateway capacity",
    "source": "https://example.com/issues/42",
    "owner": "Andreas"
  }
}
```

```json
{
  "action": "classify",
  "expectedRevision": 1,
  "id": "TASK-042",
  "note": "Current source and repository access verified",
  "task": {
    "route": "agent_preparable",
    "classificationConfidence": 95,
    "readinessConfidence": 94,
    "humanDelegable": true,
    "candidateDelegate": "Service owner",
    "handoff": "proposed",
    "scope": "Prepare a local capacity recommendation; do not apply it",
    "permissions": ["read_sources", "write_artifacts", "update_task_status"],
    "acceptance": "Recommendation cites measured demand and failure headroom",
    "stopCondition": "Stop at ready_for_review or when a decision is missing",
    "dependencies": []
  }
}
```

Routes: `agent` (fully executable deliverable), `agent_preparable`,
`human_delegable`, `you`, `verify_first`. Human delegation is also an independent
dimension: keep `humanDelegable`, `candidateDelegate`, and `handoff`
(`none`, `proposed`, `accepted`) even for agent routes. An unaccepted suggestion
does not transfer ownership. Evidence is required before recording acceptance.

States: `identified` → `awaiting_approval` → `ready` → `in_progress` →
`ready_for_review` → `completed`; active work can stop at `needs_input` or
`blocked`. User resume returns waiting work to `ready`. Review changes return
it to `awaiting_approval`. User cancellation ends at `cancelled`.

The CLI runs as an agent. It cannot approve, resume, accept, cancel, or request
review changes on the user's behalf. Only explicit sidebar actions do that.
Approval requires execution readiness **strictly greater than 90**, an agent
route, complete scope/acceptance/stop instructions, and completed dependencies.
Classification confidence is not execution readiness or authorization.
Changing classification/scope invalidates approval. Start rechecks readiness,
dependencies, and the approved scope fingerprint.

Allowed preparation permissions are `read_sources`, `write_artifacts`,
`write_isolated_code`, `run_local_checks`, and `update_task_status`.
These never authorize messages, source-system changes, publishing, Git pushes,
merges, deployment, or configuration application. Approval records permission
but **does not launch or schedule a session**.

Agent command payloads:

- `start`: supply `task.session`, referencing an already authorized session.
- `progress`: supply `note` with material progress.
- `needs_input`: supply `note` containing the exact question.
- `block`: supply `note` naming the dependency and unblocking condition.
- `submit`: supply `note`, `task.deliverable`, and `task.evidence`;
  this stops at `ready_for_review`, never `completed`.

Use HTTPS URLs or project-root-relative paths (for example
`tasks/artifacts/TASK-042/report.md`) for deliverable links. Absolute local paths
also use the existing source-file navigation. Do not use document-relative
paths for register links: the same record can appear in several documents.
Session references without a navigable URL remain visible text.

Keep a preparation deliverable separate from a broader obligation. Accepting
a report does not mean a rollout, decision, or human handoff is complete.
Create separate linked tasks when those outcomes differ. User acceptance
requires an explicit outcome-level evidence note. Completed and cancelled
records are terminal; create a follow-up rather than silently reopening them.
For human or externally finished work, use **Mark outcome complete** with
verified completion evidence; this explicit user action can close any open state.
An agent must never invoke the browser's user commands to manufacture approval.
