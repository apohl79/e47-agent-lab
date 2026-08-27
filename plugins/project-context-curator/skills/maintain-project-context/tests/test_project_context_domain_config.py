from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_context.py"


def run_context(
    *arguments: str,
    repo: Path,
    root: Path,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.update(
        {
            "PROJECT_CONTEXT_CURATOR_CONFIG_DIR": str(root / "config"),
            "PROJECT_CONTEXT_CURATOR_CACHE_DIR": str(root / "cache"),
            "PROJECT_CONTEXT_CURATOR_DATA_DIR": str(root / "data"),
        }
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments, "--repo", str(repo)],
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


@pytest.mark.parametrize(
    ("domain_id", "accepted"),
    [("a", True), ("a" * 64, True), ("a" * 65, False), ("Upper Case", False)],
)
def test_domain_id_boundaries(
    tmp_path: Path,
    domain_id: str,
    accepted: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    proc = run_context(
        "domain-set",
        "--domain",
        domain_id,
        "--project",
        str(repo),
        repo=repo,
        root=tmp_path / "xdg",
    )

    assert (proc.returncode == 0, proc.stderr == "") == (accepted, accepted)
