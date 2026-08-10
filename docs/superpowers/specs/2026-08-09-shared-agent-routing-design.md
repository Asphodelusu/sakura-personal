# Shared Agent Routing Design

## Goal

Reduce idle time and planning latency while preserving low-overlap shared-workspace safety.

## Default routing

- Claude Code owns the task: inspect the repository, write a lightweight plan, implement the main change, run tests, and report results.
- Cursor receives bounded UI, interaction, exploratory integration, or local implementation details with exclusive file ownership.
- Codex reviews the final diff, reproduces important tests, resolves integration risks, consolidates commits when useful, and performs the single push.

## Architecture

Keep the existing repository-backed handoff architecture unchanged:

- continue using `docs/agent-handoffs/CONTEXT.md` and per-batch contracts;
- continue assigning exact, non-overlapping files;
- allow local commits only when the contract permits them;
- forbid worker pushes;
- preserve unrelated dirty files;
- retain one final integration and push.

For routine single-agent tasks, skip formal handoff files and let Claude Code or Cursor complete the task directly.

## Codex escalation gate

Request a short Codex decision review before implementation only when work changes concurrency or lifecycle behavior, persistent data formats, public protocols, model/tool routing, privacy or security boundaries, or large-scale architecture. Codex reviews the decision summary instead of repeating a full repository plan.

## Skill changes

- Rewrite the default worker table around Claude ownership, Cursor detail work, and Codex review.
- Add fast-path guidance for routine tasks.
- Add the high-risk escalation gate.
- Extend the templates with a reusable Claude task-owner launch prompt and Codex final-review prompt.
- Preserve all existing handoff paths, file-ownership rules, privacy rules, and integration checks.

## Validation

- Run the Skill format validator in UTF-8 mode.
- Confirm project and global Skill copies have matching hashes.
- Inspect the diff to ensure no unrelated project files changed.
