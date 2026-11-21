# E47 Agent Lab

Plugin marketplace for Codex, Claude Code, and Xedoc.

## Plugins

| Plugin | Version | Description | Hosts |
| --- | --- | --- | --- |
| [Reviewers](plugins/reviewers/README.md) | `0.11.0` | Incremental implementation review, PR finalization, and broad reviewer-team workflows. | Xedoc, Codex, Claude Code |
| [Auto Compaction](plugins/auto-compaction/README.md) | `0.1.0` | Claude Code auto-compaction gate with setup skill and checkpoint hooks. | Claude Code |
| [Inline Discussion](plugins/inline-discussion/README.md) | `2.9.0` | Keep document editing, focused AI side threads, and main-agent updates in one view. | Xedoc, Codex, Claude Code |
| [Project Context Curator](plugins/project-context-curator/README.md) | `6.0.1` | Portable project and domain knowledge in main or dedicated Git stores. | Xedoc, Codex, Claude Code |
| [Matrix](plugins/matrix/README.md) | `0.13.4` | Connect Xedoc sessions and an always-on host chat to private Matrix rooms. | Xedoc |

## Install

### Windows (PowerShell)

Install [Git for Windows](https://git-scm.com/download/win) (including Git Bash)
and the target host CLI first, then reopen PowerShell. Install for Xedoc:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.ps1))) -Xedoc
```

Use `-Claude`, `-Codex`, or `-All` instead to select other hosts. From a local
clone, run `.\install.ps1 -Xedoc`; add `-Action uninstall` to uninstall,
`-Source PATH` to select a checkout, or `-WithContextRuntime` to provision the
optional context runtime. No administrator or symlink privileges are needed.
Standalone tools install into `~/bin` if it exists, otherwise `~/.local/bin`;
add that directory to your Windows user `PATH` to use their `.cmd` launchers.
This installs the marketplace; individual plugins and optional services may
have additional platform requirements.

### macOS / Linux / WSL (Bash)

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
