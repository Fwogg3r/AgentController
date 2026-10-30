# AgentController

Launch, watch and steer budget-capped [Claude Agent SDK](https://docs.claude.com) agents from a small localhost dashboard.
Each agent works in its own git worktree on its own branch, billed to an API key with a hard USD cap per run
and a cap across all runs. One Python package, standard library plus the SDK, no Node and no build step.

## Why the Agent SDK and not `claude -p`

Promotional and API credits are billed against **API / Agent SDK usage**. Claude Code (including `claude -p`) draws on
the plan instead. Running agents through the SDK with an API key bills exactly the budget you capped. The key is passed
through `apiKeyHelper` to the agent process only; it is never put in a global `ANTHROPIC_API_KEY`.

## Quick start (new project)

```sh
# in your project repo
cp /path/to/agentcontroller/examples/agentcontroller.toml .
mkdir .agentcontroller && echo ".agentcontroller/" >> .gitignore
echo "sk-ant-..." > .agentcontroller/api.key        # your API key, never committed

uv tool install /path/to/agentcontroller             # commands: agentctl agentdash agentpilot agentwatch agentwait agentusage agentask
agentdash                                            # http://127.0.0.1:8765/  -> "+ New agent"
```

Without installing: `uv run --project /path/to/agentcontroller agentdash --project .`.
Or from the CLI: `agentctl --brief briefs/task.md --name my-task --base main --model sonnet --budget 3`.
`agentctl --check-policy` self-tests the shell deny list.

The config file `agentcontroller.toml` is found by walking up from the current directory, or pass `--project PATH`.
See [examples/agentcontroller.toml](examples/agentcontroller.toml) for every key: secrets dir, key file, budget cap,
worktrees dir, runs dir, extra deny patterns, extra `PATH` entries, the rules appended to the agents' system prompt,
default model/base/turns and port. Project-specific instructions (build commands, "verify headless only") belong in
those rules and in the project's own `CLAUDE.md`; nothing project-specific is in the package.

## Dashboard

A page in Claude Code's terminal look (dark, monospace, terracotta accents) that works in a narrow split-screen window.
"Clawd", the orange pixel-art critter, sits in the header and beside every run: bobbing while the agent works, dozing
while it waits, asleep ("zz") when finished, with a "!" when it crashed.

- **Run states** (`state` in `/api/runs`, derived from the events log and the PID):
  `working` (thinking / writing / running a tool, with the tool and its elapsed time), `waiting` (with the reason:
  background task, its notification turn, a short idle before ending, API retry or rate limit), `finished` (with the
  outcome: `success`, `max_turns`, `budget`, `error`, `stopped`), `crashed` (process gone without an end record; shows the
  last event and log line) and `ended` (old runs with neither an events log nor a result file). `status`
  (`running`/`finished`/`dead`/`ended`) is kept for older scripts.
- **Runs list** with filters (name, finished / crashed / ended, last 24 h only, last N; saved in the browser), model, cost,
  turns, a live activity line, branch, last 3 commits, uncommitted count. **Live transcript** of the selected run: `●` text and
  tool lines with `⎿` results, thinking blocks dimmed and collapsible, your interjections highlighted, and a Claude Code style
  spinner line (`✻ Pondering… (12s · ↓ 1.4k tokens)`, `Running Bash: cargo test… (40s)`, `Waiting for background task: …`).
- **Progress events:** the runner turns on the SDK's partial messages and writes a throttled `progress` event (at most one
  every 2 s: phase, output tokens so far, tail of the thinking text). Newer models return thinking as a signature only;
  set `thinking_display = "summarized"` under `[agent]` (or `agentctl --thinking-display summarized`) to get the text.
  That only changes what is returned, not whether or how much the model thinks.
- **Spend bar:** total logged spend vs the cap, session spend, and an on-demand "actual billed" button (Admin API, needs an admin key; lags by hours).
- **New agent:** name (becomes the branch and worktree), base branch, model (with a price note), budget, max turns, brief text
  or an existing `briefs/` file, optional `--resume` session. It shows the remaining headroom (cap - logged - budgets of active runs)
  and refuses a launch that doesn't fit. The agent is spawned detached, so it survives closing the dashboard; its PID is in `<runs>/<name>.pid`
  and its output in `<runs>/<name>.log`.
