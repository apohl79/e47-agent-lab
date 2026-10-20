# Matrix (Element)

Matrix is an Xedoc-only session extension and host chat. After approval and setup, it creates
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

## Host chat

Installing the plugin with `install.sh --xedoc` also installs a per-user
launchd (macOS) or systemd (Linux) service. It never starts the Xedoc
app-server itself: while no app-server is running it stays idle, and it
connects (or reconnects after a restart) once one is available. Once
`/matrix setup` has completed, the service creates a private room named
`Xedoc: <hostname> (host)` and a durable Xedoc session in the user's home
directory. It keeps the same room and session after restarts. The service
starts at user login on macOS; on Linux, enable systemd user lingering if it
must start before login.

Send `/help` in the host room for the supported commands:

- `/list` lists approved remote-agent workspaces.
- `/list <workspace>` lists immediate project directories in that workspace.
- `/sessions [search string]` lists stored sessions with numbered
  workspace-relative directories. The optional filter matches session names
  or workspace-relative directories, case-insensitively, not conversation text.
- `/attach <num>` resumes the numbered session and enables its Matrix bridge.
- `/start <project dir>` starts a session in an approved project directory and
  enables its Matrix bridge. Prefix with `<workspace>/` when there are multiple
  approved workspaces.
- `/detach` in a session room disables that session's Matrix bridge. The host
  room remains connected; `/detach` there explains where to use it.

Other messages in the host room are forwarded to the home session. Unknown
slash commands are rejected rather than sent to the model. Session bridges
still require the normal Xedoc approval and Matrix setup; the host chat does
not bypass extension approval. If no remote-agent workspaces are configured,
the host room still works, but project commands are unavailable.
Element handles some slash commands itself (notably `/help`); enter
`/plain /help` to send the literal command to the host bridge.

`request_user_input` responses use Xedoc's leased responder capability and
retain the host's 10-second window. Matrix approval replies receive a
declaration-bound 60-second window so a numbered answer can reach the session
before Xedoc falls back to its local approval screen. A late numbered reply for
an approval already resolved in Xedoc is discarded instead of becoming a new
user message.
