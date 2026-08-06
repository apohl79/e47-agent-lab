#!/usr/bin/env python3
"""Codex hooks for project-context-curator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any


LOG_PATH = Path.home() / ".codex" / "logs" / "project-context-curator-hooks.log"
CONTEXT_FILE = Path("docs/context/context.json")
CONTEXT_INDEX = Path("docs/context/index.md")
IGNORE_MARKER = Path(".no-project-context")


def read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def emit(event_name: str, additional_context: str) -> None:
    if not additional_context.strip():
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": additional_context.strip(),
                }
            }
        )
    )


def plugin_root() -> Path:
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parent.parent


def updater_script() -> Path:
    return plugin_root() / "skills" / "maintain-project-context" / "scripts" / "project_context.py"


def cwd_from_payload(payload: dict[str, Any]) -> Path:
    raw = payload.get("cwd") or os.getcwd()
    return Path(str(raw)).expanduser().resolve()


def git_output(cwd: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = proc.stdout.strip()
    return output if proc.returncode == 0 and output else None


def git_path(cwd: Path, option: str) -> Path | None:
    raw = git_output(cwd, "rev-parse", option)
    if raw is None:
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def worktree_root(cwd: Path) -> Path:
    root = git_output(cwd, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else cwd


def main_worktree_root(worktree: Path) -> Path:
    git_dir = git_path(worktree, "--git-dir")
    common_dir = git_path(worktree, "--git-common-dir")
    if git_dir is None or common_dir is None or git_dir == common_dir:
        return worktree

    if common_dir.name != ".git":
        return worktree

    candidate = common_dir.parent.resolve()
    if candidate == worktree or not candidate.exists():
        return worktree

    candidate_common_dir = git_path(candidate, "--git-common-dir")
    if candidate_common_dir == common_dir:
        return candidate

    return worktree


def repo_root(cwd: Path) -> Path:
    return main_worktree_root(worktree_root(cwd))


def is_git_initialized(repo: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def context_ignored(repo: Path) -> bool:
    return (repo / IGNORE_MARKER).exists()


def load_context(repo: Path) -> dict[str, Any]:
    path = repo / CONTEXT_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def context_counts(data: dict[str, Any]) -> str:
    if not data:
        return "No docs/context/context.json exists yet."
    open_questions = [
        q for q in data.get("open_questions", [])
        if isinstance(q, dict) and q.get("status", "open") == "open"
    ]
    return (
        f"Existing context counts: {len(data.get('terms', []))} terms, "
        f"{len(data.get('components', []))} components, "
        f"{len(data.get('patterns', []))} patterns, "
        f"{len(open_questions)} open questions."
    )


def session_start(payload: dict[str, Any]) -> None:
    cwd = cwd_from_payload(payload)
    current_worktree = worktree_root(cwd)
    repo = main_worktree_root(current_worktree)
    if context_ignored(repo):
        return

    data = load_context(repo)
    index = repo / CONTEXT_INDEX
    script = updater_script()
    git_initialized = is_git_initialized(repo)

    lines = [
        "Project Context Curator is active for this session.",
        f"Repository root: {repo}",
        context_counts(data),
        (
            "For feature work, research, planning, or review: use durable project context. "
            "If docs/context/index.md exists, read it before making project-specific claims."
        ),
        (
            "Capturing durable project-level insight is part of the work, not a follow-up — "
            "investigation and answering questions count. The turn you verify a stable term, "
            "component, API, ownership boundary, architecture rule, environment mapping, or "
            "deployment convention from repo evidence, tool results, or user confirmation, run the "
            "updater that same turn, before you reply. Stay at project scope; never store task, "
            "feature, or research-specific details. If such an item is undocumented and its meaning "
            "is unclear, or you are unsure whether it belongs, ask one concise question first."
        ),
        (
            "Use source repo-docs for facts verified from repository docs/config/code, "
            "user-confirmed for user-provided answers, and add open questions rather than guessing."
        ),
        (
            "Initialization requires a user-confirmed enablement decision before creating "
            "docs/context. If the user declines, run the updater ignore command to create "
            f"{IGNORE_MARKER}. If the user agrees, bootstrap before responding: "
            "(1) read README, CLAUDE.md/AGENTS.md, the top-level directory layout, and main "
            "manifests/configs; (2) prepare add-component/add-term/add-pattern commands from "
            "verified findings; (3) run init and those commands in the same turn. An init that "
            "leaves all counts at 0 means the bootstrap step was skipped — do not respond to "
            "the user in that state. After init, verify docs/context/index.md shows non-zero "
            "counts (or record an explicit open question if the repository is genuinely empty)."
        ),
        f"Updater script: python3 {script} <command> --repo {repo}",
        "Do not guess definitions. Do not store secrets or transient debugging details.",
    ]
    if current_worktree != repo:
        lines.insert(2, f"Current worktree: {current_worktree}")

    if git_initialized:
        lines.append(
            "This repository is Git-initialized; after the user agrees to initialization, ask "
            "whether docs/context should be local or versioned before running init."
        )
    else:
        lines.append(
            "This directory is not Git-initialized; context is local by default and no "
            "local-vs-versioned question is needed."
        )
    if index.exists():
        lines.append(f"Context index: {index}")
    else:
        lines.append(
            "Context index is missing. Before acting on the first feature, research, planning, "
            "or review prompt, ask whether this project should use Project Context Curator. "
            "Do not initialize until the user agrees. If the user declines, run the updater "
            "ignore command."
        )

    emit("SessionStart", "\n".join(lines))


def log_exception() -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
            handle.write("\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    payload = read_payload()
    try:
        if mode == "session-start":
            session_start(payload)
        else:
            event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
            if event == "SessionStart":
                session_start(payload)
        return 0
    except Exception:
        log_exception()
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
