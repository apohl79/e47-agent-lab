#!/usr/bin/env python3
"""Bridge one Xedoc root thread to a private Matrix room used from Element.

The Xedoc plugin host invokes this file in one-shot setup/command mode and as
a persistent session child. Setup stores refreshable OAuth credentials for
the Matrix agent and user accounts under ``~/.xedoc/extensions/matrix``.
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
import time
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
MATRIX_AUTH_METADATA_PATH = "/_matrix/client/v1/auth_metadata"
MAX_RESPONSE_BYTES = 1 << 20
SYNC_TIMEOUT_MS = 25_000
MAX_BACKFILL_PAGES = 100
OAUTH_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
OAUTH_API_SCOPE = "urn:matrix:client:api:*"
CONFIG_WRITE_LOCK = threading.Lock()


class MatrixError(RuntimeError):
    """A Matrix homeserver request failed or returned an invalid response."""


class OAuthError(MatrixError):
    """An OAuth endpoint rejected a request."""

    def __init__(self, error: str, description: str | None = None) -> None:
        self.error = error
        super().__init__(description or error)


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
                        "OAuth login connects an agent account for model messages and "
                        "your account for Xedoc user messages."
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
                    ],
                    "submit": action("save", "Save and start OAuth login"),
                    "cancel": None,
                },
            },
        },
    )


def oauth_authorization_interaction(
    request: dict[str, Any], pending: dict[str, Any]
) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    for role, label in (("agent", "Agent account"), ("user", "Your account")):
        authorization = pending.get(role)
        if not isinstance(authorization, dict):
            continue
        fields.append(
            {
                "type": "text",
                "id": f"{role}-authorization",
                "label": label,
                "description": (
                    f"Open {authorization['verificationUri']} and enter "
                    f"code {authorization['userCode']}. Then press Complete "
                    "OAuth login below."
                ),
                "value": "",
                "maxBytes": 1,
                "sensitive": False,
            }
        )
    return response(
        request,
        {
            "kind": "interaction",
            "interaction": {
                "id": "matrix-oauth",
                "continuation": "matrix-oauth-complete",
                "stateRevision": "1",
                "surface": {
                    "type": "form",
                    "id": "matrix-oauth-login",
                    "title": "Authorize Matrix accounts",
                    "subtitle": (
                        "Complete both device-authorisation pages in a browser. "
                        "No token is shown or pasted into Xedoc."
                    ),
                    "fields": fields,
                    "submit": action("complete", "Complete OAuth login"),
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


def normalize_homeserver(values: dict[str, Any]) -> str:
    homeserver = str(values.get("homeserver") or "").strip().rstrip("/")
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
    return homeserver


def normalize_config(
    values: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, str]:
    existing = existing or {}
    homeserver = normalize_homeserver(values)
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
    config = normalize_config(
        {
            "homeserver": value.get("homeserver"),
            "agent-user-id": value.get("agentUserId"),
            "access-token": value.get("accessToken"),
            "target-user-id": value.get("targetUserId"),
        }
    )
    for key in (
        "refreshToken",
        "clientId",
        "targetAccessToken",
        "targetRefreshToken",
        "targetClientId",
        "oauthTokenEndpoint",
    ):
        stored = str(value.get(key) or "").strip()
        if not stored:
            raise ValueError("both Matrix accounts must be signed in with OAuth")
        config[key] = stored
    return config


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
    config: dict[str, Any],
    client: MatrixClient | None = None,
    expected_user_key: str = "agentUserId",
) -> None:
    authenticated_user = (client or MatrixClient(config)).whoami()
    if authenticated_user != config[expected_user_key]:
        raise MatrixError(
            "the configured access token belongs to "
            f"{authenticated_user}, not {config[expected_user_key]}"
        )


class MatrixClient:
    """Minimal Matrix Client-Server API client with bounded responses."""

    def __init__(
        self,
        config: dict[str, Any],
        opener: Any | None = None,
        role: str = "agent",
    ) -> None:
        self.homeserver = str(config["homeserver"]).rstrip("/")
        self.config = config
        self.role = role
        self.access_key = "accessToken" if role == "agent" else "targetAccessToken"
        self.access_token = str(config[self.access_key])
        self.opener = opener or build_opener(RejectRedirectHandler())

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
        timeout: float = 30,
        refreshed: bool = False,
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
            if error.code == 401 and not refreshed:
                try:
                    refresh_oauth_account(self.config, self.role)
                except OAuthError:
                    pass
                else:
                    self.access_token = str(self.config[self.access_key])
                    return self.request(
                        method,
                        path,
                        payload,
                        query,
                        timeout,
                        refreshed=True,
                    )
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

    def join_room(self, room_id: str) -> None:
        result = self.request("POST", f"/join/{quote(room_id, safe='')}")
        joined_room_id = result.get("room_id")
        if joined_room_id != room_id:
            raise MatrixError("Matrix join response did not include the requested room")

    def send_text(self, room_id: str, text: str) -> str | None:
        transaction_id = f"xedoc-{os.urandom(12).hex()}"
        result = self.request(
            "PUT",
            (
                f"/rooms/{quote(room_id, safe='')}/send/m.room.message/"
                f"{transaction_id}"
            ),
            {"msgtype": "m.text", "body": text},
        )
        event_id = result.get("event_id")
        return event_id if isinstance(event_id, str) else None

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


class OAuthClient:
    """Small OAuth client used for Matrix device authorisation and refresh."""

    def __init__(self, opener: Any | None = None) -> None:
        self.opener = opener or build_opener(RejectRedirectHandler())

    def request(
        self, url: str, payload: dict[str, Any], *, json_body: bool = False
    ) -> dict[str, Any]:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise OAuthError("invalid_endpoint", "OAuth endpoint must be HTTPS")
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if json_body
            else urlencode(
                {key: value for key, value in payload.items() if value is not None}
            ).encode()
        )
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": (
                    "application/json"
                    if json_body
                    else "application/x-www-form-urlencoded"
                ),
            },
        )
        try:
            with self.opener.open(request, timeout=30) as opened:
                raw = opened.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raw = error.read(4096)
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = {}
            if isinstance(value, dict):
                detail = value.get("error_description") or value.get("error")
                raise OAuthError(
                    str(value.get("error") or f"http_{error.code}"),
                    str(detail)[:500] if detail else None,
                ) from error
            raise OAuthError(f"http_{error.code}") from error
        except URLError as error:
            raise OAuthError("network_error", str(error.reason)) from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise OAuthError("response_too_large")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise OAuthError("invalid_response") from error
        if not isinstance(value, dict):
            raise OAuthError("invalid_response")
        return value

    def metadata(self, homeserver: str) -> dict[str, str]:
        url = f"{homeserver}{MATRIX_AUTH_METADATA_PATH}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as opened:
                value = json.loads(opened.read(MAX_RESPONSE_BYTES + 1))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
            raise OAuthError("metadata_failed", str(error)) from error
        if not isinstance(value, dict):
            raise OAuthError("invalid_metadata")
        required = (
            "registration_endpoint",
            "device_authorization_endpoint",
            "token_endpoint",
        )
        if not all(isinstance(value.get(key), str) for key in required):
            raise OAuthError("unsupported_homeserver", "OAuth device login is unavailable")
        return {key: str(value[key]) for key in required}


def oauth_device_id(role: str) -> str:
    return f"e47-{role}-{os.urandom(8).hex()}"


def begin_oauth_login(homeserver: str, role: str) -> dict[str, Any]:
    oauth = OAuthClient()
    metadata = oauth.metadata(homeserver)
    registration = oauth.request(
        metadata["registration_endpoint"],
        {
            "application_type": "native",
            "client_name": "E47 Matrix Bridge",
            "client_uri": "https://github.com/apohl79/e47-agent-lab",
            "grant_types": ["refresh_token", OAUTH_DEVICE_GRANT],
            "redirect_uris": [],
            "token_endpoint_auth_method": "none",
        },
        json_body=True,
    )
    client_id = registration.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError("invalid_registration", "OAuth registration omitted client_id")
    device_id = oauth_device_id(role)
    pending = oauth.request(
        metadata["device_authorization_endpoint"],
        {
            "client_id": client_id,
            "scope": f"{OAUTH_API_SCOPE} urn:matrix:client:device:{device_id}",
        },
    )
    required = ("device_code", "user_code", "verification_uri")
    if not all(isinstance(pending.get(key), str) for key in required):
        raise OAuthError("invalid_device_authorization")
    return {
        "clientId": client_id,
        "deviceCode": pending["device_code"],
        "userCode": pending["user_code"],
        "verificationUri": pending.get("verification_uri_complete")
        or pending["verification_uri"],
        "tokenEndpoint": metadata["token_endpoint"],
        "expiresAt": int(time.time()) + int(pending.get("expires_in") or 600),
        "role": role,
    }


def complete_oauth_login(pending: dict[str, Any]) -> dict[str, str]:
    if int(pending.get("expiresAt") or 0) <= time.time():
        raise OAuthError("expired_token", "OAuth authorisation expired; start again")
    result = OAuthClient().request(
        str(pending["tokenEndpoint"]),
        {
            "grant_type": OAUTH_DEVICE_GRANT,
            "device_code": str(pending["deviceCode"]),
            "client_id": str(pending["clientId"]),
        },
    )
    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise OAuthError(
            "invalid_token_response", "OAuth login did not return refreshable tokens"
        )
    return {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "clientId": str(pending["clientId"]),
        "tokenEndpoint": str(pending["tokenEndpoint"]),
    }


def configured_oauth_account(
    homeserver: str, role: str, credentials: dict[str, str]
) -> tuple[str, dict[str, str]]:
    probe = {
        "homeserver": homeserver,
        "accessToken": credentials["accessToken"],
        "targetAccessToken": credentials["accessToken"],
    }
    user_id = MatrixClient(probe, role=role).whoami()
    if not matrix_user_id(user_id):
        raise OAuthError("invalid_whoami", "Matrix OAuth login returned an invalid user ID")
    return user_id, credentials


def refresh_oauth_account(config: dict[str, Any], role: str) -> None:
    keys = (
        ("accessToken", "refreshToken", "clientId")
        if role == "agent"
        else ("targetAccessToken", "targetRefreshToken", "targetClientId")
    )
    access_key, refresh_key, client_key = keys
    with CONFIG_WRITE_LOCK:
        persisted = load_settings()
        current = persisted or config
        refresh_token = current.get(refresh_key)
        client_id = current.get(client_key)
        token_endpoint = current.get("oauthTokenEndpoint")
        if not all(
            isinstance(value, str) and value
            for value in (refresh_token, client_id, token_endpoint)
        ):
            return
        result = OAuthClient().request(
            token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
        )
        access_token = result.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthError(
                "invalid_token_response", "OAuth refresh omitted access_token"
            )
        current[access_key] = access_token
        next_refresh = result.get("refresh_token")
        if isinstance(next_refresh, str) and next_refresh:
            current[refresh_key] = next_refresh
        write_private_json(CONFIG_PATH, current)
        config.update(current)


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
    events: list[dict[str, Any]],
    target_user_id: str,
    ignored_event_ids: set[str] | None = None,
) -> list[str]:
    messages: list[str] = []
    for event in events:
        event_id = event.get("event_id")
        if (
            ignored_event_ids is not None
            and isinstance(event_id, str)
            and event_id in ignored_event_ids
        ):
            ignored_event_ids.discard(event_id)
            continue
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
    ignored_event_ids: set[str],
    ignored_event_ids_lock: threading.Lock,
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
        with ignored_event_ids_lock:
            accepted = inbound_messages(
                events, target_user_id, ignored_event_ids
            )
        for message in accepted:
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


def approval_prompt_message(prompt: dict[str, Any], token: str) -> str:
    request = prompt.get("request")
    method = request.get("method") if isinstance(request, dict) else None
    params = request.get("params") if isinstance(request, dict) else {}
    if not isinstance(params, dict):
        return "Xedoc approval details are unavailable."
    if method == "item/extensionInteraction/request":
        surface = params.get("surface")
        if not isinstance(surface, dict):
            return "Xedoc approval details are unavailable."
        lines = [f"Xedoc approval needed: {surface.get('title') or 'Confirmation'}"]
        surface_type = surface.get("type")
        body = surface.get("body")
        if isinstance(body, str) and body:
            lines.append(body)
        for detail in surface.get("details", []):
            if isinstance(detail, dict):
                label = detail.get("label")
                value = detail.get("value")
                if isinstance(label, str) and isinstance(value, str):
                    lines.append(f"{label}: {value}")
        for section in surface.get("sections", []):
            if not isinstance(section, dict):
                continue
            title = section.get("title")
            if isinstance(title, str) and title:
                lines.append(title + ":")
            for row in section.get("rows", []):
                if isinstance(row, dict) and isinstance(row.get("text"), str):
                    lines.append(f"- {row['text']}")
        actions = surface.get("actions")
        if surface_type == "menu":
            actions = [
                item.get("action")
                for item in surface.get("items", [])
                if isinstance(item, dict)
            ]
        choices = [
            f"{action['id']} ({action.get('label') or action['id']})"
            for action in actions if isinstance(action, dict)
            and isinstance(action.get("id"), str)
        ] if isinstance(actions, list) else []
        if surface_type == "form":
            fields = surface.get("fields")
            field_actions: list[dict[str, Any]] = []
            example_values: dict[str, Any] = {}
            if isinstance(fields, list):
                for field in fields:
                    if (
                        isinstance(field, dict)
                        and isinstance(field.get("id"), str)
                        and isinstance(field.get("label"), str)
                    ):
                        field_id = field["id"]
                        lines.append(
                            f"{field_id} ({field['label']}): "
                            f"{field.get('description') or ''}"
                        )
                        if field.get("sensitive") is not True and "value" in field:
                            lines.append(f"  Current: {json.dumps(field['value'])}")
                        options = field.get("options")
                        if isinstance(options, list):
                            choices = [
                                f"{option['id']} ({option.get('label') or option['id']})"
                                for option in options
                                if isinstance(option, dict)
                                and isinstance(option.get("id"), str)
                            ]
                            if choices:
                                lines.append("  Choices: " + ", ".join(choices))
                        example_values[field_id] = "value"
                        action = field.get("action")
                        if isinstance(action, dict):
                            field_actions.append(action)
            if field_actions:
                lines.append(
                    "Actions: "
                    + ", ".join(
                        f"{action['id']} ({action.get('label') or action['id']})"
                        for action in field_actions
                        if isinstance(action.get("id"), str)
                    )
                )
                lines.append(f"Reply as `approval:{token} <action-id>`.")
            submit = surface.get("submit")
            if isinstance(submit, dict) and isinstance(submit.get("id"), str):
                example = json.dumps(
                    {"action": submit["id"], "values": example_values},
                    separators=(",", ":"),
                )
                lines.append(
                    f"Reply as `approval:{token} {example}` with each field's "
                    "actual ID and value."
                )
            cancel = surface.get("cancel")
            if isinstance(cancel, dict) and isinstance(cancel.get("id"), str):
                lines.append(f"Reply as `approval:{token} {cancel['id']}` to cancel.")
            return "\n".join(lines)
        if choices:
            lines.append("Choices: " + ", ".join(choices))
            lines.append(f"Reply as `approval:{token} <choice-id>`.")
        elif surface_type == "notice":
            lines.append(f"Reply as `approval:{token} dismiss`.")
        return "\n".join(lines)
    decisions = params.get("availableDecisions")
    if not isinstance(decisions, list) and method == "item/fileChange/requestApproval":
        decisions = ["accept", "acceptForSession", "decline", "cancel"]
    lines = [
        f"Xedoc approval needed: {str(prompt.get('kind') or 'approval')}",
        str(params.get("reason") or "Review this approval in Xedoc."),
    ]
    simple_decisions = [decision for decision in decisions or [] if isinstance(decision, str)]
    if simple_decisions:
        lines.append("Choices: " + ", ".join(simple_decisions))
        lines.append(f"Reply as `approval:{token} <choice>`.")
    else:
        lines.append(
            f"Reply as `approval:{token} <JSON response>` to provide the "
            "approval response shown in Xedoc."
        )
    return "\n".join(lines)


def approval_answer(prompt: dict[str, Any], text: str) -> dict[str, Any] | None:
    request = prompt.get("request")
    method = request.get("method") if isinstance(request, dict) else None
    params = request.get("params") if isinstance(request, dict) else {}
    if not isinstance(params, dict):
        return None
    selected = text.strip()
    if method == "item/extensionInteraction/request":
        surface = params.get("surface")
        if not isinstance(surface, dict):
            return None
        surface_type = surface.get("type")
        if surface_type == "notice" and selected == "dismiss":
            return extension_interaction_response(params, "dismissed", None, None)
        if surface_type == "form":
            fields = surface.get("fields")
            action_ids = {
                field["action"]["id"]
                for field in fields or []
                if isinstance(field, dict)
                and isinstance(field.get("action"), dict)
                and isinstance(field["action"].get("id"), str)
            }
            if selected in action_ids:
                return extension_interaction_response(
                    params, "accepted", selected, None
                )
            cancel = surface.get("cancel")
            if isinstance(cancel, dict) and selected == cancel.get("id"):
                return extension_interaction_response(params, "cancelled", None, None)
            try:
                form_response = json.loads(selected)
            except json.JSONDecodeError:
                return None
            if not isinstance(form_response, dict):
                return None
            action_id = form_response.get("action")
            values = form_response.get("values")
            submit = surface.get("submit")
            if not (
                isinstance(action_id, str)
                and isinstance(values, dict)
                and isinstance(submit, dict)
                and action_id == submit.get("id")
            ):
                return None
            return extension_interaction_response(
                params, "accepted", action_id, values
            )
        actions = surface.get("actions")
        if surface_type == "menu":
            actions = [
                item.get("action")
                for item in surface.get("items", [])
                if isinstance(item, dict)
            ]
        action_ids = {
            action.get("id")
            for action in actions or []
            if isinstance(action, dict) and isinstance(action.get("id"), str)
        }
        if selected not in action_ids:
            return None
        return extension_interaction_response(params, "accepted", selected, None)
    decisions = params.get("availableDecisions")
    if not isinstance(decisions, list) and method == "item/fileChange/requestApproval":
        decisions = ["accept", "acceptForSession", "decline", "cancel"]
    if isinstance(decisions, list) and selected in decisions:
        return {"decision": selected}
    try:
        response = json.loads(selected)
    except json.JSONDecodeError:
        return None
    return response if isinstance(response, dict) else None


def extension_interaction_response(
    params: dict[str, Any],
    outcome: str,
    action_id: str | None,
    values: dict[str, Any] | None,
) -> dict[str, Any] | None:
    required = ("extensionId", "interactionId", "continuation")
    if not all(isinstance(params.get(key), str) for key in required):
        return None
    return {
        "extensionId": params["extensionId"],
        "interactionId": params["interactionId"],
        "continuation": params["continuation"],
        "stateRevision": params.get("stateRevision"),
        "outcome": outcome,
        "action": {"id": action_id} if action_id else None,
        "values": values,
    }


def command_help() -> str:
    return "\n".join(
        [
            "Matrix bridge commands:",
            "/matrix — show status",
            "/matrix on — enable this session's bridge",
            "/matrix off — disable this session's bridge",
            "/matrix restart — restart this session's bridge",
            "/matrix setup — sign in both Matrix accounts with OAuth",
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
        config = {
            key: value
            for key, value in existing.items()
            if key not in {"pendingOAuth"}
        }
        config["homeserver"] = normalize_homeserver(values)
        pending = {
            "agent": begin_oauth_login(config["homeserver"], "agent"),
            "user": begin_oauth_login(config["homeserver"], "user"),
        }
        config["pendingOAuth"] = pending
        write_private_json(CONFIG_PATH, config)
        print(
            json.dumps(
                oauth_authorization_interaction(request, pending),
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0

    if (
        method == "interaction.respond"
        and params.get("continuation") == "matrix-oauth-complete"
    ):
        config = load_settings()
        pending = config.get("pendingOAuth")
        if not isinstance(pending, dict):
            raise RuntimeError("No Matrix OAuth login is pending. Run /matrix setup.")
        if params.get("outcome") in {"cancelled", "dismissed"}:
            config.pop("pendingOAuth", None)
            write_private_json(CONFIG_PATH, config)
            print(
                json.dumps(
                    complete(request, "Matrix OAuth login cancelled."),
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return 0
        completed = pending.setdefault("credentials", {})
        if not isinstance(completed, dict):
            raise RuntimeError("Matrix OAuth state is invalid. Run /matrix setup.")
        credentials: dict[str, dict[str, str]] = {}
        for role in ("agent", "user"):
            authorization = pending.get(role)
            if not isinstance(authorization, dict):
                raise RuntimeError("Matrix OAuth state is invalid. Run /matrix setup.")
            saved_credentials = completed.get(role)
            if isinstance(saved_credentials, dict):
                credentials[role] = {
                    key: value
                    for key, value in saved_credentials.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                if all(
                    credentials[role].get(key)
                    for key in (
                        "accessToken",
                        "refreshToken",
                        "clientId",
                        "tokenEndpoint",
                    )
                ):
                    continue
                completed.pop(role, None)
            try:
                credentials[role] = complete_oauth_login(authorization)
            except OAuthError as error:
                if error.error == "authorization_pending":
                    config["pendingOAuth"] = pending
                    write_private_json(CONFIG_PATH, config)
                    print(
                        json.dumps(
                            oauth_authorization_interaction(request, pending),
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                    return 0
                raise
            completed[role] = credentials[role]
            config["pendingOAuth"] = pending
            write_private_json(CONFIG_PATH, config)
        agent_id, agent = configured_oauth_account(
            str(config["homeserver"]), "agent", credentials["agent"]
        )
        user_id, user = configured_oauth_account(
            str(config["homeserver"]), "user", credentials["user"]
        )
        if agent_id == user_id:
            raise OAuthError(
                "same_account",
                "Authorize a distinct agent account and user account.",
            )
        config.update(
            {
                "agentUserId": agent_id,
                "accessToken": agent["accessToken"],
                "refreshToken": agent["refreshToken"],
                "clientId": agent["clientId"],
                "targetUserId": user_id,
                "targetAccessToken": user["accessToken"],
                "targetRefreshToken": user["refreshToken"],
                "targetClientId": user["clientId"],
                "oauthTokenEndpoint": agent["tokenEndpoint"],
            }
        )
        config.pop("pendingOAuth", None)
        write_private_json(CONFIG_PATH, config)
        print(
            json.dumps(
                complete(request, "Matrix accounts connected with OAuth."),
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
    sent_user_event_ids: set[str] = set()
    sent_user_event_ids_lock = threading.Lock()

    def post_host_message(registration_id: str, level: str, message: str) -> None:
        try:
            client.post_message(registration_id, level, message[:500])
        except RpcError:
            pass

    registered: dict[str, Any] | None = None
    def remember_prompt(prompt: dict[str, Any]) -> None:
        if (
            prompt.get("kind")
            not in {
                "requestUserInput",
                "extensionInteraction",
                "commandExecutionApproval",
                "fileChangeApproval",
                "permissionsApproval",
            }
            or not prompt.get("canRespond")
            or not isinstance(prompt.get("promptId"), str)
            or not isinstance(prompt.get("responseLease"), str)
            or not room_id
        ):
            return
        token = os.urandom(8).hex()
        pending_prompts[prompt["promptId"]] = {"prompt": prompt, "token": token}
        message = (
            prompt_message(prompt, token)
            if prompt.get("kind") == "requestUserInput"
            else approval_prompt_message(prompt, token)
        )
        agent_matrix.send_text(room_id, message)

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
                    if text is not None:
                        with sent_user_event_ids_lock:
                            event_id = user_matrix.send_text(room_id, text)
                            if event_id:
                                sent_user_event_ids.add(event_id)
                    else:
                        text = completed_agent_message_text(item)
                        if text is not None:
                            agent_matrix.send_text(room_id, text)
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
                        agent_matrix,
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
                                agent_matrix,
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
                "prompts": [
                    "requestUserInput",
                    "extensionInteraction",
                    "commandExecutionApproval",
                    "fileChangeApproval",
                    "permissionsApproval",
                ],
                "sessionUpdates": True,
            },
            [
                "userInput.send",
                "prompt.requestUserInput.respond",
                "prompt.approval.respond",
            ],
        )
        registration_id = registered["registrationId"]
        raw_config = load_settings()
        try:
            config = stored_config(raw_config)
            agent_matrix = MatrixClient(raw_config)
            user_matrix = MatrixClient(raw_config, role="user")
            verify_access_token(config, agent_matrix)
            verify_access_token(config, user_matrix, "targetUserId")
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
            agent_matrix,
            config,
            thread_id,
            str(session.get("title") or "session"),
            lifecycle,
        )
        user_matrix.join_room(room_id)
        lifecycle(f"Matrix user account joined {room_id}.")
        initial_sync = user_matrix.sync(room_id, None, 0)
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
                user_matrix,
                room_id,
                config["targetUserId"],
                since,
                inbound,
                host_messages,
                current_debug_setting,
                sent_user_event_ids,
                sent_user_event_ids_lock,
                stop,
            ),
            daemon=True,
        )
        receiver.start()
        agent_matrix.send_text(
            room_id,
            "Matrix bridge connected. Send a message here from Element to talk to Xedoc.",
        )
        pending: deque[str] = deque()
        prompt_answers: deque[tuple[str, str, str]] = deque()

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
                        (prompt_id, state, "prompt")
                        for prompt_id, state in pending_prompts.items()
                        if message.startswith(f"prompt:{state['token']} ")
                    ),
                    None,
                )
                if matched_prompt is None:
                    matched_prompt = next(
                        (
                            (prompt_id, state, "approval")
                            for prompt_id, state in pending_prompts.items()
                            if message.startswith(f"approval:{state['token']} ")
                        ),
                        None,
                    )
                if matched_prompt is None:
                    pending.append(message)
                else:
                    prompt_id, state, prefix = matched_prompt
                    prompt_answers.append(
                        (
                            prompt_id,
                            prefix,
                            message.removeprefix(f"{prefix}:{state['token']} "),
                        )
                    )
            if prompt_answers:
                prompt_id, prefix, text = prompt_answers.popleft()
                state = pending_prompts.get(prompt_id)
                if state is None:
                    continue
                prompt = state["prompt"]
                answer = (
                    prompt_answer(prompt, text)
                    if prefix == "prompt"
                    else approval_answer(prompt, text)
                )
                if answer is None:
                    agent_matrix.send_text(
                        room_id,
                        (
                            "That response could not be mapped. Reply with "
                            f"{prefix}:{state['token']} and a listed choice "
                            "or the requested JSON."
                        ),
                    )
                    continue
                try:
                    if prefix == "prompt":
                        client.respond(
                            registered["registrationId"],
                            prompt_id,
                            prompt["responseLease"],
                            answer,
                        )
                    else:
                        client.respond_approval(
                            registered["registrationId"],
                            prompt_id,
                            prompt["responseLease"],
                            answer,
                        )
                except RpcError:
                    agent_matrix.send_text(
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
