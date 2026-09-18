from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest


EXTENSIONS_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = EXTENSIONS_ROOT.parent
REPO_ROOT = PLUGIN_ROOT.parents[1]
sys.path.insert(0, str(EXTENSIONS_ROOT))
SPEC = importlib.util.spec_from_file_location(
    "matrix_extension", EXTENSIONS_ROOT / "matrix.py"
)
assert SPEC and SPEC.loader
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "matrix"
    monkeypatch.setattr(matrix, "CONFIG_ROOT", root)
    monkeypatch.setattr(matrix, "CONFIG_PATH", root / "config.json")
    monkeypatch.setattr(matrix, "ROOMS_ROOT", root / "rooms")
    return root


def valid_values(**overrides: str) -> dict[str, str]:
    values = {
        "homeserver": "https://matrix.example.org",
        "agent-user-id": "@xedoc:example.org",
        "access-token": "secret-token",
        "target-user-id": "@andreas:example.org",
    }
    values.update(overrides)
    return values


def extension_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": matrix.PROTOCOL,
        "requestId": "request-1",
        "method": method,
        "context": {},
        "params": params,
    }


def test_setup_form_collects_agent_credentials_and_target_account() -> None:
    result = matrix.setup_interaction(
        extension_request("extension.setup.open", {}),
        {
            "homeserver": "https://matrix.example.org",
            "agentUserId": "@xedoc:example.org",
            "accessToken": "saved",
            "targetUserId": "@andreas:example.org",
        },
    )

    fields = {
        field["id"]: field
        for field in result["result"]["interaction"]["surface"]["fields"]
    }
    assert set(fields) == {
        "homeserver",
        "agent-user-id",
        "access-token",
        "target-user-id",
    }
    assert fields["agent-user-id"]["value"] == "@xedoc:example.org"
    assert fields["target-user-id"]["value"] == "@andreas:example.org"
    assert fields["access-token"]["sensitive"] is True
    assert fields["access-token"]["value"] == ""
    assert "keep the saved token" in fields["access-token"]["description"]


