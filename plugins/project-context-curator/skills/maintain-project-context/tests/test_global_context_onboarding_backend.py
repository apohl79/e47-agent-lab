from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


BACKEND = Path(__file__).resolve().parents[1] / "scripts" / "global_context.py"


@pytest.fixture
def backend() -> ModuleType:
    spec = importlib.util.spec_from_file_location("global_context_onboarding", BACKEND)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_git_repository(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def write_context(path: Path) -> None:
    context = path / "docs/context/context.json"
    context.parent.mkdir(parents=True, exist_ok=True)
    context.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "terms": [],
                "components": [],
                "patterns": [],
                "open_questions": [],
            }
        ),
        encoding="utf-8",
    )


def source(backend: ModuleType, project: Path, workspace: Path) -> object:
    return backend.ContextSource(
        source_path=str(project / "docs/context/context.json"),
        project_path=str(project),
        workspace_root=str(workspace),
    )


def test_discovery_classifies_initialized_and_missing_projects(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    context_only = workspace / "context-only"
    initialized = workspace / "initialized"
    missing = workspace / "missing"
    opted_out = workspace / "opted-out"
    ordinary = workspace / "ordinary"
    write_context(context_only)
    create_git_repository(initialized)
    write_context(initialized)
    create_git_repository(missing)
    create_git_repository(opted_out)
    (opted_out / ".no-project-context").write_text("disabled\n", encoding="utf-8")
    ordinary.mkdir()

    sources, requiring_initialization = backend.discover_context_candidates(
        (workspace,)
    )

    assert (sources, requiring_initialization) == (
        (
            source(backend, context_only, workspace),
            source(backend, initialized, workspace),
            source(backend, missing, workspace),
        ),
        (source(backend, missing, workspace),),
    )


def test_discovery_skips_dependency_copies_and_linked_worktrees(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    primary = workspace / "primary"
    dependency = workspace / "node_modules/copied"
    linked = workspace / "linked"
    symlinked = workspace / "symlinked"
    external_git = tmp_path / "external.git"
    create_git_repository(primary)
    create_git_repository(dependency)
    linked.mkdir(parents=True)
    (linked / ".git").write_text("gitdir: ../primary/.git/worktrees/linked\n", encoding="utf-8")
    symlinked.mkdir(parents=True)
    external_git.mkdir()
    (symlinked / ".git").symlink_to(external_git, target_is_directory=True)

    sources, requiring_initialization = backend.discover_context_candidates(
        (workspace,)
    )

    expected = (source(backend, primary, workspace),)
    assert (sources, requiring_initialization) == (expected, expected)


def test_discovery_deduplicates_repositories_under_overlapping_roots(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    group = workspace / "group"
    project = group / "project"
    create_git_repository(project)

    discovered = backend.discover_context_candidates((workspace, group))

    expected = (source(backend, project, workspace),)
    assert discovered == (expected, expected)


@pytest.mark.parametrize(
    "relative_path",
    (Path("docs"), Path("docs/context"), Path("docs/context/context.json")),
)
def test_discovery_does_not_offer_unsafe_context_path(
    backend: ModuleType,
    tmp_path: Path,
    relative_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    create_git_repository(project)
    unsafe_path = project / relative_path
    unsafe_path.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / (relative_path.name + "-outside")
    if relative_path.suffix:
        outside.write_text("{}", encoding="utf-8")
    else:
        outside.mkdir()
    unsafe_path.symlink_to(outside, target_is_directory=outside.is_dir())

    discovered = backend.discover_context_candidates((workspace,))

    assert discovered == ((), ())


def test_snapshot_token_remains_stable_after_approved_bootstrap(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    create_git_repository(project)
    before_sources, before_missing = backend.discover_context_candidates((workspace,))
    before_token = backend.snapshot_fingerprint(before_sources, (workspace,))

    write_context(project)
    after_sources, after_missing = backend.discover_context_candidates((workspace,))
    after_token = backend.snapshot_fingerprint(after_sources, (workspace,))

    assert (before_missing, after_missing, before_token == after_token) == (
        (source(backend, project, workspace),),
        (),
        True,
    )
