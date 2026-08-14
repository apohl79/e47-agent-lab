---
name: discuss
description: Inline-discussion browser UI — turn a document or recent response into a reviewable markdown artefact with threaded AI conversations. Use when the user runs /inline-discussion:discuss or asks to discuss a long response or an existing doc.
---

# inline-discussion:discuss

## Flow

Codex supports two runtime modes. The legacy mode keeps this turn alive and
handles signal files directly. When the current Codex app-server exposes the
running main session, use the handoff mode below: the discussion server stays
alive in a detached `screen` session, and its Apply/Finish events send prompts
back into that same main session through the generic app-server protocol.

1. **Resolve the host.**
   - If `$CODEX_THREAD_ID` or `$CODEX_SESSION_JSONL` is set, set `<HOST> = codex`.
   - Otherwise set `<HOST> = claude`.

2. **Resolve the argument.** The invocation may look like `/inline-discussion:discuss` (no arg), `/inline-discussion:discuss my-title`, `/inline-discussion:discuss docs/some-file.md`, or `/inline-discussion:discuss docs/some-file.pdf`.
   - If the arg is an existing markdown file (`.md` or `.markdown`) → use it as `<DOC_PATH>`; set `<SLUG>` = filename without extension; set `<SOURCE_PATH>` empty; **skip step 3**. The file can live anywhere — the launcher does not require it under the project root.
   - If the arg is an existing non-markdown file → set `<SOURCE_PATH>` = that file. Pick `<SLUG>` from the filename without extension. Set `<DOC_PATH> = docs/discussions/<YYYY-MM-DD>-<SLUG>.md`. Continue to step 3 to convert it into a discussion document.
   - Otherwise → treat it (or the topic if no arg) as a title. Pick a kebab-case `<SLUG>`. Set `<SOURCE_PATH>` empty. Set `<DOC_PATH> = docs/discussions/<YYYY-MM-DD>-<SLUG>.md` — fresh docs captured from a recent main-agent response always go into the current repo so they can be committed.

3. **Write the doc.** (Fresh or converted path only.) Ensure the `docs/discussions/` directory exists.
   - If `<SOURCE_PATH>` is empty → write the research content from the recent conversation into `<DOC_PATH>` as a properly structured markdown document. Add a title, logical sections, preserved code blocks. Not a summary — the same content, reorganised for review.
   - If `<SOURCE_PATH>` is set → convert the source file into a discussion markdown document at `<DOC_PATH>`. The conversion is agent-driven: use available file-read, shell, OCR, document-inspection, or extraction tools as needed for the file type. Prefer faithful structure over summarisation. Preserve headings, sections, tables, lists, links, code blocks, quoted text, and important metadata when extractable. If exact extraction is impossible, create a useful discussion document with extracted/visible facts, file metadata, and an explicit note naming what could not be extracted.
   - For rich documents such as Google Docs exports, Word documents, PDFs generated from docs, slides, and pasted browser captures, reconstruct the visual document shape before launching. Tables in the source must become Markdown tables, with one source row per Markdown table row and cell contents kept in the correct columns. Do not flatten table headers/cells into separate paragraphs. If a table cell contains bullets, use `<br>`-separated bullet fragments inside that cell instead of moving them below the table. Checklists in the source must become Markdown task lists (`- [ ]` / `- [x]`) and preserve checked state, assignees, strikethrough, and highlighted text when visible. For Google Docs/HTML exports, checkbox state and strikethrough often live in CSS classes or style rules instead of inline text; inspect those rules and convert them to inline Markdown (`- [x]` for checked items, `~~text~~` for strikethrough spans) before launching. Preserve section order and hierarchy.
   - Embedded images are part of the document content and must be materialized before launching. For Google Docs API results, enumerate `inlineObjects[*].inlineObjectProperties.embeddedObject.imageProperties.contentUri` and download each image with the authenticated Google Workspace/Drive or browser session used to read the source. For HTML or export bundles, copy the referenced media files into a directory inside the repository. Store the files under a stable path relative to `<DOC_PATH>`, rewrite every image `src` to that repository-relative path, and preserve visible dimensions as Markdown/HTML attributes where the renderer supports them. Do not leave `contentUri`, `file://`, `/tmp`, or an unresolved `media/...` path in the converted document: the discussion server can only serve files that exist inside the document's project root or alongside the converted document. If an image cannot be fetched, replace the broken image with an explicit alt-text/`Visual content` note naming what was omitted instead of launching a broken `<img>` tag.
   - For spreadsheets or CSV-like files, render tables as Markdown tables when practical; otherwise use fenced code blocks. For images or scanned PDFs, use OCR or image inspection if available and include a short "Visual content" section for non-textual information.
   - Before starting the server for a converted document, quickly inspect `<DOC_PATH>` for obvious conversion damage: table-like source content flattened into headings/paragraphs, missing checklist boxes, lost row/column grouping, repeated orphaned cell values, or image references whose files do not exist. If damage is present, fix the Markdown structure or materialize the missing assets first. Do not launch a converted document that is visibly worse than the source layout.
   - Converted documents must start with a heading based on the source filename and include a short source note, for example: `Source: path/to/file.pdf (converted for inline discussion)`. Do not mutate `<SOURCE_PATH>`.

   **Thread context boundary.** Inline thread agents are read-only reviewers.
   They may identify candidate project facts in their replies, but they must not
   write `docs/context/` directly. The explicit user Apply action hands those
   candidates to the main session, which verifies them against the repository
   and promotes durable findings with Project Context Curator in that same Apply
   turn. This keeps speculative discussion separate from durable project memory.

