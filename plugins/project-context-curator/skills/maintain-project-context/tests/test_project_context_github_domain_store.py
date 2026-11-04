from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from scope_test_support import run_context
from test_project_context_domain_stores import configured_store, manifest, successful, token


def test_github_creation_approval_binds_account_visibility_and_rewritten_transport(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, project_id = configured_store(tmp_path)
    commands = tmp_path / "commands"
    commands.mkdir()
    remote = tmp_path / "new-knowledge.git"
    state = tmp_path / "github-state.json"
    log = tmp_path / "github-commands.jsonl"
    gh = commands / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        """import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as output:
    output.write(json.dumps({"args": args, "host": os.environ.get("GH_HOST")}) + "\\n")
state = Path(os.environ["FAKE_GH_STATE"])
if args[:1] == ["api"]:
    print(os.environ["FAKE_GH_ACCOUNT"])
elif args[:2] == ["repo", "view"]:
    if not state.exists():
        print("not found", file=sys.stderr)
        sys.exit(1)
    print(state.read_text())
elif args[:2] == ["repo", "create"]:
    if state.exists():
        sys.exit(1)
    subprocess.run(["git", "init", "--quiet", "--bare", os.environ["FAKE_GH_REMOTE"]], check=True)
    subprocess.run(["git", "-C", os.environ["FAKE_GH_REMOTE"], "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    state.write_text(json.dumps({"id": "R_test", "nameWithOwner": args[2], "isPrivate": "--private" in args}))
else:
    sys.exit(2)
"""
    )
    gh.chmod(0o755)
    env.update({
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_STATE": str(state),
        "FAKE_GH_ACCOUNT": "acme",
        "FAKE_GH_REMOTE": str(remote),
    })
    index = int(env["GIT_CONFIG_COUNT"])
    env.update({
        "GIT_CONFIG_COUNT": str(index + 1),
        f"GIT_CONFIG_KEY_{index}": f"url.{remote}.insteadOf",
        f"GIT_CONFIG_VALUE_{index}": "https://github.com/acme/new-knowledge.git",
    })
    arguments = (
        "domain-store", "--domain", "cpl", "--store", "cpl",
        "--create-repo", "acme/new-knowledge", "--visibility", "private",
    )
    preview = successful(*arguments, repo=repo, env=env)
    assert not state.exists()
    approval = token(preview.stdout)

    changed_account = run_context(
        *arguments, "--approve-snapshot", approval,
        repo=repo, env={**env, "FAKE_GH_ACCOUNT": "different-account"},
    )
    assert changed_account.returncode != 0
    assert not state.exists()

    successful(*arguments, "--approve-snapshot", approval, repo=repo, env=env)

    events = [json.loads(line) for line in log.read_text().splitlines()]
    creates = [event for event in events if event["args"][:2] == ["repo", "create"]]
    assert creates == [{
        "args": ["repo", "create", "acme/new-knowledge", "--private"],
        "host": "github.com",
    }]
    assert manifest(store)["stores"]["cpl"]["remote_url"] == "https://github.com/acme/new-knowledge.git"
    assert (store / "stores/cpl/projects" / project_id / "context.json").exists()
    assert not (store / "projects" / project_id / "context.json").exists()
