---
name: review-implementation-checkpoint
description: "Use automatically during every source, test, or configuration implementation task: run a lightweight, single-reviewer checkpoint before declaring code ready and after each complete behavior slice or risky boundary change in larger tasks. Review the smallest immutable delta, return valid bugs to the implementing agent for focused repair and root-cause learning, and leave broad multi-reviewer coverage to reviewers:run-reviewer-team. Use its learning closure also whenever a committed or deployed implementation mistake is discovered, even outside a scheduled checkpoint, to identify the earliest practical control that could catch the whole error category."
---

# Review Implementation Checkpoint

Run a narrow independent review while implementation context is still fresh.
Keep this cheaper than `reviewers:run-reviewer-team`: use one reviewer, one
checkpoint delta, focused verification, and immediate fix-and-learn feedback.

Run this skill at least once for every implementation task that changes source,
tests, or configuration. For a larger task, also run it after each complete
behavior slice and before building the next slice on top of it. Do not run it
for documentation-only edits.

A valid implementation mistake discovered after commit or deployment is an
alternate entry point, not a `risk_lens`. Verify the mistake, then run the
learning record and prevention analysis under **Triage and Close Findings** even
when no checkpoint reviewer found it. Preserve its evidence-backed origin, and
do not attribute its failed assumption to the current implementation without
evidence. If fixing it changes source, tests, or configuration, review the fix
through the normal checkpoint flow.

## Inputs

Resolve omitted inputs from the task plan and repository:

- `implementation_goal`: requested behavior and acceptance criteria.
- `implementation_base`: branch or commit before the implementation began.
- `checkpoint_base`: last checkpoint accepted as verified; default to
  `implementation_base` for the first checkpoint.
- `checkpoint_head`: immutable commit or content-addressed snapshot to review,
  or the frozen current working tree for the final checkpoint only.
- `focused_verification`: commands already run and their outcomes.
- `prior_findings`: earlier checkpoint findings and their dispositions.
- `risk_lens`: optional risk named by the plan or changed behavior.

Missing optional context is not a blocker. Derive it from repository evidence
or state the neutral default in the report.

## Freeze the Checkpoint

Pause implementation edits while the reviewer reads the checkpoint. Record
`HEAD` and `git status --short` before dispatch.

- For a committed checkpoint, review `checkpoint_base..checkpoint_head`.
- Before an intermediate working-tree checkpoint, create an immutable,
  content-addressed snapshot that captures modified, deleted, and untracked
  implementation files and can be diffed against both `checkpoint_base` and a
  later snapshot. Prefer a normal checkpoint commit when the repository
  workflow authorizes it; otherwise use a host-supported private snapshot that
  does not change the branch, index, or working tree. Record its identifier and
  cleanup owner.
- A mutable working tree may be reviewed directly only for the final checkpoint,
  while edits remain paused. It cannot become a later `checkpoint_base`.
- If the checkpoint changes before review completes, discard that result and
  review the new snapshot. Do not apply findings to a moving target.
- If no safe immutable snapshot is available for an intermediate checkpoint,
  wait for the next natural commit or report that the next review must restart
  from `implementation_base`. Never claim an unavailable incremental base.

If no implementation delta exists and no committed or deployed mistake
triggered the skill, report a no-op checkpoint and stop. For an externally
discovered mistake without a fix delta, skip reviewer dispatch but complete the
learning closure within the task's authorized scope.

## Run One Reviewer

Use exactly one independent, read-only reviewer. Do not launch
`reviewers:run-reviewer-team`, external reviewer CLIs, or several lenses.

When the host supports reusable reviewer threads, keep one checkpoint reviewer
for the implementation task and send later checkpoints as follow-ups. Otherwise
start one reviewer for the current checkpoint. Do not let the reviewer delegate
or edit files.

Give the reviewer:

- the implementation goal and acceptance criteria;
- the implementation and checkpoint bases;
- the exact checkpoint snapshot;
- focused verification already run;
- prior finding dispositions; and
- the selected risk lens.

Tell the reviewer to:

1. Discover the checkpoint delta directly.
2. Read the changed behavior plus only the direct callers, callees, defaults,
   configuration, and sinks needed to judge it.
3. Check correctness, error and boundary paths, and whether focused tests would
   fail when the behavior regresses.
