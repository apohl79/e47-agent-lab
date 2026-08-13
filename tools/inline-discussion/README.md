# inline-discussion

Standalone HTTP server that backs the `inline-discussion` plugin. Renders a
markdown file (including Mermaid diagrams) to HTML, watches it on disk, streams
updates over SSE, and hosts threaded AI conversations + Apply/Finish workflow on
top of the document.

Extracted from the `plugins/inline-discussion/server/` tree so the same binary
can also be invoked from outside the marketplace skill.

## Quickstart

```bash
cd tools/inline-discussion
npm install
npm run build

# Quick mode: render a document and hold until the browser clicks Finish.
# No main host session — Apply buttons are hidden in the browser.
./bin/inline-discussion ../../README.md

# Quick mode with a bootstrapped Codex agent driving the skill:
./bin/inline-discussion -a ../../README.md

# Quick mode with a bootstrapped Claude agent driving the skill:
./bin/inline-discussion -a claude ../../README.md
```

## CLI

```
inline-discussion [-a [codex|claude]|--agent[=codex|claude]] <doc>
inline-discussion start --doc <path> [--main-jsonl <path>] [--main-session-id <id>] \
                        [--main-session-socket <path>] --session-dir <path> \
                        [--agent claude|codex] [--cwd <path>] [--hold]
inline-discussion sessions [--socket <path>]
inline-discussion wait  --session-dir <path> [--idle-exit-seconds <seconds>]
```

### Quick shortcut `inline-discussion [-a [codex|claude]|--agent[=codex|claude]] <doc>`

- Equivalent to `start --doc <doc> --session-dir <TMPDIR>/inline-discussion/<slug>
  --agent codex --cwd "$PWD" --hold`.
- `--main-jsonl` is omitted, so the server runs with `hasMainSession=false`
  and the browser hides Apply controls.
- `--cwd` is the preferred project root for repo-relative Markdown/source
  routes and linked assets; the document directory remains a fallback.
- With `-a` / `--agent`: bootstrap a main-agent session so the
  inline-discussion plugin skill drives the launcher from inside that
  session. `-a <doc>` keeps the historical Codex default and runs
  `codex exec /inline-discussion:discuss <abs_doc>`.
- Pass `-a claude <doc>` or `--agent=claude <doc>` to use Claude Code via
  `claude -p /inline-discussion:discuss <abs_doc>` instead. Pass
  `-a codex <doc>` or `--agent=codex <doc>` to select Codex explicitly.
- The selected host CLI and the `inline-discussion` plugin must be installed.
  In legacy mode, Finish returns and the host agent exits alongside the
  browser session. In Codex app-server handoff mode, Finish sends the result
  back into the running main session instead.

### `start` / `wait`

- `start` boots the server detached, prints the URL on stdout, writes
  `url.txt` and `server.pid` into `--session-dir`, and exits. With `--hold`
  it stays in the foreground until the server exits (used by the plugin
  skill on Codex).
- `wait` blocks until the session dir contains either `result.json`
  (Finish) or `apply-N.json` (Apply) and prints that path on stdout.
  `--idle-exit-seconds` exits 124 without stopping the server when no signal
  has arrived yet; Codex uses this heartbeat mode to avoid host command
  timeouts while keeping the browser session open indefinitely.
- `--main-jsonl` is optional. When omitted (or pointing at a non-existent
  file), the main-agent transcript preamble is empty and the browser hides
  Apply.
- `--main-session-id` enables app-server handoff mode. Apply and Finish send
  prompts into that running Codex session through the generic local
  app-server protocol; `--main-session-socket` overrides the default Codex
  control socket path.
- `sessions` lists the running Codex sessions exposed by the local app-server.
  It uses the same protocol as handoff mode and works with the original Codex
  app-server; `--socket` overrides the default control socket path.
- Codex thread inference is explicit. A host-launched discussion initializes
  its page defaults from the latest main-session model and reasoning effort,
  resolving the provider from the Codex app-server model catalog; standalone
  CLI use initializes them from the app-server defaults. The combined
  provider/model and reasoning selectors in the header affect only threads
  created afterward. Each thread snapshots its own settings and its selectors
  affect subsequent turns in that thread, without changing an already-running
  turn. A thread's selectors are disabled while its turn is active. Switching
  providers replaces the backing app-server thread after the active turn
  finishes and carries the persisted discussion history forward.

