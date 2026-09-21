# Matrix (Element)

Matrix is an Xedoc-only session extension. After approval and setup, it creates
one private Matrix room for each enabled root session, invites a configured
target account, mirrors Xedoc user and completed model messages into the room,
accepts messages sent from that account in Element as ordinary turns, and
routes replies to the one active `request_user_input` prompt.
Matrix-originated turns are not mirrored back into their source room.

It also relays actionable Xedoc approval prompts, including model-routing
confirmations. Element receives the approval title, details, and numbered
choices; reply with the number only (for example, `1`). Standard command and
file approvals use friendly choices such as “Approve” and “Deny”. User-input
choice prompts use the same format; multi-question prompts accept one
comma-separated number per question. An uncommon form that needs structured
data asks for its JSON values directly; the bridge never guesses an approval
response.

Completed file edits are also posted to the room as a compact summary with the
edited paths and added/removed line counts. Mirrored messages include a
plaintext fallback plus Matrix-safe rich HTML: headings, lists, quoted text,
fenced and inline code, emphasis, and HTTPS links render in capable clients.
Completed, failed, declined, and in-progress file changes receive a semantic
success, error, warning, or informational accent respectively.

The bridge persists each root-session-to-room binding, so resuming a session
after a machine reboot uses the same Matrix room. When the session is renamed,
the room name follows it.

Setup uses Matrix OAuth device authorization. Run `/matrix setup`, enter the
homeserver URL, then complete the two browser approvals shown by Xedoc:

- authorize the agent account, which sends model responses and bridge lifecycle
  messages; and
- authorize your Element account, which sends user messages entered directly
  in Xedoc and receives Element input.

The extension verifies each authenticated Matrix ID and requires distinct
accounts. It registers an OAuth client for each setup flow, retains refresh
tokens, and automatically refreshes an expired access token before retrying a
Matrix request. No access token is displayed or pasted into Xedoc.

Configuration, OAuth tokens, and per-thread room IDs are stored under
`~/.xedoc/extensions/matrix` (or `$XEDOC_HOME/extensions/matrix`) with
restrictive permissions, independent of the installed plugin cache. Existing
settings and room bindings under `~/.config/xedoc/matrix` are migrated on first
use without deleting the legacy copies. Legacy static-token settings do not
meet the OAuth requirement; run `/matrix setup` again to replace them. The
extension uses the Matrix Client-Server API directly and sends access tokens
only in the `Authorization` header.

The extension owns its settings and decides whether setup is needed. The bridge
is disabled by default after setup. Use `/matrix setup` to reopen the settings
form, `/matrix` to report bridge status, `/matrix on` to enable the bridge for
the current session, `/matrix off` to disable it for that session, and
`/matrix restart` to restart it for that session. `/matrix help` lists every
command. `/matrix debug on` enables Xedoc-side lifecycle messages for Matrix
connection, room creation, room renames, and sync recovery; `/matrix debug off`
disables those informational messages. Bridge warnings and errors are always
posted to Xedoc.

Rooms created by this version are private and invite-only, but they are not
end-to-end encrypted. This keeps the dependency-free bridge interoperable with
Element without storing Matrix device keys. Use a homeserver and accounts whose
security policy permits unencrypted private rooms.

`request_user_input` responses use Xedoc's leased responder capability and
retain the host's 10-second window. Matrix approval replies receive a
declaration-bound 60-second window so a numbered answer can reach the session
before Xedoc falls back to its local approval screen. A late numbered reply for
an approval already resolved in Xedoc is discarded instead of becoming a new
user message.
