# Design for review: tiered router + staged pipeline for local coding agents (AgentController)

## Context
AgentController (public repo, Python, uv) runs Claude API agents (agentctl, agentdash dashboard, agentask). We're adding local-model coding agents: aider 0.86.2 driving an LM Studio model over its OpenAI-compatible API. Owner's model: Qwen3.8-27B (4-bit, hybrid attention, thinking model) on a 24 GB RTX 3090 at a 64k context, ~57 tok/s, output capped at 16k per reply.

Evidence so far:
- Exact-code briefs (supervisor writes the snippets): the 27B model applied 2-3 file edits correctly on the first try.
- Idea-level brief (goal, constraints, done-criteria; 4-5 files ~40-51k tokens input): failed twice. Its reasoning was correct, but it spent the whole 16k output on thinking inside aider's single reply and wrote no edits.
- Claude Code's auto mode on the host blocks aider with auto-confirm/auto-test (agent acting without approval), so stronger permissions need a sandbox (Windows Sandbox or WSL2, only the worktree mapped; the model stays in LM Studio on the host).

## Owner requirements
1. The owner gives a high-level idea. The same model that does the work breaks it into stages sized to its own limits. No other model in the chain may be relied on; scope to the lowest-end model in use.
2. A router with tiers: higher tiers get more freedom (bigger stages, more context, longer thinking, more permissions, fewer human gates) and must not be slowed by the machinery (a top tier may take the idea whole); lower tiers get tighter scoping and more human-in-the-loop.
3. Generic enough for the public repo; anyone can wire their own models in. Owner-specific tiers and model quirks live in the owner's private dev-tools share (e.g. `Z:sharedgentcontroller	iers.toml`), which the public tool reads through a configurable path (agentcontroller.toml key or env var), never committed to the repo.
4. Human approvals pluggable: agentdash card by default, optional notifier hook (e.g. the owner's Discord bot).
5. agentdash shows each run live: context gauge per turn, thinking separate from answer, edits, tests; a distinct icon per agent kind.

## Proposed design
- **tiers.toml** (examples in repo; user overrides at `~/.agentcontroller/tiers.toml`). A tier: `context_tokens`, `max_output`, `stage_input_budget` (fraction of context for input), `max_edit_files_per_stage`, `context_mode` (whole files | edit files whole + read files as outlines from the repo map | outlines only), `plan_thinking` (budget or on), `edit_thinking` (off), `edit_format` (diff | whole), `permissions` (locked | edit | shell | full; shell/full only in the sandbox), `gates` (approve_plan, approve_each_stage, approve_commands), `decompose` (always | if_over_budget | never), `max_fix_attempts`.
- **models**: model id -> tier + quirks (sampling params, how to turn thinking off, e.g. a `/no_think` suffix or a reasoning-budget param, stop strings).
- **Router**: (model, idea, repo) -> run plan: decompose or not; for every call, which files go in whole, which as outlines, which thinking setting, which permission profile, which gates.
- **Pipeline (`agentlocal` CLI)**:
  0. *Plan*: model gets idea + repo map + instruction file + its tier limits; returns stages as JSON (goal, files to edit, files to read, acceptance check). Code validates (files exist, estimated tokens per stage vs budget); an over-budget stage goes back to the model to split. Gate: human approves the plan if the tier says so.
  1. *Each stage*: aider architect/editor split (plan pass with thinking budget, edit pass with thinking off) on only that stage's files; then the fixed test command (in the sandbox, or gated); on failure a fix pass with the failing output, up to max_fix_attempts; then a 5-line stage summary written to progress.json. Later stages get prior summaries, not the history.
  2. *Finish*: full tests, report, roadmap note; the supervisor/owner merges.
  - Resumable from progress.json; every event goes to an event log that agentdash tails.
- Token estimate: chars/3.5 plus a fixed overhead for system prompt + edit-format examples (~3k) + repo map.

## Questions for you
1. Is self-decomposition by a ~27B thinking model reliable enough? What structure (prompt/JSON schema/validation) makes it robust? Should the plan step bypass aider and call the model directly?
2. Aider architect mode vs our own two-pass loop (direct API call for the plan, aider only for edits)? What about thinking control for Qwen-family models through LM Studio?
3. Are the tier fields right? What's missing or over-engineered? What's the simplest version worth building first (MVP), and what should wait?
4. Failure modes we haven't covered (plan drift across stages, stale outlines, tests too slow per stage, stage summaries losing details, edits outside the stage's files)?
5. Anything that would make it slower or worse for a top-tier model?
Be critical and concrete. Give a recommended MVP with file layout.