4. **Resolve the main-session JSONL path.**
   - If `$CLAUDE_SESSION_JSONL` is set → use it as `<JSONL_PATH>`.
   - Else if `$CLAUDE_SESSION_ID` is set → use `$(echo "$PWD" | sed 's|/|-|g' | sed 's|^-|-|')` to construct `~/.claude/projects/<encoded-cwd>/$CLAUDE_SESSION_ID.jsonl`.
   - Else if `$CODEX_SESSION_JSONL` is set → use it as `<JSONL_PATH>`.
   - Else if `$CODEX_THREAD_ID` is set → find the current Codex rollout file:

     ```bash
     CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
     JSONL_PATH="$(find "$CODEX_HOME/sessions" -type f -name "*${CODEX_THREAD_ID}.jsonl" -print 2>/dev/null | sort | tail -n1)"
     ```

   - If no transcript path is found, create `<session-dir>/main-session.jsonl` with one JSONL line containing the user's current request and use that fallback. Do not fail the discussion just because transcript discovery is unavailable.

4a. **Prefer Codex app-server handoff when the running main session is listed.**
    This check is only for `<HOST> = codex`; Claude keeps the active-turn flow
    below. `inline-discussion sessions` is the generic app-server discovery
    command. It speaks directly to Codex's Unix-socket app-server protocol, so
    no fork-specific `codex-session` helper is required.

    ```bash
    CODEX_SESSION_BRIDGE=""
    if [[ "$HOST" = codex ]] && command -v inline-discussion >/dev/null 2>&1 \
      && [[ -n "${CODEX_THREAD_ID:-}" ]]; then
      CODEX_SESSIONS_JSON="$(inline-discussion sessions 2>/dev/null || true)"
      if python3 -c '
    import json, sys
    thread_id = sys.argv[1]
    sessions = json.load(sys.stdin)
    raise SystemExit(0 if any(
        isinstance(item, dict)
        and item.get("id") == thread_id
        and item.get("canAcceptDirectInput") is True
        for item in sessions
    ) else 1)
      ' "$CODEX_THREAD_ID" <<<"$CODEX_SESSIONS_JSON"; then
        CODEX_SESSION_BRIDGE="app-server"
      fi
    fi
    if [[ "$CODEX_SESSION_BRIDGE" = app-server ]] && ! command -v screen >/dev/null 2>&1; then
      echo 'Codex main session is listed, but screen is unavailable; using the active-turn fallback.' >&2
      CODEX_SESSION_BRIDGE=""
    fi
    ```

    When `CODEX_SESSION_BRIDGE=app-server`, do not keep this turn open and do
    not start `inline-discussion wait`. Start the server in a detached screen
    session with the running main-session id:

    ```bash
    HANDOFF_SESSION_DIR="${TMPDIR:-/tmp}/inline-discussion/<SLUG>"
    SCREEN_NAME="inline-discussion-<SLUG>-${CODEX_THREAD_ID:0:8}"
    mkdir -p "$HANDOFF_SESSION_DIR"
    screen -dmS "$SCREEN_NAME" inline-discussion start \
      --doc "<DOC_PATH>" \
      --main-jsonl "<JSONL_PATH>" \
      --main-session-id "$CODEX_THREAD_ID" \
      --session-dir "$HANDOFF_SESSION_DIR" \
      --agent codex \
      --cwd "$PWD" \
      --hold
    ```

    Wait only for `<session-dir>/url.txt` (not for a browser signal), then
    publish the URL and perform the cmux browser-open step below.
    If the URL does not appear within 30 seconds, inspect `server.log` and
    report the startup failure. The server sends Apply and Finish prompts back
    into the listed main session, so the user can continue that session after
    this turn ends. Do not start `codex exec`, `inline-discussion watch`, or a
    second agent session.

