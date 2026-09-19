#!/usr/bin/env python3
"""Bridge one Xedoc root thread to a private Matrix room used from Element.

The Xedoc plugin host invokes this file in one-shot setup/command mode and as
a persistent session child. Setup stores the Matrix homeserver, the agent
account and access token, and the target account under
``~/.config/xedoc/matrix``.
"""

from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import select
import sys
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import build_opener, HTTPRedirectHandler, Request

try:
    from session_script_sdk import RpcError, SessionScriptClient
except ModuleNotFoundError:
    # Keep the extension standalone while reusing the SDK from the Xedoc
    # checkout. Set XEDOC_SESSION_SCRIPT_SDK when the SDK lives elsewhere.
    sdk_path = os.environ.get(
        "XEDOC_SESSION_SCRIPT_SDK",
        str(Path.home() / "workspace" / "code" / "codex" / "scripts"),
    )
    sys.path.insert(0, sdk_path)
    from session_script_sdk import RpcError, SessionScriptClient


PROTOCOL = "xedoc.script/v1"
PLUGIN_VERSION = "0.1.0"
LEGACY_CONFIG_ROOT = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "xedoc"
    / "matrix"
)
CONFIG_ROOT = (
    Path(os.environ.get("XEDOC_HOME", Path.home() / ".xedoc"))
    / "extensions"
    / "matrix"
)
CONFIG_PATH = CONFIG_ROOT / "config.json"
ROOMS_ROOT = CONFIG_ROOT / "rooms"
LEGACY_CONFIG_PATH = LEGACY_CONFIG_ROOT / "config.json"
LEGACY_ROOMS_ROOT = LEGACY_CONFIG_ROOT / "rooms"
MATRIX_API_PREFIX = "/_matrix/client/v3"
MAX_RESPONSE_BYTES = 1 << 20
SYNC_TIMEOUT_MS = 25_000
MAX_BACKFILL_PAGES = 100


class MatrixError(RuntimeError):
    """A Matrix homeserver request failed or returned an invalid response."""


