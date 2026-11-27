---
name: writing
description: "Write inline-discussion documents with local source links, Mermaid diagrams, and persistent task-register metadata for Tasks mode. Use when writing content that will be opened in the inline-discussion browser."
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

## Persistent tasks and Tasks mode

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
Markdown. Do not write executable HTML, duplicate Tasks mode as a Markdown
table, or encode lifecycle state in checkbox syntax. The checkbox represents
completion only. Tracked checkboxes cannot bypass final review.

The renderer associates each task item with `data-task-id` and a
`#task-TASK-042` navigation anchor. The browser builds its Tasks table from the
referenced register and shows only tasks referenced by the open document.
The table, expanded task details, and inline badges show **current** state, including when viewing
an older document; stored Markdown remains a historical snapshot unless an
authorized workflow explicitly refreshes the current document. Ordinary
Markdown viewers show the saved snapshot without the task controls.

The toolbar switches between Document and Tasks at every panel width.
Clicking a tracked checkbox expands that task's row in the table; it does not toggle
completion or write Markdown. Ordinary checkboxes retain their normal behavior.
The table shows state, a single automation-first classification, both
confidence scores, and owner. Click a task's table title to expand or collapse
its details directly below the item. Only one task expands at a time; drafts
survive collapse and switching tasks. Details include scope, evidence, history,
and source/session/deliverable links. The title inside the expanded details
returns to the document anchor.

The Category column and category filter come from the document's section
headings, independently of status and classification. Use normal `##` headings
such as “Urgent next steps for today” and “Tasks for today”; subsection paths
are preserved, e.g. “Next actions by initiative / SAP OEM Voice”. The document
title is excluded; tasks before any section are “Uncategorized”. Quoted or
task-internal headings do not change categories. Category and status filters
combine. Live document updates refresh categories; a removed selected category
resets to all categories. Clicking a tracked checkbox clears filters to reveal
its task. Categories are document-specific and do not mutate the register or
carry urgency forward independently of the new day's headings.

The state dropdown offers only available user actions. Selecting an action
does not apply it: use the displayed button. Human notes, feedback, and reasons
are optional. Preparation approval has a separate scope confirmation and
notifies the owning main agent, which may dispatch scoped preparation.

Each collapsed table row also has a State dropdown. Choose an available
transition to reveal a compact evidence field and action button in that cell;
the task does not need to expand. Approval opens the same scope confirmation.
Quick actions and expanded controls share validation and notes. Opening one
closes the other for that task, retaining its draft. Cancel a quick action to
reset it; if the register changed, cancel and reselect rather than retrying a
stale action. Completed and cancelled rows have read-only status dropdowns.

