from __future__ import annotations

import json
from pathlib import Path

from scope_test_support import (
    configure_workspace,
    initialize,
    isolated_environment,
    run_context,
)


def add(repo: Path, env: dict[str, str], *arguments: str) -> None:
    added = run_context(*arguments, repo=repo, env=env)
    assert added.returncode == 0, added.stderr


def billing_workspace(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    env = isolated_environment(tmp_path / "xdg")
    workspace = tmp_path / "workspace"
    ledger, gateway, pending = (workspace / name for name in ("ledger", "gateway", "pending"))
    initialize(ledger, env)
    initialize(gateway, env)
    pending.mkdir()
    configure_workspace(env, workspace)
    add(
        ledger,
        env,
        "domain-set",
        "--domain",
        "billing",
        "--project",
        str(ledger),
        "--project",
        str(pending),
        "--remote",
        "git@github.com:acme/worker.git",
    )
    add(
        ledger,
        env,
        "add-pattern",
        "--name",
        "Gateway calls",
        "--summary",
        "ledger calls gateway for card payments",
    )
    add(gateway, env, "add-term", "--term", "PSP", "--definition", "Payment service provider")
    add(
        ledger,
        env,
        "add-term",
        "--term",
        "Invoice",
        "--definition",
        "Billable statement; ledger owns it",
        "--applicability",
        "domain:billing",
    )
    return env, workspace, ledger, gateway


def test_graph_exports_json_with_members_relationships_and_insights(
    tmp_path: Path,
) -> None:
    env, workspace, ledger, gateway = billing_workspace(tmp_path)
    exported = run_context("graph", "--format", "json", repo=ledger, env=env)
    graph = json.loads(exported.stdout)
    root = workspace.resolve()

    assert (
        exported.returncode,
        [(node["kind"], node["label"], node["status"]) for node in graph["nodes"]],
        [
            (edge["source"], edge["target"], edge["relation"], edge["confidence"])
            for edge in graph["edges"]
        ],
        graph["view"],
        graph["insights"]["hubs"],
        graph["insights"]["domain_coverage"],
    ) == (
        0,
        [
            ("domain", "billing", "declared"),
            ("project", "gateway", "initialized"),
            ("project", "ledger", "initialized"),
            ("project", "pending", "uninitialized"),
            ("project", "worker", "remote-only"),
        ],
        [
            ("domain:billing", f"project:{root}/ledger", "owns", 0.9),
            (f"project:{root}/ledger", "domain:billing", "member_of", 1.0),
            (f"project:{root}/ledger", f"project:{root}/gateway", "integrates_with", 0.9),
            (f"project:{root}/pending", "domain:billing", "member_of", 1.0),
            ("remote:github.com/acme/worker", "domain:billing", "member_of", 1.0),
        ],
        {
            "kind": "overview",
            "focus": "",
            "depth": 1,
            "level": "projects",
            "min_confidence": 0.0,
            "relations": [],
        },
        [
            {"label": "ledger", "degree": 2},
            {"label": "domain:billing", "degree": 1},
            {"label": "gateway", "degree": 1},
        ],
        [
            {
                "id": "billing",
                "members": 3,
                "member_statuses": {"initialized": 1, "remote-only": 1, "uninitialized": 1},
                "records": 1,
                "internal_edges": 0,
                "isolated_members": [],
            }
        ],
    )


def test_graph_text_views_focus_on_repo_domain_and_output_file(tmp_path: Path) -> None:
    env, workspace, ledger, gateway = billing_workspace(tmp_path)
    overview = run_context("graph", repo=gateway, env=env)
    focused = run_context(
        "graph", "--project", "--min-confidence", "0.95", repo=gateway, env=env
    )
    domain = run_context("graph", "--domain", "billing", "--format", "json", repo=gateway, env=env)
    written = run_context(
        "graph", "--output", str(tmp_path / "out/graph.txt"), repo=gateway, env=env
    )
    unknown = run_context("graph", "--domain", "ops", repo=gateway, env=env)
    detailed = run_context(
        "graph", "--domain", "billing", "--level", "records", repo=gateway, env=env
    )

    assert (
        overview.returncode,
        overview.stdout.splitlines()[:3],
        focused.stdout.splitlines()[0],
        [node["label"] for node in json.loads(domain.stdout)["nodes"]],
        written.stdout,
        (tmp_path / "out/graph.txt").read_text(encoding="utf-8") == overview.stdout,
        (unknown.returncode, unknown.stderr.strip()),
        detailed.stdout.splitlines()[-1],
    ) == (
        0,
        [
            "Knowledge graph (overview, depth 1, level projects): 4 projects (2 initialized, "
            "1 remote-only, 1 uninitialized), 1 domains, 0 universal stores; 3 records; "
            "5 edges (1 integrates_with, 3 member_of, 1 owns).",
            "Hubs: ledger 2, domain:billing 1, gateway 1",
            "Most referenced: gateway 1, ledger 1",
        ],
        "Knowledge graph (project gateway, depth 1, level projects): 1 projects "
        "(1 initialized), 0 domains, 0 universal stores; 1 records; 0 edges (none).",
        ["billing", "ledger", "pending", "worker"],
        f"Knowledge graph written to {tmp_path / 'out/graph.txt'}\n",
        True,
        (1, "Knowledge graph view failed: unknown domain 'ops'"),
        "Records: 2 nodes; record edges (1 owns); 1 without record edges; "
        "most mentioned: none",
    )
