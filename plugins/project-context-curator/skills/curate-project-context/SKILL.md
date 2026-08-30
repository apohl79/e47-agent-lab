---
name: curate-project-context
description: Review and clean up Project Context Curator stores by running the read-only audit, triaging stale, time-bound, duplicated, divergent, dead-path, and oversized records, and applying user-confirmed fixes. Use when session start reports context audit findings, when context feels noisy or outdated, or before promoting project facts to a domain.
---

# Curate Project Context

The updater finds hygiene problems deterministically; the agent verifies each
finding against the repository, proposes one fix per finding, and writes only
after the user confirms. Never delete or move a record without confirmation.

## Workflow

1. Locate the sibling updater at
   `<plugin-root>/skills/maintain-project-context/scripts/project_context.py`.
2. Run the audit without changing state and keep the counts for the final report:

   ```bash
   python3 <updater> audit --repo <repo>
   python3 <updater> audit --repo <repo> --format json   # for many findings
   ```

3. Triage every finding by check. Verify against repository evidence before
   proposing anything; do not guess whether a fact is still true.

   | Check | Meaning | Proposed fix |
   | --- | --- | --- |
   | `burst` | Many records written on one day | Re-apply the admission gate to the batch; propose removing implementation detail recoverable from code, tests, or docs |
   | `time-bound` | Wording tied to a moment ("temporary", "not yet", "as of 2026-…") | Rewrite as a durable invariant with `add-<kind>` (same name updates in place) or remove |
   | `aged` | Not confirmed for longer than `--stale-days` | Verify in the repo; re-add with `--source repo-docs` to refresh, or remove |
   | `stale-question` | Open question older than `--question-days` | Answer it with the user and store the answer, or remove |
   | `shadowed` | Project record also exists in a domain or universal store | Keep one copy: remove the project record, or correct the shared record if the project one is right |
   | `divergent` | Same term or component defined differently across domain members | Agree on one definition with the user, `move` one record to `domain:<id>`, remove the member copies |
   | `dead-path` | Component path missing in the repository | Update `paths` via `add-component` with the same name, or remove the component |
   | `oversized` | Store or index view too large to read per session | Consolidate overlapping patterns; shorten summaries |

4. Present the proposals as one batch grouped by check, each with the exact
   command, and ask for confirmation through the host input UI when available.
   Skip findings the user rejects; do not argue them.
5. Apply confirmed fixes with the updater only:

   ```bash
   python3 <updater> remove --repo <repo> --type <term|component|pattern|question> --value "<name>"
   python3 <updater> move --repo <repo> --type <kind> --value "<name>" --applicability domain:<id>
   python3 <updater> add-<kind> --repo <repo> ... --source repo-docs
   python3 <updater> update --repo <repo>
   ```

   Search before re-adding so a rewrite does not create a second record.
6. Re-run `audit` and report findings before and after, plus anything left for
   the user to decide.

## Rules

- Read-only until the user confirms; `audit` never writes.
- Prefer fixing the wording of a record over deleting knowledge the user still relies on.
- A `divergent` finding is resolved at domain scope, not by picking one member silently.
- Do not add new records during curation unless they replace a removed one.
- Do not edit generated Markdown or `context.json` by hand; use updater commands so provenance stays intact.