The Classification dropdown also works in collapsed rows: choose a route,
optionally enter a reason, and explicitly save or discard it. It does not launch work.
“Edit classification” changes route and both
confidence scores with an optional reason. Saving invalidates prior approval.
It does not transfer ownership or accept a handoff. Active work and outcomes
under review cannot be reclassified; return review work for changes first.
Completed and cancelled tasks are read-only. Live updates retain drafts;
stale saves are rejected. Discard classification edits to load current values.

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
    "scope": "Prepare a local capacity recommendation; do not apply it",
    "permissions": ["read_sources", "write_artifacts", "update_task_status"],
    "acceptance": "Recommendation cites measured demand and failure headroom",
    "stopCondition": "Stop at ready_for_review or when a decision is missing",
    "dependencies": []
  }
}
```

Choose one automation-first route: `agent` (can finish the scoped deliverable
without further user input), `agent_preparable` (can prepare useful work before
a human decision), `human_delegable` (another person must do the work; no
feasible agent deliverable), `you` (requires the user's personal action), or
`verify_first` (insufficient evidence to decide). Prefer an agent route whenever
feasible; the fact that a person could also do the work is not a classification.
Do not write `humanDelegable`; that obsolete field in older registers is ignored.
For `human_delegable`, record `candidateDelegate` and `handoff`
(`none`, `proposed`, `accepted`) when known. An unaccepted suggestion does not
transfer ownership. Evidence is required before recording acceptance.
If an agent can prepare only part of a human obligation, define that bounded
deliverable separately and preserve the parent obligation. Agent-executable
does not bypass scope approval or final user review.

States: `identified` → `awaiting_approval` → `ready` → `in_progress` →
`ready_for_review` → `completed`; active work can stop at `needs_input` or
`blocked`. User resume returns waiting work to `ready`. Review changes return
it to `awaiting_approval`. User cancellation ends at `cancelled`.

The CLI runs as an agent. It cannot approve, resume, accept, cancel, or request
review changes on the user's behalf. Only explicit user actions in Tasks mode do that.
Readiness scores are advisory; explicit human approval overrides the score.
Approval requires an agent route, complete scope/acceptance/stop instructions,
preparation permissions, and completed dependencies.
Classification confidence is not execution readiness or authorization.
Changing classification/scope invalidates approval. Start rechecks scope,
dependencies, and the approved scope fingerprint.

Both collapsed rows and expanded controls offer **Ready with note** alongside
**Approve preparation**. The optional note is shown in the scope confirmation,
persisted as `approvalNote`, displayed in task details and history, and covered
by the approval fingerprint. Blank notes are valid. Approval and resume save the
current note; reclassification or requested changes clear it with approval.
The owning main agent must reread it and include it verbatim in the worker
handoff. The worker uses it as user context/instructions only within approved
scope and permissions. It cannot authorize external messages or mutations;
prepare a requested follow-up for review instead. Missing or conflicting scope
still requires user input. Legacy approvals without notes remain valid.

Changing a personal or human task to an agent route does not itself grant
permissions. When permissions are empty, the approval confirmation proposes
a preparation-only scope: read existing sources, write a local report/draft,
and update task status. It retains the original obligation/outcome as context,
not execution authority or evidence of completion. Only confirmation saves
that displayed scope and approval atomically; cancellation changes neither.
Tasks with existing permissions keep their original scope. Dependencies and
stale revisions remain enforced. Action failures appear next to the task
controls and retain the user's note for retry. Dispatch notifications also
appear in the task row, including queued approval without an owning main
session or a failed notification; saved approval alone is not a worker start.

Allowed preparation permissions are `read_sources`, `write_artifacts`,
`write_isolated_code`, `run_local_checks`, and `update_task_status`.
These never authorize messages, source-system changes, publishing, Git pushes,
merges, deployment, or configuration application.

### Approval dispatch

Approval and user resume persist `ready`, then notify the owning main session
through the existing host bridge. The confirmation explains that this may start
scoped preparation through the repository's dispatch workflow. The plugin never
creates a worker itself. Without an owning session or after a notification
failure, approval remains saved; the response reports pending delivery. Do not
reapprove or assume a worker started. Read the register before retrying.

The main-session event is JSON with `type: "inline-discussion.task-ready"`,
`projectRoot`, `registerPath`, `documentPath`, `taskId`, `taskRevision` and
`approval`. It is a hint to reread the register, not an executable scope.
Use the repo's dispatch skill (for example `briefing-dispatch`); if none exists,
leave the task queued. Do not silently launch the rest of the ready backlog.

Claim before creating an idle worker, then bind its actual session before
sending any work. Both commands use the latest register `expectedRevision`:

```json
{"action":"claim","expectedRevision":12,"id":"TASK-042","expectedTaskRevision":4,"claimId":"unique-dispatch-uuid"}
```

```json
{"action":"bind","expectedRevision":13,"id":"TASK-042","claimId":"unique-dispatch-uuid","task":{"session":"actual-worker-thread-id"}}
```

`claim` atomically checks the task revision, current approval, readiness,
dependencies and absence of a previous claim. It stores `dispatch.id` and
`dispatch.approval` without changing `ready`. `bind` requires that valid claim
and refuses another session when one is already recorded. Reuse the recorded
session after user resume/review; check it is idle before sending.

Pass the claim ID to the worker. Its `start` command must include top-level
`claimId` and `task.session` equal to the bound real session. Existing explicitly
authorized manual starts without a dispatch claim remain supported.
Reclassification or user resume invalidates the prior claim; stale workers
cannot start under it. Never release a claim by timeout or retry creation/send
after an ambiguous failure. Report the durable claim/session for reconciliation;
this favors a visible stalled dispatch over duplicate execution.

Workers still stop at input or final review. Only the user accepts outcomes.
No dispatcher may approve tasks or enlarge the approved scope.

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
does not require an evidence note. Completed and cancelled
records are terminal; create a follow-up rather than silently reopening them.
For human or externally finished work, use **Mark outcome complete** with
optional completion evidence; this explicit user action can close any open state.
An agent must never invoke the browser's user commands to manufacture approval.