5. **Start the server.**

   If the Codex app-server handoff check below selected handoff mode, skip this
   section and the signal loop. The handoff block has already started the
   server, published the URL, and intentionally ends this turn.

   The `inline-discussion` CLI must be on `PATH`. It ships with this marketplace and is installed into `~/bin` (or `~/.local/bin`) by `install.sh`. If the command is missing, tell the user to run `./install.sh` from the marketplace checkout (or via the one-liner) and stop.

   If `<HOST> = codex`, run this as a background command/session and leave it running. It prints the URL quickly, then `--hold` keeps the launcher process alive until the user clicks Pause or Finish. Do not wait for this command to exit before continuing to cmux/browser opening and the signal-file loop:

   ```bash
   inline-discussion start \
     --doc "<DOC_PATH>" \
     --main-jsonl "<JSONL_PATH>" \
     --session-dir "${TMPDIR:-/tmp}/inline-discussion/<SLUG>" \
     --agent "<HOST>" \
     --cwd "$PWD" \
     --hold
   ```

   If `<HOST> = claude`, run this foreground — it boots the server detached, prints the URL, and exits within a couple seconds:

   ```bash
   inline-discussion start \
     --doc "<DOC_PATH>" \
     --main-jsonl "<JSONL_PATH>" \
     --session-dir "${TMPDIR:-/tmp}/inline-discussion/<SLUG>" \
     --agent "<HOST>" \
     --cwd "$PWD"
   ```

   The command prints the browser URL (single line, `http://127.0.0.1:<port>/`) on stdout. Echo that URL to the user and tell them to open it. The URL is also written to `<session-dir>/url.txt` as a fallback.

   **Codex active-session rule.** In legacy mode, keep this main Codex turn open after publishing the URL. The same turn owns the `inline-discussion wait` heartbeat loop, reads each signal, and handles Apply directly. Do not start `inline-discussion watch`, `codex exec`, or a second agent session for Apply. The `--hold` launcher must remain alive while the browser is open so the detached server is not reaped by the host shell. In app-server handoff mode, the `screen` session owns the launcher and this turn ends after the URL is published.

   **cmux integration.** When the environment indicates a cmux session (e.g. `$CMUX_WORKSPACE_ID` or `$CMUX_BUNDLE_ID` is set, or the `cmux` CLI is available and `cmux identify` succeeds), open the discussion URL in the workspace the user is actually looking at.

   `cmux browser open` defaults to `$CMUX_WORKSPACE_ID` and defaults `--focus` to `false`. The agent's shell is often in a different workspace than the user's focused surface, so the bare form can report `OK ... placement=reuse` while the browser opens in a workspace the user cannot see. Resolve the focused workspace first and open there with focus:

   ```bash
   cmux identify            # read .focused.workspace_ref
   cmux browser open "<URL>" --workspace <focused-workspace-ref> --focus true
   ```

   If `cmux identify` does not report a focused workspace, fall back to `cmux browser open "<URL>" --focus true`.

   Verify the tab actually loaded before telling the user it is open, using the surface ref returned by `open`:

   ```bash
   cmux browser --surface <surface-ref> get url
   ```

   Report the surface ref and the workspace it opened in. Do not close the browser tab automatically. If the command fails, fall back to echoing the URL and mention the failure briefly; do not retry with another cmux command.