## HTTP surface (relevant subset)

- `GET /` → static client bundle.
- `GET /api/bootstrap` → `{ html, blockIds, title, threads, archivedThreads,
  hasMainSession, applying, applyStatus, applyProgress, applyTasks,
  inferenceCatalog, defaultInferenceSettings }`.
- `GET /events` → SSE stream of `ready`, `doc.updated`, `thread.*`,
  `server.applying`, `server.apply-failed`, `apply.progress`,
  `apply.tasks`, etc.
- Thread CRUD under `/api/threads/...` (start, message, propose-conclusion,
  edit-conclusion, close, reopen, convert-to-thread, archive).
- Inference selection: `PATCH /api/inference-settings` changes the page default
  for new threads; `PATCH /api/threads/:id/inference-settings` changes that
  thread for subsequent turns.
- Apply lifecycle: `POST /api/apply`, `POST /api/apply/progress`,
  `POST /api/apply/done`, `POST /api/apply/failed`,
  `POST /api/apply/monitoring`. `POST /api/apply` returns 409
  `no-main-session` in standalone mode.
- `POST /api/finish` → writes `result.json` into `--session-dir` and shuts
  the server down (in non-test mode).
- Repo-relative `GET /<path>` for assets referenced from the document.
- Local file URLs such as `http://127.0.0.1:<port>/src/service.ts:44` or
  `/docs/guide.md` open in the browser view. Supported source formats render
  with line numbers and scroll to the selected line; `.md` and `.markdown`
  files use the same Markdown renderer as the main document. Markdown file
  views support notes, threads, highlights, and live updates; source-code
  file views remain read-only. Heading fragments such as `#2026-02-dining`
  scroll to the matching rendered heading. Markdown annotations are persisted
  in the selected file. New thread/note capture forms and live notes appear as
  anchor-positioned overlays so
  they do not expand the document flow; quoted notes remain hidden until the
  quoted text is hovered or focused, while whole-block notes expose a persistent
  note indicator. Closed threads keep one actionable summary card alongside
  their persisted, collapsible transcript; duplicate live/archive cards are
  suppressed. Thread agents remain read-only; verified durable project context
  is promoted by the main session during the explicit Apply action. Revealed
  notes auto-hide after one second of inactivity.
  Apply signals list every changed
  Markdown path in `documentPaths` for the host agent to scan. Apply
  availability is session-wide and stays synchronized across open document
  views. Paths may be project-root-relative or absolute.
- Mermaid diagrams follow the active theme: switching light/dark re-renders
  each diagram so its palette matches the page instead of keeping the baked
  light-mode colours. Clicking a rendered diagram (or any document image)
  opens it in the image viewer with zoom, drag-to-pan, wheel zoom, `+`/`-`/`0`
  keys, and Escape to close.

## Tests

```bash
npm test
npm run typecheck
```

### MCP tool approvals

MCP calls that require permission pause the thread and open an approval overlay.
The user can deny the call, allow it once, approve that MCP tool for the running
discussion server, or approve it permanently for the current project. Session
and project grants apply to the tool name, not just the displayed arguments.

Codex uses its app-server MCP approval elicitations. Claude MCP is disabled
unless `IND_MCP_URL` is set; the connected server's tools are then available for
approval. `IND_MCP_TOOLS` can optionally restrict exposure to a comma-separated
set. `IND_MCP_READONLY_TOOLS` remains a backwards-compatible alias. For the
local gateway, use `IND_MCP_SERVER_NAME=gateway` and names such as
`notion__notion-search,notion__notion-fetch`.

Example using the local read-only gateway session:

```bash
IND_MCP_URL=http://127.0.0.1:3100/mcp \
IND_MCP_SERVER_NAME=gateway \
IND_MCP_TOOLS=notion__notion-search,notion__notion-fetch \
./bin/inline-discussion start --doc ../../docs/sap-oem-voice-auth.md \
  --session-dir "$TMPDIR/inline-discussion/sap-oem" --agent claude --hold
```

Permanent grants are stored in `.inline-discussion-settings.json` at the
project root. The server adds that file to `.git/info/exclude` (including the
shared Git directory for linked worktrees); it never edits `.gitignore`.

The server logs MCP configuration plus approval request, cached-grant, and
resolution events without logging tool arguments.
