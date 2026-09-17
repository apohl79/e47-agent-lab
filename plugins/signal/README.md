# Signal

Signal is an Xedoc-only session extension. After approval and setup, it creates
one Signal group for each enabled root session, forwards completed agent
messages, accepts Signal messages as ordinary turns, and routes explicitly
correlated replies to `request_user_input` prompts.

The extension requires `signal-cli` to be registered or linked on the machine.
Its setup flow asks for the local Signal CLI account, the Signal recipient to
invite, and an optional `signal-cli` data directory. Configuration and
per-thread group IDs are stored under `~/.config/xedoc/signal` with restrictive
permissions.

`request_user_input` responses use Xedoc's leased responder capability. The
current Xedoc host gives plugin responders a 10-second lease; replies must
arrive within that window.
