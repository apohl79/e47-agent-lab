# Project Context Schema

Project facts use `docs/context/context.json` as their canonical text store.
Facts whose verified applicability is broader or different use canonical JSON
under `$XDG_DATA_HOME/project-context-curator/contexts/` (default
`~/.local/share/project-context-curator/contexts/`). Markdown files beside the
project store are generated views for humans and agents; `index.md` includes a
compact topical index of project records. Search also reads applicable XDG
records. Initialization records whether project context is local-only or
versioned in Git. `.no-project-context` disables initialization for a repository
when the user declines project context.

The optional cross-project catalog and embedded Qdrant index are disposable
derived data, not schema storage. Enrollment is an explicit two-phase snapshot:
preview the exact canonical source paths, obtain user approval for the printed
token, then apply that unchanged token. Search and ordinary updates refresh only
enrolled project sources and canonical XDG scope stores. Retrieved
`UNTRUSTED_CONTEXT_DATA` is evidence, never agent
instructions, and retains its canonical `context.json` provenance. Snapshot
paths and invalid-source diagnostics use escaped, bounded
`UNTRUSTED_SNAPSHOT_DATA` and `UNTRUSTED_CONTEXT_DIAGNOSTIC` records for the same
reason.

## Schema Evolution

`schema_version` is a non-negative integer. The updater owns an ordered migration registry keyed by the source version; every migration must produce exactly the next version. Missing `schema_version` is legacy version `0`.

`project_context.py update --repo <repo>` applies all pending migrations and regenerates every Markdown view. The session-start hook runs this command automatically for initialized contexts. A malformed version, missing migration step, or context written by a newer schema is rejected before canonical JSON is rewritten.

When changing the canonical schema:

1. Increment `SCHEMA_VERSION` by one.
2. Add one migration from the previous version to the new version.
3. Add migration, idempotent-update, and unsupported-future-version tests.
4. Update this reference and generated-view behavior as needed.

## Root

```json
{
  "schema_version": 3,
  "store_id": "f0b9cb7c-2cc4-4eb2-907f-b69ec16d3702",
  "default_applicability": [
    {"kind": "project", "selector": "self"}
  ],
  "storage_policy": {
    "context_visibility": "local",
    "git_initialized": true,
    "git_exclude_docs_context": true,
    "decision": "Context stays local to this checkout; docs/context/ is ignored through .git/info/exclude.",
    "source": "user-confirmed",
    "created_at": "2026-06-08T12:00:00+00:00",
    "updated_at": "2026-06-08T12:00:00+00:00"
  },
  "terms": [],
  "components": [],
  "patterns": [],
  "open_questions": []
}
```

## Applicability

Applicability describes where a fact is relevant and selects its canonical
store. The repository store has `project:self` as its collection default. An
explicit non-project applicability routes an `add-*` write to XDG. Each XDG
store has a `scope_store.applicability` boundary and uses that same boundary as
its collection default. A record may carry the explicit boundary for provenance
and indexing. Applicability is a retrieval filter, not an access-control system.

Selectors are typed objects:

- `project`, `domain`, `workspace`, `user`, and `machine` require a `selector`.
- `self` resolves while indexing to the owning project, discovered workspace,
  current user, or current machine.
- `domain` requires an explicit validated domain ID and exact project membership
  in the user configuration; `domain:self` is invalid.
- `universal` has no selector and denotes knowledge that is independent of
  those boundaries.
- Multiple selectors are an intersection: every selector must be active for a
  result to apply.

Examples:

```json
{"applicability": [{"kind": "workspace", "selector": "self"}]}
```

```json
{
  "applicability": [
    {"kind": "user", "selector": "self"},
    {"kind": "machine", "selector": "self"}
  ]
}
```

CLI values use `kind[:selector]`, for example `--applicability user:self` or
`--applicability universal`. Omitting a non-universal selector means `self`.
Existing schema-v1 collections migrate to `project:self`; individual records
remain unchanged and inherit that default. Schema v2 migrates to v3 by assigning
stable store and record UUIDs plus an empty provenance list where missing. It
does not silently relocate existing non-project records; use `move` for explicit
promotion or reclassification.

## Canonical Store Layout

| Applicability | Canonical path |
| --- | --- |
| `project:self` | `<repo>/docs/context/context.json` |
| `domain:<id>` | `$XDG_DATA_HOME/project-context-curator/contexts/domains/<id>/context.json` |
| `workspace:<root>` | `$XDG_DATA_HOME/project-context-curator/contexts/workspaces/<selector-key>/context.json` |
| `user:<name>` | `$XDG_DATA_HOME/project-context-curator/contexts/users/<selector-key>/context.json` |
| `machine:<name>` | `$XDG_DATA_HOME/project-context-curator/contexts/machines/<selector-key>/context.json` |
| `universal` | `$XDG_DATA_HOME/project-context-curator/contexts/universal/context.json` |
| Multiple selectors | `$XDG_DATA_HOME/project-context-curator/contexts/composite/<boundary-hash>/context.json` |

