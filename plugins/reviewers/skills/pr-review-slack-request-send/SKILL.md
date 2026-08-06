---
name: pr-review-slack-request-send
description: Post a PR review request (link + one-sentence summary) to a configured Slack channel. Use when asking a team to review a GitHub pull request.
---

# Send PR Review Request to Slack

Post a short PR review request to a channel listed in `~/.config/pr-review-slack/channels.json`.

## Message format

```
<pr-link>
<very short summary, max 10 words>
```

Two lines. No greeting, no signature, no emojis.

## Steps

### 1. Load channels

Read `~/.config/pr-review-slack/channels.json`. If it does not exist or is empty, stop and tell the user to run `reviewers:pr-review-slack-request-manage --add` first.

### 2. Pick a channel

Use the host user-input tool (Claude Code `AskUserQuestion`; Codex `request_user_input` when available) with one option per channel (label = `name`, description = `url`). The user picks; never type a name.

If the list has more than 4 entries, paginate the question (4 options at a time) until the user picks.

Capture the chosen channel's `name` and `url`.

### 3. Resolve the PR

In order:

1. If the user passed a GitHub PR URL as an argument, use it.
2. Else, check the current branch for an open PR:
   ```bash
   gh pr view --json url,title,body,number --jq '{url,title,body,number}'
   ```
3. If neither yields a PR, use the host user-input tool to ask for the PR URL.

Validate the URL matches `https://github.com/<owner>/<repo>/pull/<number>`.

### 4. Build the one-sentence summary

Generate a very short sentence (<= 10 words, hard limit) describing what the PR does. Source material, in order of preference:

1. PR title + body (`gh pr view --json title,body`).
2. Diff summary (`gh pr diff <url>` truncated).
3. Ask the user.

Rules for the sentence:
- <= 10 words. Count the words before proposing; if over, cut until it fits.
- Present tense, active voice ("Adds X", "Fixes Y", "Refactors Z").
- No ticket prefixes, no markdown, no trailing period required.
- Drop fillers: no "this PR", "in order to", subordinate clauses, or implementation details.
- Do not paste the PR title verbatim if it is just a conventional-commit header; rephrase into prose.

Example: "Adds retry logic to the Slack webhook client" - not "This PR introduces a retry mechanism with exponential backoff for the Slack webhook client in order to improve resilience".

Show the proposed sentence to the user and confirm via the host user-input tool (Send / Edit / Cancel) before posting.

### 5. Send

Resolve the channel ID from the URL (last path segment of `https://workspace.example.com/archives/<CHANNEL_ID>`), then post via the configured Slack MCP server's send-message tool:

```
mcp__<your-slack-mcp>__slack_send_message
  channel_id: <CHANNEL_ID>
  text: "<pr-url>\n<summary sentence>"
```

Any MCP server that exposes Slack tools works. Tool names are namespaced by the
server, so resolve the actual name from the session's available-tools list —
match a tool whose name ends in `slack_send_message` (or the equivalent
send-message tool your server exposes) instead of assuming a fixed prefix. If no
Slack MCP tool is available, stop and tell the user to configure one.

If the user picked "Edit", re-prompt for the sentence and loop back to confirmation.
If the user picked "Cancel", stop without posting.

### 6. Report result

On success, print:

```
Sent to #<name>: <permalink-or-pr-url>
```

Use the Slack MCP server's `…slack_get_permalink` tool with the returned message ts when available; otherwise just echo the PR URL.

## Notes

- Never post without explicit user confirmation of the summary sentence.
- Two lines exactly: PR link, then summary. Slack will unfurl the PR card from the link.
- Do not @-mention anyone unless the user asks.
- Channel registry is managed by `reviewers:pr-review-slack-request-manage`; do not edit `channels.json` from this skill.
