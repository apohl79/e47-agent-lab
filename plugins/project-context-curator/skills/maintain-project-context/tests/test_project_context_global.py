from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_context.py"


def run_context(
    *args: str,
    repo: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--repo", str(repo)],
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def isolated_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PROJECT_CONTEXT_CURATOR_CONFIG_DIR": str(tmp_path / "config"),
        "PROJECT_CONTEXT_CURATOR_CACHE_DIR": str(tmp_path / "cache"),
        "PROJECT_CONTEXT_CURATOR_DATA_DIR": str(tmp_path / "data"),
    }


def write_fake_uv(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-uv"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import pathlib
import sys

log = pathlib.Path(os.environ["FAKE_UV_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
command = next((value for value in sys.argv if value in {{"doctor", "sync", "search"}}), "")
if command == "doctor":
    print("Qdrant runtime ready")
elif command == "sync":
    catalog = pathlib.Path(sys.argv[sys.argv.index("--catalog") + 1])
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps(
            {{
                "index_schema_version": 3,
                "dense_model": "sentence-transformers/all-MiniLM-L6-v2",
                "dense_model_revision": "5f1b8cd78bc4fb444dd171e59b18f3a3af89a079",
                "sparse_model": "Qdrant/bm25",
                "sparse_model_revision": "22b8d2af71a76161e18dd432d2cee0eefa66e412",
                "enrollment_policy": "snapshot",
                "projects": [],
                "project_count": 0,
                "relationships": {{}},
                "sources": [],
                "records": [],
            }}
        )
        + "\\n",
        encoding="utf-8",
    )
    print("Indexed 2 projects and 3 records (3 changed, 0 removed).")
elif command == "search":
    if os.environ.get("FAKE_GLOBAL_EMPTY") == "1":
        print("No global context matches for: query")
    else:
        label = os.environ.get("FAKE_GLOBAL_LABEL", "Gateway ownership")
        source = os.environ.get("FAKE_GLOBAL_SOURCE", "/workspace/conversation-gateway/docs/context/context.json")
        print(f"UNTRUSTED_CONTEXT_DATA | conversation-gateway | pattern | {{label}} | {{source}} | Owns gateway messages | applies: project:/workspace/conversation-gateway")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def init_local_context(repo: Path, env: dict[str, str]) -> None:
    proc = run_context("init", repo=repo, env=env)
    assert proc.returncode == 0


def approval_token(output: str) -> str:
    prefix = "Snapshot token: "
    return next(
        line.removeprefix(prefix)
        for line in output.splitlines()
        if line.startswith(prefix)
    )


def init_global_context(
    repo: Path, workspace: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    preview = run_context(
        "global-init", "--workspace-root", str(workspace), repo=repo, env=env
    )
    return run_context(
        "global-init",
        "--workspace-root",
        str(workspace),
        "--approve-snapshot",
        approval_token(preview.stdout),
        repo=repo,
        env=env,
    )


def test_global_init_records_roots_and_validated_runtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")

    preview = run_context(
        "global-init",
        "--workspace-root",
        str(workspace),
        repo=repo,
        env=env,
    )
    preview_state = (
        preview.returncode,
        "No changes made" in preview.stdout,
        (tmp_path / "config/config.json").exists(),
        (tmp_path / "data/runtime.json").exists(),
    )
    proc = run_context(
        "global-init",
        "--workspace-root",
        str(workspace),
        "--approve-snapshot",
        approval_token(preview.stdout),
        repo=repo,
        env=env,
    )

    config = json.loads((tmp_path / "config/config.json").read_text(encoding="utf-8"))
    runtime = json.loads((tmp_path / "data/runtime.json").read_text(encoding="utf-8"))
    calls = [
        json.loads(line)
        for line in (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
    ]
    assert (proc.returncode, proc.stderr) == (0, "")
    assert config["workspace_roots"] == [str(workspace.resolve())]
    assert config["enrollment_policy"] == "snapshot"
    assert config["runtime_upgrade_policy"] == "prompt"
    assert runtime["fingerprint"]
    assert any("doctor" in call for call in calls)
    assert any("sync" in call and "--enroll-new" in call for call in calls)
    assert preview_state == (0, True, False, False)


def test_global_init_preview_escapes_exact_untrusted_source_inputs(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    project = workspace / "bad\n\x1b\u202eproject"
    context = project / "docs/context/context.json"
    context.parent.mkdir(parents=True)
    context.write_text("{}", encoding="utf-8")
    env = isolated_environment(tmp_path)

    preview = run_context(
        "global-init", "--workspace-root", str(workspace), repo=repo, env=env
    )
    source_line = next(
        line
        for line in preview.stdout.splitlines()
        if "UNTRUSTED_SNAPSHOT_DATA" in line
        and '"source"' in line
    )
    root_line = next(
        line
        for line in preview.stdout.splitlines()
        if "UNTRUSTED_SNAPSHOT_DATA" in line
        and '"workspace_root"' in line
        and '"source"' not in line
    )
    payload = json.loads(source_line)
    root_payload = json.loads(root_line)

    assert preview.returncode == 0
    assert "\x1b" not in preview.stdout
    assert "\u202e" not in preview.stdout
    assert "\\u001b" in source_line
    assert "\\u202e" in source_line
    assert payload["source"]["source_path"] == str(context.resolve())
    assert payload["source"]["workspace_root"] == str(workspace.resolve())
    assert root_payload["workspace_root"] == str(workspace.resolve())


def test_global_init_preview_reports_removals_from_existing_catalog(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_workspace = tmp_path / "old-workspace"
    new_workspace = tmp_path / "new-workspace"
    repo.mkdir()
    old_workspace.mkdir()
    new_workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    assert init_global_context(repo, old_workspace, env).returncode == 0
    catalog = tmp_path / "cache/catalog.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_path": str(
                            old_workspace / "old/docs/context/context.json"
                        ),
                        "project_path": str(old_workspace / "old"),
                        "workspace_root": str(old_workspace),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    preview = run_context(
        "global-init",
        "--workspace-root",
        str(new_workspace),
        repo=repo,
        env=env,
    )

    assert "Projects to remove: 1" in preview.stdout
    assert '"change": "remove"' in preview.stdout


def test_global_init_rejects_a_non_ascii_snapshot_token_cleanly(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)

    proc = run_context(
        "global-init",
        "--workspace-root",
        str(workspace),
        "--approve-snapshot",
        "é",
        repo=repo,
        env=env,
    )

    assert proc.returncode != 0
    assert "snapshot token" in proc.stderr.casefold()
    assert "Traceback" not in proc.stderr


def test_global_upgrade_revalidates_a_stale_runtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "runtime.json").write_text(
        json.dumps({"fingerprint": "stale"}), encoding="utf-8"
    )

    proc = run_context("global-upgrade", repo=repo, env=env)

    runtime = json.loads((data_dir / "runtime.json").read_text(encoding="utf-8"))
    calls = [
        json.loads(line)
        for line in (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
    ]
    assert (proc.returncode, proc.stderr) == (0, "")
    assert runtime["fingerprint"] != "stale"
    assert any("doctor" in call for call in calls)


def test_search_uses_global_backend_when_runtime_is_current(tmp_path: Path) -> None:
    repo = tmp_path / "telephony-bridge"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    init_local_context(repo, env)
    assert init_global_context(repo, workspace, env).returncode == 0
    assert run_context(
        "domain-set",
        "--domain",
        "voice",
        "--project",
        str(repo),
        repo=repo,
        env=env,
    ).returncode == 0

    proc = run_context("search", "--query", "gateway owner", repo=repo, env=env)
    status = run_context("global-status", "--format", "hook", repo=repo, env=env)

    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout.startswith(
        "UNTRUSTED_CONTEXT_DATA | conversation-gateway | pattern | Gateway ownership"
    )
    calls = [
        json.loads(line)
        for line in (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
    ]
    search_call = next(call for call in calls if "search" in call)
    assert (
        str(repo.resolve()) in search_call,
        "--scope-root" in search_call,
        "domain:voice" in search_call,
        "Active context domains: voice" in status.stdout,
    ) == (True, True, True, True)


def test_search_refreshes_a_schema_v2_catalog_without_new_approval(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    init_local_context(repo, env)
    assert init_global_context(repo, workspace, env).returncode == 0
    catalog_path = tmp_path / "cache/catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["index_schema_version"] = 2
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    proc = run_context("search", "--query", "gateway", repo=repo, env=env)

    calls = [
        json.loads(line)
        for line in (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
    ]
    refreshed = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert (
        proc.returncode,
        proc.stderr,
        proc.stdout.startswith("UNTRUSTED_CONTEXT_DATA"),
        sum("sync" in call for call in calls),
        refreshed["index_schema_version"],
    ) == (0, "", True, 2, 3)


def test_search_falls_back_locally_when_runtime_fingerprint_is_stale(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    init_local_context(repo, env)
    assert (
        run_context(
            "add-pattern",
            "--name",
            "Local fallback",
            "--summary",
            "Searches canonical local context",
            repo=repo,
            env=env,
        ).returncode
        == 0
    )
    assert init_global_context(repo, workspace, env).returncode == 0
    runtime_path = tmp_path / "data/runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["fingerprint"] = "stale"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    proc = run_context("search", "--query", "local fallback", repo=repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout.startswith("pattern | Local fallback")
    assert "Global context runtime update required" in proc.stderr


def test_search_merges_local_exact_match_before_global_results(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    init_local_context(repo, env)
    run_context(
        "add-pattern",
        "--name",
        "Local exact",
        "--summary",
        "Searches canonical local context",
        repo=repo,
        env=env,
    )
    init_global_context(repo, workspace, env)

    proc = run_context("search", "--query", "local exact", repo=repo, env=env)

    assert proc.stdout.splitlines() == [
        "pattern | Local exact | docs/context/architecture.md | Searches canonical local context | matched: local exact",
        "UNTRUSTED_CONTEXT_DATA | conversation-gateway | pattern | Gateway ownership | /workspace/conversation-gateway/docs/context/context.json | Owns gateway messages | applies: project:/workspace/conversation-gateway",
    ]


def test_search_uses_local_results_when_global_search_is_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    env["FAKE_GLOBAL_EMPTY"] = "1"
    init_local_context(repo, env)
    run_context(
        "add-pattern",
        "--name",
        "Local fallback",
        "--summary",
        "Searches canonical local context",
        repo=repo,
        env=env,
    )
    init_global_context(repo, workspace, env)

    proc = run_context("search", "--query", "local fallback", repo=repo, env=env)

    assert proc.stdout.startswith("pattern | Local fallback")


def test_search_deduplicates_a_truncated_global_label(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    label = "L" * 258
    env["FAKE_GLOBAL_LABEL"] = label[:199] + "…"
    env["FAKE_GLOBAL_SOURCE"] = str(repo / "docs/context/context.json")
    init_local_context(repo, env)
    run_context(
        "add-pattern",
        "--name",
        label,
        "--summary",
        "Long local label",
        repo=repo,
        env=env,
    )
    init_global_context(repo, workspace, env)

    proc = run_context("search", "--query", "L" * 20, repo=repo, env=env)

    assert len(proc.stdout.splitlines()) == 1
    assert proc.stdout.startswith("pattern | ")


def test_search_preserves_initialization_guidance_when_global_is_enabled(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    init_global_context(repo, workspace, env)

    proc = run_context("search", "--query", "gateway", repo=repo, env=env)

    assert proc.returncode != 0
    assert "not initialized" in proc.stderr.casefold()


def test_search_retains_global_results_when_local_context_is_invalid(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    init_local_context(repo, env)
    init_global_context(repo, workspace, env)
    (repo / "docs/context/context.json").write_text(
        json.dumps({"schema_version": 999}), encoding="utf-8"
    )

    proc = run_context("search", "--query", "gateway", repo=repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout.startswith("UNTRUSTED_CONTEXT_DATA")
    assert "UNTRUSTED_CONTEXT_DIAGNOSTIC" in proc.stderr


def test_global_enroll_requires_a_fresh_approved_snapshot(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")
    init_global_context(repo, workspace, env)

    run_context("global-update", repo=repo, env=env)
    pending = workspace / "pending/docs/context"
    pending.mkdir(parents=True)
    (pending / "context.json").write_text("{}", encoding="utf-8")
    preview = run_context("global-enroll", repo=repo, env=env)
    run_context(
        "global-enroll",
        "--approve-snapshot",
        approval_token(preview.stdout),
        repo=repo,
        env=env,
    )
    calls = [
        json.loads(line)
        for line in (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
        if "sync" in line
    ]

    assert ["--enroll-new" in call for call in calls] == [True, False, True]


def test_global_status_is_dependency_free_and_reports_upgrade_command(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "workspace_roots": [str(workspace)],
                "runtime_upgrade_policy": "prompt",
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "runtime.json").write_text(
        json.dumps({"fingerprint": "stale"}), encoding="utf-8"
    )

    proc = run_context("global-status", "--format", "hook", repo=repo, env=env)

    assert proc.returncode == 0
    assert "Global context runtime update required" in proc.stdout
    assert "global-upgrade" in proc.stdout
