"""Real TUI regression test for Matrix request_user_input responses.

This test deliberately uses the installed Matrix bridge as a session child,
but replaces both external services:

* a loopback HTTPS Matrix Client-Server API implementation; and
* a loopback OpenAI Responses API implementation.

It therefore exercises the actual app-server child transport, prompt lease,
TUI prompt, Matrix sync loop, and model continuation without touching a real
Xedoc home, Matrix account, or model endpoint.

Run it against an already-built Xedoc binary:

    XEDOC_MATRIX_E2E_BIN=/path/to/xedoc \
    XEDOC_MATRIX_E2E_MODELS=/path/to/models.json \
    pytest -q plugins/matrix/extensions/tests/test_matrix_tmux_e2e.py
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from websockets.sync.client import connect


TEST_ROOT = Path(__file__).resolve().parent
EXTENSIONS_ROOT = TEST_ROOT.parent
PLUGIN_ROOT = EXTENSIONS_ROOT.parent
REPO_ROOT = PLUGIN_ROOT.parents[1]
QUESTION_CALL_ID = "matrix-e2e-question"
QUESTION_ID = "matrix-e2e-choice"
FINAL_MARKER = "MATRIX_E2E_COMPLETE: Option B"


class WaitTimeout(AssertionError):
    """A tmux pane or fake service did not reach the expected state."""


def wait_for(
    description: str,
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 30,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise WaitTimeout(f"timed out waiting for {description}")


def tmux(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def pane_text(session: str) -> str:
    result = tmux("capture-pane", "-p", "-t", session, "-S", "-300", check=False)
    if result.returncode:
        return f"<tmux pane unavailable: {result.stderr.strip()}>"
    return result.stdout


def read_test_log(path: Path) -> str:
    if not path.exists():
        return "<not started>"
    contents = path.read_text(errors="replace")
    return contents[-20_000:]


def visible_pane_text(session: str) -> str:
    result = tmux("capture-pane", "-p", "-t", session, check=False)
    if result.returncode:
        return f"<tmux pane unavailable: {result.stderr.strip()}>"
    return result.stdout


def send_keys(session: str, text: str) -> None:
    tmux("send-keys", "-t", session, "-l", text)
    tmux("send-keys", "-t", session, "Enter")


def press(session: str, key: str) -> None:
    tmux("send-keys", "-t", session, key)


def app_server_request(
    endpoint: str, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Make one real app-server request through the public websocket protocol."""

    with connect(endpoint, open_timeout=5, close_timeout=1) as client:
        client.send(
            json.dumps(
                {
                    "id": "matrix-e2e-initialize",
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "matrix-e2e",
                            "title": "Matrix E2E",
                            "version": "0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                },
                separators=(",", ":"),
            )
        )
        while True:
            response = json.loads(client.recv(timeout=5))
            if response.get("id") == "matrix-e2e-initialize":
                if "error" in response:
                    raise AssertionError(f"app-server initialize failed: {response!r}")
                break
        client.send(
            json.dumps(
                {
                    "id": "matrix-e2e-request",
                    "method": method,
                    "params": params,
                },
                separators=(",", ":"),
            )
        )
        while True:
            response = json.loads(client.recv(timeout=5))
            if response.get("id") == "matrix-e2e-request":
                return response


def app_server_thread_id(endpoint: str) -> str:
    response = app_server_request(
        endpoint,
        "thread/loaded/list",
        {"limit": 10},
    )
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise AssertionError(f"thread/loaded/list failed: {response!r}")
    for thread_id in result["data"]:
        if isinstance(thread_id, str):
            return thread_id
    raise AssertionError(f"thread/loaded/list returned no threads: {response!r}")


def sse_event(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


def completed(response_id: str) -> dict[str, Any]:
    return sse_event(
        "response.completed",
        response={
            "id": response_id,
            "usage": {
                "input_tokens": 1,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 1,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 2,
            },
        },
    )


class FakeResponses:
    """Deterministic model endpoint: tool call first, completion after answer."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler_type()
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def saw_option_b_answer(self) -> bool:
        with self._lock:
            return any(
                QUESTION_CALL_ID in json.dumps(request)
                and "Option B" in json.dumps(request)
                for request in self.requests
            )

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/responses":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers["content-length"])
                    request = json.loads(self.rfile.read(length))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                if not isinstance(request, dict):
                    self.send_error(400)
                    return
                with outer._lock:
                    outer.requests.append(request)
                events = outer._events_for(request)
                body = b"".join(
                    (
                        f"event: {event['type']}\n"
                        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    ).encode()
                    for event in events
                )
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return Handler

    def _events_for(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        encoded = json.dumps(request, separators=(",", ":"))
        if QUESTION_CALL_ID not in encoded:
            return [
                sse_event("response.created", response={"id": "matrix-e2e-question"}),
                sse_event(
                    "response.output_item.done",
                    item={
                        "type": "function_call",
                        "call_id": QUESTION_CALL_ID,
                        "name": "request_user_input",
                        "arguments": json.dumps(
                            {
                                "questions": [
                                    {
                                        "id": QUESTION_ID,
                                        "header": "Matrix E2E",
                                        "question": "Which option do you choose?",
                                        "options": [
                                            {
                                                "label": "Option A",
                                                "description": "First option.",
                                            },
                                            {
                                                "label": "Option B",
                                                "description": "Second option.",
                                            },
                                            {
                                                "label": "Option C",
                                                "description": "Third option.",
                                            },
                                        ],
                                    }
                                ]
                            },
                            separators=(",", ":"),
                        ),
                    },
                ),
                completed("matrix-e2e-question"),
            ]
        return [
            sse_event("response.created", response={"id": "matrix-e2e-final"}),
            sse_event(
                "response.output_item.added",
                item={
                    "type": "message",
                    "role": "assistant",
                    "id": "matrix-e2e-message",
                    "status": "in_progress",
                    "content": [],
                },
            ),
            sse_event("response.output_text.delta", delta=FINAL_MARKER),
            sse_event(
                "response.output_text.done",
                item_id="matrix-e2e-message",
                output_index=0,
                content_index=0,
                text=FINAL_MARKER,
            ),
            sse_event(
                "response.content_part.done",
                item_id="matrix-e2e-message",
                output_index=0,
                content_index=0,
                part={"type": "output_text", "text": FINAL_MARKER, "annotations": []},
            ),
            sse_event(
                "response.output_item.done",
                item={
                    "type": "message",
                    "role": "assistant",
                    "id": "matrix-e2e-message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": FINAL_MARKER}],
                },
            ),
            completed("matrix-e2e-final"),
        ]


class FakeMatrix:
    """Small HTTPS Matrix endpoint with a test-only inbound event endpoint."""

    room_id = "!matrix-e2e:mock"

    def __init__(self, certificate: Path, private_key: Path) -> None:
        self.sent_bodies: list[str] = []
        self._sent_lock = threading.Lock()
        self.request_log: list[str] = []
        self.event_log: list[str] = []
        self._log_lock = threading.Lock()
        self._started_at = time.monotonic()
        self._events: list[dict[str, Any]] = []
        self._events_condition = threading.Condition()
        self._event_counter = 0
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler_type()
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def homeserver(self) -> str:
        return f"https://127.0.0.1:{self._server.server_port}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def inject_target_message(self, message: str, certificate: Path) -> None:
        body = json.dumps({"body": message}).encode()
        context = ssl.create_default_context(cafile=str(certificate))
        request = Request(
            f"{self.homeserver}/__inject",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        self._record_event(f"inject client begin body={message!r}")
        with urlopen(request, context=context, timeout=5) as response:
            assert response.status == 200
        self._record_event(f"inject client complete body={message!r}")

    def enqueue_target_message(self, message: str) -> None:
        with self._events_condition:
            self._event_counter += 1
            self._events.append(
                {
                    "event_id": f"$target-{self._event_counter}",
                    "type": "m.room.message",
                    "sender": "@user:mock",
                    "content": {"msgtype": "m.text", "body": message},
                }
            )
            self._events_condition.notify_all()
        self._record_event(f"enqueue body={message!r}")

    def has_sent(self, text: str) -> bool:
        with self._sent_lock:
            return any(text in body for body in self.sent_bodies)

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/_matrix/client/v3/account/whoami":
                    outer._record_request(
                        f"GET whoami auth={self.headers.get('authorization', '')!r}"
                    )
                    token = self.headers.get("authorization", "")
                    user_id = "@user:mock" if token == "Bearer user-token" else "@agent:mock"
                    self._json({"user_id": user_id})
                    return
                if parsed.path == "/_matrix/client/v3/sync":
                    outer._record_request(f"GET sync {parsed.query}")
                    self._sync(parse_qs(parsed.query))
                    return
                self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/__inject":
                    try:
                        body = json.loads(
                            self.rfile.read(int(self.headers["content-length"]))
                        )
                        message = body["body"]
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        self.send_error(400)
                        return
                    if not isinstance(message, str):
                        self.send_error(400)
                        return
                    outer.enqueue_target_message(message)
                    outer._record_request(f"POST inject body={message!r}")
                    self._json({})
                    return
                if parsed.path == "/_matrix/client/v3/createRoom":
                    outer._record_request("POST createRoom")
                    self._json({"room_id": outer.room_id})
                    return
                if parsed.path.startswith("/_matrix/client/v3/join/"):
                    outer._record_request(f"POST join {parsed.path}")
                    self._json({"room_id": outer.room_id})
                    return
                self.send_error(404)

            def do_PUT(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if "/send/m.room.message/" not in parsed.path:
                    if "/state/m.room.name" in parsed.path:
                        self._json({})
                        return
                    self.send_error(404)
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                    text = body["body"]
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                if not isinstance(text, str):
                    self.send_error(400)
                    return
                with outer._sent_lock:
                    outer.sent_bodies.append(text)
                outer._record_event(f"send body={text!r}")
                outer._record_request(f"PUT message body={text!r}")
                self._json({"event_id": f"$agent-{len(outer.sent_bodies)}"})

            def _sync(self, query: dict[str, list[str]]) -> None:
                raw_since = query.get("since", ["0"])[0]
                try:
                    since = int(raw_since.removeprefix("s"))
                except ValueError:
                    since = 0
                try:
                    timeout_seconds = int(query.get("timeout", ["0"])[0]) / 1000
                except ValueError:
                    timeout_seconds = 0
                with outer._events_condition:
                    if len(outer._events) <= since:
                        outer._events_condition.wait(timeout=max(0, timeout_seconds))
                    events = outer._events[since:]
                    next_batch = f"s{len(outer._events)}"
                outer._record_request(
                    f"SYNC since={raw_since!r} events={[event['event_id'] for event in events]!r} "
                    f"next={next_batch!r}"
                )
                if events:
                    outer._record_event(
                        f"sync since={raw_since!r} events="
                        f"{[event['event_id'] for event in events]!r}"
                    )
                self._json(
                    {
                        "next_batch": next_batch,
                        "rooms": {
                            "join": {
                                outer.room_id: {
                                    "timeline": {
                                        "events": events,
                                        "limited": False,
                                        "prev_batch": f"s{since}",
                                    }
                                }
                            }
                        },
                    }
                )

            def _json(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return Handler

    def _record_request(self, message: str) -> None:
        with self._log_lock:
            self.request_log.append(message)

    def _record_event(self, message: str) -> None:
        with self._log_lock:
            self.event_log.append(
                f"+{time.monotonic() - self._started_at:.3f}s {message}"
            )


def generate_certificate(directory: Path) -> tuple[Path, Path]:
    certificate = directory / "matrix-cert.pem"
    private_key = directory / "matrix-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return certificate, private_key


def free_tcp_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def tcp_port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def write_test_home(
    home: Path,
    *,
    matrix: FakeMatrix,
    responses: FakeResponses,
    models: Path,
) -> None:
    plugin_root = home / "plugins" / "cache" / "local-test" / "matrix" / "0.12.5"
    shutil.copytree(PLUGIN_ROOT, plugin_root)
    manifest_path = plugin_root / ".xedoc-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["extensions"][0]["entrypoint"] = "./extensions/matrix-e2e-wrapper.sh"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")))
    wrapper = plugin_root / "extensions" / "matrix-e2e-wrapper.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"tee {shlex.quote(str(home / 'matrix-child-stdin.jsonl'))} | "
        f'{shlex.quote(sys.executable)} "$(dirname "$0")/matrix.py" | '
        f"tee {shlex.quote(str(home / 'matrix-child-stdout.jsonl'))}\n"
    )
    wrapper.chmod(0o700)
    extension_state = home / "extensions" / "matrix"
    extension_state.mkdir(parents=True)
    (extension_state / "config.json").write_text(
        json.dumps(
            {
                "homeserver": matrix.homeserver,
                "agentUserId": "@agent:mock",
                "accessToken": "agent-token",
                "refreshToken": "agent-refresh",
                "clientId": "agent-client",
                "targetUserId": "@user:mock",
                "targetAccessToken": "user-token",
                "targetRefreshToken": "user-refresh",
                "targetClientId": "user-client",
                "oauthTokenEndpoint": f"{matrix.homeserver}/token",
            },
            separators=(",", ":"),
        )
    )
    (home / "auth.json").write_text(
        '{"auth_mode":"apikey","OPENAI_API_KEY":"matrix-e2e-key"}\n'
    )
    (home / "config.toml").write_text(
        "\n".join(
            [
                'model = "gpt-5.6-luna"',
                'model_provider = "openai"',
                f'model_catalog_json = "{models}"',
                "suppress_unstable_features_warning = true",
                'sandbox_mode = "read-only"',
                'approval_policy = "never"',
                "auto_session_name = false",
                f'openai_base_url = "{responses.base_url}"',
                "",
                "[shell_environment_policy]",
                'inherit = "all"',
                "",
                "[features]",
                "plugins = true",
                "default_mode_request_user_input = true",
                "",
                '[plugins."matrix@local-test"]',
                "enabled = true",
                "",
            ]
        )
    )


@contextmanager
def tmux_session(name: str, command: str, environment: dict[str, str]) -> Any:
    keep_pane_open = (
        f"({command}); exit_code=$?; "
        'printf "\\n[MATRIX_E2E_PROCESS_EXIT %s]\\n" "$exit_code"; '
        "exec sleep 300"
    )
    tmux(
        "new-session",
        "-d",
        "-s",
        name,
        "env",
        *[f"{key}={value}" for key, value in environment.items()],
        "zsh",
        "-lc",
        keep_pane_open,
    )
    try:
        yield name
    finally:
        tmux("kill-session", "-t", name, check=False)


def test_matrix_reply_completes_real_tui_request_user_input() -> None:
    binary_value = os.environ.get("XEDOC_MATRIX_E2E_BIN")
    models_value = os.environ.get("XEDOC_MATRIX_E2E_MODELS")
    if not binary_value or not models_value:
        pytest.skip(
            "set XEDOC_MATRIX_E2E_BIN and XEDOC_MATRIX_E2E_MODELS to run real TUI E2E"
        )
    binary = Path(binary_value).resolve()
    models = Path(models_value).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail(f"XEDOC_MATRIX_E2E_BIN is not executable: {binary}")
    if not models.is_file():
        pytest.fail(f"XEDOC_MATRIX_E2E_MODELS is missing: {models}")
    if shutil.which("tmux") is None or shutil.which("openssl") is None:
        pytest.skip("tmux and openssl are required for this isolated E2E")

    with tempfile.TemporaryDirectory(prefix="xedoc-matrix-e2e-") as temporary:
        root = Path(temporary)
        home = root / "home"
        test_bin = home / "test-bin"
        test_bin.mkdir(parents=True)
        (test_bin / "python3").symlink_to(Path(sys.executable))
        certificate, private_key = generate_certificate(root)
        matrix = FakeMatrix(certificate, private_key)
        responses = FakeResponses()
        matrix.start()
        responses.start()
        server_name = f"matrix-e2e-server-{uuid4().hex}"
        tui_name = f"matrix-e2e-tui-{uuid4().hex}"
        port = free_tcp_port()
        endpoint = f"ws://127.0.0.1:{port}"
        environment = {
            "XEDOC_HOME": str(home),
            "SSL_CERT_FILE": str(certificate),
            "OPENAI_API_KEY": "matrix-e2e-key",
            "PATH": f"{test_bin}:{os.environ['PATH']}",
        }
        failure_details = ""
        write_test_home(
            home, matrix=matrix, responses=responses, models=models
        )
        try:
            with tmux_session(
                server_name,
                f"exec {binary} app-server --listen {endpoint}",
                environment,
            ):
                wait_for(
                    "temporary app-server websocket port",
                    lambda: tcp_port_is_open(port),
                )
                with tmux_session(
                    tui_name,
                    f"exec {binary} --remote {endpoint} --no-alt-screen -C {REPO_ROOT}",
                    environment,
                ):
                    try:
                        wait_for(
                            "initial TUI screen",
                            lambda: bool(pane_text(tui_name).strip()),
                        )
                        if "Meet GPT-6 Luna" in pane_text(tui_name):
                            press(tui_name, "Down")
                            press(tui_name, "Enter")
                        wait_for(
                            "Xedoc welcome screen",
                            lambda: "To get started, describe a task"
                            in pane_text(tui_name),
                        )
                        wait_for(
                            "Matrix extension setup or approval",
                            lambda: (
                                "Matrix bridge is already configured."
                                in pane_text(tui_name)
                                or "Allow “Matrix (Element)” in this session?"
                                in visible_pane_text(tui_name)
                            ),
                        )
                        if "Allow “Matrix (Element)” in this session?" in visible_pane_text(
                            tui_name
                        ):
                            press(tui_name, "Enter")
                            wait_for(
                                "Matrix extension approval to close",
                                lambda: "Allow “Matrix (Element)” in this session?"
                                not in visible_pane_text(tui_name),
                            )
                        wait_for(
                            "Matrix extension setup",
                            lambda: "Matrix bridge is already configured."
                            in pane_text(tui_name),
                        )
                    except WaitTimeout:
                        failure_details = pane_text(tui_name)
                        raise
                    thread_id = app_server_thread_id(endpoint)
                    commands = app_server_request(
                        endpoint,
                        "sessionExtension/list",
                        {"threadId": thread_id},
                    )
                    assert commands == {
                        "id": "matrix-e2e-request",
                        "result": {
                            "commands": [
                                {
                                    "extensionId": "matrix:matrix",
                                    "name": "matrix",
                                    "description": (
                                        "Manage Matrix bridge status, setup, lifecycle "
                                        "debugging, and help with /matrix."
                                    ),
                                }
                            ]
                        },
                    }, commands
                    enable = app_server_request(
                        endpoint,
                        "sessionExtension/command/invoke",
                        {
                            "threadId": thread_id,
                            "extensionId": "matrix:matrix",
                            "command": "matrix",
                            "arguments": ["on"],
                        },
                    )
                    assert enable == {
                        "id": "matrix-e2e-request",
                        "result": {},
                    }, enable
                    try:
                        wait_for(
                            "Matrix bridge connection",
                            lambda: matrix.has_sent("Matrix bridge connected."),
                        )
                    except WaitTimeout:
                        failure_details = pane_text(tui_name)
                        raise
                    turn = app_server_request(
                        endpoint,
                        "turn/start",
                        {
                            "threadId": thread_id,
                            "input": [
                                {
                                    "type": "text",
                                    "text": "ask the Matrix E2E question",
                                }
                            ],
                        },
                    )
                    assert turn.get("id") == "matrix-e2e-request", turn
                    assert isinstance(turn.get("result", {}).get("turn"), dict), turn
                    wait_for(
                        "real Xedoc question screen",
                        lambda: "Which option do you choose?" in pane_text(tui_name),
                    )
                    wait_for(
                        "question mirrored by Matrix",
                        lambda: matrix.has_sent("Which option do you choose?"),
                    )
                    prompt_visible_at = time.monotonic()
                    wait_for(
                        "both prompt channels to remain available beyond the old lease",
                        lambda: (
                            time.monotonic() - prompt_visible_at >= 11
                            and "Which option do you choose?" in pane_text(tui_name)
                            and matrix.has_sent("Which option do you choose?")
                        ),
                        timeout_seconds=15,
                    )
                    matrix.enqueue_target_message("2")
                    wait_for(
                        "model continuation with Matrix Option B",
                        lambda: FINAL_MARKER in pane_text(tui_name),
                        timeout_seconds=45,
                    )
                    assert responses.saw_option_b_answer(), (
                        "model continuation did not receive the Option B tool result: "
                        f"{responses.requests!r}"
                    )
        except WaitTimeout as error:
            pytest.fail(
                f"{error}\n\nTUI pane:\n{failure_details or pane_text(tui_name)}\n\n"
                f"Matrix agent messages:\n{matrix.sent_bodies!r}\n\n"
                f"Matrix event log:\n{chr(10).join(matrix.event_log)}\n\n"
                "Matrix child stdin:\n"
                f"{read_test_log(home / 'matrix-child-stdin.jsonl')}\n\n"
                "Matrix child stdout:\n"
                f"{read_test_log(home / 'matrix-child-stdout.jsonl')}\n\n"
                f"Responses request count: {len(responses.requests)}\n"
                f"Responses saw Option B: {responses.saw_option_b_answer()}"
            )
        finally:
            responses.close()
            matrix.close()
