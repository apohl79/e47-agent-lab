---
name: apply
description: Handle one Apply event from an inline-discussion browser session. Use when the active main-agent turn or the app-server handoff bridge reads an apply-N.json signal and applies its follow-ups directly.
---

# inline-discussion:apply

The argument is the absolute path to one `apply-N.json` signal file. Handle exactly that signal, return control to the active discussion loop or the app-server handoff turn, and do not wait for later browser events.

## App-server active-session guard

In legacy mode, Apply is owned by the active main-agent turn. Keep the
`inline-discussion start --hold` command and the
`inline-discussion wait --idle-exit-seconds 60` heartbeat loop in that same
turn, then handle the signal directly. In Codex or Xedoc app-server handoff
mode, the discussion server has already kept the launcher alive in `screen`
and sent this prompt into the current main session; handle only the supplied
signal and return when it is complete. In both modes, do not start a watcher,
another host CLI session, second server, or second agent session. The launcher
detaches the server, but `--hold` keeps its parent command alive so host-shell
child reaping cannot terminate the browser session while the user reviews.

1. Read the signal JSON and its containing session directory. Set `SIGNAL_PATH` to the supplied absolute `apply-N.json` path and `SESSION_DIR="$(dirname "$SIGNAL_PATH")"`; then read `<session-dir>/url.txt` for the browser URL. Read the signal's `documentPaths` array as the exact absolute paths to scan. It contains the main discussion document plus every Markdown subdocument changed by this Apply round; do not infer, glob, or replace these paths with only the triggering document. If the signal, URL, or `documentPaths` is missing, stop and report the error.
   If Project Context Curator is active, read `docs/context/index.md` from the
   repository root before making project-context claims. Apply is the durable
   context promotion boundary: the child thread agent may surface candidates,
   but only this main-session Apply turn promotes verified facts.
2. Set up the browser progress requests in the same main-agent turn. The browser modal is the user-facing progress channel; text in chat does not update it. Normalize the URL because `url.txt` may or may not end with `/`; appending an API path to an unnormalized trailing slash creates `//api/...`, which does not match the server route:

   ```sh
   BASE_URL="$(<"$SESSION_DIR/url.txt")"
   PROGRESS_URL="${BASE_URL%/}/api/apply/progress"
   FAILED_URL="${BASE_URL%/}/api/apply/failed"
   post_progress() {
     local payload="$1"
     if ! curl --fail --silent --show-error \
       -H 'content-type: application/json' \
       --data "$payload" "$PROGRESS_URL" >/dev/null; then
       curl --silent --show-error \
         -H 'content-type: application/json' \
         --data '{"error":"Apply progress update failed"}' "$FAILED_URL" >/dev/null || true
       return 1
     fi
   }
   ```

   Successful progress updates renew the server's 15-minute inactivity lease.
   For a follow-up that may take longer than that window, send an interim
   progress update before the lease expires so active work is not mistaken for
   an abandoned Apply.

   Every `post_progress` call is mandatory and must succeed. If it returns non-zero, stop Apply handling after the helper has signalled `/api/apply/failed`; do not continue silently.
3. Before any scanning tool call, run `post_progress '{"status":"Scanning follow-ups","percent":5}'`. Re-read every path in `documentPaths` in order. In each document, inspect every new `<details>` `💬 Thread on …` and `📝 Note on …` block and find concrete follow-ups in a thread `Conclusion:` line or note body. Actionable follow-ups include explicit TODO/FIXME/action-item text, imperatives addressed to the agent, concrete code or document changes, and questions requiring research. Ignore acknowledgements. Keep the document path attached to every punch-list item.
   During the same scan, identify durable project-level findings that are
   verified by repository evidence or explicit user confirmation: stable terms,
   components, APIs, ownership boundaries, architecture rules, environment
   mappings, and deployment conventions. For each verified finding, run the
   Project Context Curator updater immediately in this Apply turn using the
   updater command supplied by the active context instructions and source
   `repo-docs` (or `user-confirmed` when applicable). Do not edit generated
   `docs/context/*.md` files directly. Do not promote speculative thread text;
   record an open question instead when the meaning or ownership is unclear.
4. After scanning all listed documents, immediately run a progress request, even when the punch list is empty. Use a short status and either `percent` or `current`/`total`, for example `post_progress '{"status":"Scanned 3 documents; found 2 follow-ups","percent":20}'` or `post_progress '{"status":"Scanned all documents; no actionable follow-ups found","percent":80}'`.
5. Execute every actionable follow-up that does not need a user decision. Keep user-decision items in their `<details>` block. Before each follow-up, send a short status naming it; after it completes, send `current`/`total` (or an equivalent percent). Do not perform multiple follow-ups without a progress request between them. For example:

   ```sh
   post_progress '{"status":"Updating renderer","current":0,"total":2}'
   # execute the follow-up
   post_progress '{"status":"Updated renderer","current":1,"total":2}'
   ```
   Treat each context promotion as an actionable Apply item for progress
   reporting. Update context before moving to the next verified finding, and do
   not wait for a later turn.

6. Before completion, send `post_progress '{"status":"Reloading updated document","percent":95}'`, then run `curl --fail --silent --show-error --request POST "${BASE_URL%/}/api/apply/done" >/dev/null`. Require a successful response from the done request as well.
7. If any Apply handling step fails after the URL is known, send `POST "${BASE_URL%/}/api/apply/failed"` with JSON `{"error":"<short error>"}`. Do not leave the browser in applying state. The progress helper already does this for progress-request failures.

Return a concise status to the active discussion loop after this Apply event. Do not start a second server, watcher, or agent session, and do not wait for another discussion signal here.
