---
name: maintain-project-context
description: Maintain durable project domain context while working in a repository. Use when implementing features, researching a project, reviewing code, planning architecture, or encountering unknown abbreviations, domain terms, component names, service names, events, APIs, ownership boundaries, or architecture patterns that should be clarified and stored for future Codex or Claude Code sessions.
---

# Maintain Project Context

## Purpose

Keep durable project knowledge in local repository files instead of conversation history. Detect missing or ambiguous domain context during normal work, ask concise clarification questions when relevant project-specific meaning or ownership is unclear, and proactively update `docs/context/` as soon as stable project insights are confirmed or verified so future fresh sessions can load them.

## Storage Model

Use text files as the canonical store:

- Canonical machine-readable file: `docs/context/context.json`
- Generated human-readable files: `docs/context/index.md`, `glossary.md`, `components.md`, `architecture.md`, and `inbox.md`
- Opt-out marker: `.no-project-context`
- Initialization records a user-confirmed storage policy in `context.json`.
- If the user chooses local context in a Git repository, the updater adds `docs/context/` to the target repo's `.git/info/exclude`.
- If the user chooses versioned context, the updater removes an exact `docs/context/` entry from `.git/info/exclude` if one exists and leaves repository `.gitignore` files unchanged.
- If the target directory is not a Git repository, context is local by default and no local-vs-versioned question is needed.
- If the current repository directory is a Git linked worktree, store and read project context from the primary checkout that owns the shared `.git` directory, not from the linked worktree directory.
- Do not use vector databases or SQLite as canonical storage. They may be added later only as derived indexes.

Use the updater script for writes:

```bash
python3 <skill-dir>/scripts/project_context.py init --repo . --visibility local
python3 <skill-dir>/scripts/project_context.py ignore --repo .
```

Use `python3 <skill-dir>/scripts/project_context.py --help` and
`python3 <skill-dir>/scripts/project_context.py <command> --help` for current command syntax.

Read `references/context-schema.md` before changing the schema or adding new record types.

## Workflow

1. At the start of feature work, research, planning, or review, check whether `docs/context/index.md` exists.
   - In a Git linked worktree, resolve `docs/context/index.md` against the primary checkout path reported by the hook/updater.
2. If it exists, read it and any linked context file relevant to the task.
3. If `.no-project-context` exists at the repository root, do not initialize context and do not ask again.
4. If `docs/context/index.md` does not exist, ask one concise question before executing ordinary feature, research, planning, or review work: whether Project Context Curator should be initialized for this project.
   - If the user says no, run `scripts/project_context.py ignore --repo <repo>` and continue without project context.
   - If the user says yes, bootstrap before responding — even when the enablement decision arrived alongside an unrelated primary task:
     1. Read the repository README, CLAUDE.md/AGENTS.md, the top-level directory layout, and the main manifests/configs (e.g. `package.json`, `pyproject.toml`, `Cargo.toml`, CI config).
     2. From verified findings, prepare `add-component`/`add-term`/`add-pattern` commands. Use only verified project-level facts; source `repo-docs`.
     3. Run `init` and those add commands in the same turn. Do not run `init` alone and move on to the primary task.
     4. Verify: read `docs/context/index.md` after init. If all counts are 0, the bootstrap step was skipped — complete it before responding to the user. If the repository genuinely yields no entries, record an explicit open question instead.
   - If the target is a Git repository, ask whether `docs/context/` should be local or versioned before running init.
   - If the target is not a Git repository, do not ask local vs versioned; run `scripts/project_context.py init --repo <repo>` and it defaults to local.
   - Do not initialize context before the enablement decision. Do not store guessed definitions.
5. During work, watch for context gaps and durable context-worthy facts:
   - Abbreviations or acronyms whose meaning is not already in `glossary.md`
   - Component, service, API, package, event, queue, table, or domain object names whose responsibility is unclear
   - Architecture patterns or boundaries that affect implementation choices
   - Environment mappings, deployment conventions, ownership facts, and operational lookup paths that future agents should reuse
   - Terms used differently from their common industry meaning
   - Conflicting names, aliases, or overloaded words
6. If a relevant project-specific term, abbreviation, component, API, event, ownership boundary, or architecture rule is unclear and not documented in context or repo evidence, ask a concise clarification question before proceeding or storing anything. Prefer one to three concise questions.
7. Store durable insights immediately with the updater script once they are clear from repository evidence, tool results, or user confirmation. Do not wait for a separate "remember this" request.
8. If the user cannot answer yet, add an open question instead of guessing.

## Question Style

Ask only questions that affect durable project understanding.

Good:

```text
What does ACS mean in this repo, and is it project-wide or only this package?
```

```text
Is BillingOrchestrator the owner of invoice state, or only the coordinator for invoice creation?
```

Avoid asking about obvious language/framework concepts, transient implementation choices, or information already documented in `docs/context/`.

## Update Commands

Initialize context files after asking the user whether Project Context Curator should be enabled and after reading the repository README/CLAUDE.md/AGENTS.md, top-level layout, and main manifests. Run `init` together with the `add-component`/`add-term`/`add-pattern` commands derived from that analysis, in the same turn; an init left at all-zero counts means the bootstrap was skipped. For Git repositories, ask the user to choose one visibility mode:

```bash
python3 <skill-dir>/scripts/project_context.py init --repo . --visibility local
python3 <skill-dir>/scripts/project_context.py init --repo . --visibility versioned
```

For non-Git directories, initialize local context without asking for visibility:

```bash
python3 <skill-dir>/scripts/project_context.py init --repo .
```

When the user declines initialization:

```bash
python3 <skill-dir>/scripts/project_context.py ignore --repo .
```

For the current command list and flags, use CLI help:

```bash
python3 <skill-dir>/scripts/project_context.py --help
python3 <skill-dir>/scripts/project_context.py add-term --help
```

Use scan output as hints only. Do not rely on deterministic term detection for decisions; confirm meanings through repo evidence or user clarification before writing them as facts.

## Rules

- Store only durable project knowledge, and keep it local unless the user explicitly asks to commit/share it.
- Mark user-confirmed answers with `--source "user-confirmed"` and repository-verified facts with `--source "repo-docs"`.
- Prefer exact definitions over vague descriptions.
- Include code paths for components whenever known.
- Preserve generated Markdown headers that say the files are generated.
- Do not manually edit generated Markdown unless repairing script output.
- Do not write secrets, credentials, private customer data, or transient debugging details.
- Do not invent definitions. Add an open question when knowledge is missing.
