---
name: pr-review-slack-request-manage
description: Manage the list of Slack channels used for PR review requests. Use when adding, removing, or listing entries in ~/.config/pr-review-slack/channels.json.
argument-hint: [--list | --add | --remove]
---

# Manage PR Review Slack Channels

Maintain `~/.config/pr-review-slack/channels.json`, the channel registry consumed by `reviewers:pr-review-slack-request-send`.

## File format

```json
[
  { "name": "team-foo-reviews", "url": "https://workspace.example.com/archives/C0123456" },
  { "name": "team-bar-reviews", "url": "https://workspace.example.com/archives/C0789ABC" }
]
```

- File path: `~/.config/pr-review-slack/channels.json`
- Create the directory and file if missing (start with `[]`).
- Each entry has exactly two fields: `name` (Slack channel name without `#`) and `url` (channel link).

## Arguments

The skill is invoked with one of these actions. If no action is passed, ask via the host user-input tool.

| Action      | Effect                                                  |
|-------------|---------------------------------------------------------|
| `--list`    | Print every entry as `name - url`.                      |
| `--add`     | Prompt for name + url, append to the file.              |
| `--remove`  | Show entries, let user pick one, remove it.             |

## Steps

### Resolve the action

1. Parse the argument. Accept `--list`, `--add`, `--remove` (also without `--`).
2. If none was provided, use the host user-input tool to ask which action to run (List / Add / Remove).

### Ensure the file exists

```bash
mkdir -p ~/.config/pr-review-slack
[ -f ~/.config/pr-review-slack/channels.json ] || echo '[]' > ~/.config/pr-review-slack/channels.json
```

### List

```bash
jq -r '.[] | "\(.name) - \(.url)"' ~/.config/pr-review-slack/channels.json
```

If the array is empty, report `No channels configured.` and stop.

### Add

1. Ask the user for the Slack channel URL via the host user-input tool.
   - Validate the URL starts with `https://` and contains `/archives/` on the configured workspace host.
   - Extract the channel ID = last path segment (e.g. `C0123456` from `https://workspace.example.com/archives/C0123456`).
2. Resolve the channel name from Slack; do not ask the user:

   ```
   mcp__<your-slack-mcp>__slack_get_channel_info
     channel_id: <CHANNEL_ID>
   ```

   Any MCP server exposing Slack tools works. Tool names are namespaced by the
   server, so resolve the actual name from the session's available-tools list —
   match a tool whose name ends in `slack_get_channel_info` rather than assuming
   a fixed prefix.

   Use the `name` field from the response. If the lookup fails (private channel without access, archived, invalid ID), report the error and stop; do not fall back to a typed name.
3. Refuse duplicates: error if any existing entry has the same `name` OR the same `url`.
4. Append:

```bash
jq --arg n "$NAME" --arg u "$URL" \
   '. + [{name:$n, url:$u}]' \
   ~/.config/pr-review-slack/channels.json > /tmp/channels.json && \
   mv /tmp/channels.json ~/.config/pr-review-slack/channels.json
```

5. Report `Added: <name>`.

### Remove

1. Read entries. If empty, report `No channels configured.` and stop.
2. Use the host user-input tool with one option per channel (label = name, description = url). The user MUST pick; never type the name.
3. Delete the chosen entry:

```bash
jq --arg n "$NAME" 'map(select(.name != $n))' \
   ~/.config/pr-review-slack/channels.json > /tmp/channels.json && \
   mv /tmp/channels.json ~/.config/pr-review-slack/channels.json
```

4. Report `Removed: <name>`.

## Notes

- Do not invent URLs or names. Always source them from user input or the file.
- Keep the JSON valid: write through `jq` + atomic `mv`, never with `echo >>`.
- The host user-input tool allows a limited number of options. If the channel list has more entries during `--remove`, paginate in small batches until the user makes a choice.
