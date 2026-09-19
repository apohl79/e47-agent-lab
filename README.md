# E47 Agent Lab

Plugin marketplace for Codex, Claude Code, and Xedoc.

## Plugins

| Plugin | Version | Description | Hosts |
| --- | --- | --- | --- |
| [Reviewers](plugins/reviewers/README.md) | `0.11.0` | Incremental implementation review, PR finalization, and broad reviewer-team workflows. | Xedoc, Codex, Claude Code |
| [Auto Compaction](plugins/auto-compaction/README.md) | `0.1.0` | Claude Code auto-compaction gate with setup skill and checkpoint hooks. | Claude Code |
| [Inline Discussion](plugins/inline-discussion/README.md) | `2.1.1` | Keep document editing, focused AI side threads, and main-agent updates in one view. | Xedoc, Codex, Claude Code |
| [Project Context Curator](plugins/project-context-curator/README.md) | `5.7.2` | Durable project knowledge with audit and graph-assisted retrieval. | Xedoc, Codex, Claude Code |
| [Matrix](plugins/matrix/README.md) | `0.4.0` | Connect approved Xedoc sessions to private Matrix rooms for Element-based bidirectional messaging. | Xedoc |

## Install

Install for Xedoc:

```bash
curl -fsSL https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.sh | bash -s -- --xedoc
```

Install for Claude Code:

```bash
curl -fsSL https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.sh | bash -s -- --claude
```

Install for Codex:

```bash
curl -fsSL https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.sh | bash -s -- --codex
```

From a local clone, `./install.sh` installs for Xedoc. Pass `--xedoc`,
`--claude`, or `--codex` to select one harness, or `--all` to require and
install all three. Claude Code and Codex are never selected implicitly.
`auto-compaction` remains Claude Code only.
