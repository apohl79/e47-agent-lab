"""Publish canonical commits using the destination repository's write policy."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any


PENDING_FILE = "project-context-curator-publication.json"
QUEUE_FILE = "project-context-curator-publish-queue.json"
WORKER_SECONDS = 900
RETRY_SECONDS = 15


def pending_path(pc: Any, root: Path) -> Path:
    return pc.git_path(root, "--git-dir") / PENDING_FILE


def queue_path(pc: Any, root: Path) -> Path:
    return pc.git_path(root, "--git-dir") / QUEUE_FILE


def _save(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def enqueue(pc: Any, root: Path, repo: Path, *, error: str | None = None) -> None:
    path = queue_path(pc, root)
    data = {"repo": str(repo), "updated_at": pc.now_iso(), "state": "pending"}
    if error:
        data["error"] = error
    _save(path, data)
    if os.environ.get("PROJECT_CONTEXT_CURATOR_NO_BACKGROUND") == "1":
        return
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(root)],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        _save(path, {"repo": str(repo), "state": "pending", "error": str(exc)})


def _gh(pc: Any, root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *arguments], cwd=root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "GH_HOST": "github.com"},
            timeout=pc.GIT_STORE_NETWORK_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"Git context store GitHub publication failed: {exc}") from exc
    if result.returncode:
        raise SystemExit("Git context store GitHub publication failed: " + pc.git_error_detail(result))
    return result.stdout


def _json(pc: Any, root: Path, *arguments: str) -> Any:
    try:
        return json.loads(_gh(pc, root, *arguments))
    except ValueError:
        raise SystemExit("GitHub returned invalid publication JSON.") from None


def policy(pc: Any, root: Path, upstream: Any, *, detect: bool = True) -> dict[str, Any]:
    mode = pc.git_config_value(root, "project-context-curator.writeMode") or "auto"
    if mode not in {"auto", "direct", "pull-request"}:
        raise SystemExit("project-context-curator.writeMode must be auto, direct, or pull-request.")
    admin = pc.git_config_value(root, "project-context-curator.adminMerge") or "false"
    if admin not in {"true", "false"}:
        raise SystemExit("project-context-curator.adminMerge must be true or false.")
    remote = pc.normalize_remote_url(upstream.push_url) or ""
    repository = remote.removeprefix("github.com/")
    github = remote.startswith("github.com/") and len(repository.split("/")) == 2
    if mode == "pull-request" and not github:
        raise SystemExit("Pull-request publication requires a github.com push URL.")
    if mode == "auto" and detect:
        mode = "direct"
        if github:
            branch = _json(
                pc, root, "api", "--hostname", "github.com",
                f"repos/{repository}/branches/{upstream.branch}",
            )
            if not isinstance(branch.get("protected"), bool):
                raise SystemExit("GitHub returned invalid branch protection status.")
            if branch["protected"]:
                rules = _json(
                    pc, root, "api", "--hostname", "github.com",
                    f"repos/{repository}/rules/branches/{upstream.branch}",
                )
                if not isinstance(rules, list):
                    raise SystemExit("GitHub returned invalid branch rules.")
                if any(rule.get("type") in {"pull_request", "workflows", "required_status_checks"}
                       for rule in rules):
                    mode = "pull-request"
    return {"mode": mode, "repository": repository, "admin_merge": admin == "true"}


def _fetch(pc: Any, root: Path, upstream: Any) -> str:
    pc.git_checked(
        root, "fetch", "--no-tags", upstream.remote, upstream.branch,
        action="publication refresh",
    )
    return pc.git_checked(root, "rev-parse", "FETCH_HEAD", action="publication revision").stdout.strip()


def _contains(pc: Any, root: Path, ancestor: str, descendant: str) -> bool:
    result = pc.git_process(root, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in {0, 1}:
        raise SystemExit("Cannot verify the published commit's ancestry.")
    return result.returncode == 0


def synchronize(pc: Any, root: Path, remote_head: str) -> None:
    result = pc.git_process(root, "merge", "--no-edit", remote_head)
    if result.returncode:
        pc.git_process(root, "merge", "--abort")
        raise SystemExit("Merged PR could not be reconciled with later local writes; publication journal retained: " + pc.git_error_detail(result))


def _finish(pc: Any, root: Path, upstream: Any, head: str, path: Path) -> None:
    remote_head = _fetch(pc, root, upstream)
    if not _contains(pc, root, head, remote_head):
        raise SystemExit("PR is not published on main; local commit and publication journal retained.")
    synchronize(pc, root, remote_head)
    path.unlink(missing_ok=True)


def publish(pc: Any, root: Path, upstream: Any, selected: dict[str, Any]) -> None:
    path = pending_path(pc, root)
    current = pc.git_checked(root, "rev-parse", "HEAD", action="publication head").stdout.strip()
    saved = pc.read_json_object(path) if path.exists() else None
    head = saved["head"] if saved else current
    if not _contains(pc, root, head, current):
        raise SystemExit("Pending publication commit is no longer in local history; publication retained.")
    identity = {
        "head": head, "remote": upstream.remote, "branch": upstream.branch,
        "push_url": upstream.push_url, **selected,
    }
    if saved and saved != identity:
        raise SystemExit("Pending PR publication changed; retain its commit and original write settings before retrying.")
    remote_head = _fetch(pc, root, upstream)
    if _contains(pc, root, head, remote_head):
        _finish(pc, root, upstream, head, path)
        return
    if not _contains(pc, root, remote_head, head):
        raise SystemExit("Remote main advanced; synchronize before creating or merging the context PR.")
    repository = selected["repository"]
    metadata = _json(
        pc, root, "api", "--hostname", "github.com", f"repos/{repository}",
    )
    if not metadata.get("allow_merge_commit"):
        raise SystemExit("PCC pull-request publication requires merge commits enabled to preserve canonical commit identity.")
    if not path.exists():
        _save(path, identity)
    branch = f"pcc/context-{head}"
    pc.git_checked(
        root, "push", upstream.remote, f"{head}:refs/heads/{branch}",
        action="context PR branch push",
    )
    pulls = _json(
        pc, root, "pr", "list", "--repo", f"github.com/{repository}",
        "--head", branch, "--base", upstream.branch, "--state", "all",
        "--json", "number,state,headRefOid,url",
    )
    if len(pulls) > 1 or (pulls and pulls[0]["headRefOid"] != head):
        raise SystemExit("Context PR identity is ambiguous or changed; publication retained locally.")
    if pulls:
        pull = pulls[0]
        if pull["state"] == "CLOSED":
            raise SystemExit(f"Context PR {pull['url']} was closed without merging; reopen it before retrying.")
        number = str(pull["number"])
    else:
        url = _gh(
            pc, root, "pr", "create", "--repo", f"github.com/{repository}",
            "--head", branch, "--base", upstream.branch,
            "--title", "chore(context): publish canonical context",
            "--body", f"Publish canonical context commit {head}. Managed by Project Context Curator.",
        ).strip()
        number = url
    print(f"Context publication PR: {number}")
    # Never merge a stale canonical snapshot into a concurrently changed base.
    if _fetch(pc, root, upstream) != remote_head:
        raise SystemExit("Remote main changed while preparing the context PR; publication retained locally.")
    arguments = [
        "pr", "merge", number, "--repo", f"github.com/{repository}",
        "--merge", "--match-head-commit", head,
    ]
    if selected["admin_merge"]:
        arguments.append("--admin")
    _gh(pc, root, *arguments)
    _finish(pc, root, upstream, head, path)


def resume(pc: Any, root: Path, upstream: Any) -> None:
    path = pending_path(pc, root)
    if path.exists():
        saved = pc.read_json_object(path)
        selected = policy(pc, root, upstream)
        if selected != {key: saved.get(key) for key in selected}:
            raise SystemExit("Pending PR publication write settings changed; restore them before retrying.")
        publish(pc, root, upstream, selected)


def deferred(pc: Any, root: Path, children: dict[str, Path]) -> bool:
    if queue_path(pc, root).exists():
        for child in children.values():
            policy(pc, child, pc.git_store_upstream(child), detect=False)
        return True
    return any(
        policy(pc, child, pc.git_store_upstream(child))["mode"] == "pull-request"
        for child in children.values()
    )


def run(pc: Any, args: Any) -> None:
    configured = pc.configured_git_store()
    if configured is None:
        raise SystemExit("No canonical Git context store is configured.")
    root = pc.validate_git_store_root(Path(configured["path"]))
    journal_path = pc.git_path(root, "--git-dir") / pc.domain_stores.JOURNAL
    if journal_path.exists():
        journal = pc.read_json_object(journal_path)
        request = journal["plan"]["request"]
        migration = argparse.Namespace(
            repo=args.repo, domain=request["domain"], store=request["store"],
            remote=request["remote"], create_repo=request["create_repo"],
            visibility=request["visibility"], approve_snapshot=journal["plan"]["token"],
        )
        pc.domain_stores.run(pc, migration)
        if journal_path.exists():
            queue = queue_path(pc, root)
            error = pc.read_json_object(queue).get("error") if queue.exists() else None
            raise SystemExit(error or "Domain migration publication remains pending.")
    with ExitStack() as locks:
        locks.enter_context(pc.git_store_lock(root))
        pc.require_no_unmanaged_git_store_changes(root)
        children = pc.domain_stores.roots(pc, root)
        if len(children) > 1:
            pc.require_unchanged_remote_routes(root, pc.git_store_upstream(root))
        for name, child in sorted(children.items()):
            if name == "main":
                continue
            locks.enter_context(pc.git_store_lock(child))
            pc.domain_stores.attach_branch(pc, child)
            pc.prepare_git_store(child, pc.git_store_upstream(child))
            if pending_path(pc, child).exists():
                raise SystemExit("Child-store PR publication remains pending.")
        pc.commit_git_store_changes(root, "chore(context): update published domain stores")
        pc.prepare_git_store(root, pc.git_store_upstream(root))
        queue_path(pc, root).unlink(missing_ok=True)
        print("Git context store publication complete.")


def worker(root: Path) -> None:
    import project_context as pc

    root = pc.validate_git_store_root(root)
    os.environ["PROJECT_CONTEXT_CURATOR_NO_BACKGROUND"] = "1"
    path = queue_path(pc, root)
    with (pc.git_path(root, "--git-dir") / "project-context-curator-publisher.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        deadline = time.monotonic() + WORKER_SECONDS
        while path.exists() and time.monotonic() < deadline:
            try:
                with pc.git_store_lock(root):
                    data = pc.read_json_object(path)
                    configured = pc.configured_git_store()
                    if configured is None or Path(configured["path"]).resolve() != root:
                        raise SystemExit("Configured store changed; the queued publication was not applied.")
                run(pc, argparse.Namespace(repo=Path(data["repo"])))
            except (SystemExit, OSError) as exc:
                with pc.git_store_lock(root):
                    if path.exists():
                        data = pc.read_json_object(path)
                        data.update(error=pc.safe_display_field(exc, pc.DIAGNOSTIC_OUTPUT_LIMIT),
                                    last_attempt=pc.now_iso(), state="pending")
                        _save(path, data)
                time.sleep(min(RETRY_SECONDS, max(0, deadline - time.monotonic())))
        if path.exists():
            with pc.git_store_lock(root):
                if path.exists():
                    data = pc.read_json_object(path)
                    data.update(state="retry-needed", worker_finished=pc.now_iso())
                    _save(path, data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    worker(parser.parse_args().worker)
