from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import stat
import sys
import threading
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
    legacy_root = tmp_path / "legacy-matrix"
    monkeypatch.setattr(matrix, "CONFIG_ROOT", root)
    monkeypatch.setattr(matrix, "CONFIG_PATH", root / "config.json")
    monkeypatch.setattr(matrix, "ROOMS_ROOT", root / "rooms")
    monkeypatch.setattr(matrix, "LEGACY_CONFIG_ROOT", legacy_root)
    monkeypatch.setattr(matrix, "LEGACY_CONFIG_PATH", legacy_root / "config.json")
    monkeypatch.setattr(matrix, "LEGACY_ROOMS_ROOT", legacy_root / "rooms")
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


def valid_oauth_config() -> dict[str, str]:
    return {
        **matrix.normalize_config(valid_values()),
        "refreshToken": "agent-refresh",
        "clientId": "agent-client",
        "targetAccessToken": "user-token",
        "targetRefreshToken": "user-refresh",
        "targetClientId": "user-client",
        "oauthTokenEndpoint": "https://account.example.org/oauth2/token",
    }


def extension_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": matrix.PROTOCOL,
        "requestId": "request-1",
        "method": method,
        "context": {},
        "params": params,
    }


def test_setup_form_collects_homeserver_for_oauth_login() -> None:
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
    assert set(fields) == {"homeserver"}
    assert fields["homeserver"]["value"] == "https://matrix.example.org"
    assert "OAuth" in result["result"]["interaction"]["surface"]["subtitle"]


