---
name: api-request
description: Paid Claude API work through AgentController - one-shot questions and design debates with another model (agentask), budget-capped background agents in their own git worktrees (agentctl), the agentdash dashboard, and spend checks. Use ONLY when the user explicitly asks for an API call, a second opinion or debate from another model, or an API agent. Trigger with "/api-request", "ask opus/fable via the API", "debate this with another model", "run an API agent".
argument-hint: [question | "agent <task>" | "spend" | "dash"]
---

Everything here bills the user's **API key**, separately from their Claude plan. Run it only when the
user asked for API work (this skill being invoked counts). Rules that are never relaxed:

- Never export or pass the key as `ANTHROPIC_API_KEY` (Claude Code would then bill the API for the
  whole session). The tools read it from the project's key file themselves.
- Never read, print or copy the key file. Never raise or bypass the budget cap (`[budget]` in the toml
  or the cap file); if a call is refused for budget, tell the user and stop.
- Prefer `/batchy` for non-urgent bulk work (50% off, ~1 h turnaround).

## Pre-flight

```sh
agentask --spend
```

- Prints `estimated spend: $X of $Y budget`: ready.
- `agentask: command not found`: the tool isn't installed. Use `uv run ~/.claude/skills/api-request/ask_api.py`
  instead of `agentask` (it runs AgentController from GitHub through uvx), or tell the user to run
  `uv run install.py` from their AgentController checkout.
- Complains about a missing key / no `agentcontroller.toml`: the project isn't set up. Tell the user to
  run `uv run <AgentController checkout>/install.py --init <project dir>` (copies the example toml, makes
  the gitignored secrets dir, stores the key). Don't create the key file yourself.

The config is found by walking up from the cwd; pass `--project PATH` otherwise.

## One-shot question (`agentask`)

```sh
agentask "question" -f path/to/file.rs -f other.md -m opus -e high -o answer.md
```

- Models: `haiku`, `sonnet`, `opus`, `fable` (most capable and most expensive). Defaults come from
  `[oneshot]` in the toml (system prompt, model, effort).
- The other model has **no** access to the repo or this conversation: attach the files it needs with
  `-f` and state the question, constraints and what you've already ruled out in the prompt.
- Long prompts: write them to a file and pass `-f prompt.md` with a short instruction, or pipe via stdin.
- Use `-o` and then read only the answer file; the call also shows up in `agentdash` with an `ask` badge.

## Design debate

When the user (or the project's CLAUDE.md) asks for a design choice to be debated:

1. Write a short position: the options, your recommendation, the trade-offs, the code it touches.
2. `agentask "Critique this design. Argue for the strongest alternative; be concrete." -f position.md -f <key files> -m opus -e high -o round1.md`
3. Reply to its strongest points in one more round if they change the decision (send `round1.md` back with your answer).
4. Record options, decision and reasoning where the project keeps decisions (e.g. `docs/DECISIONS.md`)
   and save the transcript if the project has a place for it (e.g. `docs/reviews/YYYY-MM-DD-topic.md`).
   Tell the user what was decided and what it cost (`agentask --spend` before and after).

## Background agent (`agentctl`)

For multi-file implementation work that would bloat this session's context:

1. Write the brief to `briefs/<name>.md`: goal, files to read, constraints, how to verify, what to
   report. The agent knows only the brief, the repo and its `CLAUDE.md`.
2. Launch it in the background (Bash `run_in_background`), one run per task:
   ```sh
   agentctl --brief briefs/<name>.md --name <name> --model sonnet --budget 3
   ```
   Each agent works in `<worktrees>/<name>` on branch `<name>` (from `--base`, default in the toml)
   and commits there; it never pushes. `sonnet` is the default; use `opus` only for hard work.
3. Don't poll. When the background task finishes, read only the end of its output: the final report
   and the `[agent <name>] success turns=N $X` line. `agentwatch <name>` prints the event log if needed;
   never dump whole transcripts into context.
4. Verify with a few commands in the worktree, then merge the branch if the user wants it.

Follow-up on the same agent: `agentctl --brief followup.md --name <name> --resume <session id>`.

## Dashboard, spend, autopilot

- `agentdash` serves http://127.0.0.1:8765/: live runs, costs, steering messages, a New agent form, and
  the user's Claude Code sessions. Start it in the background if the user wants to watch.
- `agentask --spend`: logged estimate against the cap. `agentusage [--days N] [--by-key]`: actual spend
  from the Admin API (needs the admin key; hours of lag).
- `agentpilot --sessions N --hours H`: back-to-back plan-billed `claude -p` sessions driven by
  `[pilot]` in the toml. Only start it when the user asks for autopilot.
