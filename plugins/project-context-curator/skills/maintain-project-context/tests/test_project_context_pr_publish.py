from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scope_test_support import (
    initialize_git_repository,
    initialize_git_store,
    isolated_environment,
    read_json,
    run_context,
    run_git,
)
from test_project_context_domain_stores import configured_store, domain_remote, place_domain
from test_project_context_git_sync import (
    add_term,
    configure_store,
    snapshot_token,
    store_context_path,
)


def github_store(
    tmp_path: Path, mode: str = "pull-request", *, empty: bool = False,
) -> tuple[dict[str, str], Path, Path, Path]:
    if empty:
        env = isolated_environment(tmp_path / "xdg")
        repo = tmp_path / "repo"
        store = tmp_path / "store"
        initialize_git_repository(repo)
        run_context("init", "--visibility", "local", repo=repo, env=env)
        remote = initialize_git_store(store)
    else:
        env, repo, store, remote = configure_store(tmp_path)
    commands = tmp_path / "commands"
    commands.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    git = commands / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        """import os, sys
args = sys.argv[1:]
if "push" in args:
    index = args.index("push")
    if "origin" in args[index + 1:]:
        args[args.index("origin", index + 1)] = os.environ["FAKE_GH_REMOTE"]
os.execv(os.environ["FAKE_REAL_GIT"], ["git", *args])
"""
    )
    git.chmod(0o755)
    gh = commands / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        """import json, os, subprocess, sys, tempfile
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as output:
    output.write(json.dumps(args) + "\\n")
state_path = Path(os.environ["FAKE_GH_STATE"])
pulls = json.loads(state_path.read_text()) if state_path.exists() else []
def git(*arguments):
    return subprocess.run([os.environ["FAKE_REAL_GIT"], *arguments],
                          check=True, text=True, capture_output=True).stdout.strip()
if args[:1] == ["api"]:
    if any("/rules/branches/" in argument for argument in args):
        print(json.dumps([{"type": "pull_request"}]))
    elif any("/branches/" in argument for argument in args):
        branch = subprocess.run(
            [os.environ["FAKE_REAL_GIT"], "--git-dir", os.environ["FAKE_GH_REMOTE"],
             "rev-parse", "--verify", "refs/heads/main"],
            text=True, capture_output=True, check=False,
        )
        if branch.returncode:
            print("Not Found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"protected": os.environ.get("FAKE_GH_PROTECTED") != "false"}))
    else:
        print(json.dumps({"allow_merge_commit": True}))
elif args[:2] == ["pr", "list"]:
    branch = args[args.index("--head") + 1]
    print(json.dumps([pull for pull in pulls if pull["headRefName"] == branch]))
elif args[:2] == ["pr", "create"]:
    branch = args[args.index("--head") + 1]
    head = git("--git-dir", os.environ["FAKE_GH_REMOTE"],
               "rev-parse", "refs/heads/" + branch)
    pull = {"number": len(pulls) + 1, "state": "OPEN", "headRefOid": head,
            "headRefName": branch,
            "url": "https://github.com/acme/knowledge/pull/" + str(len(pulls) + 1)}
    pulls.append(pull)
    state_path.write_text(json.dumps(pulls))
    print(pull["url"])
elif args[:2] == ["pr", "merge"]:
    if os.environ.get("FAKE_GH_PENDING") == "1":
        print("required checks are pending", file=sys.stderr)
        sys.exit(1)
    number = int(args[2].rsplit("/", 1)[-1])
    pull = next(pull for pull in pulls if pull["number"] == number)
    expected = args[args.index("--match-head-commit") + 1]
    assert pull["headRefOid"] == expected
    if pull["state"] != "MERGED":
        with tempfile.TemporaryDirectory() as directory:
            git("clone", "--quiet", os.environ["FAKE_GH_REMOTE"], directory)
            git("-C", directory, "config", "user.name", "Context Curator Tests")
            git("-C", directory, "config", "user.email", "context-curator@example.invalid")
            git("-C", directory, "config", "commit.gpgsign", "false")
            git("-C", directory, "merge", "--no-ff", expected, "-m", "Merge context PR")
            git("-C", directory, "push", "origin", "main")
        pull["state"] = "MERGED"
        state_path.write_text(json.dumps(pulls))
else:
    print("unexpected fake gh arguments: " + repr(args), file=sys.stderr)
    sys.exit(2)
"""
    )
    gh.chmod(0o755)
    env.update({
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "FAKE_REAL_GIT": real_git,
        "FAKE_GH_REMOTE": str(remote),
        "FAKE_GH_LOG": str(tmp_path / "gh.jsonl"),
        "FAKE_GH_STATE": str(tmp_path / "pulls.json"),
        "PROJECT_CONTEXT_CURATOR_NO_BACKGROUND": "1",
    })
    run_git(store, "config", "remote.origin.pushurl", "https://github.com/acme/knowledge.git")
    run_git(store, "config", "project-context-curator.writeMode", mode)
    return env, repo, store, remote