4. Emphasize one additional lens when the change warrants it:
   trust/security, async lifecycle, public contract, persistence/migration, or
   cross-component data flow.
5. Treat the lens as an emphasis, not permission to ignore another clear bug.
6. Run no broad test suite. Run a focused command only when needed to confirm a
   concrete finding.
7. Make no changes.

Require either exactly `STATUS: OK` or one block per finding:

```text
CATEGORY: <FIX_REQUIRED|VERIFIED_FIX|REJECTED|DEFERRED>
SEVERITY: <critical|major|minor|nit>
ORIGIN: <INTRODUCED|PRE_EXISTING>
WHERE: <relative/path>:<line>
ISSUE: <concrete description>
WHY: <evidence-backed consequence>
FIX: <smallest concrete fix>
```

Judge `ORIGIN` against `implementation_base`, not only
`checkpoint_base`. Use `INTRODUCED` when the implementation creates the bug,
activates a latent bug, or materially worsens its impact. Use `PRE_EXISTING`
only when the same bug demonstrably exists at `implementation_base` and the
implementation neither activates nor worsens it.

## Triage and Close Findings

Verify every finding against the cited code and the implementation goal. Do not
accept a finding only because the reviewer asserted it.

- Reclassify an unsupported finding as `REJECTED` with evidence.
- Keep an intentional residual risk as `DEFERRED` only with likelihood, impact,
  and ownership evidence.
- Do not attribute a `PRE_EXISTING` finding to the current implementation
  approach.
- Return every verified `INTRODUCED` `FIX_REQUIRED` finding to the agent that
  implemented the checkpoint. If the current agent implemented it, close the
  finding directly.

For each returned bug, require this concise learning record. Record observable
reasoning summaries, never hidden chain-of-thought:

```text
FINDING_ID:
FAILURE_MECHANISM:
VIOLATED_INVARIANT:
FAILED_ASSUMPTION:
ASSUMPTION_EVIDENCE: <OBSERVED|INFERRED>
ESCAPE_REASON:
ERROR_CATEGORY:
EARLIEST_PREVENTION_POINT:
PREVENTION_APPLIED:
VERIFICATION:
CONTEXT_CANDIDATE:
RETRIEVAL_CUES:
```

If the current task does not authorize implementing a fix, stop after the
learning record and report the proposed prevention without changing files.
Otherwise:

1. Reproduce or otherwise prove the bug.
2. Apply the smallest complete fix.
3. Add a regression test that fails for the original mechanism.
4. Identify the earliest practical control that could catch the whole error
   category before commit or deployment, not only this instance. Apply the
   prevention supported by evidence: prefer a safe API, type, validator, static
   check, or reusable test before a prompt reminder.
5. Run focused verification.
6. Send the fix and learning record to the same reviewer when reuse is
   available; otherwise run one narrow verification pass.
7. Accept the checkpoint only when the reviewer verifies the fix and the
   prevention claim is supported.

After two failed fix-and-verify cycles for the same cause, stop automatic
retries and report the evidence and escalation need.

## Context Admission

Treat `CONTEXT_CANDIDATE` as a candidate, not a write instruction. Project
Context Curator owns search, consolidation, applicability, and admission when
it is active.

Do not store an active branch's bug report or implementation detail. After the
work is complete and verified on a long-lived branch, admit only a non-obvious
invariant or reusable implementation rule that benefits unrelated future work.
Prefer the implemented code, test, type, validator, or skill when it already
makes the lesson readily recoverable.

## Escalation

Do not invoke `reviewers:run-reviewer-team` automatically. Recommend it when
broad independent coverage is justified, including:

- security, privacy, authorization, data-loss, or migration risk;
- a public or cross-repository contract change;
- concurrency or lifecycle behavior spanning several components;
- a critical or disputed checkpoint finding;
- two failed fix-and-verify cycles; or
- a checkpoint too broad to review as one coherent behavior slice.

Ask before starting the expensive reviewer-team run unless the user or active
workflow already required it.

## Completion

Report:

- checkpoint base and reviewed snapshot;
- reviewer used or why no independent reviewer was available;
- accepted, rejected, and deferred findings;
- fixes and focused verification;
- learning records for introduced bugs;
- the next verified checkpoint base as an immutable snapshot identifier, or
  `none (final working-tree checkpoint)`; and
- any recommended escalation or pending context candidate.