- **Steering:** *Send to agent* (confirm step; delivered at the agent's next turn), *Stop after this turn*, and *Forward to primary*
  (writes `primary_inbox.jsonl` for a coordinating session; it runs `agentwait` in the background to be woken).
- **Removing:** per run and each behind a confirmation: stop gracefully, kill the process tree (spend so far is logged),
  remove from list (logs archived to `<runs>/archive/`), delete the worktree and branch. Deleting is offered only when the branch
  is merged into its base and clean; otherwise it shows the unmerged commits and the uncommitted count and needs an explicit
  "discard unmerged work". Protected branches (`main`, `master`, ...) and the primary working tree are never touched.

### Claude Code sessions (plan-billed)

The dashboard also lists the interactive Claude Code sessions of the project and the subagents they spawn through the
`Agent` tool, so plan-billed work shows up beside the `agentctl` runs. They are read from the transcripts Claude Code writes
(`~/.claude/projects/<slug>/<session>.jsonl` and `<session>/subagents/agent-<id>.jsonl` + `.meta.json`); no hooks, nothing is
written. The slug is the project root with every non-alphanumeric turned into `-` (matched case-insensitively); override
the transcript directory with `[claude] projects_dir` in `agentcontroller.toml`.

- `/api/runs` gets entries with `plan: true`, `kind: "plan"` (session, name `plan.<id8>`) or `"plan-agent"` (name
  `plan.<id8>.<agent8>`, `parent` = the session), `title` (first prompt line / subagent description), model, token totals
  (`tokens_in/out`, `cache_read/write`), `tool_calls`, and `cost` = an **API-equivalent estimate** at the price table
  (`cost_estimate: true`). `state` is `working` when the file was written in the last 2 minutes or ends in an unfinished tool
  call (up to 30 min, e.g. waiting for a subagent), else `finished`.
- They are **not** part of spend totals, the budget or headroom (those only look at `agentctl` runs). Query parameters:
  `?plan=0` leaves them out, `?plan_age=SECONDS` sets the age window (default 7 days, at most 40 sessions).
- Files are parsed incrementally: per file the offset and a running summary are cached, and only appended complete lines are
  read on each refresh. Unknown or malformed lines are skipped.
- UI: a `plan` / `plan agent` badge, subagents indented under their session, sidebar checkbox "hide plan sessions" (saved
  with the other filters), the transcript rendered like a run (prompts, text, tool calls, truncated results, collapsed
  thinking). No send/kill/archive/worktree actions for these.
- Limits: transcripts of git worktree checkouts live in their own `~/.claude/projects` directories and are not included; the
  cost ignores cache-tier price differences beyond the shared price table; a session idle between turns reads `finished`.
  Tests: `uv run python -m unittest discover tests`.

### How steering works

The runner appends structured events to `<runs>/<name>.events.jsonl` and polls `<runs>/<name>.inbox.jsonl` about once a second.
`{"text": ...}` lines become user messages; `{"control":"stop"}` ends the run after the current turn. The prompt is a
**streaming** generator: this is also what keeps the SDK's permission callback (`can_use_tool`) alive. A run ends by itself
2 s after a result when no message is pending. Background tasks (`run_in_background`) hold the run open until they end
and the agent has answered their notification; closing the stream earlier breaks every later tool approval ("Stream closed").

## One-shot calls (`agentask`)

```sh
agentask "prompt" -f file.rs -m opus -e high -o answer.md   # also: --spend, --backfill
```

A single Messages API call (key from the configured key file, budget refusal from the cap, `[oneshot]` in
the toml for the system prompt and default model/effort). Each call is recorded as a run (`kind: oneshot` in
its `start` event: prompt, attached file names, streamed response, cost, tokens) and in the usage log, so
`agentdash` lists it with an `ask` badge (sidebar filter "hide non-agentic", off by default) and spend
totals include it. `--backfill` imports older usage-log lines (idempotent; runs automatically before each call).

## Autopilot (`agentpilot`)

Runs coordinator sessions back to back, each a fresh `claude -p` (like `/clear`), **billed to the Claude plan**
(`ANTHROPIC_API_KEY` is removed from its environment), in the project root:

```
agentpilot --sessions 5 --hours 8 [--model opus] [--name pilot-x]
agentpilot --stop pilot-x          # stop after the current session
```

- **What each session does:** it gets the project's prompt (`[pilot] prompt_file`, else a default that reads CLAUDE.md and the handoff file), does the next item, commits, pushes, updates the handoff, and ends its final message with `AUTOPILOT: CONTINUE` or `AUTOPILOT: STOP <reason>`.
- **When it ends:** on a STOP line, on a session that exits non-zero (a usage limit) or lacks the line, at the session or hour limit, or on a stop request.
- **Background tasks:** `claude -p` waits for them before it exits.
- **In the dashboard:** the pilot shows up like a run.
  - **Stop:** ends it after the current session.
  - **Kill:** ends it now.
  - **A message:** goes into the next session's prompt as an owner note.
  - **Cost:** shown as `plan_usd` (the API-equivalent estimate) and never logged as API spend.
- **Files:** a session's stderr goes to `<runs>/<name>.stderr.txt`.
- **The prompt goes in on stdin:** on Windows `claude` is a `.cmd` shim, and cmd.exe cuts arguments at newlines.

```toml
[pilot]
prompt_file = "briefs/autopilot.md"   # optional; {handoff} is replaced
handoff = "docs/HANDOFF.md"
model = "opus"
permission_mode = "auto"
```

### Watching from other devices

`agentdash --remote` accepts requests from any host that carries a token. The token is kept in `<secrets>/dash_token.txt`, created on first use. Open `http://<host>:8765/?token=<token>` once per device; after that a cookie (HttpOnly, SameSite=Strict, 30 days) carries it. POSTs must also be same-origin.
- **LAN:** `agentdash --remote --host 0.0.0.0`. This is plain HTTP, so anyone on the network who can sniff traffic can read the token.
- **Anywhere:** keep the default host and tunnel it, for example `cloudflared tunnel --url http://127.0.0.1:8765` (HTTPS). Put Cloudflare Access in front if the URL must stay private beyond the token.

## Security model

- Binds to `127.0.0.1` by default; requests need a matching `Host`, and POSTs a matching `Origin` and a JSON content type (blocks DNS rebinding and cross-site forms).
  With `--remote`, every request needs the token (query or cookie) instead, and POSTs must be same-origin. `--host` other than localhost requires `--remote`.
- Key files are never read or served by the dashboard. The agent's shell can't mention the secrets dir or key file name,
  `ANTHROPIC_API_KEY`, `curl`/`wget`/`ssh`, `git push/reset --hard/checkout/switch/worktree/clean/rebase/stash`, destructive `rm -r`, `npm install`, ...; add more via `deny`.
- File writes (`Edit`, `Write`, ...) are confined to the agent's own worktree; `WebFetch`/`WebSearch` are denied.
- Names are validated as slugs; brief files are taken by base name; session ids are validated.
- The deny list is a guard rail against mistakes, not a sandbox: a determined agent can route around regex rules. Don't give agents
  credentials you can't afford to lose.

## Cost notes

Every run has `max_budget_usd`; the runner also refuses to start if `logged spend + run budget > cap`. Cost per turn is shown live
(cumulative); the total is appended once to `api_usage.jsonl` when the run ends (or when you kill it). The dashboard's headroom counts
the full budget of active runs, so parallel launches can't oversubscribe the cap. Tool results with screenshots can be large; keep
briefs specific and ask agents to read files in ranges.

## Development

`python -m agentcontroller.selftest` launches the dashboard against a temp git project with a stub runner and checks every endpoint,
launch, graceful stop, kill, archive and worktree deletion without spending anything.

MIT licensed.
