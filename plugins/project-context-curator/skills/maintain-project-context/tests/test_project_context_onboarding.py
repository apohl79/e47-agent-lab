from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest


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


def configured_global_environment(
    tmp_path: Path,
    catalog: dict[str, object] | None,
) -> tuple[dict[str, str], Path]:
    env = isolated_environment(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_dir = Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"])
    data_dir = Path(env["PROJECT_CONTEXT_CURATOR_DATA_DIR"])
    cache_dir = Path(env["PROJECT_CONTEXT_CURATOR_CACHE_DIR"])
    config_dir.mkdir()
    data_dir.mkdir()
    cache_dir.mkdir()
    config = {"enabled": True, "workspace_roots": [str(workspace.resolve())]}
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    fingerprint = runpy.run_path(str(SCRIPT))["runtime_fingerprint"]()
    (data_dir / "runtime.json").write_text(
        json.dumps({"fingerprint": fingerprint}), encoding="utf-8"
    )
    if catalog is not None:
        (cache_dir / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    return env, workspace


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


@pytest.mark.parametrize(
    "catalog",
    (
        None,
        {},
        {"index_schema_version": 2, "sources": {}},
        {"index_schema_version": 4, "sources": []},
    ),
    ids=("missing", "empty", "malformed-v2", "future"),
)
def test_invalid_global_catalog_status_requires_approved_repair(
    tmp_path: Path,
    catalog: dict[str, object] | None,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env, workspace = configured_global_environment(tmp_path, catalog)

    proc = run_context("global-status", "--format", "hook", repo=repo, env=env)

    assert proc.stdout.splitlines() == [
        "Global context enrollment repair required: catalog is missing or invalid.",
        f"Workspace roots: {workspace.resolve()}",
        (
            "Before normal project work, proactively use the Project Context Curator "
            "skill to preview global-enroll for the configured workspace roots, show "
            "the exact snapshot, ask the user to approve that snapshot, and rerun "
            "global-enroll with the approved token."
        ),
    ]


def test_schema_v2_catalog_status_remains_active_during_automatic_upgrade(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    catalog = {
        "index_schema_version": 2,
        "sources": [],
        "projects": [],
        "project_count": 0,
        "records": [],
    }
    env, workspace = configured_global_environment(tmp_path, catalog)

    proc = run_context("global-status", "--format", "hook", repo=repo, env=env)

    assert proc.stdout.splitlines() == [
        "Global context index: active across 0 projects and 0 records.",
        f"Workspace roots: {workspace.resolve()}",
        "The standard search command queries this global index automatically.",
    ]


def test_search_with_invalid_catalog_emits_repair_trigger_and_local_results(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env, _ = configured_global_environment(tmp_path, None)
    env["PROJECT_CONTEXT_CURATOR_UV"] = str(tmp_path / "missing-uv")
    run_context("init", repo=repo, env=env)
    run_context(
        "add-pattern",
        "--name",
        "Local fallback",
        "--summary",
        "Local result remains available",
        repo=repo,
        env=env,
    )

    proc = run_context("search", "--query", "local result", repo=repo, env=env)

    assert (
        proc.returncode,
        "pattern | Local fallback" in proc.stdout,
        "Global context enrollment repair required" in proc.stderr,
        "preview global-enroll" in proc.stderr,
    ) == (0, True, True, True)
