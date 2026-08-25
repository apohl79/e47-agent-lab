# Project Context Schema

`docs/context/context.json` is the canonical text store. Markdown files in the same directory are generated views for humans and agents; `index.md` includes a compact topical index of every record for task-term retrieval. Initialization records whether context is local-only or versioned in Git. `.no-project-context` disables initialization for a repository when the user declines project context.

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
  "schema_version": 2,
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

Applicability describes where a fact is relevant; it does not control where the
canonical record is stored or who may read it. Every collection has a
`default_applicability`. A record may replace that default with its own
`applicability` array.

Selectors are typed objects:

- `project`, `workspace`, `user`, and `machine` require a `selector`.
- `self` resolves while indexing to the owning project, discovered workspace,
  current user, or current machine.
- `universal` has no selector and denotes knowledge that is independent of
  those boundaries.

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
remain unchanged and inherit that default.

## Storage Policy

Use for the user-confirmed decision about whether `docs/context/` should remain local to a checkout or be committed and shared through Git. Non-Git directories default to local context without asking the user for local-vs-versioned storage.

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

- `question`: concise question
- `status`: `open` or `answered`
- `created_at`, `updated_at`: ISO timestamps

Optional fields:

- `context`: where the question came from
- `answer`: confirmed answer once known
- `applicability`: typed applicability selectors overriding the collection default
