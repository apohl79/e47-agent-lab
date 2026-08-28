# Project Context Schema

By default, project facts use `docs/context/context.json` as their canonical text
store. Broader or different applicability uses canonical JSON under
`$XDG_DATA_HOME/project-context-curator/contexts/` (default
`~/.local/share/project-context-curator/contexts/`). An optional configured Git
store becomes exclusive canonical storage for opted-in project, domain, and
universal facts. User, machine, and composites containing either remain private
in XDG.

Project Markdown files are generated views for humans and agents; `index.md`
includes a compact topical index. In Git-store mode, the project checkout has no
writable `context.json` mirror. `.no-project-context` disables initialization
when the user declines project context.

The optional cross-project catalog and embedded Qdrant index are disposable
derived data, not schema storage. Enrollment is an explicit two-phase snapshot:
preview the exact canonical source paths, obtain user approval for the printed
token, then apply that unchanged token. Search and ordinary updates refresh only
enrolled project sources and canonical Git/XDG scope stores. Retrieved
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
explicit non-project applicability routes an `add-*` write to its configured
Git or private XDG store. Each scope store has a
`scope_store.applicability` boundary and uses that same boundary as its
collection default. A record may carry the explicit boundary for provenance and
indexing. Applicability is a retrieval filter, not an access-control system.

Selectors are typed objects:

- `project`, `domain`, `user`, and `machine` require a `selector`.
- `self` resolves while indexing to the owning project, current user, or current
  machine.
- `domain` requires an explicit validated domain ID and exact project membership
  in the user configuration; `domain:self` is invalid.
- `universal` has no selector and denotes knowledge that is independent of
  those boundaries.
- Multiple selectors are an intersection: every selector must be active for a
  result to apply.
- `workspace` is accepted only while reading legacy data. New writes reject it;
  use `move` to reclassify legacy records before Git-store migration.

Examples:

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

| Applicability | Default mode | Configured Git-store mode |
| --- | --- | --- |
| `project:self` | `<repo>/docs/context/context.json` | `<git-store>/projects/<store-id>/context.json` |
| `domain:<id>` | `<xdg>/contexts/domains/<id>/context.json` | `<git-store>/scopes/domains/<id>/context.json` |
| `user:<name>` | `<xdg>/contexts/users/<selector-key>/context.json` | Same private XDG path |
| `machine:<name>` | `<xdg>/contexts/machines/<selector-key>/context.json` | Same private XDG path |
| `universal` | `<xdg>/contexts/universal/context.json` | `<git-store>/scopes/universal/context.json` |
| Shareable intersection | `<xdg>/contexts/composite/<boundary-hash>/context.json` | `<git-store>/scopes/composite/<boundary-hash>/context.json` |
| Intersection containing user/machine | `<xdg>/contexts/composite/<boundary-hash>/context.json` | Same private XDG path |

Selector keys and composite hashes are deterministic. Domain IDs are lowercase,
1–64 characters, and may contain letters, digits, dots, underscores, or hyphens.
Without Git-store mode, domain membership is stored in XDG configuration. A Git
store persists membership as stable project store IDs in its manifest; absolute
checkout bindings remain local in XDG configuration.

A scope root has no `storage_policy`; that policy applies only to a project
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

## Canonical Git Store

`project-context-store.json` is the portable catalog. It contains no absolute
checkout paths:

```json
{
  "schema_version": 1,
  "store_id": "d83e4a64-1ca7-4e4f-a449-ffb698b066c0",
  "projects": {
    "f0b9cb7c-2cc4-4eb2-907f-b69ec16d3702": {
      "name": "billing-api",
      "created_at": "2026-08-28T08:00:00+00:00",
      "updated_at": "2026-08-28T08:00:00+00:00"
    }
  },
  "domains": {
    "billing": ["f0b9cb7c-2cc4-4eb2-907f-b69ec16d3702"]
  },
  "created_at": "2026-08-28T08:00:00+00:00",
  "updated_at": "2026-08-28T08:00:00+00:00"
}
```

XDG configuration records the store checkout path, store identity, and absolute
checkout-to-project-ID bindings. `git-store-init` changes that configuration
only after an exact snapshot approval. Its token covers source and destination
paths and content hashes plus the current manifest. It validates all conflicts,
writes canonical destinations and the manifest, writes local configuration,
then removes relocated sources. User/machine stores are excluded. Existing
workspace records block the operation until explicit reclassification.

On another machine, configure the cloned store through the same preview/approval
flow, list IDs with `git-store-status`, and attach each checkout with
`git-store-bind --project-store-id <uuid>`. Binding regenerates local Markdown
views and restores domain membership. No updater command commits or pushes Git.

## Record Identity and Provenance

Every v3 record has:

- `id`: stable UUID retained when the record moves between stores.
- `provenance`: ordered entries recording `action`, `recorded_at`, originating
  `repo`, evidence `source`, and the Git `commit` when available.

Repeated identical writes update one canonical record. Promotion or
reclassification uses the `move` command, which writes the destination first,
removes the source copy, preserves `id`, and appends move provenance.

## Storage Policy

Use only in a project store. It records whether `docs/context/` remains local,
is versioned in the project repository, or whether canonical JSON lives in the
separate configured Git store. XDG user/machine scope stores are private.
Non-Git directories default to local project context unless Git-store mode is
already configured.

Required fields:

- `context_visibility`: `local`, `versioned`, or `git-store`
- `git_initialized`: boolean; `true` means the target repository was Git-initialized when the policy was recorded
- `git_exclude_docs_context`: boolean; `true` means the updater manages a `docs/context/` entry in `.git/info/exclude` (`local` and `git-store` modes)
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
