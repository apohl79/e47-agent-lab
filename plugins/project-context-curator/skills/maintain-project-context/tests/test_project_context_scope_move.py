from __future__ import annotations

from pathlib import Path

from scope_test_support import (
    initialize,
    isolated_environment,
    project_context,
    read_json,
    run_context,
)


def test_move_reclassifies_without_duplication_and_preserves_identity(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    initialize(repo, env)
    run_context(
        "add-pattern",
        "--name",
        "Release labels",
        "--summary",
        "Release labels use semantic versions",
        repo=repo,
        env=env,
    )
    original = project_context(repo)["patterns"][0]
    moved = run_context(
        "move",
        "--type",
        "pattern",
        "--value",
        "Release labels",
        "--applicability",
        "universal",
        repo=repo,
        env=env,
    )
    target = read_json(tmp_path / "xdg/data/contexts/universal/context.json")
    record = target["patterns"][0]

    assert (
        moved.returncode,
        project_context(repo)["patterns"],
        len(target["patterns"]),
        record["id"],
        record["applicability"],
        [entry["action"] for entry in record["provenance"]],
    ) == (
        0,
        [],
        1,
        original["id"],
        [{"kind": "universal"}],
        ["recorded", "moved"],
    )
