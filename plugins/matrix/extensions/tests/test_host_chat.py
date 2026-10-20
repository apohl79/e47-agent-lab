from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
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


def test_host_room_uses_short_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_chat.socket, "gethostname", lambda: "leda.example.org")
    agent = Mock()
    user = Mock()
    config = {"targetUserId": "@user:example.org"}
    monkeypatch.setattr(host_chat, "workspaces", lambda _client: {})
    monkeypatch.setattr(host_chat.HostChat, "_home_session", lambda _self: "home")
    monkeypatch.setattr(host_chat, "load_settings", lambda: {})
    monkeypatch.setattr(host_chat, "has_stored_config", lambda _raw: True)
    monkeypatch.setattr(host_chat, "stored_config", lambda _raw: config)
    monkeypatch.setattr(host_chat, "MatrixClient", Mock(side_effect=[agent, user]))
    monkeypatch.setattr(host_chat, "verify_access_token", Mock())
    ensure_room = Mock(return_value="!host:example.org")
    monkeypatch.setattr(host_chat, "ensure_room", ensure_room)

    host_chat.HostChat(Mock())

    ensure_room.assert_called_once_with(
        agent,
        config,
        "host:leda.example.org",
        "leda",
        display_name="leda",
    )
    user.join_room.assert_called_once_with("!host:example.org")


def test_session_search_matches_project_paths_not_titles_or_transcripts(
    tmp_path: Path,
) -> None:
    client = Mock()
    client.request.return_value = {
        "data": [
            {
                "id": "folder-match",
                "name": "Unrelated title",
                "cwd": str(tmp_path / "code" / "e47-agent-lab"),
                "updatedAt": 1_700_000_000,
            },
            {
                "id": "title-only",
                "name": "e47 appears only in the title",
                "cwd": str(tmp_path / "random"),
                "updatedAt": 1_700_000_001,
            },
            {
                "id": "codex",
                "name": "Session Name Generation",
                "cwd": str(tmp_path / "code" / "codex"),
                "updatedAt": 1_700_000_002,
            },
            {
                "id": "host-chat",
                "name": "Matrix host chat",
                "cwd": str(tmp_path / "code" / "e47-agent-lab"),
                "updatedAt": 1_700_000_003,
            },
            {
                "id": "outside",
                "name": "e47 elsewhere",
                "cwd": "/tmp/e47",
            },
        ],
        "nextCursor": None,
    }
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = client
    chat.roots = {"work": tmp_path}
    chat.thread_id = "host-chat"
    chat.matches = []
    result = chat.handle("/sessions e47")
    timestamp = host_chat.update_timestamp({"updatedAt": 1_700_000_000})
    assert result == (
        f"1 Unrelated title\n`  {timestamp}`\n`  work/code/e47-agent-lab`"
    )
    assert [row["id"] for row in chat.matches] == ["folder-match"]
    assert chat.handle("/attach 2") == "Use a number from the last /sessions result."
    client.request.assert_called_once_with(
        "thread/list",
        {
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "limit": 100,
        },
    )


def test_session_search_preserves_newest_first_order_across_pages(
    tmp_path: Path,
) -> None:
    client = Mock()
    client.request.side_effect = [
        {
            "data": [
                {
                    "id": "newest",
                    "name": "Zebra title",
                    "cwd": str(tmp_path / "e47-project-a"),
                    "updatedAt": 1_700_000_003,
                },
                {
                    "id": "unmatched",
                    "name": "Session Name Generation",
                    "cwd": str(tmp_path / "code" / "codex"),
                    "updatedAt": 1_700_000_004,
                },
            ],
            "nextCursor": "page-two",
        },
        {
            "data": [
                {
                    "id": "older",
                    "name": "Alpha title",
                    "cwd": str(tmp_path / "e47-project-b"),
                    "updatedAt": 1_700_000_001,
                }
            ],
            "nextCursor": None,
        },
    ]
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = client
    chat.roots = {"work": tmp_path}
    chat.thread_id = "host-chat"
    chat.matches = []

    result = chat.handle("/sess e47")

    assert [row["id"] for row in chat.matches] == ["newest", "older"]
    assert result.startswith("1 Zebra title\n")
    assert "\n2 Alpha title\n" in result
    assert client.request.call_args_list[0].args == (
        "thread/list",
        {
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "limit": 100,
        },
    )
    assert client.request.call_args_list[1].args == (
        "thread/list",
        {
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "limit": 100,
            "cursor": "page-two",
        },
    )