6. **Loop on signal files (legacy mode only).** Steps 6a–6d repeat until the user clicks Pause or Finish. App-server handoff mode has already returned control to the running main session and must not enter this loop.
   For both Codex and Claude, continue with the loop below in the current main agent turn. Codex uses the bounded heartbeat form in step 6a; Claude may use the unbounded form.
   This loop is intentionally unbounded. The browser may sit idle for hours
   while the user reviews. Never conclude, report "blocked", stop the server,
   or clean up just because no Apply, Pause, or Finish signal has arrived yet. Only
   stop waiting on an actual `result.json`/`pause.json`/`apply-N.json` signal, explicit user
   cancellation, or server failure.

   **Turn-continuation rule (non-negotiable).** While this loop is active you
   MUST NOT emit a final, turn-ending message. On hosts where `wait` runs in a
   background command/session (Codex), the tool call can *yield* — return
   control to you with no output — while the server is still alive and no signal
   has been written yet. An empty/early yield with no signal path is **not** a
   completion event and **not** a reason to stop: it is identical to the exit
   `124` heartbeat case below. When it happens, immediately re-enter step 6a
   (re-poll the same session or re-invoke `wait`). There is no callback that
   will re-drive this loop for you after the turn ends, so ending the turn here
   silently drops any Apply/Pause/Finish the user later clicks. The loop ends only on
   a real `result.json`/`pause.json`/`apply-*.json` signal path, explicit user cancellation,
   or a dead server (see the liveness check below).

   **Server-liveness check.** "No signal yet" is never a stop condition while
   the server is alive. Before you ever continue past the loop or report a
   problem, confirm the server process:

   ```bash
   kill -0 "$(cat "${TMPDIR:-/tmp}/inline-discussion/<SLUG>/server.pid" 2>/dev/null)" 2>/dev/null \
     && echo alive || echo dead
   ```

   - `alive` + no signal file → keep looping (go to step 6a).
   - `dead` → report server failure and stop.
   - Absence of `result.json`, `pause.json`, and `apply-*.json` by itself is never "blocked".

   a. Run this as a background command/session so the agent can continue when it exits. It blocks until `result.json`, `pause.json`, or `apply-N.json` appears, prints the absolute path on stdout, and exits 0:

      ```bash
      inline-discussion wait \
        --session-dir "${TMPDIR:-/tmp}/inline-discussion/<SLUG>"
      ```

      **Codex timeout guard.** Codex command sessions can time out while the
      browser is correctly idle. When `<HOST> = codex`, do not use one
      unbounded wait command. Instead run bounded heartbeat waits:

      ```bash
      inline-discussion wait \
        --session-dir "${TMPDIR:-/tmp}/inline-discussion/<SLUG>" \
        --idle-exit-seconds 60
      ```

      If this exits `124` and prints no signal path, that means "still waiting,
      server alive". It is not an error and not a user-visible event. Immediately
      repeat step 6a. Treat **every** non-signal outcome the same way: exit `124`,
      an early/empty yield of the background session, a tool timeout, or an
      interruption without a printed signal path are all "still waiting" as long
      as the server-liveness check reports `alive`. In every one of those cases,
      immediately restart step 6a — do not end the turn, do not summarise, do not
      ask the user anything, and never report "no Apply or Finish signal arrived"
      as blocked. Only a printed `result.json`/`apply-*.json` path (handled in
      6b–6d) or a `dead` server may exit this loop.

   b. When the wait command exits 0, read the path it printed and load that JSON file.

   c. **If the filename is `result.json`** (Finish): continue to step 7 with the existing flow (scan, report, ask how to proceed). The loop exits.

   **If the filename is `pause.json`** (Pause): read it, report that the discussion is paused with its open thread and highlight counts, then end the turn. Do not scan for follow-ups or archive open threads. A later invocation with the same document uses the same session directory and resumes the persisted live threads and highlights.

   d. **If the filename matches `apply-*.json`** (Apply):
      - Read the URL from `${TMPDIR:-/tmp}/inline-discussion/<SLUG>/url.txt`.
      - Load and follow `inline-discussion:apply` for Apply handling. Its URL-normalized `post_progress` helper is mandatory and response-checked; the browser modal is the user-facing surface, and text in chat does not update it.
      - POST an initial progress update before any scanning tool call, for example `{"status":"Scanning follow-ups","percent":5}`.
      - Read `documentPaths` from the `apply-N.json` signal and scan every exact path listed there, including changed Markdown subdocuments. Do not scan only the triggering document or infer additional paths. Build the punch list silently — do NOT show it to the user — and keep each item's document path.
      - POST progress after scanning all listed documents. If there are no actionable items, use a status like `Scanned all documents; no actionable follow-ups found`.
      - Execute every actionable follow-up automatically. Use the available file read/edit tools to mutate `<DOC_PATH>`. Skip silently any item that requires a user decision; it stays in its `<details>` block for the next round.
      - POST progress before and after each actionable item using `current`/`total`, with a status naming the item in a few words; do not perform multiple items without an update between them.
      - Progress updates renew a 15-minute Apply inactivity lease; send an interim update before that window expires if a single follow-up takes longer.
      - POST a final progress update such as `{"status":"Reloading updated document","percent":95}` before signalling completion.
      - POST to the normalized `${BASE_URL%/}/api/apply/done` URL with no body. On HTTP error, POST instead to `${BASE_URL%/}/api/apply/failed` with body `{"error":"<error>"}`.
      - Continue the loop immediately (go to step 6a). The next `inline-discussion wait` call signals to the browser that the main session is back in monitoring mode, which dismisses the Apply checklist.

