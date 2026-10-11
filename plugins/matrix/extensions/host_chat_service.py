#!/usr/bin/env python3
"""Install or remove the per-user Matrix host chat supervisor."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import json
import shutil
import subprocess
import sys


LABEL = "io.xedoc.matrix-host-chat"
SCRIPT = Path(__file__).with_name("host_chat.py").resolve()
XEDOC = shutil.which("xedoc")


def service_environment() -> dict[str, str]:
    environment = {"XEDOC_BIN": XEDOC or ""}
    if os.environ.get("XEDOC_HOME"):
        environment["XEDOC_HOME"] = os.environ["XEDOC_HOME"]
    return environment


def run(*arguments: str) -> None:
    subprocess.run(arguments, check=True)


def main(action: str) -> None:
    if action == "install" and XEDOC is None:
        raise RuntimeError("Xedoc CLI is not installed")
    if sys.platform == "darwin":
        target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        domain = f"gui/{os.getuid()}"
        if target.exists():
            subprocess.run(["launchctl", "bootout", domain, str(target)], check=False)
            target.unlink()
        if action == "install":
            target.parent.mkdir(parents=True, exist_ok=True)
            log = Path(os.environ.get("XEDOC_HOME", Path.home() / ".xedoc")) / "extensions/matrix/host-chat.log"
            log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("wb") as output:
                plistlib.dump(
                    {
                        "Label": LABEL,
                        "ProgramArguments": [sys.executable, str(SCRIPT)],
                        "RunAtLoad": True,
                        "KeepAlive": True,
                        "EnvironmentVariables": service_environment(),
                        "StandardErrorPath": str(log),
                    },
                    output,
                )
            run("launchctl", "bootstrap", domain, str(target))
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        target = Path.home() / ".config/systemd/user" / f"{LABEL}.service"
        if target.exists():
            subprocess.run(["systemctl", "--user", "stop", LABEL], check=False)
            target.unlink()
        if action == "install":
            target.parent.mkdir(parents=True, exist_ok=True)
            environment = "".join(
                f"Environment={json.dumps(f'{key}={value}')}\n"
                for key, value in service_environment().items()
            )
            target.write_text(
                "[Unit]\nDescription=Xedoc Matrix host chat\n"
                "[Service]\n"
                f"{environment}"
                f"ExecStart={sys.executable} {SCRIPT}\nRestart=always\nRestartSec=10\n"
                "[Install]\nWantedBy=default.target\n"
            )
        run("systemctl", "--user", "daemon-reload")
        if action == "install":
            run("systemctl", "--user", "enable", "--now", LABEL)
        else:
            subprocess.run(["systemctl", "--user", "disable", LABEL], check=False)
    else:
        raise RuntimeError("Host chat startup requires launchd or systemd user services")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"install", "uninstall"}:
        raise SystemExit("Usage: host_chat_service.py install|uninstall")
    main(sys.argv[1])