def test_session_titles_are_truncated_to_64_characters(tmp_path: Path) -> None:
    client = Mock()
    client.request.return_value = {
        "data": [
            {
                "id": "long-title",
                "name": "e47 " + "prompt " * 20,
                "cwd": str(tmp_path / "e47-project"),
                "updatedAt": 1_700_000_000,
            }
        ],
        "nextCursor": None,
    }
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = client
    chat.roots = {"work": tmp_path}
    chat.thread_id = "host-chat"
    chat.matches = []

    result = chat.handle("/sessions e47")

    title = result.splitlines()[0][2:]
    assert len(title) == 64
    assert title.endswith("…")
    assert chat.matches[0]["name"] == "e47 " + "prompt " * 20


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
                "updatedAt": 1_700_000_003,
            },
            {
                "id": "zulu",
                "name": "Zulu",
                "cwd": str(workspace / "project-a"),
                "updatedAt": 1_700_000_002,
            },
            {
                "id": "host-chat",
                "name": "Matrix Host Chat Session",
                "cwd": str(workspace / "project-a"),
                "updatedAt": 1_700_000_004,
            },
            {
                "id": "alpha",
                "name": "Alpha",
                "cwd": str(workspace / "project-a"),
                "updatedAt": 1_700_000_001,
            },
            {
                "id": "other",
                "name": "Alpha",
                "cwd": str(other_workspace / "project-a"),
                "updatedAt": 1_700_000_000,
            },
            {"id": "outside", "name": "Other", "cwd": "/tmp/outside"},
        ],
        "nextCursor": None,
    }
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = client
    chat.roots = {"work": workspace, "other": other_workspace}
    chat.thread_id = "host-chat"
    chat.matches = []

    result = chat.handle("/sessions")
    assert [row["id"] for row in chat.matches] == ["beta", "zulu", "alpha", "other"]
    assert result == (
        f"1 Beta\n`  {host_chat.update_timestamp({'updatedAt': 1_700_000_003})}`\n"
        "`  work/project-b`\n"
        f"2 Zulu\n`  {host_chat.update_timestamp({'updatedAt': 1_700_000_002})}`\n"
        "`  work/project-a`\n"
        f"3 Alpha\n`  {host_chat.update_timestamp({'updatedAt': 1_700_000_001})}`\n"
        "`  work/project-a`\n"
        f"4 Alpha\n`  {host_chat.update_timestamp({'updatedAt': 1_700_000_000})}`\n"
        "`  other/project-a`"
    )
    assert matrix.matrix_formatted_body(result) == (
        f"<p>1 Beta<br><code>  {host_chat.update_timestamp({'updatedAt': 1_700_000_003})}</code><br>"
        "<code>  work/project-b</code><br>"
        f"2 Zulu<br><code>  {host_chat.update_timestamp({'updatedAt': 1_700_000_002})}</code><br>"
        "<code>  work/project-a</code><br>"
        f"3 Alpha<br><code>  {host_chat.update_timestamp({'updatedAt': 1_700_000_001})}</code><br>"
        "<code>  work/project-a</code><br>"
        f"4 Alpha<br><code>  {host_chat.update_timestamp({'updatedAt': 1_700_000_000})}</code><br>"
        "<code>  other/project-a</code></p>"
    )
    client.request.assert_called_once_with(
        "thread/list",
        {"sortKey": "updated_at", "sortDirection": "desc", "limit": 100},
    )


def test_update_timestamp_uses_machine_local_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Etc/GMT-2")
    time.tzset()
    try:
        assert host_chat.update_timestamp({"updatedAt": 0}) == "1970-01-01 02:00:00 +02"
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


def test_attach_requires_approved_extension() -> None:
    client = Mock()
    client.request.return_value = {"commands": []}
    with pytest.raises(ValueError, match="not approved"):
        host_chat.extension_command(client, "thread-id", "on")
    client.request.assert_called_once_with("sessionExtension/list", {"threadId": "thread-id"})


def test_attach_resumes_without_loading_thread_history() -> None:
    client = Mock()
    client.request.side_effect = [
        {},
        {"commands": [{"extensionId": "matrix:matrix", "name": "matrix"}]},
        {},
    ]
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = client
    chat.matches = [{"id": "thread-id"}]

    assert chat.handle("/attach 1") == "Matrix enabled in that session."
    client.request.assert_any_call(
        "thread/resume", {"threadId": "thread-id", "excludeTurns": True}
    )


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
    (tmp_path / "project" / "nested-project").mkdir(parents=True)
    assert chat.handle("/list") == "work"
    assert chat.handle("/list work") == "project"
    assert chat.handle("/list work/project") == "nested-project"
    assert chat.handle("/list work/../project") == "Directory path must stay inside its workspace."
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


def test_short_command_prefixes_and_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    chat = host_chat.HostChat.__new__(host_chat.HostChat)
    chat.client = Mock()
    chat.client.request.return_value = {"thread": {"id": "new-thread"}}
    chat.roots = {"work": tmp_path}
    chat.thread_id = "home"
    chat.active_turn_id = None
    chat.matches = []
    enabled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        host_chat,
        "extension_command",
        lambda client, thread_id, action: enabled.append((thread_id, action)),
    )

    assert chat.handle("/l") == "work"
    assert chat.handle("/s") == "Ambiguous command. Use /sessions or /start."
    assert chat.handle("/st project") == "Started project and enabled Matrix."
    assert enabled == [("new-thread", "on")]
    chat.client.request.assert_called_once_with(
        "thread/start", {"cwd": str(project)}
    )
    assert chat.handle("/unknown").startswith("Unknown command")


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
        "thread/resume", {"threadId": "session", "excludeTurns": True}
    )
    chat.client.request.assert_any_call(
        "sessionExtension/command/invoke",
        {
            "threadId": "session",
            "extensionId": "matrix:matrix",
            "command": "matrix",
            "arguments": ["off"],
        },
    )


class StopLoop(Exception):
    pass


def test_main_without_app_server_waits_without_starting_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    trace: list[str] = []
    monkeypatch.setattr(host_chat, "CONTROL_SOCKET", tmp_path / "missing.sock")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: trace.append("spawn"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: trace.append("spawn"))

    def sleep(seconds: float) -> None:
        trace.append(f"sleep {seconds}")
        if len(trace) == 2:
            raise StopLoop

    monkeypatch.setattr(host_chat.time, "sleep", sleep)
    with pytest.raises(StopLoop):
        host_chat.main()
    assert trace == ["sleep 10", "sleep 10"]


def test_main_with_running_app_server_connects_to_control_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    monkeypatch.setattr(host_chat.SessionScriptClient, "connect_unix_socket", lambda path, timeout: client)
    monkeypatch.setattr(host_chat, "HostChat", Mock(side_effect=StopLoop))
    with pytest.raises(StopLoop):
        host_chat.main()
    client.initialize.assert_called_once_with(
        "matrix-host-chat", "Matrix host chat", host_chat.PLUGIN_VERSION
    )