7. **Read the result (Finish only).** When step 6c fired, parse `<session-dir>/result.json` and continue.

8. **Scan the updated doc for follow-ups (Finish only).** Re-read `<DOC_PATH>`. Inside every new `<details>` `💬 Thread on …` and `📝 Note on …` block, look at the **Conclusion:** line (for threads) or the note body (for notes) and extract any action items the user likely wants to act on. Signals to treat as follow-ups:

   - explicit TODO / FIXME / action-item phrasing
   - imperatives addressed to you ("rewrite X", "add a test for Y", "fix the regex in Z", "open a PR for …")
   - unresolved questions that need a decision or research
   - concrete code/doc change requests, even if phrased as suggestions

   Cosmetic acknowledgements ("sounds good", "ack", "yep") are not follow-ups.

   Build a short punch list: each item gets a 1-line summary + the anchor it came from (heading or quoted snippet), grouped by doc section.

9. **Report back and ask how to proceed (Finish only).** Summarise the discussion (thread count, each conclusion with a short anchor label, archived count if > 0, path to `<DOC_PATH>`). Then present the follow-up punch list and ask the user how they want to proceed.

   If the host provides a structured question tool, use it with options like:
   - `Work through them now` — start executing the punch list in order
   - `Pick a subset` — ask the user which items to take on
   - `Just save the list` — stop, leave the doc as-is, no further action
   - `Ignore, we're done` — nothing more to do

   If no structured question tool is available, ask the same question directly in plain text. If there are no follow-ups, say so explicitly and stop — do not fabricate work.

## Output contract

Return in plain English:
- Path to the updated doc (`<DOC_PATH>`).
- Source path when a non-markdown document was converted (`<SOURCE_PATH>`).
- Each new thread's conclusion with a short anchor label.
- Archived thread count, if > 0.
- Follow-up punch list extracted from threads/notes (empty list if nothing actionable).
- The user's chosen next step from the follow-up prompt — omit when the punch list is empty and no prompt was shown.
- During an in-session Apply, no user-visible report is produced — the browser is the user-facing surface. The follow-up list is processed silently and the loop resumes.
- In Codex app-server handoff mode, report the URL and detached screen session, then end this turn after startup; the server prompts the same main session for later Apply and Finish actions.

## Runtime state

Transient runtime data (transcript preamble, server log, url.txt, pid, result.json) lives under `${TMPDIR:-/tmp}/inline-discussion/<SLUG>/`. Nothing is written into the project directory, so no `.gitignore` entry is needed.
