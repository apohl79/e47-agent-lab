---
name: pr-finalize
description: Get an existing GitHub pull request into a mergeable ready state by moving drafts to ready, syncing with the base branch, addressing review comments and quality gates, waiting for checks, resolving threads, and optionally merging only when explicitly requested.
---

# PR Finalizer

Drive one existing pull request to a ready state. The agent monitors the PR directly with available GitHub MCP tools, `gh`, and GitHub APIs. Do not launch a deterministic monitor script, sidecar wrapper, background job, or nested fixer mode.

Invocation: `[pr-link-or-number] [--merge] [--merge-admin]`.

Ready means all of the following are true:

- The PR branch is in sync with the base branch.
- The PR is not a draft.
- All required and reported checks have finished.
- All checks are passing or explicitly non-blocking with evidence.
- All review comments and review threads are addressed, replied to, and resolved.
- No open comments remain that request a change.

## Guardrails

- Merge only when the user explicitly passed `--merge` or `--merge-admin`.
- Use `--merge-admin` only when explicitly passed; never infer it.
- Do not mark a finding resolved without either fixing it or replying with a clear rejection/deferment reason.
- Do not ignore failing checks because they look flaky; prove whether they are unrelated to the PR branch before classifying them non-blocking.
- Do not use browser automation for GitHub operations. Prefer GitHub MCP tools for PR reads, checks, comments, and threads when available; fall back to `gh` and GitHub APIs when MCP tooling is unavailable or incomplete. Use Sonar-capable MCP tools for Sonar issues when available.
- Do not stop while checks are pending, threads are unresolved, or requested changes are unaddressed unless blocked by missing access or a human-only decision.

## Workflow

### 1. Resolve the PR

If an argument is provided, treat it as a PR URL or number. If not, resolve the PR from the current branch:

```bash
gh pr view --json number,url,state,isDraft,headRefName,headRefOid,baseRefName,mergeStateStatus,reviewDecision
```

Record owner, repo, PR number, URL, head branch, head SHA, and base branch. If the PR cannot be resolved, stop with the exact `gh` error.

### 2. Move Drafts to Ready

If `isDraft` is true, run:

```bash
gh pr ready <number> --repo <owner>/<repo>
```

Re-read the PR and verify `isDraft` is false before continuing.

### 3. Sync With Main/Base

Fetch the base branch and bring the PR branch up to date:

```bash
git fetch origin <base-branch>
git rebase origin/<base-branch>
```

If rebase is inappropriate for the repository, merge the base branch instead and state why. Resolve conflicts in the worktree, run relevant tests, and push with `--force-with-lease` after a rebase. Re-read the PR after pushing because the head SHA changed.

### 4. Build the Issue Inventory

Collect every open item before fixing:

- PR reviews and review decision:
  ```bash
  gh pr view <number> --repo <owner>/<repo> --json reviews,reviewDecision,comments
  ```
- Review threads through GraphQL, including `id`, `isResolved`, path, line, author, body, and replies.
- Check runs and statuses for the current head SHA:
  ```bash
  gh pr checks <number> --repo <owner>/<repo> --watch=false
  gh api repos/<owner>/<repo>/commits/<head-sha>/check-runs --paginate
  gh api repos/<owner>/<repo>/commits/<head-sha>/status
  ```
- Workflow job logs for failed GitHub Actions jobs.
- Quality gates from PR checks, bot comments, artifacts, and available MCP tools.

For Sonar issues, first look for a Sonar-capable MCP server or tool in the current session. Use it to fetch issue details, rule metadata, severity, file/line, and status when available. If no Sonar-capable MCP is available, use SonarCloud/SonarQube PR comments, check summaries, artifacts, or linked logs from GitHub.

### 5. Triage Everything

For each item, classify it:

- `FIX_REQUIRED`: real issue caused by or exposed by this PR.
- `REJECTED`: false positive, incorrect review, duplicate, or unrelated to the PR, with evidence.
- `DEFERRED`: real issue intentionally left outside this PR, with a concrete reason.
- `BLOCKED`: cannot be resolved without missing access, external service recovery, or a human decision.

Only use `REJECTED` or `DEFERRED` when you can explain the evidence in a PR reply. If an issue is unrelated, prove it by comparing changed files, base-branch behavior, logs, or ownership.

### 6. Fix and Verify

For each `FIX_REQUIRED` item:

1. Make the smallest relevant code, test, config, or documentation change.
2. Run focused verification first.
3. Run the repository's standard checks when discoverable.
4. Commit with a clear conventional message.
5. Push the branch.

If multiple comments describe one root cause, fix them in one commit and mention every affected thread in the reply.

### 7. Reply and Resolve Threads

For every review thread and bot comment:

- Reply with what changed, or why the finding is `REJECTED` or `DEFERRED`.
- Resolve the thread after replying.
- Verify the thread is resolved through GraphQL.

Use GitHub GraphQL for thread resolution:

```bash
gh api graphql -f query='mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}) { thread { id isResolved } } }' -F id=<thread-id>
```

If a comment is not part of a resolvable review thread, reply in the appropriate PR conversation and include the classification.

### 8. Monitor Directly Until Ready

After every push, re-read the PR, current head SHA, checks, reviews, comments, and unresolved threads. Wait for checks to finish by polling with available GitHub MCP tools first; use `gh` and GitHub APIs as fallback. Continue the fix/reply/resolve loop until the ready definition is satisfied.

Use a short polling interval and report meaningful state changes. Do not end with "checks are still running" unless the user asked to stop or the wait is blocked by missing access/service failure.

### 9. Merge Only If Requested

When the ready definition is satisfied:

- No merge flag: stop and report that the PR is ready but not merged.
- `--merge`: run `gh pr merge <number> --repo <owner>/<repo> --merge`.
- `--merge-admin`: run `gh pr merge <number> --repo <owner>/<repo> --merge --admin`.

After a merge command, verify the PR state is `MERGED` and report the merge commit when GitHub returns one.

## Final Report

Report:

- PR URL and final state.
- Whether draft was moved to ready.
- How the branch was synced with the base branch.
- Checks and quality gates addressed.
- Comment threads replied to and resolved.
- Remaining blockers, if any, with exact owner/action needed.
- Merge result, only when a merge flag was passed.
