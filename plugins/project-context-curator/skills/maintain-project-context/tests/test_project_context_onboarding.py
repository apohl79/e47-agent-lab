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


def test_global_init_preview_lists_contexts_requiring_initialization(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    missing = workspace / "missing"
    repo.mkdir()
    (missing / ".git").mkdir(parents=True)

    proc = run_context(
        "global-init",
        "--workspace-root",
        str(workspace),
        repo=repo,
        env=isolated_environment(tmp_path),
    )
    initialization_payloads = [
        json.loads(line)
        for line in proc.stdout.splitlines()
        if '"change": "initialize"' in line
    ]

    assert (
        proc.returncode,
        proc.stderr,
        "Projects requiring context initialization: 1" in proc.stdout,
        initialization_payloads,
    ) == (
        0,
        "",
        True,
        [
            {
                "change": "initialize",
                "source": {
                    "project_path": str(missing.resolve()),
                    "source_path": str(
                        missing.resolve() / "docs/context/context.json"
                    ),
                    "workspace_root": str(workspace.resolve()),
                },
                "type": "UNTRUSTED_SNAPSHOT_DATA",
            }
        ],
    )


def test_disabled_global_status_in_hook_format_requires_onboarding(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    proc = run_context(
        "global-status",
        "--format",
        "hook",
        repo=repo,
        env=isolated_environment(tmp_path),
    )

    assert proc.stdout.splitlines() == [
        "Global context index: disabled.",
        (
            "Global context onboarding required. Before normal project work, "
            "proactively use the Project Context Curator skill to confirm workspace "
            "roots and one local-or-versioned policy, preview global-init, request "
            "approval for the exact snapshot, bootstrap every listed missing context "
            "with verified non-empty records, and rerun global-init with the approved token."
        ),
    ]
