# E47 Agent Lab

Plugin marketplace for Codex, Claude Code, and Xedoc.

## Plugins

| Plugin | Version | Description | Hosts |
| --- | --- | --- | --- |
| [Reviewers](plugins/reviewers/README.md) | `0.8.0` | Incremental implementation review, PR finalization, and broad reviewer-team workflows. | Codex, Claude Code |
| [Auto Compaction](plugins/auto-compaction/README.md) | `0.1.0` | Claude Code auto-compaction gate with setup skill and checkpoint hooks. | Claude Code |
| [Inline Discussion](plugins/inline-discussion/README.md) | `2.1.0` | Keep document editing, focused AI side threads, and main-agent updates in one view. | Codex, Claude Code, Xedoc |
| [Project Context Curator](plugins/project-context-curator/README.md) | `5.5.1` | Durable project knowledge with audit and graph-assisted retrieval. | Codex, Claude Code |

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
Claude Code, Codex, Xedoc, or any combination. `auto-compaction` is Claude
Code only. Use `./install.sh --claude`, `./install.sh --codex`, or
`./install.sh --xedoc` to target one host.
