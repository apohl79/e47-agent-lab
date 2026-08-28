# E47 Agent Lab

Dual-host plugin marketplace for Codex and Claude Code.

## Plugins

| Plugin | Version | Description | Source | Hosts |
| --- | --- | --- | --- | --- |
| `reviewers` | `0.7.1` | PR finalization, reviewer-team, and Slack review-request workflows. | `plugins/reviewers` | Codex, Claude Code |
| `auto-compaction` | `0.1.0` | Claude Code auto-compaction gate with setup skill and checkpoint hooks. | `plugins/auto-compaction` | Claude Code |
| `inline-discussion` | `1.7.7` | Browser UI for markdown docs with threaded AI conversations. Requires the `inline-discussion` CLI on PATH (installed by `./install.sh`). | `plugins/inline-discussion` | Codex, Claude Code |
| `project-context-curator` | `3.0.0` | Canonical project, private XDG, and optional Git-backed context with graph-assisted hybrid retrieval. | `plugins/project-context-curator` | Codex, Claude Code |

## Install

One-line installer:

```bash
curl -fsSL https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.sh | bash
```

From a local clone:

```bash
./install.sh
```

The installer detects available CLIs and installs host-appropriate plugins into
Claude Code, Codex, or both. `auto-compaction` is Claude Code only. Use
`./install.sh --claude` or `./install.sh --codex` to target one host.

Cross-project semantic retrieval is optional. It uses Qdrant's embedded local
mode, so it does not install or run a Qdrant server. When global context is not
configured, the session hook proactively directs the agent to begin onboarding.
The agent performs a read-only recursive preview, includes primary Git checkouts
that still need context, requests approval for the exact snapshot and storage
policy, bootstraps approved repositories with verified context, and provisions
pinned Python packages and models through the deterministic `global-init`
command.

The disposable catalog derives a typed, evidence-backed relationship graph from
canonical context. Search considers the current repository, repositories named
in the query, direct relationships, and high-confidence relationships within two
graph hops. Explicit workspace-wide queries consider every enrolled repository,
while strongly matched otherwise unrelated records are eligible individually.
Applicability remains the truth scope: non-project selectors such as domain,
user, machine, and universal must still all be active. Ranking
combines hybrid relevance, graph distance and confidence, and per-project quotas.

Shareable context can optionally use one dedicated Git checkout as its canonical
store. `git-store-init` previews and hashes the exact project/domain/universal
migration before approval; user/machine context stays private in XDG. Project
checkouts keep generated views, and the updater never commits or pushes the
store. Stable IDs plus `git-store-bind` restore a cloned store on another
machine. See the Project Context Curator skill for the complete workflow.

`./install.sh --with-context-runtime` remains a deterministic convenience for
provisioning the same runtime directly.
For the one-line path, pass the same opt-in explicitly:
`curl -fsSL https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.sh | bash -s -- --with-context-runtime`.
Existing global-index users are prompted when an interactive installer detects
runtime drift; non-interactive installs print the same explicit upgrade path.
Host-managed marketplace updates are detected again by the plugin at session
start because those update paths do not execute this installer. Codex then asks
before running the deterministic `global-upgrade`; session hooks never install
or upgrade dependencies on their own.

The installer also symlinks standalone CLI tools shipped under `tools/` into the
user bin dir (`~/bin` if it exists, otherwise `~/.local/bin`). Currently:

- `inline-discussion` → `tools/inline-discussion/bin/inline-discussion`
  (powers the `/inline-discussion:discuss` skill at runtime — the plugin
  expects this CLI on `PATH`).

The launcher needs the source tree on disk. When `./install.sh` is invoked from
a local checkout, it links straight into that checkout. When run via the curl
one-liner, it clones the marketplace into `~/.local/share/e47-agent-lab` and
links from there. It uses `git clone https://github.com/<slug>.git`, or
`gh repo clone` when `gh` is already available.
`./install.sh uninstall` removes the installed symlinks and that managed
checkout. If neither `gh` nor `git` can clone the repository (e.g. a private
repo without credentials), CLI tool install is skipped with a warning and
plugin install still proceeds.

## Host Manifests

- Codex marketplace: `.agents/plugins/marketplace.json`
- Claude Code marketplace: `.claude-plugin/marketplace.json`

## Versioning

Canonical marketplace and plugin versions live in `plugin-versions.json`. Do not
hand-edit version fields in host manifests; use the helper so Codex and Claude
plugin manifests stay aligned.

```bash
./scripts/plugin-versioning.py list
./scripts/plugin-versioning.py check
./scripts/plugin-versioning.py sync
./scripts/plugin-versioning.py bump reviewers patch
./scripts/plugin-versioning.py set inline-discussion 0.2.0
```

`check` verifies `plugin-versions.json`, `.claude-plugin/marketplace.json`, and
all plugin host manifests. Local `./install.sh` runs the same check before
installing from a checkout.

Manual Codex CLI install:

```bash
codex plugin marketplace add apohl79/e47-agent-lab
codex plugin add reviewers@e47
codex plugin add inline-discussion@e47
codex plugin add project-context-curator@e47
```

Manual Claude Code CLI install:

```bash
claude plugin marketplace add apohl79/e47-agent-lab --scope user
claude plugin install reviewers@e47
claude plugin install auto-compaction@e47
claude plugin install inline-discussion@e47
claude plugin install project-context-curator@e47
```
