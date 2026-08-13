from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


HOOK = Path(__file__).resolve().parents[1] / "project-context-hook.py"


def load_hook_module():
    spec = importlib.util.spec_from_file_location("project_context_hook", HOOK)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_hook_process(mode: str, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK), mode],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def run_hook(mode: str, payload: dict) -> dict:
    proc = run_hook_process(mode, payload)
    assert proc.stderr == ""
    assert proc.stdout.strip()
    return json.loads(proc.stdout)


def git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def git_commit(repo: Path, message: str = "initial commit") -> None:
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def git_add_worktree(repo: Path, worktree: Path) -> None:
    subprocess.run(
        ["git", "worktree", "add", "-b", "context-worktree", str(worktree)],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def write_context(repo: Path) -> None:
    context_dir = repo / "docs" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "index.md").write_text("# Project Context\n", encoding="utf-8")
    (context_dir / "context.json").write_text(
        json.dumps(
            {"terms": [{"term": "ACS"}], "components": [], "patterns": [], "open_questions": []}
        ),
        encoding="utf-8",
    )


def test_session_start_emits_context(tmp_path: Path):
    output = run_hook("session-start", {"cwd": str(tmp_path)})
    text = output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Project Context Curator is active" in text
    assert "Capturing durable project-level insight is part of the work" in text
    assert "Stay at project scope" in text
    assert "never store task, feature, or research-specific details" in text
    assert "you are unsure whether it belongs" in text
    assert "user-confirmed enablement decision" in text
    assert ".no-project-context" in text
    assert "run init and those commands in the same turn" in text
    assert "leaves all counts at 0" in text
    assert "not Git-initialized" in text
    assert "no local-vs-versioned question is needed" in text
    assert "Updater script:" in text
    assert "Search command:" in text
    assert "Update command:" in text
    assert "read the index and its topical index" in text
    assert "run the updater search command with 1–3 distinctive task terms" in text
    assert "open only the matching generated sections" in text
    assert "fall back to rg against docs/context/context.json" in text


def test_session_start_uses_main_repo_context_from_linked_worktree(tmp_path: Path):
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    repo.mkdir()
    git_init(repo)
    git_commit(repo)
    write_context(repo)
    git_add_worktree(repo, linked)

    output = run_hook("session-start", {"cwd": str(linked)})
    text = output["hookSpecificOutput"]["additionalContext"]

    assert f"Repository root: {repo.resolve()}" in text
    assert f"Current worktree: {linked.resolve()}" in text
    assert "Existing context counts: 1 terms, 0 components, 0 patterns, 0 open questions." in text
    assert f"Context index: {repo.resolve() / 'docs' / 'context' / 'index.md'}" in text
    assert str(linked.resolve() / "docs" / "context") not in text


def test_session_start_migrates_context_and_refreshes_views(tmp_path: Path) -> None:
    write_context(tmp_path)

    run_hook("session-start", {"cwd": str(tmp_path)})
    data = json.loads((tmp_path / "docs/context/context.json").read_text(encoding="utf-8"))
    index = (tmp_path / "docs/context/index.md").read_text(encoding="utf-8")

    assert (data["schema_version"], "## Topical Index" in index, "ACS" in index) == (
        1,
        True,
        True,
    )


def test_context_update_failure_is_non_blocking_and_logged(tmp_path: Path) -> None:
    module = load_hook_module()
    write_context(tmp_path)
    module.LOG_PATH = tmp_path / "hook.log"

    warning = module.update_existing_context(tmp_path, tmp_path / "missing-updater.py")

    assert (
        warning.startswith("Automatic project context update failed:"),
        "Automatic project context update failed:" in module.LOG_PATH.read_text(encoding="utf-8"),
    ) == (True, True)


def test_hooks_are_silent_when_context_is_ignored(tmp_path: Path):
    (tmp_path / ".no-project-context").write_text(
        "Project Context Curator is disabled for this repository.\n",
        encoding="utf-8",
    )

    session_proc = run_hook_process("session-start", {"cwd": str(tmp_path)})

    assert session_proc.stderr == ""
    assert session_proc.stdout == ""