def publication_events(env: dict[str, str]) -> list[list[str]]:
    path = Path(env["FAKE_GH_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def publish(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_context("git-store-publish", repo=repo, env=env)


def test_pr_write_queues_local_commit_without_waiting_for_github(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path)
    before = run_git(remote, "rev-parse", "main").stdout.strip()

    result = add_term(repo, env, "Queued", "Available locally before publication")

    assert result.returncode == 0, result.stderr
    assert "publication pending" in result.stdout.lower()
    assert run_git(store, "rev-parse", "HEAD").stdout.strip() != before
    assert run_git(remote, "rev-parse", "main").stdout.strip() == before
    assert (store / ".git/project-context-curator-publish-queue.json").exists()
    assert not any(event[:1] == ["pr"] for event in publication_events(env))


def test_auto_write_mode_detects_required_pull_requests(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path, "auto")
    before = run_git(remote, "rev-parse", "main").stdout.strip()

    result = add_term(repo, env, "Automatic", "Protected repositories publish through PRs")

    assert result.returncode == 0, result.stderr
    assert "publication pending" in result.stdout.lower()
    assert run_git(store, "rev-parse", "HEAD").stdout.strip() != before
    assert run_git(remote, "rev-parse", "main").stdout.strip() == before
    assert any("/rules/branches/main" in " ".join(event) for event in publication_events(env))


def test_auto_unprotected_repo_does_not_require_rulesets_api(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path, "auto")
    env["FAKE_GH_PROTECTED"] = "false"

    result = add_term(repo, env, "Direct", "Unprotected repositories keep direct publishing")

    assert result.returncode == 0, result.stderr
    assert "synchronized" in result.stdout
    assert run_git(store, "rev-parse", "HEAD").stdout == run_git(remote, "rev-parse", "main").stdout
    assert not any("/rules/branches/" in " ".join(event) for event in publication_events(env))
    assert not any(event[:1] == ["pr"] for event in publication_events(env))


def test_auto_policy_bootstraps_when_remote_main_does_not_exist(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path, "auto", empty=True)
    arguments = ("git-store-init", "--store", str(store))
    preview = run_context(*arguments, repo=repo, env=env)
    result = run_context(
        *arguments, "--approve-snapshot", snapshot_token(preview.stdout),
        repo=repo, env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "synchronized" in result.stdout
    assert run_git(remote, "rev-parse", "refs/heads/main").stdout.strip()
    events = publication_events(env)
    assert not any(event[:1] == ["pr"] for event in events)


def test_detached_domain_submodule_is_attached_before_policy_detection(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, _project_id = configured_store(tmp_path)
    place_domain(repo, env, "cpl", domain_remote(tmp_path))
    child = store / "stores" / "cpl"
    run_git(child, "checkout", "--detach", "HEAD")

    result = run_context(
        "add-term", "--term", "Detached write", "--definition", "Submodule checkout recovers",
        "--applicability", "domain:cpl", repo=repo, env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert run_git(child, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert "Detached write" in read_json(child / "scopes/domains/cpl/context.json")["terms"][0]["term"]


def test_pending_checks_reuse_one_pr_and_keep_publication_journal(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path)
    assert add_term(repo, env, "Pending", "Wait for required checks").returncode == 0
    before = run_git(remote, "rev-parse", "main").stdout.strip()
    pending_env = {**env, "FAKE_GH_PENDING": "1"}

    first = publish(repo, pending_env)
    second = publish(repo, pending_env)

    assert first.returncode != 0
    assert second.returncode != 0
    assert "synchronized" not in (first.stdout + second.stdout).lower()
    assert (store / ".git/project-context-curator-publication.json").exists()
    assert (store / ".git/project-context-curator-publish-queue.json").exists()
    assert run_git(remote, "rev-parse", "main").stdout.strip() == before
    assert len([event for event in publication_events(env) if event[:2] == ["pr", "create"]]) == 1


def test_second_local_write_survives_pending_pr_and_both_writes_publish(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path)
    assert add_term(repo, env, "First", "First unpublished term").returncode == 0
    first_commit = run_git(store, "rev-parse", "HEAD").stdout.strip()
    assert publish(repo, {**env, "FAKE_GH_PENDING": "1"}).returncode != 0

    second_write = add_term(repo, env, "Second", "Second unpublished term")
    assert second_write.returncode == 0, second_write.stderr
    second_commit = run_git(store, "rev-parse", "HEAD").stdout.strip()
    result = publish(repo, env)
    if result.returncode == 0 and (store / ".git/project-context-curator-publish-queue.json").exists():
        result = publish(repo, env)

    assert result.returncode == 0, result.stderr
    remote_head = run_git(remote, "rev-parse", "main").stdout.strip()
    run_git(remote, "merge-base", "--is-ancestor", first_commit, remote_head)
    run_git(remote, "merge-base", "--is-ancestor", second_commit, remote_head)
    terms = read_json(store_context_path(env, repo, store))["terms"]
    assert {"First", "Second"}.issubset({term["term"] for term in terms})
    assert run_git(store, "status", "--short").stdout == ""


def test_successful_pr_merge_preserves_commit_and_synchronizes_main(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path)
    assert add_term(repo, env, "Published", "Preserves canonical commit identity").returncode == 0
    committed = run_git(store, "rev-parse", "HEAD").stdout.strip()

    result = publish(repo, env)

    assert result.returncode == 0, result.stderr
    remote_head = run_git(remote, "rev-parse", "main").stdout.strip()
    run_git(remote, "merge-base", "--is-ancestor", committed, remote_head)
    assert run_git(store, "rev-parse", "HEAD").stdout.strip() == remote_head
    assert not (store / ".git/project-context-curator-publication.json").exists()
    assert not (store / ".git/project-context-curator-publish-queue.json").exists()
    assert run_git(store, "status", "--short").stdout == ""
    merges = [event for event in publication_events(env) if event[:2] == ["pr", "merge"]]
    assert len(merges) == 1
    assert "--admin" not in merges[0]


def test_admin_merge_setting_is_applied_only_when_enabled(tmp_path: Path) -> None:
    env, repo, store, _remote = github_store(tmp_path)
    run_git(store, "config", "project-context-curator.adminMerge", "true")
    assert add_term(repo, env, "Admin", "Approved repository admin merge policy").returncode == 0

    result = publish(repo, env)

    assert result.returncode == 0, result.stderr
    merges = [event for event in publication_events(env) if event[:2] == ["pr", "merge"]]
    assert len(merges) == 1
    assert "--admin" in merges[0]
    assert "--merge" in merges[0]
    assert "--match-head-commit" in merges[0]


def test_noop_publisher_does_not_open_pr(tmp_path: Path) -> None:
    env, repo, _store, _remote = github_store(tmp_path)

    result = publish(repo, env)

    assert result.returncode == 0, result.stderr
    assert not any(event[:1] == ["pr"] for event in publication_events(env))


def test_background_worker_publishes_without_an_explicit_command(tmp_path: Path) -> None:
    env, repo, store, remote = github_store(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_NO_BACKGROUND"] = "0"
    before = run_git(remote, "rev-parse", "main").stdout.strip()

    result = add_term(repo, env, "Background", "A detached publisher handles the PR")

    assert result.returncode == 0, result.stderr
    assert "publication pending" in result.stdout.lower()
    queue = store / ".git/project-context-curator-publish-queue.json"
    deadline = time.monotonic() + 30
    while queue.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not queue.exists()
    assert run_git(remote, "rev-parse", "main").stdout.strip() != before
    assert run_git(store, "status", "--short").stdout == ""


def test_bounded_worker_releases_write_lock_between_pending_attempts(tmp_path: Path) -> None:
    env, repo, store, _remote = github_store(tmp_path)
    assert add_term(repo, env, "Before", "A queued write").returncode == 0
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    worker = subprocess.Popen(
        [
            sys.executable, "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "import git_store_publish as p; p.WORKER_SECONDS=5; p.RETRY_SECONDS=0.2; "
            "p.worker(Path(sys.argv[2]))", str(scripts), str(store),
        ],
        env={**os.environ, **env, "FAKE_GH_PENDING": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        path = store / ".git/project-context-curator-publication.json"
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert path.exists()
        result = add_term(repo, env, "During", "Writes continue while checks are pending")
        assert result.returncode == 0, result.stderr
        stdout, stderr = worker.communicate(timeout=20)
        assert worker.returncode == 0, stdout + stderr
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
    queue = read_json(store / ".git/project-context-curator-publish-queue.json")
    assert queue["state"] == "retry-needed"
    assert "pending" in queue["error"]
    assert publish(repo, env).returncode == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("project-context-curator.writeMode", "guess"),
        ("project-context-curator.adminMerge", "yes"),
    ],
)
def test_invalid_write_settings_fail_before_canonical_mutation(
    tmp_path: Path, key: str, value: str,
) -> None:
    env, repo, store, remote = github_store(tmp_path)
    run_git(store, "config", key, value)
    before = run_git(store, "rev-parse", "HEAD").stdout.strip()
    context = store_context_path(env, repo, store).read_bytes()

    result = add_term(repo, env, "Rejected", "Invalid settings must not mutate context")

    assert result.returncode != 0
    assert key in result.stderr
    assert run_git(store, "rev-parse", "HEAD").stdout.strip() == before
    assert run_git(remote, "rev-parse", "main").stdout.strip() == before
    assert store_context_path(env, repo, store).read_bytes() == context
    assert run_git(store, "status", "--short").stdout == ""


def test_invalid_settings_also_block_writes_when_publication_is_already_queued(tmp_path: Path) -> None:
    env, repo, store, _remote = github_store(tmp_path)
    assert add_term(repo, env, "Pending", "An existing queued write").returncode == 0
    before = store_context_path(env, repo, store).read_bytes()
    run_git(store, "config", "project-context-curator.writeMode", "invalid")

    result = add_term(repo, env, "Rejected", "Must not bypass validation while queued")

    assert result.returncode != 0
    assert "writeMode" in result.stderr
    assert store_context_path(env, repo, store).read_bytes() == before