def test_setup_response_persists_private_config(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        matrix,
        "begin_oauth_login",
        lambda _homeserver, role: {
            "clientId": f"{role}-client",
            "deviceCode": f"{role}-device-code",
            "userCode": f"{role}-code",
            "verificationUri": f"https://account.example.org/{role}",
            "tokenEndpoint": "https://account.example.org/oauth2/token",
            "expiresAt": 4_000_000_000,
            "role": role,
        },
    )
    request = extension_request(
        "interaction.respond",
        {
            "continuation": "matrix-setup",
            "values": {"homeserver": "https://matrix.example.org"},
        },
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    assert matrix.run_one_shot() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["result"]["interaction"]["id"] == "matrix-oauth"
    config = matrix.load_json(matrix.CONFIG_PATH)
    assert config["homeserver"] == "https://matrix.example.org"
    assert set(config["pendingOAuth"]) == {"agent", "user"}
    assert stat.S_IMODE(isolated_config.stat().st_mode) == 0o700
    assert stat.S_IMODE(matrix.CONFIG_PATH.stat().st_mode) == 0o600


def test_oauth_completion_preserves_first_account_while_second_is_pending(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pending = {
        "agent": {
            "clientId": "agent-client",
            "deviceCode": "agent-device",
            "tokenEndpoint": "https://account.example.org/oauth2/token",
            "userCode": "agent-code",
            "verificationUri": "https://account.example.org/agent",
            "expiresAt": 4_000_000_000,
        },
        "user": {
            "clientId": "user-client",
            "deviceCode": "user-device",
            "tokenEndpoint": "https://account.example.org/oauth2/token",
            "userCode": "user-code",
            "verificationUri": "https://account.example.org/user",
            "expiresAt": 4_000_000_000,
        },
    }
    matrix.write_private_json(
        matrix.CONFIG_PATH,
        {"homeserver": "https://matrix.example.org", "pendingOAuth": pending},
    )
    request = extension_request(
        "interaction.respond",
        {"continuation": "matrix-oauth-complete", "values": {}},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    def complete(authorization: dict[str, Any]) -> dict[str, str]:
        if authorization["clientId"] == "user-client":
            raise matrix.OAuthError("authorization_pending")
        return {
            "accessToken": "agent-access",
            "refreshToken": "agent-refresh",
            "clientId": "agent-client",
            "tokenEndpoint": "https://account.example.org/oauth2/token",
        }

    monkeypatch.setattr(matrix, "complete_oauth_login", complete)

    assert matrix.run_one_shot() == 0
    saved = matrix.load_json(matrix.CONFIG_PATH)
    assert saved["pendingOAuth"]["credentials"]["agent"]["accessToken"] == (
        "agent-access"
    )
    assert json.loads(capsys.readouterr().out)["result"]["interaction"]["id"] == (
        "matrix-oauth"
    )


def test_oauth_completion_configures_distinct_agent_and_user_accounts(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    matrix.write_private_json(
        matrix.CONFIG_PATH,
        {
            "homeserver": "https://matrix.example.org",
            "pendingOAuth": {
                role: {
                    "clientId": f"{role}-client",
                    "deviceCode": f"{role}-device",
                    "tokenEndpoint": "https://account.example.org/oauth2/token",
                    "userCode": f"{role}-code",
                    "verificationUri": f"https://account.example.org/{role}",
                    "expiresAt": 4_000_000_000,
                }
                for role in ("agent", "user")
            },
        },
    )
    request = extension_request(
        "interaction.respond",
        {"continuation": "matrix-oauth-complete", "values": {}},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(
        matrix,
        "complete_oauth_login",
        lambda authorization: {
            "accessToken": f"{authorization['clientId']}-access",
            "refreshToken": f"{authorization['clientId']}-refresh",
            "clientId": str(authorization["clientId"]),
            "tokenEndpoint": "https://account.example.org/oauth2/token",
        },
    )
    monkeypatch.setattr(
        matrix,
        "configured_oauth_account",
        lambda _homeserver, role, credentials: (
            "@agent:example.org" if role == "agent" else "@user:example.org",
            credentials,
        ),
    )

    assert matrix.run_one_shot() == 0
    saved = matrix.load_json(matrix.CONFIG_PATH)
    assert saved["agentUserId"] == "@agent:example.org"
    assert saved["targetUserId"] == "@user:example.org"
    assert saved["accessToken"] == "agent-client-access"
    assert saved["targetAccessToken"] == "user-client-access"
    assert "pendingOAuth" not in saved
    assert "Matrix accounts connected with OAuth." == json.loads(
        capsys.readouterr().out
    )["result"]["summary"]


def test_load_settings_migrates_legacy_configuration_once(
    isolated_config: Path,
) -> None:
    legacy = matrix.normalize_config(valid_values())
    legacy["debug"] = True
    matrix.write_private_json(matrix.LEGACY_CONFIG_PATH, legacy)

    assert matrix.load_settings() == legacy
    assert matrix.load_json(matrix.CONFIG_PATH) == legacy

    matrix.write_private_json(
        matrix.LEGACY_CONFIG_PATH,
        matrix.normalize_config(
            valid_values(**{"access-token": "replacement-legacy-token"})
        ),
    )
    assert matrix.load_settings() == legacy


def test_setup_preserves_existing_config_when_oauth_login_cannot_start(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = valid_oauth_config()
    matrix.write_private_json(matrix.CONFIG_PATH, saved)
    request = extension_request(
        "interaction.respond",
        {
            "continuation": "matrix-setup",
            "values": {"homeserver": "https://matrix.example.org"},
        },
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    monkeypatch.setattr(
        matrix,
        "begin_oauth_login",
        lambda _homeserver, _role: (_ for _ in ()).throw(
            matrix.OAuthError("metadata_failed")
        ),
    )

    with pytest.raises(matrix.OAuthError, match="metadata_failed"):
        matrix.run_one_shot()

    assert matrix.load_json(matrix.CONFIG_PATH) == saved


def test_verify_access_token_requires_the_configured_agent_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WrongAccountClient:
        def __init__(self, _config: dict[str, str]) -> None:
            pass

        def whoami(self) -> str:
            return "@other:example.org"

    monkeypatch.setattr(matrix, "MatrixClient", WrongAccountClient)

    with pytest.raises(matrix.MatrixError, match="@other:example.org"):
        matrix.verify_access_token(matrix.normalize_config(valid_values()))


def test_cancelled_setup_does_not_write_config(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = extension_request(
        "interaction.respond",
        {"continuation": "matrix-setup", "outcome": "cancelled"},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    assert matrix.run_one_shot() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["result"]["summary"] == "Matrix setup cancelled."
    assert not isolated_config.exists()


def test_setup_open_skips_form_for_complete_config(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    matrix.write_private_json(
        matrix.CONFIG_PATH,
        valid_oauth_config(),
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
        valid_oauth_config(),
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


def test_help_lists_all_matrix_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = extension_request(
        "extension.command.invoke", {"arguments": ["help"]}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    assert matrix.run_one_shot() == 0

    summary = json.loads(capsys.readouterr().out)["result"]["summary"]
    for command in (
        "/matrix on",
        "/matrix off",
        "/matrix restart",
        "/matrix setup",
        "/matrix debug on|off",
        "/matrix help",
    ):
        assert command in summary


def test_debug_command_persists_lifecycle_setting(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    matrix.write_private_json(
        matrix.CONFIG_PATH, valid_oauth_config()
    )
    request = extension_request(
        "extension.command.invoke", {"arguments": ["debug", "on"]}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    assert matrix.run_one_shot() == 0
    assert json.loads(capsys.readouterr().out)["result"]["summary"] == (
        "Matrix lifecycle messages enabled."
    )
    assert matrix.load_json(matrix.CONFIG_PATH)["debug"] is True

    request = extension_request(
        "extension.command.invoke", {"arguments": ["debug", "off"]}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    assert matrix.run_one_shot() == 0
    assert matrix.load_json(matrix.CONFIG_PATH)["debug"] is False


def test_current_debug_setting_reloads_persisted_value(
    isolated_config: Path,
) -> None:
    config = matrix.normalize_config(valid_values())
    matrix.write_private_json(matrix.CONFIG_PATH, config)
    assert matrix.current_debug_setting() is False

    config["debug"] = True
    matrix.write_private_json(matrix.CONFIG_PATH, config)
    assert matrix.current_debug_setting() is True

    config["debug"] = False
    matrix.write_private_json(matrix.CONFIG_PATH, config)
    assert matrix.current_debug_setting() is False


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


def test_stored_config_requires_refreshable_oauth_credentials() -> None:
    config = matrix.normalize_config(valid_values())
    config["targetAccessToken"] = "user-access"
    with pytest.raises(ValueError, match="both Matrix accounts"):
        matrix.stored_config(config)


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


def test_matrix_client_joins_room_as_the_user_account() -> None:
    opener = FakeOpener({"room_id": "!room:example.org"})
    client = matrix.MatrixClient(
        valid_oauth_config(), opener=opener, role="user"
    )

    client.join_room("!room:example.org")

    assert (
        opener.request.full_url
        == "https://matrix.example.org/_matrix/client/v3/join/%21room%3Aexample.org"
    )
    assert opener.request.get_header("Authorization") == "Bearer user-token"


def test_matrix_client_sends_rich_html_with_plaintext_fallback() -> None:
    opener = FakeOpener({"event_id": "$event"})
    client = matrix.MatrixClient(
        matrix.normalize_config(valid_values()), opener=opener
    )

    assert (
        client.send_text(
            "!room:example.org",
            "# Completed\n\n- `src/main.py`\n- **2 tests**",
            "success",
        )
        == "$event"
    )

    assert json.loads(opener.request.data) == {
        "msgtype": "m.text",
        "body": "# Completed\n\n- `src/main.py`\n- **2 tests**",
        "format": "org.matrix.custom.html",
        "formatted_body": (
            '<h1><span data-mx-color="#16a34a">Completed</span></h1>'
            "<ul><li><code>src/main.py</code></li>"
            "<li><strong>2 tests</strong></li></ul>"
        ),
    }


def test_matrix_formatted_body_escapes_html_and_preserves_code_blocks() -> None:
    assert matrix.matrix_formatted_body(
        "Use <script>alert(1)</script>\n\n```python\nprint('<safe>')\n```"
    ) == (
        "<p>Use &lt;script&gt;alert(1)&lt;/script&gt;</p>"
        "<pre><code class=\"language-python\">print(&#x27;&lt;safe&gt;&#x27;)"
        "</code></pre>"
    )


def test_matrix_formatted_body_colors_file_change_counts_only_when_requested() -> None:
    text = (
        "File changes applied (2 file(s)): (+3 -1)\n"
        "- [src/main.py](https://example.org/delta=-2&added=+3) [update] (+2 -1)"
    )

    assert matrix.matrix_formatted_body(text) == (
        "<p>File changes applied (2 file(s)): (+3 -1)</p>"
        '<ul><li><a href="https://example.org/delta=-2&amp;added=+3">src/main.py</a>'
        " [update] (+2 -1)</li></ul>"
    )
    assert matrix.matrix_formatted_body(text, color_file_change_counts=True) == (
        "<p>File changes applied (2 file(s)): "
        '(<span data-mx-color="#16a34a">+3</span> '
        '<span data-mx-color="#dc2626">-1</span>)</p>'
        '<ul><li><a href="https://example.org/delta=-2&amp;added=+3">src/main.py</a>'
        " [update] "
        '(<span data-mx-color="#16a34a">+2</span> '
        '<span data-mx-color="#dc2626">-1</span>)</li></ul>'
    )


def test_matrix_formatted_body_keeps_link_query_parameters() -> None:
    assert matrix.matrix_formatted_body(
        "[Open](https://example.org/path?one=1&two=2)"
    ) == (
        '<p><a href="https://example.org/path?one=1&amp;two=2">Open</a></p>'
    )


def test_oauth_metadata_uses_the_supported_client_api_endpoint() -> None:
    opener = FakeOpener(
        {
            "registration_endpoint": "https://account.example.org/register",
            "device_authorization_endpoint": "https://account.example.org/device",
            "token_endpoint": "https://account.example.org/token",
        }
    )

    metadata = matrix.OAuthClient(opener).metadata("https://matrix.example.org")

    assert metadata["token_endpoint"] == "https://account.example.org/token"
    assert (
        opener.request.full_url
        == "https://matrix.example.org/_matrix/client/v1/auth_metadata"
    )


def test_refresh_rotates_and_persists_only_the_account_being_refreshed(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = valid_oauth_config()
    matrix.write_private_json(matrix.CONFIG_PATH, config)

    class FakeOAuthClient:
        def request(
            self, endpoint: str, payload: dict[str, str], **_kwargs: Any
        ) -> dict[str, str]:
            assert endpoint == "https://account.example.org/oauth2/token"
            assert payload == {
                "grant_type": "refresh_token",
                "refresh_token": "user-refresh",
                "client_id": "user-client",
            }
            return {
                "access_token": "rotated-user-access",
                "refresh_token": "rotated-user-refresh",
            }

    monkeypatch.setattr(matrix, "OAuthClient", FakeOAuthClient)

    matrix.refresh_oauth_account(config, "user")

    assert config["targetAccessToken"] == "rotated-user-access"
    assert config["targetRefreshToken"] == "rotated-user-refresh"
    assert config["accessToken"] == "secret-token"
    assert matrix.load_json(matrix.CONFIG_PATH)["targetAccessToken"] == (
        "rotated-user-access"
    )


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


def test_ensure_room_migrates_legacy_thread_binding(
    isolated_config: Path,
) -> None:
    class FakeClient:
        def create_room(self, _title: str, _target_user_id: str) -> str:
            raise AssertionError("existing room binding must be reused")

        def update_room_name(self, _room_id: str, _title: str) -> None:
            raise AssertionError("unchanged room title must not be updated")

    config = matrix.normalize_config(valid_values())
    binding = {
        "roomId": "!room:example.org",
        "title": "Session",
        "homeserver": config["homeserver"],
        "agentUserId": config["agentUserId"],
        "targetUserId": config["targetUserId"],
    }
    matrix.write_private_json(matrix.legacy_room_path("thread/1"), binding)

    assert matrix.ensure_room(FakeClient(), config, "thread/1", "Session") == (
        "!room:example.org"
    )
    assert matrix.load_json(matrix.room_path("thread/1")) == binding


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
    lifecycle: list[str] = []
    assert (
        matrix.ensure_room(
            client, config, "thread/1", "Before", lifecycle.append
        )
        == "!room:example.org"
    )
    assert (
        matrix.ensure_room(
            client, config, "thread/1", "After", lifecycle.append
        )
        == "!room:example.org"
    )

    assert client.create_calls == 1
    assert client.update_calls == [("!room:example.org", "After")]
    assert matrix.load_json(matrix.room_path("thread/1"))["title"] == "After"
    assert lifecycle == [
        "Matrix room created as 'Xedoc: Before'.",
        "Matrix room renamed to 'Xedoc: After'.",
    ]


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


def test_inbound_messages_ignores_xedoc_messages_sent_with_the_user_account() -> None:
    event = {
        "event_id": "$from-xedoc",
        "type": "m.room.message",
        "sender": "@andreas:example.org",
        "content": {"msgtype": "m.text", "body": "do not loop"},
    }

    ignored = {"$from-xedoc"}
    assert matrix.inbound_messages([event], "@andreas:example.org", ignored) == []
    assert ignored == set()


def test_receiver_waits_for_user_send_to_register_its_echo_event() -> None:
    stop = threading.Event()
    synced = threading.Event()
    sent_event_ids: set[str] = set()
    sent_event_ids_lock = threading.Lock()
    sent_event_ids_lock.acquire()

    class FakeClient:
        calls = 0

        def sync(
            self, _room_id: str, _since: str, _timeout: int
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                synced.set()
                return {
                    "next_batch": "next",
                    "rooms": {
                        "join": {
                            "!room:example.org": {
                                "timeline": {
                                    "events": [
                                        {
                                            "event_id": "$from-xedoc",
                                            "type": "m.room.message",
                                            "sender": "@andreas:example.org",
                                            "content": {
                                                "msgtype": "m.text",
                                                "body": "do not loop",
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    },
                }
            stop.wait(0.01)
            return {"next_batch": "next", "rooms": {}}

    inbound: queue.Queue[str] = queue.Queue()
    host_messages: queue.Queue[tuple[str, str]] = queue.Queue()
    receiver = threading.Thread(
        target=matrix.receive_messages,
        args=(
            FakeClient(),
            "!room:example.org",
            "@andreas:example.org",
            "prior",
            inbound,
            host_messages,
            lambda: False,
            sent_event_ids,
            sent_event_ids_lock,
            stop,
        ),
    )
    receiver.start()
    assert synced.wait(1)
    assert inbound.empty()
    sent_event_ids.add("$from-xedoc")
    sent_event_ids_lock.release()
    stop.set()
    receiver.join(1)

    assert inbound.empty()
    assert not receiver.is_alive()


def test_native_user_message_text_mirrors_xedoc_input_without_matrix_echo() -> None:
    native_message = {
        "type": "userMessage",
        "clientId": "xedoc-tui-1",
        "content": [
            {"type": "text", "text": "Summarize recent commits"},
            {"type": "text", "text": " and show the diff"},
        ],
    }
    matrix_message = {
        "type": "userMessage",
        "clientId": "matrix-123",
        "content": [{"type": "text", "text": "Already in Matrix"}],
    }

    assert (
        matrix.native_user_message_text(native_message)
        == "Summarize recent commits and show the diff"
    )
    assert matrix.native_user_message_text(matrix_message) is None


def test_completed_agent_message_text_only_mirrors_completed_model_output() -> None:
    assert matrix.completed_agent_message_text(
        {"type": "agentMessage", "text": "Done."}
    ) == "Done."
    assert matrix.completed_agent_message_text(
        {"type": "userMessage", "text": "Not model output"}
    ) is None
    assert matrix.completed_agent_message_text(
        {"type": "agentMessage", "text": " "}
    ) is None


def test_file_change_message_includes_edit_summary() -> None:
    item = {
        "id": "change-1",
        "type": "fileChange",
        "status": "completed",
        "changes": [
            {
                "path": "scripts/model-router/reference-router",
                "kind": {"type": "update"},
                "diff": "@@\n-old\n+new\n",
            },
            {
                "path": "scripts/test_model_router_tmux.sh",
                "kind": {"type": "add"},
                "diff": "first\nsecond\n",
            },
            {
                "path": "scripts/obsolete.sh",
                "kind": {"type": "delete"},
                "diff": "old-first\nold-second\n",
            },
        ],
    }

    message = matrix.file_change_message(item)
    assert message is not None
    assert "File changes applied (3 file(s)): (+3 -3)" in message
    assert "- scripts/model-router/reference-router [update] (+1 -1)" in message
    assert "- scripts/test_model_router_tmux.sh [add] (+2 -0)" in message
    assert "- scripts/obsolete.sh [delete] (+0 -2)" in message
    assert matrix.file_change_tone(item) == "success"


def test_file_change_items_from_notification_handles_live_event() -> None:
    item = {
        "id": "change-1",
        "type": "fileChange",
        "status": "completed",
        "changes": [{"path": "README.md", "kind": {"type": "update"}, "diff": "@@\n"}],
    }

    assert matrix.file_change_items_from_notification(
        {"method": "item/completed", "params": {"item": item}}
    ) == [item]
    assert matrix.file_change_items_from_notification(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "itemsView": "notLoaded",
                    "items": [],
                }
            },
        }
    ) == []


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


def test_prompt_answer_maps_numbered_single_and_multi_question_replies() -> None:
    single = {
        "request": {
            "params": {
                "questions": [
                    {
                        "id": "choice",
                        "question": "Continue?",
                        "options": [
                            {"label": "Approve"},
                            {"label": "Deny"},
                        ],
                    }
                ]
            }
        }
    }
    message = matrix.prompt_message(single)
    assert "1 - Approve" in message
    assert "2 - Deny" in message
    assert "Reply with the number only." in message
    assert matrix.prompt_answer(single, "2") == {
        "kind": "requestUserInput",
        "answers": {"choice": {"answers": ["Deny"]}},
    }
    assert matrix.prompt_answer(single, "yes") == {
        "kind": "requestUserInput",
        "answers": {"choice": {"answers": ["yes"]}},
    }

    multiple = {
        "request": {
            "params": {
                "questions": [
                    {
                        "id": "first",
                        "question": "First?",
                        "options": [{"label": "A"}, {"label": "B"}],
                    },
                    {
                        "id": "second",
                        "question": "Second?",
                        "options": [{"label": "C"}, {"label": "D"}],
                    },
                ]
            }
        }
    }
    assert matrix.prompt_answer(multiple, "2, 1") == {
        "kind": "requestUserInput",
        "answers": {
            "first": {"answers": ["B"]},
            "second": {"answers": ["C"]},
        },
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


def test_approval_prompt_renders_and_answers_router_confirmation() -> None:
    prompt = {
        "kind": "extensionInteraction",
        "request": {
            "method": "item/extensionInteraction/request",
            "params": {
                "extensionId": "model-router",
                "interactionId": "route-confirmation",
                "continuation": "route-confirmation",
                "stateRevision": "1",
                "surface": {
                    "type": "confirmation",
                    "title": "Use openai/gpt-5.6-luna/low?",
                    "body": "The reference router requests confirmation.",
                    "details": [{"label": "Scope", "value": "root"}],
                    "sections": [
                        {
                            "title": "Prompt",
                            "rows": [{"text": "and in munich?", "indent": 0}],
                        }
                    ],
                    "actions": [
                        {"id": "accept", "label": "Accept this route"},
                        {"id": "keep", "label": "Keep current route"},
                    ],
                },
            },
        },
    }

    message = matrix.approval_prompt_message(prompt)

    assert "Use openai/gpt-5.6-luna/low?" in message
    assert "Scope: root" in message
    assert "1 - Accept this route" in message
    assert "2 - Keep current route" in message
    assert "Reply with the number only" in message
    assert matrix.approval_answer(prompt, "accept") == {
        "extensionId": "model-router",
        "interactionId": "route-confirmation",
        "continuation": "route-confirmation",
        "stateRevision": "1",
        "outcome": "accepted",
        "action": {"id": "accept"},
        "values": None,
    }
    assert matrix.approval_answer(prompt, "unknown") is None


def test_approval_answer_supports_standard_decisions_and_explicit_json() -> None:
    command = {
        "kind": "commandExecutionApproval",
        "request": {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "reason": "Network access is needed.",
                "availableDecisions": ["accept", "decline"],
            },
        },
    }
    permissions = {
        "kind": "permissionsApproval",
        "request": {
            "method": "item/permissions/requestApproval",
            "params": {},
        },
    }

    command_message = matrix.approval_prompt_message(command)
    assert "1 - Approve" in command_message
    assert "2 - Deny" in command_message
    assert matrix.approval_answer(command, "1") == {"decision": "accept"}
    assert matrix.approval_answer(command, "2") == {"decision": "decline"}
    assert matrix.approval_answer(command, "accept") == {"decision": "accept"}
    assert matrix.approval_answer(command, "cancel") is None
    assert matrix.approval_answer(
        permissions, '{"permissions":{},"scope":"turn"}'
    ) == {"permissions": {}, "scope": "turn"}


def test_extension_interaction_menu_form_and_notice_are_not_trapped() -> None:
    base = {
        "extensionId": "extension",
        "interactionId": "interaction",
        "continuation": "continue",
        "stateRevision": None,
    }
    menu = {
        "kind": "extensionInteraction",
        "request": {
            "method": "item/extensionInteraction/request",
            "params": {
                **base,
                "surface": {
                    "type": "menu",
                    "title": "Choose",
                    "items": [
                        {
                            "id": "item",
                            "action": {"id": "select", "label": "Select"},
                        }
                    ],
                },
            },
        },
    }
    form = {
        "kind": "extensionInteraction",
        "request": {
            "method": "item/extensionInteraction/request",
            "params": {
                **base,
                "surface": {
                    "type": "form",
                    "title": "Settings",
                    "fields": [
                        {
                            "type": "text",
                            "id": "opaque_name",
                            "label": "Name",
                            "description": "A name",
                            "value": "Saved",
                        },
                        {
                            "type": "select",
                            "id": "route",
                            "label": "Route",
                            "description": "Choose a route",
                            "value": "fast",
                            "options": [
                                {"id": "fast", "label": "Fast"},
                                {"id": "safe", "label": "Safe"},
                            ],
                        },
                        {
                            "type": "text",
                            "id": "access_token",
                            "label": "Access token",
                            "description": "Secret",
                            "value": "not-visible",
                            "sensitive": True,
                        },
                    ],
                    "submit": {"id": "save", "label": "Save"},
                },
            },
        },
    }
    notice = {
        "kind": "extensionInteraction",
        "request": {
            "method": "item/extensionInteraction/request",
            "params": {
                **base,
                "surface": {"type": "notice", "title": "Done"},
            },
        },
    }

    assert "1 - Select" in matrix.approval_prompt_message(menu)
    assert matrix.approval_answer(menu, "1")["action"] == {"id": "select"}
    assert matrix.approval_answer(menu, "select")["action"] == {"id": "select"}
    form_message = matrix.approval_prompt_message(form)
    assert 'opaque_name (Name)' in form_message
    assert "Current: \"Saved\"" in form_message
    assert "fast (Fast), safe (Safe)" in form_message
    assert "not-visible" not in form_message
    assert (
        '"action":"save","values":{"opaque_name":"value","route":"value",'
        '"access_token":"value"}' in form_message
    )
    assert matrix.approval_answer(
        form, '{"action":"save","values":{"opaque_name":"Andreas"}}'
    )["values"] == {"opaque_name": "Andreas"}
    assert matrix.approval_answer(form, '{"action":"save","values":[]}') is None
    assert matrix.approval_answer(notice, "dismiss")["outcome"] == "dismissed"


def test_matrix_reply_matches_only_one_pending_prompt() -> None:
    approval = {
        "token": "approval-token",
        "prompt": {
            "kind": "commandExecutionApproval",
            "request": {
                "params": {
                    "availableDecisions": ["accept", "decline"],
                }
            },
        },
    }
    user_input = {"prompt": {"kind": "requestUserInput"}}

    matched = matrix.match_pending_prompt("2", {"approval": approval})
    assert matched == ("approval", approval, "approval", "2")
    assert matrix.match_pending_prompt("2", {"input": user_input}) == (
        "input",
        user_input,
        "prompt",
        "2",
    )
    assert matrix.match_pending_prompt(
        "2", {"approval": approval, "input": user_input}
    ) is None
    assert matrix.match_pending_prompt("2", {"approval": approval, "other": approval}) is None
    assert matrix.is_numbered_approval_reply("1", 2)
    assert matrix.is_numbered_approval_reply("2", 2)
    assert not matrix.is_numbered_approval_reply("3", 2)
    assert not matrix.is_numbered_approval_reply("reply 1", 2)


def test_repository_registers_matrix_and_removes_signal() -> None:
    versions = json.loads((REPO_ROOT / "plugin-versions.json").read_text())
    marketplace = json.loads(
        (REPO_ROOT / ".agents/plugins/marketplace.json").read_text()
    )
    manifest = json.loads(
        (PLUGIN_ROOT / ".xedoc-plugin" / "plugin.json").read_text()
    )
    names = {entry["name"] for entry in marketplace["plugins"]}

    assert versions["plugins"]["matrix"] == {
        "version": "0.12.0",
        "hosts": ["xedoc"],
    }
    assert matrix.PLUGIN_VERSION == versions["plugins"]["matrix"]["version"]
    assert "signal" not in versions["plugins"]
    assert "matrix" in names
    assert "signal" not in names
    assert not (REPO_ROOT / "plugins/signal").exists()
    assert os.access(EXTENSIONS_ROOT / "matrix.py", os.X_OK)
    assert "prompt.approval.respond" in manifest["extensions"][0][
        "requestedCapabilities"
    ]
    assert manifest["extensions"][0]["approvalResponseTimeoutMs"] == 60000
