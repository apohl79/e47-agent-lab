#!/usr/bin/env python3
"""Bridge one Xedoc root thread to a Signal group using signal-cli.

The Xedoc plugin host invokes this file in one-shot setup/command mode and as
a persistent session child. The setup interaction stores the signal-cli
account, Signal recipient, and optional data directory under
``~/.config/xedoc/signal``.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
import queue
import select
import shutil
import subprocess
import sys
import threading
from typing import Any

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
CONFIG_ROOT = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "xedoc"
    / "signal"
)
CONFIG_PATH = CONFIG_ROOT / "config.json"
GROUPS_ROOT = CONFIG_ROOT / "groups"
SIGNAL_CLI = os.environ.get("SIGNAL_CLI", "signal-cli")


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


def setup_interaction(request: dict[str, Any], account: str) -> dict[str, Any]:
    return response(
        request,
        {
            "kind": "interaction",
            "interaction": {
                "id": "signal-setup",
                "continuation": "signal-setup",
                "stateRevision": "1",
                "surface": {
                    "type": "form",
                    "id": "signal-settings",
                    "title": "Configure Signal bridge",
                    "subtitle": "signal-cli must already be registered or linked.",
                    "fields": [
                        {
                            "type": "text",
                            "id": "account",
                            "label": "Signal CLI account",
                            "description": "The registered phone number used by signal-cli.",
                            "value": account,
                            "maxBytes": 64,
                            "sensitive": False,
                        },
                        {
                            "type": "text",
                            "id": "recipient",
                            "label": "Your Signal number",
                            "description": "The Signal user invited to every session group.",
                            "value": "",
                            "maxBytes": 64,
                            "sensitive": False,
                        },
                        {
                            "type": "text",
                            "id": "data-dir",
                            "label": "signal-cli data directory (optional)",
                            "description": "Leave blank to use signal-cli's default.",
                            "value": "",
                            "maxBytes": 512,
                            "sensitive": False,
                        },
                    ],
                    "submit": action("save", "Save"),
                    "cancel": None,
                },
            },
        },
    )


def default_account() -> str:
    executable = shutil.which(SIGNAL_CLI)
    if executable is None:
        return ""
    try:
        result = subprocess.run(
            [executable, "listAccounts"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    accounts = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return accounts[0] if len(accounts) == 1 else ""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(config, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, CONFIG_PATH)


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
        config = load_json(CONFIG_PATH)
        print(
            json.dumps(
                setup_interaction(
                    request, str(config.get("account") or default_account())
                ),
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0

    if method == "interaction.respond" and params.get("continuation") == "signal-setup":
        values = params.get("values")
        if not isinstance(values, dict):
            raise RuntimeError("setup response is missing values")
        account = str(values.get("account") or "").strip()
        recipient = str(values.get("recipient") or "").strip()
        data_dir = str(values.get("data-dir") or "").strip()
        if not account or not recipient:
            raise RuntimeError("account and recipient are required")
        if any(character.isspace() for character in account + recipient):
            raise RuntimeError("account and recipient must not contain whitespace")
        save_config(
            {
                "account": account,
                "recipient": recipient,
                "dataDir": data_dir,
            }
        )
        return (
            print(
                json.dumps(
                    complete(request, "Signal bridge configured."),
                    separators=(",", ":"),
                ),
                flush=True,
            )
            or 0
        )

    if method == "extension.command.invoke":
        return (
            print(
                json.dumps(
                    complete(request, "Signal bridge is active for this session."),
                    separators=(",", ":"),
                ),
                flush=True,
            )
            or 0
        )

    raise RuntimeError(f"unsupported extension method: {method}")


def signal_command(config: dict[str, Any], *arguments: str) -> list[str]:
    command = [SIGNAL_CLI]
    data_dir = str(config.get("dataDir") or "")
    if data_dir:
        command.extend(["--data-dir", data_dir])
    command.extend(["--output", "json", "--account", str(config["account"])])
    command.extend(arguments)
    return command


def run_signal(config: dict[str, Any], *arguments: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        signal_command(config, *arguments),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or "signal-cli failed"
        raise RuntimeError(detail[:500])
    records: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def group_path(thread_id: str) -> Path:
    safe_id = "".join(
        character if character.isalnum() else "_" for character in thread_id
    )
    return GROUPS_ROOT / f"{safe_id}.json"


def ensure_group(config: dict[str, Any], thread_id: str, title: str) -> str:
    stored = load_json(group_path(thread_id))
    if isinstance(stored.get("groupId"), str) and stored["groupId"]:
        return stored["groupId"]
    GROUPS_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    records = run_signal(
        config,
        "updateGroup",
        "--name",
        f"Xedoc: {title or thread_id}"[:80],
        "--member",
        str(config["recipient"]),
    )
    group_id = next(
        (
            record.get("groupId") or record.get("id")
            for record in records
            if isinstance(record.get("groupId") or record.get("id"), str)
        ),
        None,
    )
    if not group_id:
        raise RuntimeError("signal-cli did not return the new group ID")
    path = group_path(thread_id)
    path.write_text(json.dumps({"groupId": group_id}) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return group_id


def send(config: dict[str, Any], group_id: str, text: str) -> None:
    subprocess.run(
        signal_command(config, "send", "--group-id", group_id, "--message", text),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


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


def receive_messages(
    config: dict[str, Any],
    group_id: str,
    messages: queue.Queue[str],
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        try:
            records = run_signal(
                config,
                "receive",
                "--timeout",
                "1",
                "--max-messages",
                "20",
                "--ignore-attachments",
                "--ignore-stories",
                "--ignore-avatars",
                "--ignore-stickers",
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            continue
        for record in records:
            envelope = record.get("envelope")
            if not isinstance(envelope, dict):
                continue
            message = envelope.get("dataMessage")
            if not isinstance(message, dict) or not isinstance(
                message.get("message"), str
            ):
                continue
            source = envelope.get("sourceNumber") or envelope.get("source")
            if source != config.get("recipient"):
                continue
            group_info = message.get("groupInfo")
            if isinstance(group_info, dict) and group_info.get("groupId") == group_id:
                messages.put(message["message"])


def run_persistent() -> int:
    thread_id = os.environ["XEDOC_SESSION_SCRIPT_THREAD_ID"]
    script_id = os.environ["XEDOC_SESSION_SCRIPT_ID"]
    config = load_json(CONFIG_PATH)
    if not config.get("account") or not config.get("recipient"):
        raise RuntimeError(
            "Signal bridge is not configured; enable it again to configure it"
        )

    client = SessionScriptClient.from_host_child()
    inbound: queue.Queue[str] = queue.Queue()
    stop = threading.Event()
    group_id: str | None = None
    pending_prompts: dict[str, dict[str, Any]] = {}

    def remember_prompt(prompt: dict[str, Any]) -> None:
        if (
            prompt.get("kind") != "requestUserInput"
            or not prompt.get("canRespond")
            or not isinstance(prompt.get("promptId"), str)
            or not isinstance(prompt.get("responseLease"), str)
            or not group_id
        ):
            return
        token = os.urandom(8).hex()
        pending_prompts[prompt["promptId"]] = {"prompt": prompt, "token": token}
        send(config, group_id, prompt_message(prompt, token))

    def replace_pending_prompts(snapshot: dict[str, Any]) -> None:
        pending_prompts.clear()
        prompts = snapshot.get("pendingPrompts")
        if not isinstance(prompts, list):
            return
        for prompt in prompts:
            if isinstance(prompt, dict):
                remember_prompt(prompt)

    def on_notification(message: dict[str, Any]) -> None:
        nonlocal group_id
        method = message.get("method")
        params = message.get("params", {})
        if method == "item/completed":
            item = params.get("item") if isinstance(params, dict) else None
            if (
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and isinstance(item.get("text"), str)
                and group_id
            ):
                send(config, group_id, item["text"])
        elif method == "script/promptOpened" and isinstance(params, dict):
            remember_prompt(params)
        elif method == "script/promptClosed" and isinstance(params, dict):
            prompt_id = params.get("promptId")
            if isinstance(prompt_id, str):
                pending_prompts.pop(prompt_id, None)
        elif method == "script/resyncRequired" and isinstance(params, dict):
            registration_id = params.get("registrationId")
            if isinstance(registration_id, str):
                refreshed = client.read(registration_id)
                snapshot = refreshed.get("snapshot")
                if isinstance(snapshot, dict):
                    replace_pending_prompts(snapshot)

    client.set_notification_handler(on_notification)
    try:
        client.initialize("signal-extension", "Signal bridge", "0.1.0")
        registered = client.register(
            thread_id,
            script_id,
            "Signal bridge",
            "0.1.0",
            {
                "modelResponseCompleted": True,
                "turnCompleted": True,
                "prompts": ["requestUserInput"],
                "sessionUpdates": True,
            },
            ["userInput.send", "prompt.requestUserInput.respond"],
        )
        snapshot = registered.get("snapshot", {})
        session = snapshot.get("session", {}) if isinstance(snapshot, dict) else {}
        group_id = ensure_group(
            config, thread_id, str(session.get("title") or "session")
        )
        replace_pending_prompts(snapshot)
        send(
            config,
            group_id,
            "Signal bridge connected. Send a message here to talk to Xedoc.",
        )
        receiver = threading.Thread(
            target=receive_messages,
            args=(config, group_id, inbound, stop),
            daemon=True,
        )
        receiver.start()
        pending: deque[str] = deque()
        prompt_answers: deque[tuple[str, str]] = deque()

        while True:
            readable, _, _ = select.select([sys.stdin], [], [], 0.5)
            if readable:
                client.handle_message(client.receive_message())
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
                    send(
                        config,
                        group_id,
                        f"That answer could not be mapped. Reply with prompt:{state['token']} and the requested JSON.",
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
                    send(config, group_id, "Xedoc could not accept that answer.")
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
                                "clientUserMessageId": f"signal-{os.urandom(8).hex()}",
                                "input": [{"type": "text", "text": text}],
                            },
                        )
                    except RpcError:
                        continue
                    pending.popleft()
    except RpcError:
        return 0
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
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"signal extension error: {error}", file=sys.stderr)
        raise SystemExit(1)
