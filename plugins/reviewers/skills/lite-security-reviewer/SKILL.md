---
name: lite-security-reviewer
description: Lightweight security reviewer used as a fallback when no dedicated security review skill is installed. Performs a focused OWASP Top 10 / secure-coding review over the changed files and returns findings in the reviewers reporting contract.
---

# Lite Security Reviewer

This skill is the fallback security reviewer for `reviewers:run-reviewer-team` when no dedicated security review skill is available in the current session.

It is intentionally lightweight: no team coordination, no sub-agent dispatch, no iterative approval. One in-process review pass over the changed files, producing triageable findings.

Do NOT use this skill when a dedicated security review skill is available — the reviewer-team orchestrators pick the richer skill first.

## When to use

- No dedicated security review skill is in the available-skills list for this session (see "Security reviewer skill selection" in `reviewers:run-reviewer-team`).
- Caller is `reviewers:run-reviewer-team`.
- Caller has passed the reviewer prompt contract inputs (review scope, changed files, prior review context, reporting contract).

## Required inputs

- `review_scope` — plan context / execution summary that tells you what the change is supposed to do
- `changed_files` — absolute paths of files created or modified
- `prior_review_context` — already-fixed, rejected, and deferred findings from prior review attempts; treat empty object `{}` as first attempt
- `reporting_contract` — the classification contract from the caller (FIX_REQUIRED / VERIFIED_FIX / REJECTED / DEFERRED)

If any required input is missing, return `status: blocked` with the missing field in `notes`.

## Review checklist

Apply this checklist to each changed file. Skip sections that are not applicable to the file's language or role, but do not skip the whole checklist.

1. **Secrets and credentials**
   - Hard-coded API keys, tokens, passwords, private keys, connection strings.
   - Secrets committed to config files, fixtures, tests, or documentation.
   - Predictable or default credentials.

2. **Injection and untrusted input**
   - SQL / NoSQL query construction via string concatenation or template interpolation.
   - Shell command construction from user input without safe argv arrays.
   - Path traversal in file operations built from untrusted input.
   - Unsafe deserialization (pickle, yaml.load, Java Serializable, etc.).
   - Server-side request forgery via user-controlled URLs / hostnames.

3. **Output handling**
   - XSS in HTML / template rendering without escaping.
   - Unvalidated redirects.
   - Unsafe logging of secrets, tokens, PII, or full request bodies.

4. **AuthN / AuthZ**
   - Missing or weak authentication on new endpoints, routes, handlers, or jobs.
   - Missing authorization / role checks on privileged actions.
   - Insecure session handling: predictable IDs, missing revocation, overly long TTLs.
   - JWT / token handling that skips signature or audience validation.

5. **Cryptography**
   - Use of broken or deprecated primitives (MD5, SHA-1 for security, DES, RC4, ECB).
   - Hard-coded IVs / salts, non-cryptographic randomness used for security decisions.
   - Rolling custom crypto instead of vetted libraries.

6. **Configuration and infrastructure touchpoints**
   - Overly broad IAM permissions, public bucket / blob exposure, wildcard CORS.
   - Disabled TLS verification, `rejectUnauthorized: false`, `verify=False`.
   - Debug / verbose modes left enabled for production paths.

7. **Dependency and supply-chain risk**
   - New dependencies with known-vulnerable versions, typosquats, or unpinned floating ranges on security-sensitive packages.
   - Lockfile diffs that silently bump across majors for security-critical libraries.

8. **AI / LLM surfaces (only when the change touches LLM prompts, tools, or model I/O)**
   - Prompt injection exposure: untrusted content concatenated into system prompts without isolation.
   - Insecure tool / function calling: no allowlist, no argument validation, no rate limits.
   - Secrets embedded in prompts or tool descriptions.
   - Model output used in `eval`, shell, or SQL without sanitization.

## Execution

1. Validate all required inputs are present.
2. Read each file in `changed_files`. Skip files you cannot read and note the skip in `notes`.
3. Apply the review checklist above to every readable changed file.
4. For every concern discovered, classify it against the reporting contract:
   - `FIX_REQUIRED` — real, in-scope, must be fixed
   - `VERIFIED_FIX` — a prior FIX_REQUIRED finding that is now correctly fixed
   - `REJECTED` — invalid, out of scope, or based on incorrect assumptions
   - `DEFERRED` — real but intentionally left unresolved (must state reason)
5. Respect `prior_review_context`: do NOT re-raise findings already marked fixed, rejected, or deferred unless you have new evidence that invalidates the prior decision.
6. Do NOT make code changes. This is a review pass, not a fix pass.

## Completion contract

Return a structured report with these fields:

- `status` — `complete` | `blocked`
- `reviewer` — `"reviewers:lite-security-reviewer"`
- `findings` — list of findings; each entry:
  - `id` — short unique identifier (e.g. `S1`, `S2`)
  - `category` — one of `FIX_REQUIRED` | `VERIFIED_FIX` | `REJECTED` | `DEFERRED`
  - `file` — affected file path
  - `line` — line reference when applicable
  - `description` — concrete description of the finding
  - `reasoning` — why this is (or is not) a security issue in context
  - `checklist_area` — which checklist section the finding came from (e.g. `secrets`, `injection`, `authz`, `crypto`, `ai-llm`)
  - `deferred_reason` — populated only for `DEFERRED`
- `notes` — free-text observations, skipped files, or blockers

### `status: complete`

Review completed over every readable changed file. `findings` may be empty if nothing actionable was found — that is a valid outcome.

### `status: blocked`

A required input was missing or every changed file was unreadable. `notes` must contain the exact blocker.

## Constraints

- Do NOT dispatch sub-agents.
- Do NOT invoke any other security skill or security orchestrator agent. This skill is the fallback precisely because those are unavailable.
- Do NOT write code fixes. Report findings only.
- Do NOT invent findings to fill a quota. An empty `findings` list is a valid `status: complete` outcome.
- Do NOT re-raise findings already resolved in `prior_review_context`.
