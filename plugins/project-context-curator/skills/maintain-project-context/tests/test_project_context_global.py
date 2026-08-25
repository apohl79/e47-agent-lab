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
    print("Indexed 2 projects and 3 records (3 changed, 0 removed).")
elif command == "search":
    print("conversation-gateway | pattern | Gateway ownership | /workspace/conversation-gateway/docs/context/architecture.md | Owns gateway messages")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def init_local_context(repo: Path, env: dict[str, str]) -> None:
    proc = run_context("init", repo=repo, env=env)
    assert proc.returncode == 0


def test_global_init_records_roots_and_validated_runtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    env = isolated_environment(tmp_path)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(write_fake_uv(tmp_path))
    env["FAKE_UV_LOG"] = str(tmp_path / "uv.log")

    proc = run_context(
        "global-init",
        "--workspace-root",
        str(workspace),
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
    assert config["runtime_upgrade_policy"] == "prompt"
    assert runtime["fingerprint"]
    assert any("doctor" in call for call in calls)
    assert any("sync" in call for call in calls)


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
    assert (
        run_context(
            "global-init", "--workspace-root", str(workspace), repo=repo, env=env
        ).returncode
        == 0
    )

    proc = run_context("search", "--query", "gateway owner", repo=repo, env=env)

    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout.startswith("conversation-gateway | pattern | Gateway ownership")
    calls = [
        json.loads(line)
        for line in (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
    ]
    assert any("search" in call and str(repo.resolve()) in call for call in calls)


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
    assert (
        run_context(
            "global-init", "--workspace-root", str(workspace), repo=repo, env=env
        ).returncode
        == 0
    )
    runtime_path = tmp_path / "data/runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["fingerprint"] = "stale"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    proc = run_context("search", "--query", "local fallback", repo=repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout.startswith("pattern | Local fallback")
    assert "Global context runtime update required" in proc.stderr


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
