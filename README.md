# E47 Agent Lab

Dual-host plugin marketplace for Codex and Claude Code.

## Plugins

| Plugin | Version | Description | Source | Hosts |
| --- | --- | --- | --- | --- |
| `reviewers` | `0.7.0` | PR finalization, reviewer-team, and Slack review-request workflows. | `plugins/reviewers` | Codex, Claude Code |
| `auto-compaction` | `0.1.0` | Claude Code auto-compaction gate with setup skill and checkpoint hooks. | `plugins/auto-compaction` | Claude Code |
| `inline-discussion` | `1.7.7` | Browser UI for markdown docs with threaded AI conversations. Requires the `inline-discussion` CLI on PATH (installed by `./install.sh`). | `plugins/inline-discussion` | Codex, Claude Code |
| `project-context-curator` | `0.2.1` | Durable repository domain context for fresh agent sessions. | `plugins/project-context-curator` | Codex, Claude Code |

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
