# Matrix (Element)

Matrix is an Xedoc-only session extension. After approval and setup, it creates
one private Matrix room for each enabled root session, invites a configured
target account, mirrors completed Xedoc turns into the room, accepts messages
sent from that account in Element as ordinary turns, and routes explicitly
correlated replies to `request_user_input` prompts. Matrix-originated turns
are not mirrored back into their source room.

The bridge persists each root-session-to-room binding, so resuming a session
after a machine reboot uses the same Matrix room. When the session is renamed,
the room name follows it.

Setup asks for:

- the Matrix homeserver HTTPS URL;
- the full Matrix ID and access token of the account the agent should use; and
- the full Matrix ID of the target Element account.

Configuration, access tokens, and per-thread room IDs are stored under
`~/.config/xedoc/matrix` with restrictive permissions. The extension uses the
Matrix Client-Server API directly and sends the access token only in the
`Authorization` header.

The extension owns its settings and decides whether setup is needed. The bridge
is disabled by default after setup. Use `/matrix setup` to reopen the settings
form, `/matrix` to report bridge status, `/matrix on` to enable the bridge for
the current session, and `/matrix off` to disable it for that session.

Rooms created by this version are private and invite-only, but they are not
end-to-end encrypted. This keeps the dependency-free bridge interoperable with
Element without storing Matrix device keys. Use a homeserver and accounts whose
security policy permits unencrypted private rooms.

`request_user_input` responses use Xedoc's leased responder capability. The
current Xedoc host gives plugin responders a 10-second lease; replies must
arrive within that window.
