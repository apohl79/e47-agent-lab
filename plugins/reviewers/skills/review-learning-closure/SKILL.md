---
name: review-learning-closure
description: "Create one evidence-backed learning closure for every verified INTRODUCED FIX_REQUIRED finding from an implementation checkpoint or PR finalization. Use after proving and fixing the issue, before accepting the re-review or resolving the PR item; do not use it for PRE_EXISTING, REJECTED, or DEFERRED findings."
---

# Review Learning Closure

Close one verified introduced finding, or one verified shared root cause with all
of its finding IDs. This skill is mandatory from
`reviewers:review-implementation-checkpoint` and `reviewers:pr-finalize`.
It records observable evidence, not hidden chain-of-thought.

## Preconditions

Before creating a closure:

- Verify the finding against the cited code and behavior.
- Confirm `ORIGIN: INTRODUCED` by comparing the issue-bearing behavior with the
  implementation or PR base.
- Assign a stable `FINDING_ID`. Preserve the source's ID when available;
  otherwise create one that includes the source and an ordinal.
- Prove the failure mechanism, apply the authorized fix, and run focused
  verification. Link all IDs when several findings share one cause.

Do not create a closure for a `PRE_EXISTING`, `REJECTED`, or `DEFERRED` finding.
If fixing is not authorized, record `CLOSURE_STATUS: PROPOSED`, state the
unverified prevention, and do not treat the review as accepted.

## Produce the Closure

Produce one concise, evidence-backed record:

```text
CLOSURE_STATUS: <COMPLETE|PROPOSED>
FINDING_ID: <stable ID, or comma-separated IDs for one shared root cause>
SOURCE: <checkpoint reviewer|PR review|CI|quality gate|other>
SOURCE_REFERENCE: <review snapshot, PR thread/check URL, or equivalent>
ORIGIN: INTRODUCED
FAILURE_MECHANISM: <how the failure occurs>
VIOLATED_INVARIANT: <behavior that must always hold>
FAILED_ASSUMPTION: <incorrect belief or missing constraint>
ASSUMPTION_EVIDENCE: <OBSERVED|INFERRED>
ESCAPE_REASON: <why existing review, test, type, or control missed it>
ERROR_CATEGORY: <reusable category, not an implementation label>
EARLIEST_PREVENTION_POINT: <earliest practical control>
PREVENTION_APPLIED: <control added, or why it cannot yet be applied>
VERIFICATION: <commands, assertions, or reviewer recheck and outcome>
CONTEXT_CANDIDATE: <none|concise durable invariant or rule>
RETRIEVAL_CUES: <terms that would retrieve a durable candidate>
```

Use `COMPLETE` only when the fix and its verification are evidenced. A
reviewer’s `STATUS: OK`, passing checks, or a resolved PR thread does not
substitute for this record.

## Accept or Escalate

Require one complete closure for every verified introduced finding before
accepting the checkpoint, resolving the PR item as fixed, or declaring the PR
ready. Send the fix and the closure to the same reviewer for recheck when that
reviewer is available; otherwise perform one narrow verification pass.

After two failed fix-and-verify cycles for the same root cause, stop automatic
retries and report the evidence and escalation need.

## Handle Context Candidates

Treat `CONTEXT_CANDIDATE` as a candidate, not a write instruction. Let Project
Context Curator search for overlap and decide admission only after the behavior
is complete and verified on a long-lived branch. Store only a non-obvious,
reusable invariant or rule; prefer code, tests, types, validators, or this
skill when they already make the lesson readily recoverable.
