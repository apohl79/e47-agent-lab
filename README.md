# E47 Agent Lab

Dual-host plugin marketplace for Codex and Claude Code.

## Plugins

| Plugin | Version | Description | Hosts |
| --- | --- | --- | --- |
| [Reviewers](plugins/reviewers/README.md) | `0.8.0` | Incremental implementation review, PR finalization, and broad reviewer-team workflows. | Codex, Claude Code |
| [Auto Compaction](plugins/auto-compaction/README.md) | `0.1.0` | Claude Code auto-compaction gate with setup skill and checkpoint hooks. | Claude Code |
| [Inline Discussion](plugins/inline-discussion/README.md) | `1.12.4` | Keep document editing, focused AI side threads, and main-agent updates in one view. | Codex, Claude Code |
| [Project Context Curator](plugins/project-context-curator/README.md) | `5.4.0` | Durable project knowledge with audit and graph-assisted retrieval. | Codex, Claude Code |

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
