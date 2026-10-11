from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_chat
import matrix


def test_project_path_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = root / "project"
    project.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    roots = {"work": root}
    assert host_chat.project_path(roots, "work/project") == project
    assert host_chat.project_path(roots, "project") == project
    for argument in ("work/../outside", "work/escape", "/tmp", "unknown/project"):
        with pytest.raises(ValueError):
            host_chat.project_path(roots, argument)


def test_workspaces_use_host_config_only(tmp_path: Path) -> None:
    client = Mock()
    client.request.return_value = {
        "config": {"remote_agent": {"workspaces": {"work": str(tmp_path)}}},
        "layers": None,
    }
    assert host_chat.workspaces(client) == {"work": tmp_path}
    client.request.assert_called_once_with("config/read", {})
    client.request.return_value["layers"] = [{"name": "project"}]
    with pytest.raises(ValueError, match="project-layer"):
        host_chat.workspaces(client)


def test_session_search_numbers_only_approved_project_paths(tmp_path: Path) -> None:
    client = Mock()
    client.request.return_value = {
        "data": [
            {
                "thread": {
                    "id": "first",
                    "name": "First",
                    "cwd": str(tmp_path / "one"),
                    "updatedAt": 1_700_000_000,
                }
            },
            {"thread": {"id": "outside", "name": "Other", "cwd": "/tmp/outside"}},
        ],
        "nextCursor": None,
    }
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = client
    chat.roots = {"work": tmp_path}
    chat.matches = []
    result = chat.handle("/sessions first")
    assert result == "1 First (`2023-11-14 22:13:20 UTC`)\n`  work/one`"
    assert [row["id"] for row in chat.matches] == ["first"]
    assert chat.handle("/attach 2") == "Use a number from the last /sessions result."


def test_sessions_without_search_lists_all_approved_project_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    client = Mock()
    client.request.return_value = {
        "data": [
            {
                "id": "beta",
                "name": "Beta",
                "cwd": str(workspace / "project-b"),
                "updatedAt": 1_700_000_000,
            },
            {
                "id": "zulu",
                "name": "Zulu",
                "cwd": str(workspace / "project-a"),
                "updatedAt": 1_700_000_000,
            },
            {
                "id": "other",
                "name": "Alpha",
                "cwd": str(other_workspace / "project-a"),
                "updatedAt": 1_700_000_000,
            },
            {
                "id": "alpha",
                "name": "Alpha",
                "cwd": str(workspace / "project-a"),
                "updatedAt": 1_700_000_000,
            },
            {"id": "outside", "name": "Other", "cwd": "/tmp/outside"},
        ],
        "nextCursor": None,
    }
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = client
    chat.roots = {"work": workspace, "other": other_workspace}
    chat.matches = []

    result = chat.handle("/sessions")
    assert result == (
        "1 Alpha (`2023-11-14 22:13:20 UTC`)\n"
        "`  other/project-a`\n"
        "2 Alpha (`2023-11-14 22:13:20 UTC`)\n"
        "`  work/project-a`\n"
        "3 Zulu (`2023-11-14 22:13:20 UTC`)\n"
        "`  work/project-a`\n"
        "4 Beta (`2023-11-14 22:13:20 UTC`)\n"
        "`  work/project-b`"
    )
    assert matrix.matrix_formatted_body(result) == (
        "<p>1 Alpha (<code>2023-11-14 22:13:20 UTC</code>)<br>"
        "<code>  other/project-a</code><br>"
        "2 Alpha (<code>2023-11-14 22:13:20 UTC</code>)<br>"
        "<code>  work/project-a</code><br>"
        "3 Zulu (<code>2023-11-14 22:13:20 UTC</code>)<br>"
        "<code>  work/project-a</code><br>"
        "4 Beta (<code>2023-11-14 22:13:20 UTC</code>)<br>"
        "<code>  work/project-b</code></p>"
    )
    assert [row["id"] for row in chat.matches] == ["other", "alpha", "zulu", "beta"]
    client.request.assert_called_once_with(
        "thread/list",
        {"sortKey": "updated_at", "limit": 100},
    )


def test_attach_requires_approved_extension() -> None:
    client = Mock()
    client.request.return_value = {"commands": []}
    with pytest.raises(ValueError, match="not approved"):
        host_chat.extension_command(client, "thread-id", "on")
    client.request.assert_called_once_with("sessionExtension/list", {"threadId": "thread-id"})


def test_host_commands_and_regular_message(tmp_path: Path) -> None:
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = Mock()
    chat.client.request.return_value = {
        "data": [{"id": "home", "status": {"type": "idle"}}]
    }
    chat.roots = {"work": tmp_path}
    chat.thread_id = "home"
    chat.active_turn_id = None
    chat.matches = []
    (tmp_path / "project").mkdir()
    assert chat.handle("/list") == "work"
    assert chat.handle("/list work") == "project"
    assert chat.handle("/help") == host_chat.HELP
    assert chat.handle("/unknown").startswith("Unknown command")
    assert chat.handle("hello") is None
    method, payload = chat.client.request.call_args.args
    assert method == "turn/start"
    assert payload["threadId"] == "home"
    assert payload["input"] == [{"type": "text", "text": "hello"}]
    assert payload["clientUserMessageId"].startswith("matrix-")
    chat.active_turn_id = "turn-1"
    assert chat.handle("more") is None
    method, payload = chat.client.request.call_args.args
    assert method == "turn/steer"
    assert payload["expectedTurnId"] == "turn-1"


def test_detach_requests_disable_only_named_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_chat, "DETACH_ROOT", tmp_path)
    (tmp_path / "request.json").write_text('{"threadId":"session"}')
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = Mock()
    chat.client.request.side_effect = [
        {},
        {"commands": [{"extensionId": "matrix:matrix", "name": "matrix"}]},
        {},
    ]
    chat.thread_id = "home"
    chat.process_detach_requests()
    assert not (tmp_path / "request.json").exists()
    chat.client.request.assert_any_call(
        "sessionExtension/command/invoke",
        {
            "threadId": "session",
            "extensionId": "matrix:matrix",
            "command": "matrix",
            "arguments": ["off"],
        },
    )
