# Repository Instructions

## Plugin Version Maintenance

Canonical marketplace and plugin versions live in `plugin-versions.json`.

Do not hand-edit version fields in:

- `.claude-plugin/marketplace.json`
- `plugins/*/.codex-plugin/plugin.json`
- `plugins/*/.claude-plugin/plugin.json`

Use the version helper instead:

```bash
./scripts/plugin-versioning.py list
./scripts/plugin-versioning.py check
./scripts/plugin-versioning.py sync
./scripts/plugin-versioning.py bump <plugin-name|marketplace> patch
./scripts/plugin-versioning.py bump <plugin-name|marketplace> minor
./scripts/plugin-versioning.py bump <plugin-name|marketplace> major
./scripts/plugin-versioning.py set <plugin-name|marketplace> <semver>
```

Version bump rules:

- `patch`: documentation corrections, metadata-only fixes, bug fixes, or compatible skill/hook/script fixes.
- `minor`: new compatible skills, hooks, commands, scripts, plugin capabilities, or installer behavior.
- `major`: breaking changes to skill invocation, expected files, configuration, hook behavior, or host support.

When adding a plugin:

1. Add the plugin to `plugin-versions.json` with its SemVer version and supported `hosts`.
2. Add the plugin to the relevant marketplace manifest(s).
3. Create the matching host plugin manifest(s).
4. Run `./scripts/plugin-versioning.py sync`.
5. Update the README plugin table.
6. Run `./scripts/plugin-versioning.py check`.

When changing host support:

1. Update the plugin `hosts` array in `plugin-versions.json`.
2. Add or remove the matching host manifest and marketplace entry.
3. Run `./scripts/plugin-versioning.py sync`.
4. Update the README plugin table.
5. Run `./scripts/plugin-versioning.py check`.

Before committing plugin changes, run:

```bash
./scripts/plugin-versioning.py check
find .agents .claude-plugin plugins -name '*.json' -print0 | xargs -0 jq empty
bash -n install.sh
```

Also run host validators for changed plugins:

```bash
python3 "$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py" plugins/<plugin>
claude plugin validate plugins/<plugin>
```

If local `python3` lacks `yaml`, run the Codex validator with a Python that has PyYAML installed, for example:

```bash
python3 "$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py" plugins/<plugin>
```

Notes:

- Codex marketplace entries do not carry plugin versions.
- The Claude marketplace manifest carries the marketplace version.
- Plugin versions are written to each plugin host manifest by `sync`.
- Local `./install.sh` runs `./scripts/plugin-versioning.py check` before installing from a checkout.
