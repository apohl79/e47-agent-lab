from __future__ import annotations

from pathlib import Path
import plistlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_chat_service


def test_service_preserves_custom_xedoc_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XEDOC_HOME", str(tmp_path / "custom home"))
    monkeypatch.setattr(host_chat_service, "XEDOC", "/usr/local/bin/xedoc")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(host_chat_service.subprocess, "run", lambda args, **kwargs: calls.append(args))

    host_chat_service.main("install")

    target = tmp_path / "Library/LaunchAgents/io.xedoc.matrix-host-chat.plist"
    with target.open("rb") as handle:
        config = plistlib.load(handle)
    assert config["EnvironmentVariables"]["XEDOC_HOME"] == str(tmp_path / "custom home")
    assert config["StandardErrorPath"].startswith(str(tmp_path / "custom home"))
    assert calls[-1][:2] == ("launchctl", "bootstrap")


def test_systemd_service_preserves_custom_xedoc_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XEDOC_HOME", str(tmp_path / "custom home"))
    monkeypatch.setattr(host_chat_service, "XEDOC", "/usr/local/bin/xedoc")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(host_chat_service.shutil, "which", lambda command: "/bin/systemctl")
    monkeypatch.setattr(host_chat_service.subprocess, "run", lambda *args, **kwargs: None)

    host_chat_service.main("install")

    unit = (tmp_path / ".config/systemd/user/io.xedoc.matrix-host-chat.service").read_text()
    assert f'Environment="XEDOC_HOME={tmp_path / "custom home"}"' in unit
