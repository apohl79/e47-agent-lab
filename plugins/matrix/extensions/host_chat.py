#!/usr/bin/env python3
"""Always-on Matrix room for a local Xedoc app-server."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import socket
import sys
import threading
import time
from typing import Any

from matrix import (
    CONFIG_ROOT,
    DETACH_ROOT,
    MatrixClient,
    MatrixError,
    PLUGIN_VERSION,
    ensure_room,
    events_for_sync,
    has_stored_config,
    inbound_messages,
    load_settings,
    native_user_message_text,
    stored_config,
    verify_access_token,
    write_private_json,
)
from session_script_sdk import RpcError, SessionScriptClient


HOST_STATE = CONFIG_ROOT / "host-chat.json"
COMMANDS = ("/help", "/list", "/sessions", "/attach", "/detach", "/start")
CONTROL_SOCKET = Path(
    os.environ.get(
        "XEDOC_APP_SERVER_CONTROL_SOCKET",
        Path(os.environ.get("XEDOC_HOME", Path.home() / ".xedoc"))
        / "app-server-control"
        / "app-server-control.sock",
    )
)
HELP = "\n".join(
    [
        "Host chat commands:",
        "/help — show commands",
        "/list — list approved workspaces",
        "/list <workspace> — list project directories",
        "/sessions [project-path filter] — list matching sessions newest first",
        "/attach <num> — enable Matrix in a listed session",
        "/detach — disable Matrix in this session",
        "/start <project dir> — create and enable a session (prefix workspace when needed)",
    ]
)
MAX_SESSION_TITLE_LENGTH = 64


def workspaces(client: SessionScriptClient) -> dict[str, Path]:
    result = client.request("config/read", {})
    if result.get("layers") or result.get("origin"):
        raise ValueError("project-layer configuration cannot authorize workspaces")
    config = result.get("config", result)
    remote = config.get("remote_agent") if isinstance(config, dict) else None
    raw = remote.get("workspaces") if isinstance(remote, dict) else None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("invalid remote-agent workspace configuration")
    result_roots: dict[str, Path] = {}
    for name, root in raw.items():
        if not isinstance(name, str) or not isinstance(root, str):
            raise ValueError("invalid workspace configuration")
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("workspace root must be absolute")
        result_roots[name] = path.resolve()
    return result_roots


def project_path(roots: dict[str, Path], text: str) -> Path:
    workspace, separator, relative = text.partition("/")
    if workspace not in roots and len(roots) == 1:
        workspace, relative = next(iter(roots)), text
        separator = "/"
    if not separator or workspace not in roots or not relative or relative in {".", "/"}:
        raise ValueError("Use /start <workspace>/<project dir>.")
    if "\\" in relative or "\x00" in relative or ".." in Path(relative).parts:
        raise ValueError("Project path must stay inside its workspace.")
    path = (roots[workspace] / relative).resolve()
    if not path.is_relative_to(roots[workspace]) or not path.is_dir():
        raise ValueError("Project directory does not exist in that workspace.")
    return path


def workspace_project(roots: dict[str, Path], cwd: str) -> str | None:
    path = Path(cwd).resolve()
    for workspace, root in roots.items():
        if path.is_relative_to(root):
            relative = path.relative_to(root)
            return f"{workspace}/{relative}" if relative.parts else f"{workspace}/."
    return None


def session_name(row: dict[str, Any]) -> str:
    return row.get("name") or row.get("preview") or row["id"]


def display_session_name(row: dict[str, Any]) -> str:
    name = session_name(row)
    if len(name) > MAX_SESSION_TITLE_LENGTH:
        return f"{name[: MAX_SESSION_TITLE_LENGTH - 1]}…"
    return name


def update_timestamp(row: dict[str, Any]) -> str:
    updated_at = row.get("updatedAt")
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        return "unknown"
    try:
        return datetime.fromtimestamp(updated_at).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    except (OverflowError, OSError, ValueError):
        return "unknown"


def session_rows(client: SessionScriptClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        page = client.request("thread/list", params)
        data = page.get("data")
        if not isinstance(data, list):
            raise RpcError("invalid session list response")
        rows.extend(row for row in data if isinstance(row, dict))
        cursor = page.get("nextCursor")
        if not isinstance(cursor, str) or not cursor:
            break
    return rows


def extension_command(client: SessionScriptClient, thread_id: str, action: str) -> None:
    commands = client.request("sessionExtension/list", {"threadId": thread_id}).get("commands")
    if not isinstance(commands, list) or not any(
        isinstance(command, dict)
        and command.get("extensionId") == "matrix:matrix"
        and command.get("name") == "matrix"
        for command in commands
    ):
        raise ValueError("Matrix is not approved in this session. Approve/setup it in Xedoc first.")
    client.request(
        "sessionExtension/command/invoke",
        {
            "threadId": thread_id,
            "extensionId": "matrix:matrix",
            "command": "matrix",
            "arguments": [action],
        },
    )


def resume_session(client: SessionScriptClient, thread_id: str) -> dict[str, Any]:
    return client.request(
        "thread/resume", {"threadId": thread_id, "excludeTurns": True}
    )


class HostChat:
    def __init__(self, client: SessionScriptClient) -> None:
        self.client = client
        self.roots = workspaces(client)
        self.matches: list[dict[str, Any]] = []
        self.thread_id = self._home_session()
        self.active_turn_id: str | None = None
        raw = load_settings()
        if not has_stored_config(raw):
            raise ValueError("Matrix is not configured. Run /matrix setup in Xedoc.")
        config = stored_config(raw)
        self.agent = MatrixClient(raw)
        self.user = MatrixClient(raw, role="user")
        verify_access_token(config, self.agent)
        verify_access_token(config, self.user, "targetUserId")
        hostname = socket.gethostname()
        short_hostname = hostname.split(".", 1)[0]
        self.room_id = ensure_room(
            self.agent,
            config,
            f"host:{hostname}",
            short_hostname,
            display_name=short_hostname,
        )
        self.user.join_room(self.room_id)
        self.target_user_id = config["targetUserId"]
        self.ignored_events: set[str] = set()
        self.ignored_events_lock = threading.Lock()
        self.inbound: queue.Queue[str] = queue.Queue()
        self.received_ids: deque[str] = deque(maxlen=1000)
        self.received_ids_set: set[str] = set()
        self.client.set_notification_handler(self.on_notification)

    def _home_session(self) -> str:
        try:
            state = json.loads(HOST_STATE.read_text())
            thread_id = state.get("threadId")
            if isinstance(thread_id, str) and thread_id:
                resume_session(self.client, thread_id)
                return thread_id
        except (OSError, ValueError, RpcError):
            pass
        result = self.client.request("thread/start", {"cwd": str(Path.home())})
        thread = result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise RpcError("home session start omitted thread id")
        write_private_json(HOST_STATE, {"threadId": thread_id})
        return thread_id

    def on_notification(self, notification: dict[str, Any]) -> None:
        params = notification.get("params")
        if isinstance(params, dict) and params.get("threadId") == self.thread_id:
            method = notification.get("method")
            if method == "turn/started":
                turn = params.get("turn")
                if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                    self.active_turn_id = turn["id"]
            elif method == "turn/completed":
                self.active_turn_id = None
        if notification.get("method") != "item/completed":
            return
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
            return
        item = params.get("item")
        if isinstance(item, dict):
            if item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text:
                    self.agent.send_text(self.room_id, text)
            else:
                text = native_user_message_text(item)
                if text:
                    with self.ignored_events_lock:
                        event_id = self.user.send_text(self.room_id, text)
                        if event_id:
                            self.ignored_events.add(event_id)

    def handle(self, message: str) -> str | None:
        command, _, argument = message.strip().partition(" ")
        argument = argument.strip()
        if command.startswith("/"):
            matches = [candidate for candidate in COMMANDS if candidate.startswith(command)]
            if len(matches) == 1:
                command = matches[0]
            elif len(matches) > 1:
                return f"Ambiguous command. Use {' or '.join(matches)}."
        if command == "/help":
            return HELP
        if command == "/list":
            if not argument:
                return "\n".join(self.roots) or "No approved workspaces."
            workspace, separator, relative = argument.partition("/")
            root = self.roots.get(workspace)
            if root is None:
                return "Unknown workspace."
            directory = root
            if separator:
                if (
                    not relative
                    or "\\" in relative
                    or "\x00" in relative
                    or ".." in Path(relative).parts
                ):
                    return "Directory path must stay inside its workspace."
                directory = (root / relative).resolve()
                if not directory.is_relative_to(root) or not directory.is_dir():
                    return "Directory does not exist in that workspace."
            return "\n".join(
                sorted(
                    item.name
                    for item in directory.iterdir()
                    if item.is_dir() and not item.is_symlink()
                )
            ) or "No project directories."
        if command == "/sessions":
            search = argument.casefold()
            self.matches = []
            for row in session_rows(self.client):
                cwd = row.get("cwd")
                if row.get("id") == self.thread_id or not isinstance(cwd, str):
                    continue
                project = workspace_project(self.roots, cwd)
                if project is not None and (not search or search in project.casefold()):
                    self.matches.append(row)
            return "\n".join(
                f"{number} {display_session_name(row)}\n"
                f"`  {update_timestamp(row)}`\n"
                f"`  {workspace_project(self.roots, row['cwd'])}`"
                for number, row in enumerate(self.matches, 1)
            ) or "No resumable sessions found."
        if command == "/attach":
            if not argument.isdecimal() or not 1 <= int(argument) <= len(self.matches):
                return "Use a number from the last /sessions result."
            thread_id = self.matches[int(argument) - 1]["id"]
            resume_session(self.client, thread_id)
            extension_command(self.client, thread_id, "on")
            return "Matrix enabled in that session."
        if command == "/detach":
            return "The host chat stays connected. Send /detach in a session room."
        if command == "/start":
            path = project_path(self.roots, argument)
            result = self.client.request("thread/start", {"cwd": str(path)})
            thread_id = result["thread"]["id"]
            extension_command(self.client, thread_id, "on")
            return f"Started {path.name} and enabled Matrix."
        if command.startswith("/"):
            return "Unknown command. Send /help for available commands."
        payload = {
            "threadId": self.thread_id,
            "clientUserMessageId": f"matrix-{os.urandom(8).hex()}",
            "input": [{"type": "text", "text": message}],
        }
        if self.active_turn_id:
            payload["expectedTurnId"] = self.active_turn_id
            self.client.request("turn/steer", payload)
        else:
            result = self.client.request("turn/start", payload)
            turn = result.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self.active_turn_id = turn["id"]
        return None

    def run(self) -> None:
        initial = self.user.sync(self.room_id, None, 0)
        cursor = initial.get("next_batch")
        if not isinstance(cursor, str):
            raise MatrixError("Matrix initial sync omitted next_batch")
        self.agent.send_text(self.room_id, "Host chat connected. Send /help for commands.")

        def receive() -> None:
            nonlocal cursor
            while True:
                try:
                    batch = self.user.sync(self.room_id, cursor, 25_000)
                    events = []
                    for event in events_for_sync(self.user, batch, self.room_id, cursor):
                        event_id = event.get("event_id")
                        if isinstance(event_id, str):
                            if event_id in self.received_ids_set:
                                continue
                            if len(self.received_ids) == self.received_ids.maxlen:
                                self.received_ids_set.discard(self.received_ids.popleft())
                            self.received_ids.append(event_id)
                            self.received_ids_set.add(event_id)
                        events.append(event)
                    with self.ignored_events_lock:
                        messages = inbound_messages(
                            events, self.target_user_id, self.ignored_events
                        )
                    for body in messages:
                        self.inbound.put(body)
                    next_cursor = batch.get("next_batch")
                    if not isinstance(next_cursor, str):
                        raise MatrixError("Matrix sync omitted next_batch")
                    cursor = next_cursor
                except MatrixError:
                    time.sleep(5)

        threading.Thread(target=receive, daemon=True).start()
        while True:
            self.process_detach_requests()
            try:
                message = self.inbound.get(timeout=1)
            except queue.Empty:
                self.client.request(
                    "config/read", {}
                )
                continue
            try:
                reply = self.handle(message)
            except (RpcError, MatrixError, OSError, ValueError, KeyError) as error:
                reply = f"Host chat: {error}"
            if reply:
                self.agent.send_text(self.room_id, reply)

    def process_detach_requests(self) -> None:
        if not DETACH_ROOT.is_dir():
            return
        for path in DETACH_ROOT.glob("*.json"):
            try:
                request = json.loads(path.read_text())
                thread_id = request.get("threadId")
                if isinstance(thread_id, str) and thread_id != self.thread_id:
                    resume_session(self.client, thread_id)
                    extension_command(self.client, thread_id, "off")
                    path.unlink()
            except (OSError, ValueError, RpcError, KeyError):
                continue


def connect_app_server() -> SessionScriptClient | None:
    try:
        return SessionScriptClient.connect_unix_socket(CONTROL_SOCKET, 30)
    except (OSError, RpcError):
        return None


def main() -> int:
    idle_reported = False
    while True:
        client = connect_app_server()
        if client is None:
            if not idle_reported:
                print("Matrix host chat idle: Xedoc app-server is not running", file=sys.stderr, flush=True)
                idle_reported = True
            time.sleep(10)
            continue
        idle_reported = False
        try:
            client.initialize("matrix-host-chat", "Matrix host chat", PLUGIN_VERSION)
            HostChat(client).run()
        except (OSError, RpcError, MatrixError, ValueError) as error:
            print(f"Matrix host chat waiting: {error}", file=sys.stderr, flush=True)
            time.sleep(10)
        finally:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