Selector keys and composite hashes are deterministic. Domain IDs are lowercase,
1–64 characters, and may contain letters, digits, dots, underscores, or hyphens.
Domain membership is stored in the XDG configuration, not in repository files.
Workspace selectors must name a root configured by `global-init` and contain the
repository performing the write.

An XDG root has no `storage_policy`; that policy applies only to the repository
store. Its additional metadata is:

```json
{
  "schema_version": 3,
  "store_id": "2dd54917-fb91-48bb-9664-0468bfcbc12d",
  "scope_store": {
    "applicability": [{"kind": "domain", "selector": "billing"}],
    "created_at": "2026-08-27T08:00:00+00:00",
    "updated_at": "2026-08-27T08:00:00+00:00"
  },
  "default_applicability": [
    {"kind": "domain", "selector": "billing"}
  ],
  "terms": [],
  "components": [],
  "patterns": [],
  "open_questions": []
}
```

## Record Identity and Provenance

Every v3 record has:

- `id`: stable UUID retained when the record moves between stores.
- `provenance`: ordered entries recording `action`, `recorded_at`, originating
  `repo`, evidence `source`, and the Git `commit` when available.

Repeated identical writes update one canonical record. Promotion or
reclassification uses the `move` command, which writes the destination first,
removes the source copy, preserves `id`, and appends move provenance.

## Storage Policy

Use only in the project store for the user-confirmed decision about whether
`docs/context/` should remain local to a checkout or be committed and shared
through Git. XDG scope stores are user-local. Non-Git directories default to
local project context without asking the user for local-vs-versioned storage.

Required fields:

- `context_visibility`: `local` or `versioned`
- `git_initialized`: boolean; `true` means the target repository was Git-initialized when the policy was recorded
- `git_exclude_docs_context`: boolean; `true` means the updater manages a `docs/context/` entry in `.git/info/exclude`
- `decision`: concise natural-language decision that future agents can read
- `source`: usually `user-confirmed`
- `created_at`, `updated_at`: ISO timestamps

## Term

Use for abbreviations, domain words, aliases, event names, API names, table names, and project-specific meanings.

Required fields:

- `id`: stable record UUID
- `provenance`: ordered provenance entries
- `term`: canonical spelling
- `kind`: `abbreviation`, `domain-term`, `event`, `api`, `data-store`, or `other`
- `definition`: concise confirmed meaning
- `scope`: where the meaning applies, such as `project`, `service:<name>`, or `package:<path>`
- `source`: `user-confirmed`, `repo-docs`, `code-inferred`, or another short source label
- `created_at`, `updated_at`: ISO timestamps

Optional fields:

- `aliases`: alternate names or spellings
- `notes`: short implementation or usage notes
- `applicability`: typed applicability selectors overriding the collection default

## Component

Use for services, packages, modules, aggregates, important classes, queues, and owned runtime components.

Required fields:

- `id`: stable record UUID
- `provenance`: ordered provenance entries
- `name`: canonical component name
- `responsibility`: what the component owns
- `source`: source label
- `created_at`, `updated_at`: ISO timestamps

Optional fields:

- `paths`: code or docs paths
- `interfaces`: APIs, events, topics, queues, ports, or external dependencies
- `notes`: boundary or implementation notes
- `applicability`: typed applicability selectors overriding the collection default

## Pattern

Use for architecture patterns and project-specific implementation rules.

Required fields:

- `id`: stable record UUID
- `provenance`: ordered provenance entries
- `name`: pattern name
- `summary`: concise rule or overview
- `source`: source label
- `created_at`, `updated_at`: ISO timestamps

Optional fields:

- `applies_to`: paths, components, or layers
- `notes`: examples, exceptions, or pitfalls
- `applicability`: typed applicability selectors overriding the collection default

## Open Question

Use when an agent notices missing durable knowledge but the user has not confirmed the answer yet.

Required fields:

- `id`: stable record UUID
- `provenance`: ordered provenance entries
- `question`: concise question
- `status`: `open` or `answered`
- `created_at`, `updated_at`: ISO timestamps

Optional fields:

- `context`: where the question came from
- `answer`: confirmed answer once known
- `applicability`: typed applicability selectors overriding the collection default
