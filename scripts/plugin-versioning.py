#!/usr/bin/env python3
"""Manage canonical plugin versions for the e47 marketplace."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


VERSION_FILE = "plugin-versions.json"
HOST_MANIFESTS = {
    "codex": Path(".codex-plugin/plugin.json"),
    "claude": Path(".claude-plugin/plugin.json"),
    "xedoc": Path(".xedoc-plugin/plugin.json"),
}
MARKETPLACE_MANIFESTS = {
    "codex": Path(".agents/plugins/marketplace.json"),
    "claude": Path(".claude-plugin/marketplace.json"),
    "xedoc": Path(".agents/plugins/marketplace.json"),
}
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
CORE_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: Any) -> bool:
    rendered = json.dumps(data, indent=2) + "\n"
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == rendered:
        return False
    path.write_text(rendered, encoding="utf-8")
    return True


def load_registry(root: Path) -> dict[str, Any]:
    path = root / VERSION_FILE
    try:
        registry = read_json(path)
    except FileNotFoundError:
        raise SystemExit(f"Error: missing {VERSION_FILE}") from None
    if not isinstance(registry, dict):
        raise SystemExit(f"Error: {VERSION_FILE} must contain a JSON object")
    return registry


def assert_semver(value: str, field: str) -> list[str]:
    if not isinstance(value, str) or not SEMVER_RE.match(value):
        return [f"{field} must be a SemVer string, got {value!r}"]
    return []


def registry_errors(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    marketplace = registry.get("marketplace")
    plugins = registry.get("plugins")

    if not isinstance(marketplace, dict):
        errors.append("marketplace must be an object")
    else:
        if not isinstance(marketplace.get("name"), str) or not marketplace["name"]:
            errors.append("marketplace.name must be a non-empty string")
        errors.extend(assert_semver(marketplace.get("version"), "marketplace.version"))

    if not isinstance(plugins, dict) or not plugins:
        errors.append("plugins must be a non-empty object")
        return errors

    for plugin_name, plugin in plugins.items():
        if not isinstance(plugin_name, str) or not plugin_name:
            errors.append("plugin names must be non-empty strings")
            continue
        if not isinstance(plugin, dict):
            errors.append(f"plugins.{plugin_name} must be an object")
            continue
        errors.extend(assert_semver(plugin.get("version"), f"plugins.{plugin_name}.version"))
        hosts = plugin.get("hosts")
        if not isinstance(hosts, list) or not hosts:
            errors.append(f"plugins.{plugin_name}.hosts must be a non-empty array")
            continue
        for host in hosts:
            if host not in HOST_MANIFESTS:
                errors.append(f"plugins.{plugin_name}.hosts contains unsupported host {host!r}")

    return errors


def plugin_dirs(root: Path) -> set[str]:
    plugins_root = root / "plugins"
    if not plugins_root.exists():
        return set()
    return {path.name for path in plugins_root.iterdir() if path.is_dir()}


def host_manifest_path(root: Path, plugin_name: str, host: str) -> Path:
    return root / "plugins" / plugin_name / HOST_MANIFESTS[host]


def marketplace_plugin_names(root: Path, host: str) -> set[str]:
    path = root / MARKETPLACE_MANIFESTS[host]
    data = read_json(path)
    plugins = data.get("plugins", [])
    if not isinstance(plugins, list):
        return set()
    return {entry.get("name") for entry in plugins if isinstance(entry, dict) and entry.get("name")}


def collect_errors(root: Path, registry: dict[str, Any], check_versions: bool) -> list[str]:
    errors = registry_errors(registry)
    if errors:
        return errors

    marketplace = registry["marketplace"]
    plugins = registry["plugins"]
    registry_names = set(plugins)
    existing_plugin_dirs = plugin_dirs(root)

    for name in sorted(registry_names - existing_plugin_dirs):
        errors.append(f"plugins.{name} is versioned but plugins/{name} does not exist")
    for name in sorted(existing_plugin_dirs - registry_names):
        errors.append(f"plugins/{name} exists but is missing from {VERSION_FILE}")

    try:
        codex_marketplace = read_json(root / MARKETPLACE_MANIFESTS["codex"])
        claude_marketplace = read_json(root / MARKETPLACE_MANIFESTS["claude"])
        xedoc_marketplace = read_json(root / MARKETPLACE_MANIFESTS["xedoc"])
    except FileNotFoundError as exc:
        errors.append(f"missing marketplace manifest: {exc.filename}")
        return errors

    for host, manifest in (
        ("codex", codex_marketplace),
        ("claude", claude_marketplace),
        ("xedoc", xedoc_marketplace),
    ):
        if manifest.get("name") != marketplace["name"]:
            errors.append(
                f"{MARKETPLACE_MANIFESTS[host]} name is {manifest.get('name')!r}, "
                f"expected {marketplace['name']!r}"
            )

    if check_versions and claude_marketplace.get("version") != marketplace["version"]:
        errors.append(
            f"{MARKETPLACE_MANIFESTS['claude']} version is {claude_marketplace.get('version')!r}, "
            f"expected {marketplace['version']!r}"
        )

    for host in HOST_MANIFESTS:
        expected = {name for name, plugin in plugins.items() if host in plugin["hosts"]}
        actual = marketplace_plugin_names(root, host)
        for name in sorted(expected - actual):
            errors.append(f"{MARKETPLACE_MANIFESTS[host]} is missing plugin {name!r}")
        # Codex and Xedoc share the .agents marketplace manifest. A plugin
        # may be listed there for Codex before it gains an Xedoc manifest.
        if host != "xedoc":
            for name in sorted(actual - expected):
                errors.append(f"{MARKETPLACE_MANIFESTS[host]} lists unversioned plugin {name!r}")

    for plugin_name, plugin in plugins.items():
        expected_hosts = set(plugin["hosts"])
        for host in HOST_MANIFESTS:
            path = host_manifest_path(root, plugin_name, host)
            if host not in expected_hosts:
                if path.exists():
                    errors.append(f"{path.relative_to(root)} exists but {host!r} is not listed for {plugin_name}")
                continue

            if not path.exists():
                errors.append(f"{path.relative_to(root)} is missing")
                continue

            manifest = read_json(path)
            if manifest.get("name") != plugin_name:
                errors.append(
                    f"{path.relative_to(root)} name is {manifest.get('name')!r}, expected {plugin_name!r}"
                )
            if check_versions and manifest.get("version") != plugin["version"]:
                errors.append(
                    f"{path.relative_to(root)} version is {manifest.get('version')!r}, "
                    f"expected {plugin['version']!r}"
                )

    return errors


def sync_versions(root: Path, registry: dict[str, Any]) -> list[Path]:
    changed: list[Path] = []
    marketplace = registry["marketplace"]
    plugins = registry["plugins"]

    claude_marketplace_path = root / MARKETPLACE_MANIFESTS["claude"]
    claude_marketplace = read_json(claude_marketplace_path)
    claude_marketplace["version"] = marketplace["version"]
    if write_json(claude_marketplace_path, claude_marketplace):
        changed.append(claude_marketplace_path)

    for plugin_name, plugin in plugins.items():
        for host in plugin["hosts"]:
            path = host_manifest_path(root, plugin_name, host)
            manifest = read_json(path)
            manifest["version"] = plugin["version"]
            if write_json(path, manifest):
                changed.append(path)

    return changed


def print_errors(errors: list[str]) -> None:
    for error in errors:
        print(f"Error: {error}", file=sys.stderr)


def command_list(root: Path, registry: dict[str, Any]) -> int:
    errors = collect_errors(root, registry, check_versions=True)
    if errors:
        print_errors(errors)
        return 1

    marketplace = registry["marketplace"]
    print(f"{'component':<26} {'version':<12} hosts")
    print(f"{marketplace['name']:<26} {marketplace['version']:<12} marketplace")
    for plugin_name, plugin in registry["plugins"].items():
        print(f"{plugin_name:<26} {plugin['version']:<12} {','.join(plugin['hosts'])}")
    return 0


def command_check(root: Path, registry: dict[str, Any]) -> int:
    errors = collect_errors(root, registry, check_versions=True)
    if errors:
        print_errors(errors)
        return 1
    print("Plugin versions are consistent.")
    return 0


def command_sync(root: Path, registry: dict[str, Any]) -> int:
    errors = collect_errors(root, registry, check_versions=False)
    if errors:
        print_errors(errors)
        return 1
    changed = sync_versions(root, registry)
    errors = collect_errors(root, registry, check_versions=True)
    if errors:
        print_errors(errors)
        return 1
    if changed:
        for path in changed:
            print(f"Updated {path.relative_to(root)}")
    else:
        print("Plugin versions already synced.")
    return 0


def set_registry_version(registry: dict[str, Any], target: str, version: str) -> None:
    errors = assert_semver(version, "version")
    if errors:
        raise SystemExit(f"Error: {errors[0]}")

    if target == "marketplace":
        registry["marketplace"]["version"] = version
        return

    plugins = registry["plugins"]
    if target not in plugins:
        raise SystemExit(f"Error: unknown plugin {target!r}")
    plugins[target]["version"] = version


def bump_version(version: str, part: str) -> str:
    match = CORE_VERSION_RE.match(version)
    if not match:
        raise SystemExit(f"Error: cannot bump non-core SemVer {version!r}; use set with an explicit version")
    major, minor, patch = (int(group) for group in match.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"Error: unsupported bump part {part!r}")


def current_version(registry: dict[str, Any], target: str) -> str:
    if target == "marketplace":
        return registry["marketplace"]["version"]
    plugins = registry["plugins"]
    if target not in plugins:
        raise SystemExit(f"Error: unknown plugin {target!r}")
    return plugins[target]["version"]


def command_set(root: Path, registry: dict[str, Any], target: str, version: str) -> int:
    errors = collect_errors(root, registry, check_versions=False)
    if errors:
        print_errors(errors)
        return 1
    set_registry_version(registry, target, version)
    write_json(root / VERSION_FILE, registry)
    return command_sync(root, registry)


def command_bump(root: Path, registry: dict[str, Any], target: str, part: str) -> int:
    version = bump_version(current_version(registry, target), part)
    return command_set(root, registry, target, version)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage e47 plugin versions")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="Print marketplace and plugin versions")
    subparsers.add_parser("check", help="Validate manifests against plugin-versions.json")
    subparsers.add_parser("sync", help="Write canonical versions into host manifests")

    set_parser = subparsers.add_parser("set", help="Set an explicit SemVer version and sync manifests")
    set_parser.add_argument("target", help="marketplace or plugin name")
    set_parser.add_argument("version", help="SemVer version")

    bump_parser = subparsers.add_parser("bump", help="Bump major, minor, or patch and sync manifests")
    bump_parser.add_argument("target", help="marketplace or plugin name")
    bump_parser.add_argument("part", choices=("major", "minor", "patch"))

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    registry = load_registry(root)

    if args.command == "list":
        return command_list(root, registry)
    if args.command == "check":
        return command_check(root, registry)
    if args.command == "sync":
        return command_sync(root, registry)
    if args.command == "set":
        return command_set(root, registry, args.target, args.version)
    if args.command == "bump":
        return command_bump(root, registry, args.target, args.part)

    raise SystemExit(f"Error: unsupported command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