class RejectRedirectHandler(HTTPRedirectHandler):
    """Never replay a bearer-authenticated request at a redirect target."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def action(action_id: str, label: str) -> dict[str, Any]:
    return {
        "id": action_id,
        "opens": None,
        "hostAction": None,
        "label": label,
        "keyBindings": [],
        "context": None,
        "value": None,
    }


def response(request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "requestId": request["requestId"],
        "result": result,
    }


def complete(request: dict[str, Any], summary: str) -> dict[str, Any]:
    return response(request, {"kind": "complete", "summary": summary})


def setup_interaction(
    request: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    saved_token = bool(config.get("accessToken"))
    token_description = (
        "Leave blank to keep the saved token."
        if saved_token
        else "Create an access token for the agent account in Element."
    )
    return response(
        request,
        {
            "kind": "interaction",
            "interaction": {
                "id": "matrix-setup",
                "continuation": "matrix-setup",
                "stateRevision": "1",
                "surface": {
                    "type": "form",
                    "id": "matrix-settings",
                    "title": "Configure Matrix (Element) bridge",
                    "subtitle": (
                        "The agent account creates one private room per Xedoc session "
                        "and invites the target account."
                    ),
                    "fields": [
                        {
                            "type": "text",
                            "id": "homeserver",
                            "label": "Matrix homeserver",
                            "description": "HTTPS base URL, for example https://matrix.org.",
                            "value": str(config.get("homeserver") or ""),
                            "maxBytes": 512,
                            "sensitive": False,
                        },
                        {
                            "type": "text",
                            "id": "agent-user-id",
                            "label": "Agent Matrix account",
                            "description": "Full Matrix ID used by the agent, such as @xedoc:example.org.",
                            "value": str(config.get("agentUserId") or ""),
                            "maxBytes": 255,
                            "sensitive": False,
                        },
                        {
                            "type": "text",
                            "id": "access-token",
                            "label": "Agent access token",
                            "description": token_description,
                            "value": "",
                            "maxBytes": 4096,
                            "sensitive": True,
                        },
                        {
                            "type": "text",
                            "id": "target-user-id",
                            "label": "Target Matrix account",
                            "description": "Full Matrix ID of the Element user invited to each room.",
                            "value": str(config.get("targetUserId") or ""),
                            "maxBytes": 255,
                            "sensitive": False,
                        },
                    ],
                    "submit": action("save", "Save"),
                    "cancel": None,
                },
            },
        },
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_settings() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return load_json(CONFIG_PATH)
    legacy = load_json(LEGACY_CONFIG_PATH)
    if legacy:
        write_private_json(CONFIG_PATH, legacy)
    return legacy


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def matrix_user_id(value: str) -> bool:
    if not value.startswith("@") or any(character.isspace() for character in value):
        return False
    localpart, separator, server = value[1:].partition(":")
    return bool(localpart and separator and server)


def normalize_config(
    values: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, str]:
    existing = existing or {}
    homeserver = str(values.get("homeserver") or "").strip().rstrip("/")
    agent_user_id = str(values.get("agent-user-id") or "").strip()
    target_user_id = str(values.get("target-user-id") or "").strip()
    access_token = str(values.get("access-token") or "").strip()
    if not access_token:
        access_token = str(existing.get("accessToken") or "").strip()

    parsed = urlparse(homeserver)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("homeserver must be an HTTPS base URL")
    if not matrix_user_id(agent_user_id):
        raise ValueError("agent account must be a full Matrix user ID")
    if not matrix_user_id(target_user_id):
        raise ValueError("target account must be a full Matrix user ID")
    if agent_user_id == target_user_id:
        raise ValueError("agent and target accounts must be different")
    if not access_token:
        raise ValueError("agent access token is required")
    return {
        "homeserver": homeserver,
        "agentUserId": agent_user_id,
        "accessToken": access_token,
        "targetUserId": target_user_id,
    }


def stored_config(value: dict[str, Any]) -> dict[str, str]:
    return normalize_config(
        {
            "homeserver": value.get("homeserver"),
            "agent-user-id": value.get("agentUserId"),
            "access-token": value.get("accessToken"),
            "target-user-id": value.get("targetUserId"),
        }
    )


def debug_enabled(value: dict[str, Any]) -> bool:
    return value.get("debug") is True


def current_debug_setting() -> bool:
    return debug_enabled(load_settings())


def has_stored_config(value: dict[str, Any]) -> bool:
    try:
        stored_config(value)
    except ValueError:
        return False
    return True


def verify_access_token(
    config: dict[str, Any], client: MatrixClient | None = None
) -> None:
    authenticated_user = (client or MatrixClient(config)).whoami()
    if authenticated_user != config["agentUserId"]:
        raise MatrixError(
            "the configured access token belongs to "
            f"{authenticated_user}, not {config['agentUserId']}"
        )


class MatrixClient:
    """Minimal Matrix Client-Server API client with bounded responses."""

    def __init__(self, config: dict[str, Any], opener: Any | None = None) -> None:
        self.homeserver = str(config["homeserver"]).rstrip("/")
        self.access_token = str(config["accessToken"])
        self.opener = opener or build_opener(RejectRedirectHandler())

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        url = f"{self.homeserver}{MATRIX_API_PREFIX}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else None
        )
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=timeout) as opened:
                raw = opened.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            try:
                message = json.loads(detail).get("error", detail)
            except json.JSONDecodeError:
                message = detail
            raise MatrixError(
                f"Matrix request failed ({error.code}): {str(message)[:500]}"
            ) from error
        except URLError as error:
            raise MatrixError(f"Matrix request failed: {error.reason}") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MatrixError("Matrix response exceeds the size limit")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise MatrixError("Matrix returned invalid JSON") from error
        if not isinstance(value, dict):
            raise MatrixError("Matrix returned a non-object response")
        return value

    def whoami(self) -> str:
        result = self.request("GET", "/account/whoami")
        user_id = result.get("user_id")
        if not isinstance(user_id, str):
            raise MatrixError("Matrix whoami response did not include user_id")
        return user_id

    def create_room(self, title: str, target_user_id: str) -> str:
        result = self.request(
            "POST",
            "/createRoom",
            {
                "invite": [target_user_id],
                "is_direct": True,
                "name": room_name(title),
                "preset": "trusted_private_chat",
                "topic": "Private Xedoc session bridge used from Element.",
            },
        )
        room_id = result.get("room_id")
        if not isinstance(room_id, str) or not room_id:
            raise MatrixError("Matrix createRoom response did not include room_id")
        return room_id

    def update_room_name(self, room_id: str, title: str) -> None:
        self.request(
            "PUT",
            f"/rooms/{quote(room_id, safe='')}/state/m.room.name",
            {"name": room_name(title)},
        )

    def send_text(self, room_id: str, text: str) -> None:
        transaction_id = f"xedoc-{os.urandom(12).hex()}"
        self.request(
            "PUT",
            (
                f"/rooms/{quote(room_id, safe='')}/send/m.room.message/"
                f"{transaction_id}"
            ),
            {"msgtype": "m.text", "body": text},
        )

    def sync(
        self, room_id: str, since: str | None, timeout_ms: int
    ) -> dict[str, Any]:
        room_filter = {
            "room": {
                "rooms": [room_id],
                "timeline": {"types": ["m.room.message"], "limit": 20},
                "state": {"types": []},
                "ephemeral": {"types": []},
                "account_data": {"types": []},
            },
            "presence": {"types": []},
        }
        query: dict[str, str | int] = {
            "filter": json.dumps(room_filter, separators=(",", ":")),
            "timeout": timeout_ms,
        }
        if since:
            query["since"] = since
        return self.request(
            "GET",
            "/sync",
            query=query,
            timeout=max(10, timeout_ms / 1000 + 10),
        )

    def backfill(
        self, room_id: str, from_token: str, to_token: str
    ) -> list[dict[str, Any]]:
        cursor = from_token
        chronological: list[dict[str, Any]] = []
        visited: set[str] = set()
        for _ in range(MAX_BACKFILL_PAGES):
            if cursor in visited:
                raise MatrixError("Matrix room history pagination repeated a token")
            visited.add(cursor)
            result = self.request(
                "GET",
                f"/rooms/{quote(room_id, safe='')}/messages",
                query={
                    "dir": "f",
                    "from": cursor,
                    "to": to_token,
                    "limit": 100,
                },
            )
            chunk = result.get("chunk")
            if not isinstance(chunk, list):
                raise MatrixError("Matrix room history response omitted chunk")
            chronological.extend(
                event for event in chunk if isinstance(event, dict)
            )
            next_cursor = result.get("end")
            if next_cursor is None or next_cursor == to_token:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MatrixError("Matrix room history returned an invalid end")
            cursor = next_cursor
        else:
            raise MatrixError("Matrix room history exceeded the pagination limit")
        return chronological


def room_path(thread_id: str) -> Path:
    safe_id = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return ROOMS_ROOT / f"{safe_id}.json"


def legacy_room_path(thread_id: str) -> Path:
    safe_id = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return LEGACY_ROOMS_ROOT / f"{safe_id}.json"


def load_room_binding(thread_id: str) -> dict[str, Any]:
    path = room_path(thread_id)
    if path.exists():
        return load_json(path)
    legacy = load_json(legacy_room_path(thread_id))
    if legacy:
        write_private_json(path, legacy)
    return legacy


def room_name(title: str) -> str:
    return f"Xedoc: {title}"[:255]


def ensure_room(
    client: MatrixClient,
    config: dict[str, Any],
    thread_id: str,
    title: str,
    lifecycle: Callable[[str], None] | None = None,
) -> str:
    path = room_path(thread_id)
    stored = load_room_binding(thread_id)
    room_id = stored.get("roomId")
    binding = {
        "homeserver": config["homeserver"],
        "agentUserId": config["agentUserId"],
        "targetUserId": config["targetUserId"],
    }
    if (
        isinstance(room_id, str)
        and room_id
        and all(stored.get(key) == value for key, value in binding.items())
    ):
        if stored.get("title") != title:
            client.update_room_name(room_id, title)
            write_private_json(path, {"roomId": room_id, "title": title, **binding})
            if lifecycle:
                lifecycle(f"Matrix room renamed to {room_name(title)!r}.")
        return room_id
    room_id = client.create_room(title or thread_id, str(config["targetUserId"]))
    write_private_json(path, {"roomId": room_id, "title": title, **binding})
    if lifecycle:
        lifecycle(f"Matrix room created as {room_name(title or thread_id)!r}.")
    return room_id


def room_timeline(sync: dict[str, Any], room_id: str) -> dict[str, Any]:
    rooms = sync.get("rooms")
    joined = rooms.get("join") if isinstance(rooms, dict) else {}
    room = joined.get(room_id) if isinstance(joined, dict) else {}
    timeline = room.get("timeline") if isinstance(room, dict) else {}
    return timeline if isinstance(timeline, dict) else {}


def unique_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen:
                continue
            seen.add(event_id)
        unique.append(event)
    return unique


def events_for_sync(
    client: MatrixClient,
    sync: dict[str, Any],
    room_id: str,
    prior_token: str,
) -> list[dict[str, Any]]:
    timeline = room_timeline(sync, room_id)
    raw_events = timeline.get("events")
    events = (
        [event for event in raw_events if isinstance(event, dict)]
        if isinstance(raw_events, list)
        else []
    )
    if timeline.get("limited") is True:
        previous_batch = timeline.get("prev_batch")
        if not isinstance(previous_batch, str) or not previous_batch:
            raise MatrixError("limited Matrix timeline omitted prev_batch")
        events = client.backfill(room_id, prior_token, previous_batch) + events
    return unique_events(events)


def inbound_messages(
    events: list[dict[str, Any]], target_user_id: str
) -> list[str]:
    messages: list[str] = []
    for event in events:
        if (
            event.get("type") != "m.room.message"
            or event.get("sender") != target_user_id
        ):
            continue
        content = event.get("content")
        if not isinstance(content, dict) or content.get("msgtype") != "m.text":
            continue
        relates_to = content.get("m.relates_to")
        if isinstance(relates_to, dict) and relates_to.get("rel_type") == "m.replace":
            continue
        body = content.get("body")
        if isinstance(body, str) and body.strip():
            messages.append(body)
    return messages


def native_user_message_text(item: dict[str, Any]) -> str | None:
    if item.get("type") != "userMessage":
        return None
    client_id = item.get("clientId")
    if isinstance(client_id, str) and client_id.startswith("matrix-"):
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    text = "".join(
        fragment["text"]
        for fragment in content
        if isinstance(fragment, dict)
        and fragment.get("type") == "text"
        and isinstance(fragment.get("text"), str)
    )
    return text if text.strip() else None


def completed_agent_message_text(item: dict[str, Any]) -> str | None:
    if item.get("type") != "agentMessage":
        return None
    text = item.get("text")
    return text if isinstance(text, str) and text.strip() else None


def receive_messages(
    client: MatrixClient,
    room_id: str,
    target_user_id: str,
    since: str,
    messages: queue.Queue[str],
    host_messages: queue.Queue[tuple[str, str]],
    debug: Callable[[], bool],
    stop: threading.Event,
) -> None:
    last_error: str | None = None
    while not stop.is_set():
        try:
            result = client.sync(room_id, since, SYNC_TIMEOUT_MS)
            events = events_for_sync(client, result, room_id, since)
        except MatrixError as error:
            detail = f"Matrix sync failed: {str(error)[:500]}"
            if detail != last_error:
                host_messages.put(("warning", detail))
                last_error = detail
            stop.wait(1)
            continue
        next_batch = result.get("next_batch")
        if not isinstance(next_batch, str) or not next_batch:
            detail = "Matrix sync response did not include next_batch."
            if detail != last_error:
                host_messages.put(("warning", detail))
                last_error = detail
            stop.wait(1)
            continue
        if last_error is not None and debug():
            host_messages.put(("info", "Matrix sync connection re-established."))
        last_error = None
        for message in inbound_messages(events, target_user_id):
            messages.put(message)
        since = next_batch


def prompt_message(prompt: dict[str, Any], token: str) -> str:
    request = prompt.get("request")
    params = request.get("params") if isinstance(request, dict) else {}
    questions = params.get("questions") if isinstance(params, dict) else []
    lines = ["Xedoc needs your answer:"]
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        question_id = question.get("id")
        text = question.get("question")
        lines.append(f"- {question_id}: {text}")
        options = question.get("options")
        if isinstance(options, list):
            labels = [
                option.get("label")
                for option in options
                if isinstance(option, dict) and isinstance(option.get("label"), str)
            ]
            if labels:
                lines.append(f"  Choices: {', '.join(labels)}")
    lines.append(f'Reply as `prompt:{token} {{"question-id":["answer"]}}`.')
    return "\n".join(lines)


def prompt_answer(prompt: dict[str, Any], text: str) -> dict[str, Any] | None:
    request = prompt.get("request")
    params = request.get("params") if isinstance(request, dict) else {}
    questions = params.get("questions") if isinstance(params, dict) else []
    questions = [question for question in questions if isinstance(question, dict)]
    if not questions:
        return None
    try:
        raw_answers = json.loads(text)
    except json.JSONDecodeError:
        raw_answers = None
    if isinstance(raw_answers, dict):
        answers: dict[str, dict[str, list[str]]] = {}
        for question in questions:
            question_id = question.get("id")
            values = raw_answers.get(question_id)
            if not isinstance(question_id, str):
                return None
            if isinstance(values, str):
                values = [values]
            if not (
                isinstance(values, list)
                and values
                and all(isinstance(value, str) for value in values)
            ):
                return None
            answers[question_id] = {"answers": values}
        return {"kind": "requestUserInput", "answers": answers}
    if len(questions) == 1 and isinstance(questions[0].get("id"), str):
        return {
            "kind": "requestUserInput",
            "answers": {questions[0]["id"]: {"answers": [text]}},
        }
    return None


def command_help() -> str:
    return "\n".join(
        [
            "Matrix bridge commands:",
            "/matrix — show status",
            "/matrix on — enable this session's bridge",
            "/matrix off — disable this session's bridge",
            "/matrix restart — restart this session's bridge",
            "/matrix setup — configure Matrix accounts and token",
            "/matrix debug on|off — enable or disable lifecycle messages",
            "/matrix help — show this help",
        ]
    )


def run_one_shot() -> int:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
        raise RuntimeError("invalid extension request")
    method = request.get("method")
    context = request.get("context")
    params = request.get("params")
    if not isinstance(context, dict) or not isinstance(params, dict):
        raise RuntimeError("extension request is missing context or params")

    if method == "extension.setup.open":
        config = load_settings()
        if has_stored_config(config):
            print(
                json.dumps(
                    complete(request, "Matrix bridge is already configured."),
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return 0
        print(
            json.dumps(
                setup_interaction(request, config),
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0

    if method == "interaction.respond" and params.get("continuation") == "matrix-setup":
        if params.get("outcome") in {"cancelled", "dismissed"}:
            print(
                json.dumps(
                    complete(request, "Matrix setup cancelled."),
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return 0
        values = params.get("values")
        if not isinstance(values, dict):
            raise RuntimeError("setup response is missing values")
        existing = load_settings()
        config = normalize_config(values, existing)
        verify_access_token(config)
        if debug_enabled(existing):
            config["debug"] = True
        write_private_json(CONFIG_PATH, config)
        print(
            json.dumps(
                complete(request, "Matrix bridge configured."),
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0

    if method == "extension.command.invoke":
        arguments = params.get("arguments")
        if isinstance(arguments, list) and arguments:
            command = arguments[0] if isinstance(arguments[0], str) else ""
            config = load_settings()
            if command == "help":
                print(
                    json.dumps(
                        complete(request, command_help()), separators=(",", ":")
                    ),
                    flush=True,
                )
                return 0
            if command in {"on", "off", "restart"}:
                if not has_stored_config(config):
                    raise RuntimeError("Matrix is not configured. Run /matrix setup first.")
                summary = (
                    "Matrix bridge enabled for this session."
                    if command == "on"
                    else (
                        "Matrix bridge disabled for this session."
                        if command == "off"
                        else "Matrix bridge restarted for this session."
                    )
                )
                print(
                    json.dumps(complete(request, summary), separators=(",", ":")),
                    flush=True,
                )
                return 0
            if command == "debug":
                value = arguments[1] if len(arguments) > 1 else None
                if value not in {"on", "off"}:
                    summary = "Usage: /matrix debug on|off."
                elif not has_stored_config(config):
                    summary = "Matrix is not configured. Run /matrix setup first."
                else:
                    config["debug"] = value == "on"
                    write_private_json(CONFIG_PATH, config)
                    summary = (
                        "Matrix lifecycle messages enabled."
                        if config["debug"]
                        else "Matrix lifecycle messages disabled."
                    )
                print(
                    json.dumps(complete(request, summary), separators=(",", ":")),
                    flush=True,
                )
                return 0
            if command in {"setup", "settings", "configure"}:
                print(
                    json.dumps(
                        setup_interaction(request, config),
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                return 0
        config = load_settings()
        if config.get("agentUserId") and config.get("targetUserId"):
            session = context.get("session")
            extension_enabled = (
                session.get("extensionEnabled") is True
                if isinstance(session, dict)
                else False
            )
            summary = (
                f"Matrix bridge is {'active' if extension_enabled else 'disabled'} for this session: "
                f"{config['agentUserId']} → {config['targetUserId']}."
            )
        else:
            summary = "Matrix bridge is not configured."
        print(
            json.dumps(complete(request, summary), separators=(",", ":")),
            flush=True,
        )
        return 0

    raise RuntimeError(f"unsupported extension method: {method}")


def run_persistent() -> int:
    thread_id = os.environ["XEDOC_SESSION_SCRIPT_THREAD_ID"]
    script_id = os.environ["XEDOC_SESSION_SCRIPT_ID"]
    client = SessionScriptClient.from_host_child()
    inbound: queue.Queue[str] = queue.Queue()
    host_messages: queue.Queue[tuple[str, str]] = queue.Queue()
    stop = threading.Event()
    room_id: str | None = None
    pending_prompts: dict[str, dict[str, Any]] = {}

    def post_host_message(registration_id: str, level: str, message: str) -> None:
        try:
            client.post_message(registration_id, level, message[:500])
        except RpcError:
            pass

    registered: dict[str, Any] | None = None
    def remember_prompt(prompt: dict[str, Any]) -> None:
        if (
            prompt.get("kind") != "requestUserInput"
            or not prompt.get("canRespond")
            or not isinstance(prompt.get("promptId"), str)
            or not isinstance(prompt.get("responseLease"), str)
            or not room_id
        ):
            return
        token = os.urandom(8).hex()
        pending_prompts[prompt["promptId"]] = {"prompt": prompt, "token": token}
        matrix.send_text(room_id, prompt_message(prompt, token))

    def replace_pending_prompts(snapshot: dict[str, Any]) -> None:
        pending_prompts.clear()
        prompts = snapshot.get("pendingPrompts")
        if not isinstance(prompts, list):
            return
        for prompt in prompts:
            if isinstance(prompt, dict):
                remember_prompt(prompt)

    def on_notification(message: dict[str, Any]) -> None:
        nonlocal room_id
        try:
            method = message.get("method")
            params = message.get("params", {})
            if method == "item/completed" and isinstance(params, dict) and room_id:
                item = params.get("item")
                if isinstance(item, dict):
                    text = native_user_message_text(item)
                    if text is None:
                        text = completed_agent_message_text(item)
                    if text is not None:
                        matrix.send_text(room_id, text)
            elif method == "script/promptOpened" and isinstance(params, dict):
                remember_prompt(params)
            elif method == "script/promptClosed" and isinstance(params, dict):
                prompt_id = params.get("promptId")
                if isinstance(prompt_id, str):
                    pending_prompts.pop(prompt_id, None)
            elif method == "script/sessionUpdated" and isinstance(params, dict):
                session = params.get("session")
                if isinstance(session, dict):
                    room_id = ensure_room(
                        matrix,
                        config,
                        thread_id,
                        str(session.get("title") or "session"),
                        lifecycle,
                    )
            elif method == "script/resyncRequired" and isinstance(params, dict):
                registration_id = params.get("registrationId")
                if isinstance(registration_id, str):
                    refreshed = client.read(registration_id)
                    snapshot = refreshed.get("snapshot")
                    if isinstance(snapshot, dict):
                        session = snapshot.get("session")
                        if isinstance(session, dict):
                            room_id = ensure_room(
                                matrix,
                                config,
                                thread_id,
                                str(session.get("title") or "session"),
                                lifecycle,
                            )
                        replace_pending_prompts(snapshot)
        except (MatrixError, RpcError, ValueError) as error:
            if registered is not None:
                registration_id = registered.get("registrationId")
                if isinstance(registration_id, str):
                    post_host_message(
                        registration_id,
                        "warning",
                        f"Matrix bridge could not process an update: {error}",
                    )

    try:
        client.initialize("matrix-extension", "Matrix bridge", PLUGIN_VERSION)
        registered = client.register(
            thread_id,
            script_id,
            "Matrix bridge",
            PLUGIN_VERSION,
            {
                "modelResponseCompleted": True,
                "userMessages": True,
                "prompts": ["requestUserInput"],
                "sessionUpdates": True,
            },
            ["userInput.send", "prompt.requestUserInput.respond"],
        )
        registration_id = registered["registrationId"]
        raw_config = load_settings()
        try:
            config = stored_config(raw_config)
            matrix = MatrixClient(config)
            verify_access_token(config, matrix)
        except (MatrixError, ValueError) as error:
            post_host_message(
                registration_id,
                "error",
                f"Matrix bridge could not connect: {error}",
            )
            return 0

        def lifecycle(message: str) -> None:
            if current_debug_setting():
                post_host_message(registration_id, "info", message)

        client.set_notification_handler(on_notification)
        snapshot = registered.get("snapshot", {})
        session = snapshot.get("session", {}) if isinstance(snapshot, dict) else {}
        room_id = ensure_room(
            matrix,
            config,
            thread_id,
            str(session.get("title") or "session"),
            lifecycle,
        )
        initial_sync = matrix.sync(room_id, None, 0)
        since = initial_sync.get("next_batch")
        if not isinstance(since, str) or not since:
            raise MatrixError("Matrix initial sync did not include next_batch")
        replace_pending_prompts(snapshot)
        lifecycle(
            f"Matrix connection established to {config['homeserver']} as "
            f"{config['agentUserId']}."
        )
        receiver = threading.Thread(
            target=receive_messages,
            args=(
                matrix,
                room_id,
                config["targetUserId"],
                since,
                inbound,
                host_messages,
                current_debug_setting,
                stop,
            ),
            daemon=True,
        )
        receiver.start()
        matrix.send_text(
            room_id,
            "Matrix bridge connected. Send a message here from Element to talk to Xedoc.",
        )
        pending: deque[str] = deque()
        prompt_answers: deque[tuple[str, str]] = deque()

        while True:
            readable, _, _ = select.select([sys.stdin], [], [], 0.5)
            if readable:
                client.handle_message(client.receive_message())
            while True:
                try:
                    level, message = host_messages.get_nowait()
                except queue.Empty:
                    break
                post_host_message(registration_id, level, message)
            while True:
                try:
                    message = inbound.get_nowait()
                except queue.Empty:
                    break
                matched_prompt = next(
                    (
                        (prompt_id, state)
                        for prompt_id, state in pending_prompts.items()
                        if message.startswith(f"prompt:{state['token']} ")
                    ),
                    None,
                )
                if matched_prompt is None:
                    pending.append(message)
                else:
                    prompt_id, state = matched_prompt
                    prompt_answers.append(
                        (prompt_id, message.removeprefix(f"prompt:{state['token']} "))
                    )
            if prompt_answers:
                prompt_id, text = prompt_answers.popleft()
                state = pending_prompts.get(prompt_id)
                if state is None:
                    continue
                prompt = state["prompt"]
                answer = prompt_answer(prompt, text)
                if answer is None:
                    matrix.send_text(
                        room_id,
                        (
                            "That answer could not be mapped. Reply with "
                            f"prompt:{state['token']} and the requested JSON."
                        ),
                    )
                    continue
                try:
                    client.respond(
                        registered["registrationId"],
                        prompt_id,
                        prompt["responseLease"],
                        answer,
                    )
                except RpcError:
                    matrix.send_text(
                        room_id, "Xedoc could not accept that answer."
                    )
                    post_host_message(
                        registration_id,
                        "warning",
                        "Xedoc could not accept the Matrix prompt answer.",
                    )
                pending_prompts.pop(prompt_id, None)
            elif pending:
                latest = client.read(registered["registrationId"])
                thread = latest.get("snapshot", {}).get("thread", {})
                if thread.get("canAcceptDirectInput"):
                    text = pending[0]
                    try:
                        client.request(
                            "turn/start",
                            {
                                "threadId": thread_id,
                                "clientUserMessageId": (
                                    f"matrix-{os.urandom(8).hex()}"
                                ),
                                "input": [{"type": "text", "text": text}],
                            },
                        )
                    except RpcError:
                        post_host_message(
                            registration_id,
                            "warning",
                            "Xedoc could not accept the Matrix message yet.",
                        )
                        continue
                    pending.popleft()
    except RpcError:
        return 0
    except (MatrixError, OSError, ValueError) as error:
        if registered is not None:
            registration_id = registered.get("registrationId")
            if isinstance(registration_id, str):
                post_host_message(
                    registration_id, "error", f"Matrix bridge stopped: {error}"
                )
                return 0
        raise
    finally:
        stop.set()
        client.close()


def main() -> int:
    if os.environ.get("XEDOC_SESSION_SCRIPT_ID") and os.environ.get(
        "XEDOC_SESSION_SCRIPT_THREAD_ID"
    ):
        return run_persistent()
    return run_one_shot()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MatrixError, OSError, RuntimeError, ValueError) as error:
        print(f"matrix extension error: {error}", file=sys.stderr)
        raise SystemExit(1)
