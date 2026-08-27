from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_context.py"


def isolated_environment(root: Path) -> dict[str, str]:
    return {
        "PROJECT_CONTEXT_CURATOR_CONFIG_DIR": str(root / "config"),
        "PROJECT_CONTEXT_CURATOR_CACHE_DIR": str(root / "cache"),
        "PROJECT_CONTEXT_CURATOR_DATA_DIR": str(root / "data"),
    }


def run_context(
    *arguments: str,
    repo: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments, "--repo", str(repo)],
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def initialize(repo: Path, env: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    assert run_context("init", repo=repo, env=env).returncode == 0


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def project_context(repo: Path) -> dict[str, object]:
    return read_json(repo / "docs/context/context.json")


def configure_workspace(env: dict[str, str], workspace: Path) -> None:
    config = Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "enabled": False,
                "workspace_roots": [str(workspace.resolve())],
                "domains": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
