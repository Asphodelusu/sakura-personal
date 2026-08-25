# Cursor Task: Sakura Relationship Initiative

## Objective

Implement the approved Sakura relationship-initiative design with independent A/B switches and default `natural` expression bias.

Authoritative spec:

- `docs/superpowers/specs/2026-08-24-sakura-relationship-initiative-design.md`

## Phase 1 — Plan only

Read the spec and inspect the direct implementation dependencies. Create:

- `docs/superpowers/plans/2026-08-24-sakura-relationship-initiative.md`

The plan must follow the repository's `writing-plans` conventions:

- exact files and interfaces;
- TDD RED/GREEN commands using `.\.venv\Scripts\python.exe`;
- small independently reviewable commits;
- no placeholders;
- explicit coverage of character resource loading/archive, A prompt injection, B no-VLM scheduling, cancellation, config, logs, and tests;
- preserve the design rule that initiative has no hard intimacy ceiling and never auto-enables `贴紧`.

Perform plan self-review against the spec. In this phase do not modify production code or tests, do not commit, and do not push.

## Shared-workspace constraints

- Current branch: `dev5`.
- Preserve the pre-existing `.gitignore` working-tree state.
- `characters/` and `data/intimacy_guide.txt` are ignored/private. Do not force-add ignored files.
- Do not modify runtime chat history, memories, mood, logs, API keys, or user config.
- You are already the worker: do not dispatch subagents or load unrelated repository memory.

## Handback

Return:

- plan path;
- proposed task/commit boundaries;
- highest-risk interfaces;
- any spec ambiguity requiring a decision;
- files inspected;
- elapsed time and model used.

## Phase 2 — TDD implementation (authorized)

Implement the reviewed plan at:

- `docs/superpowers/plans/2026-08-24-sakura-relationship-initiative.md`

Follow its tasks in order using RED/GREEN with `\.\.venv\Scripts\python.exe` corrected to the repository command `\.\.venv` only if the plan text is wrong; the actual required interpreter is `.\.venv\Scripts\python.exe`.

Coordinator review has locked these boundaries:

- B remains a sibling no-screenshot / no-UIA / no-VLM path.
- A remains active during L2; B may be busy-gated only to avoid collision with an active reply or intimacy continuation.
- Do not introduce an affection score, relationship-stage state machine, intimacy allow-list, hard ceiling, or automatic `贴紧` entry.
- Preserve independent screen/B cooldowns and generation-based cancellation of unshown B replies.
- Preserve the pre-existing `.gitignore` change and all unrelated dirty/untracked files.
- Use `cursor-grok-4.6-high-fast`; do not dispatch subagents.

Implement the production code and tests described by the plan. You may make small local thematic commits, but do not push. Do not force-add ignored character/config files. Update only the Cursor section of `integration-notes.md` with:

- commits and exact changed files;
- RED evidence and GREEN commands/results;
- full regression result if run;
- ignored/private files written but not staged;
- remaining risks, denied commands, and elapsed time.
