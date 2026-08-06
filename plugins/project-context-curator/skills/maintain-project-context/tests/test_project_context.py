from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_context.py"


def run_context(*args: str, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--repo", str(repo)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def read_context(repo: Path) -> dict:
    return json.loads((repo / "docs" / "context" / "context.json").read_text(encoding="utf-8"))


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


def git_exclude(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def assert_git_exclude_has_context_entry(repo: Path) -> None:
    assert "docs/context/" in git_exclude(repo).read_text(encoding="utf-8").splitlines()


def assert_git_exclude_lacks_context_entry(repo: Path) -> None:
    if not git_exclude(repo).exists():
        return
    assert "docs/context/" not in git_exclude(repo).read_text(encoding="utf-8").splitlines()


def write_git_exclude(repo: Path, value: str) -> None:
    path = git_exclude(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def init_context(
    repo: Path,
    visibility: str | None = "local",
) -> subprocess.CompletedProcess[str]:
    args = ["init"]
    if visibility is not None:
        args.extend(["--visibility", visibility])
    return run_context(*args, repo=repo)


def test_init_without_visibility_defaults_local_for_non_git(tmp_path: Path):
    proc = run_context("init", repo=tmp_path)

    assert proc.returncode == 0
    assert "Context visibility: local" in proc.stdout
    assert "Git initialized: False" in proc.stdout
    assert "Git exclude: skipped (not a Git repository)" in proc.stdout
    data = read_context(tmp_path)
    assert data["storage_policy"]["context_visibility"] == "local"
    assert data["storage_policy"]["git_initialized"] is False
    assert data["storage_policy"]["git_exclude_docs_context"] is False
    assert not (tmp_path / ".gitignore").exists()


def test_init_requires_visibility_decision_for_git_repo(tmp_path: Path):
    git_init(tmp_path)

    proc = run_context("init", repo=tmp_path)

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "Context visibility decision required for Git repositories" in proc.stderr
    assert not (tmp_path / "docs" / "context").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_init_local_records_policy_and_updates_git_exclude_for_git_repo(tmp_path: Path):
    git_init(tmp_path)

    proc = init_context(tmp_path, "local")

    assert proc.returncode == 0
    assert "Context visibility: local" in proc.stdout
    assert "Git initialized: True" in proc.stdout
    assert "Git exclude: updated" in proc.stdout
    assert str(git_exclude(tmp_path)) in proc.stdout
    assert not (tmp_path / ".gitignore").exists()
    assert_git_exclude_has_context_entry(tmp_path)
    data = read_context(tmp_path)
    assert data["storage_policy"]["context_visibility"] == "local"
    assert data["storage_policy"]["git_initialized"] is True
    assert data["storage_policy"]["git_exclude_docs_context"] is True
    assert data["storage_policy"]["source"] == "user-confirmed"
    index = (tmp_path / "docs" / "context" / "index.md").read_text(encoding="utf-8")
    assert "## Storage Policy" in index
    assert "- Visibility: local" in index
    assert "- Git exclude docs/context: True" in index


def test_init_from_linked_worktree_writes_context_to_main_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    repo.mkdir()
    git_init(repo)
    git_commit(repo)
    git_add_worktree(repo, linked)

    proc = init_context(linked, "local")

    assert proc.returncode == 0
    assert f"Initialized local context: {repo.resolve() / 'docs' / 'context'}" in proc.stdout
    assert (repo / "docs" / "context" / "context.json").exists()
    assert not (linked / "docs" / "context" / "context.json").exists()
    assert not (repo / ".gitignore").exists()
    assert_git_exclude_has_context_entry(repo)

    add_proc = run_context(
        "add-term",
        "--term",
        "ACS",
        "--definition",
        "Agent Conversation Service",
        repo=linked,
    )

    assert add_proc.returncode == 0
    data = read_context(repo)
    assert [term["term"] for term in data["terms"]] == ["ACS"]
    assert not (linked / "docs" / "context" / "glossary.md").exists()


def test_init_versioned_records_policy_without_git_exclude_entry(tmp_path: Path):
    git_init(tmp_path)

    proc = init_context(tmp_path, "versioned")

    assert proc.returncode == 0
    assert "Context visibility: versioned" in proc.stdout
    assert "Git initialized: True" in proc.stdout
    assert not (tmp_path / ".gitignore").exists()
    assert_git_exclude_lacks_context_entry(tmp_path)
    data = read_context(tmp_path)
    assert data["storage_policy"]["context_visibility"] == "versioned"
    assert data["storage_policy"]["git_initialized"] is True
    assert data["storage_policy"]["git_exclude_docs_context"] is False


def test_init_versioned_removes_existing_context_git_exclude_entry(tmp_path: Path):
    git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("docs/context/\n", encoding="utf-8")
    write_git_exclude(tmp_path, "build/\ndocs/context/\n# docs/context/\n")

    proc = init_context(tmp_path, "versioned")

    assert proc.returncode == 0
    assert "Git exclude: removed docs/context/ entry" in proc.stdout
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "docs/context/\n"
    assert git_exclude(tmp_path).read_text(encoding="utf-8") == "build/\n# docs/context/\n"


def test_init_versioned_leaves_unrelated_git_exclude_unchanged(tmp_path: Path):
    git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("build/", encoding="utf-8")
    write_git_exclude(tmp_path, "build/")

    proc = init_context(tmp_path, "versioned")

    assert proc.returncode == 0
    assert "Git exclude: unchanged" in proc.stdout
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "build/"
    assert git_exclude(tmp_path).read_text(encoding="utf-8") == "build/"


def test_add_term_requires_initialized_context(tmp_path: Path):
    proc = run_context(
        "add-term",
        "--term",
        "ACS",
        "--kind",
        "abbreviation",
        "--definition",
        "App Configuration Service",
        repo=tmp_path,
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "Context is not initialized" in proc.stderr
    assert "Ask the user whether project context should be initialized" in proc.stderr
    assert not (tmp_path / "docs" / "context").exists()


def test_ignore_context_writes_marker_without_initializing_context(tmp_path: Path):
    proc = run_context("ignore", repo=tmp_path)

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "Project context disabled" in proc.stdout
    assert (tmp_path / ".no-project-context").read_text(encoding="utf-8") == (
        "Project Context Curator is disabled for this repository.\n"
    )
    assert not (tmp_path / "docs" / "context").exists()


def test_ignore_context_refuses_when_context_exists(tmp_path: Path):
    assert init_context(tmp_path, None).returncode == 0

    proc = run_context("ignore", repo=tmp_path)

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "Context is already initialized" in proc.stderr
    assert not (tmp_path / ".no-project-context").exists()


def test_remove_term_updates_context_and_rendered_markdown(tmp_path: Path):
    assert init_context(tmp_path).returncode == 0
    add_proc = run_context(
        "add-term",
        "--term",
        "ACS",
        "--kind",
        "abbreviation",
        "--definition",
        "App Configuration Service",
        repo=tmp_path,
    )
    assert add_proc.returncode == 0

    remove_proc = run_context("remove", "--type", "term", "--value", "acs", repo=tmp_path)

    assert remove_proc.returncode == 0
    assert remove_proc.stderr == ""
    assert remove_proc.stdout.strip() == "Removed term: ACS"
    data = read_context(tmp_path)
    assert data["terms"] == []
    assert "- Terms: 0" in (tmp_path / "docs" / "context" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "No terms recorded yet." in (
        tmp_path / "docs" / "context" / "glossary.md"
    ).read_text(encoding="utf-8")


def test_remove_component_by_plural_type(tmp_path: Path):
    assert init_context(tmp_path).returncode == 0
    add_proc = run_context(
        "add-component",
        "--name",
        "BillingOrchestrator",
        "--responsibility",
        "Coordinates billing runs",
        repo=tmp_path,
    )
    assert add_proc.returncode == 0

    remove_proc = run_context(
        "remove",
        "--type",
        "components",
        "--value",
        "BillingOrchestrator",
        repo=tmp_path,
    )

    assert remove_proc.returncode == 0
    assert remove_proc.stdout.strip() == "Removed component: BillingOrchestrator"
    assert read_context(tmp_path)["components"] == []


def test_remove_missing_entry_exits_nonzero_without_creating_context(tmp_path: Path):
    remove_proc = run_context("remove", "--type", "term", "--value", "ACS", repo=tmp_path)

    assert remove_proc.returncode == 1
    assert remove_proc.stdout == ""
    assert "No term found for 'ACS'" in remove_proc.stderr
    assert not (tmp_path / "docs" / "context").exists()


def test_init_warns_when_context_left_empty(tmp_path: Path):
    proc = run_context("init", repo=tmp_path)

    assert proc.returncode == 0
    assert "WARNING: context is empty" in proc.stdout
    assert "add-component/add-term/add-pattern" in proc.stdout
    assert "in this same turn" in proc.stdout


def test_init_does_not_warn_when_context_has_entries(tmp_path: Path):
    run_context("init", repo=tmp_path)
    run_context(
        "add-component",
        "--name",
        "api",
        "--responsibility",
        "Serves the API",
        "--source",
        "repo-docs",
        repo=tmp_path,
    )

    proc = run_context("init", repo=tmp_path)

    assert proc.returncode == 0
    assert "WARNING: context is empty" not in proc.stdout


def test_init_writes_context_gitignore_for_hook_state(tmp_path: Path):
    proc = run_context("init", repo=tmp_path)

    assert proc.returncode == 0
    gitignore = tmp_path / "docs" / "context" / ".gitignore"
    assert gitignore.read_text(encoding="utf-8").splitlines() == [".hook-state.json"]


def test_save_appends_hook_state_to_existing_context_gitignore(tmp_path: Path):
    run_context("init", repo=tmp_path)
    gitignore = tmp_path / "docs" / "context" / ".gitignore"
    gitignore.write_text("custom-entry\n", encoding="utf-8")

    run_context(
        "add-term",
        "--term",
        "ACS",
        "--definition",
        "Agent Conversation Service",
        repo=tmp_path,
    )

    assert gitignore.read_text(encoding="utf-8").splitlines() == [
        "custom-entry",
        ".hook-state.json",
    ]