def test_setup_response_persists_private_config(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = extension_request(
        "interaction.respond",
        {"continuation": "matrix-setup", "values": valid_values()},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    assert matrix.run_one_shot() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["result"]["summary"] == "Matrix bridge configured."
    assert matrix.load_json(matrix.CONFIG_PATH) == {
        "homeserver": "https://matrix.example.org",
        "agentUserId": "@xedoc:example.org",
        "accessToken": "secret-token",
        "targetUserId": "@andreas:example.org",
    }
    assert stat.S_IMODE(isolated_config.stat().st_mode) == 0o700
    assert stat.S_IMODE(matrix.CONFIG_PATH.stat().st_mode) == 0o600


def test_setup_open_skips_form_for_complete_config(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    matrix.write_private_json(
        matrix.CONFIG_PATH,
        matrix.normalize_config(valid_values()),
    )
    request = extension_request("extension.setup.open", {})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    assert matrix.run_one_shot() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["result"] == {
        "kind": "complete",
        "summary": "Matrix bridge is already configured.",
    }


def test_setup_command_reopens_settings(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    matrix.write_private_json(
        matrix.CONFIG_PATH,
        matrix.normalize_config(valid_values()),
    )
    request = extension_request(
        "extension.command.invoke",
        {"arguments": ["setup"]},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    assert matrix.run_one_shot() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["result"]["kind"] == "interaction"
    assert output["result"]["interaction"]["surface"]["id"] == "matrix-settings"


def test_normalize_config_preserves_saved_token_and_rejects_unsafe_values() -> None:
    config = matrix.normalize_config(
        valid_values(**{"access-token": ""}),
        {"accessToken": "saved-token"},
    )
    assert config["accessToken"] == "saved-token"

    with pytest.raises(ValueError, match="HTTPS"):
        matrix.normalize_config(
            valid_values(homeserver="http://matrix.example.org")
        )
    with pytest.raises(ValueError, match="different"):
        matrix.normalize_config(
            valid_values(**{"target-user-id": "@xedoc:example.org"})
        )


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode()


class FakeOpener:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.request: Any = None
        self.timeout: float | None = None

    def open(self, request: Any, timeout: float) -> FakeResponse:
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.payload)


class SequenceOpener:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = iter(payloads)
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: float) -> FakeResponse:
        self.requests.append(request)
        return FakeResponse(next(self.payloads))


def test_matrix_client_uses_bearer_header_and_json_body(
) -> None:
    opener = FakeOpener({"room_id": "!room:example.org"})
    client = matrix.MatrixClient(
        matrix.normalize_config(valid_values()), opener=opener
    )

    assert (
        client.create_room("A session", "@andreas:example.org")
        == "!room:example.org"
    )

    request = opener.request
    assert request.full_url == "https://matrix.example.org/_matrix/client/v3/createRoom"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert "secret-token" not in request.full_url
    assert json.loads(request.data) == {
        "invite": ["@andreas:example.org"],
        "is_direct": True,
        "name": "Xedoc: A session",
        "preset": "trusted_private_chat",
        "topic": "Private Xedoc session bridge used from Element.",
    }


def test_matrix_client_updates_room_name() -> None:
    opener = FakeOpener({})
    client = matrix.MatrixClient(
        matrix.normalize_config(valid_values()), opener=opener
    )

    client.update_room_name("!room:example.org", "Renamed session")

    assert (
        opener.request.full_url
        == "https://matrix.example.org/_matrix/client/v3/rooms/"
        "%21room%3Aexample.org/state/m.room.name"
    )
    assert json.loads(opener.request.data) == {"name": "Xedoc: Renamed session"}


def test_authenticated_matrix_requests_never_follow_redirects() -> None:
    client = matrix.MatrixClient(matrix.normalize_config(valid_values()))
    handler = next(
        handler
        for handler in client.opener.handlers
        if isinstance(handler, matrix.RejectRedirectHandler)
    )
    original = matrix.Request(
        "https://matrix.example.org/_matrix/client/v3/sync",
        headers={"Authorization": "Bearer secret-token"},
    )

    redirected = handler.redirect_request(
        original,
        None,
        302,
        "Found",
        {"Location": "http://attacker.example/token"},
        "http://attacker.example/token",
    )

    assert redirected is None


def test_backfill_paginates_forward_across_sync_gap() -> None:
    opener = SequenceOpener(
        [
            {
                "chunk": [{"event_id": "$one"}, {"event_id": "$two"}],
                "end": "middle",
            },
            {"chunk": [{"event_id": "$three"}], "end": "gap-end"},
        ]
    )
    client = matrix.MatrixClient(
        matrix.normalize_config(valid_values()), opener=opener
    )

    events = client.backfill(
        "!room:example.org", "prior-sync", "gap-end"
    )

    assert [event["event_id"] for event in events] == [
        "$one",
        "$two",
        "$three",
    ]
    first_query = parse_qs(urlparse(opener.requests[0].full_url).query)
    second_query = parse_qs(urlparse(opener.requests[1].full_url).query)
    assert first_query == {
        "dir": ["f"],
        "from": ["prior-sync"],
        "to": ["gap-end"],
        "limit": ["100"],
    }
    assert second_query["from"] == ["middle"]
    assert second_query["to"] == ["gap-end"]


def test_backfill_continues_after_empty_page_and_accepts_terminal_page() -> None:
    opener = SequenceOpener(
        [
            {"chunk": [], "end": "middle"},
            {"chunk": [{"event_id": "$one"}]},
        ]
    )
    client = matrix.MatrixClient(
        matrix.normalize_config(valid_values()), opener=opener
    )

    events = client.backfill(
        "!room:example.org", "prior-sync", "gap-end"
    )

    assert [event["event_id"] for event in events] == ["$one"]
    assert len(opener.requests) == 2
    second_query = parse_qs(urlparse(opener.requests[1].full_url).query)
    assert second_query["from"] == ["middle"]


def test_ensure_room_reuses_private_thread_mapping_across_restart(
    isolated_config: Path,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.create_calls = 0
            self.update_calls: list[tuple[str, str]] = []
            self.targets: list[str] = []

        def create_room(self, title: str, target_user_id: str) -> str:
            self.create_calls += 1
            assert title == "Session"
            self.targets.append(target_user_id)
            return "!room:example.org"

        def update_room_name(self, room_id: str, title: str) -> None:
            self.update_calls.append((room_id, title))

    first_process = FakeClient()
    config = matrix.normalize_config(valid_values())

    assert (
        matrix.ensure_room(first_process, config, "thread/1", "Session")
        == "!room:example.org"
    )
    assert first_process.create_calls == 1

    restarted_process = FakeClient()
    assert (
        matrix.ensure_room(restarted_process, config, "thread/1", "Session")
        == "!room:example.org"
    )
    assert restarted_process.create_calls == 0
    assert restarted_process.update_calls == []
    stored = matrix.room_path("thread/1")
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert matrix.load_json(stored) == {
        "roomId": "!room:example.org",
        "title": "Session",
        "homeserver": "https://matrix.example.org",
        "agentUserId": "@xedoc:example.org",
        "targetUserId": "@andreas:example.org",
    }

    changed = matrix.normalize_config(
        valid_values(**{"target-user-id": "@other:example.org"})
    )
    assert (
        matrix.ensure_room(first_process, changed, "thread/1", "Session")
        == "!room:example.org"
    )
    assert first_process.create_calls == 2
    assert first_process.targets == ["@andreas:example.org", "@other:example.org"]


def test_ensure_room_updates_name_without_replacing_persistent_binding(
    isolated_config: Path,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.create_calls = 0
            self.update_calls: list[tuple[str, str]] = []

        def create_room(self, _title: str, _target_user_id: str) -> str:
            self.create_calls += 1
            return "!room:example.org"

        def update_room_name(self, room_id: str, title: str) -> None:
            self.update_calls.append((room_id, title))

    client = FakeClient()
    config = matrix.normalize_config(valid_values())
    assert matrix.ensure_room(client, config, "thread/1", "Before") == "!room:example.org"
    assert matrix.ensure_room(client, config, "thread/1", "After") == "!room:example.org"

    assert client.create_calls == 1
    assert client.update_calls == [("!room:example.org", "After")]
    assert matrix.load_json(matrix.room_path("thread/1"))["title"] == "After"


def test_inbound_messages_accepts_only_target_text_and_ignores_edits() -> None:
    events = [
        {
            "type": "m.room.message",
            "sender": "@andreas:example.org",
            "content": {"msgtype": "m.text", "body": "continue"},
        },
        {
            "type": "m.room.message",
            "sender": "@stranger:example.org",
            "content": {"msgtype": "m.text", "body": "ignore"},
        },
        {
            "type": "m.room.message",
            "sender": "@andreas:example.org",
            "content": {
                "msgtype": "m.text",
                "body": "edited",
                "m.relates_to": {"rel_type": "m.replace"},
            },
        },
        {
            "type": "m.room.message",
            "sender": "@andreas:example.org",
            "content": {"msgtype": "m.image", "body": "image.png"},
        },
    ]

    assert matrix.inbound_messages(events, "@andreas:example.org") == ["continue"]


def test_turn_messages_mirrors_xedoc_input_and_output_without_matrix_echo() -> None:
    turn = {
        "items": [
            {
                "type": "userMessage",
                "clientId": "xedoc-tui-1",
                "content": [{"type": "text", "text": "Summarize recent commits"}],
            },
            {
                "type": "agentMessage",
                "text": "I will inspect the recent history.",
            },
            {
                "type": "userMessage",
                "clientId": "matrix-123",
                "content": [{"type": "text", "text": "Already in Matrix"}],
            },
            {
                "type": "userMessage",
                "clientId": "xedoc-tui-2",
                "content": [{"type": "image", "url": "mxc://example.org/image"}],
            },
            {"type": "commandExecution", "command": "git log"},
        ]
    }

    assert matrix.turn_messages(turn) == [
        "Summarize recent commits",
        "I will inspect the recent history.",
    ]


def test_limited_sync_backfills_gap_in_order_and_deduplicates() -> None:
    class FakeClient:
        calls: list[tuple[str, str, str]] = []

        def backfill(
            self, room_id: str, from_token: str, to_token: str
        ) -> list[dict[str, Any]]:
            self.calls.append((room_id, from_token, to_token))
            return [
                {"event_id": "$one", "type": "m.room.message"},
                {"event_id": "$two", "type": "m.room.message"},
            ]

    sync = {
        "rooms": {
            "join": {
                "!room:example.org": {
                    "timeline": {
                        "limited": True,
                        "prev_batch": "gap-start",
                        "events": [
                            {"event_id": "$two", "type": "m.room.message"},
                            {"event_id": "$three", "type": "m.room.message"},
                        ],
                    }
                }
            }
        }
    }
    client = FakeClient()

    events = matrix.events_for_sync(
        client, sync, "!room:example.org", "prior-sync"
    )

    assert [event["event_id"] for event in events] == ["$one", "$two", "$three"]
    assert client.calls == [
        ("!room:example.org", "prior-sync", "gap-start")
    ]


def test_limited_sync_without_backfill_token_is_rejected() -> None:
    sync = {
        "rooms": {
            "join": {
                "!room:example.org": {
                    "timeline": {"limited": True, "events": []}
                }
            }
        }
    }

    with pytest.raises(matrix.MatrixError, match="prev_batch"):
        matrix.events_for_sync(
            object(), sync, "!room:example.org", "prior-sync"
        )


def test_prompt_answer_maps_single_and_multi_question_replies() -> None:
    single = {
        "request": {
            "params": {
                "questions": [{"id": "choice", "question": "Continue?"}]
            }
        }
    }
    assert matrix.prompt_answer(single, "yes") == {
        "kind": "requestUserInput",
        "answers": {"choice": {"answers": ["yes"]}},
    }

    multiple = {
        "request": {
            "params": {
                "questions": [
                    {"id": "first", "question": "First?"},
                    {"id": "second", "question": "Second?"},
                ]
            }
        }
    }
    assert matrix.prompt_answer(
        multiple, '{"first":["a"],"second":"b"}'
    ) == {
        "kind": "requestUserInput",
        "answers": {
            "first": {"answers": ["a"]},
            "second": {"answers": ["b"]},
        },
    }
    assert matrix.prompt_answer(multiple, "not-json") is None


def test_repository_registers_matrix_and_removes_signal() -> None:
    versions = json.loads((REPO_ROOT / "plugin-versions.json").read_text())
    marketplace = json.loads(
        (REPO_ROOT / ".agents/plugins/marketplace.json").read_text()
    )
    names = {entry["name"] for entry in marketplace["plugins"]}

    assert versions["plugins"]["matrix"] == {
        "version": "0.3.0",
        "hosts": ["xedoc"],
    }
    assert "signal" not in versions["plugins"]
    assert "matrix" in names
    assert "signal" not in names
    assert not (REPO_ROOT / "plugins/signal").exists()
    assert os.access(EXTENSIONS_ROOT / "matrix.py", os.X_OK)
