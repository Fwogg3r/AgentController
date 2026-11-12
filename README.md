# AgentController

Launch, watch and steer budget-capped [Claude Agent SDK](https://docs.claude.com) agents, and run local-model coding
agents (LM Studio + aider), from one always-on localhost dashboard that shows every agent in every project.
Each API agent works in its own git worktree on its own branch, billed to an API key with a hard USD cap per run
and a cap across all runs. One Python package, standard library plus the SDK, no Node and no build step.

What's in it:

- **`agentctl`**: budget-capped Claude API agents in worktrees. **`agentask`**: one-shot API calls and design debates, spend and balance checks. **`agentpilot`**: back-to-back plan-billed `claude -p` sessions.
- **`agentlocal`**: a staged pipeline for local models (idea -> plan -> stages -> edit -> test), with tiers that set how much freedom a model gets. See [Local agents](#local-agents-agentlocal).
- **`agentdash`**: the unified dashboard ([docs/unified-dash.md](docs/unified-dash.md), decision in [docs/DECISIONS.md](docs/DECISIONS.md)). See [Dashboard](#dashboard).
- Skills for Claude Code: `/api-request` and `/batchy`.

## Why the Agent SDK and not `claude -p`

Promotional and API credits are billed against **API / Agent SDK usage**. Claude Code (including `claude -p`) draws on
the plan instead. Running agents through the SDK with an API key bills exactly the budget you capped. The key is passed
through `apiKeyHelper` to the agent process only; it is never put in a global `ANTHROPIC_API_KEY`.

## Quick start

Follow this top to bottom on a fresh machine (Windows, macOS or Linux).

**1. Prerequisites.** Install these yourself; the installer checks for them but never installs system software.

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (runs everything; no pip or manual venvs)
- git (agents work in git worktrees)
- [Claude Code](https://docs.claude.com/en/docs/claude-code/setup), for the `/api-request` and `/batchy` skills and `agentpilot`
  (`agentctl` and `agentask` don't need it: the Agent SDK bundles its own copy)
- an Anthropic API key (`sk-ant-api...`, from console.anthropic.com) with some credit, for API agents and one-shot calls

**2. Install.** Add `--local` if you also want local-model agents (installs aider; see [Local agents](#local-agents-agentlocal)).

```sh
git clone https://github.com/Fwogg3r/AgentController
cd AgentController
uv run install.py --init /path/to/your/project      # asks for your Anthropic API key once (hidden input)
```

`uv run install.py --dry-run -y` first shows the plan and changes nothing. The installer asks before installing the `roadmap`
CLI (and aider with `--local`), then runs `agentdoctor` at the end.

**3. Check it.** `agentdoctor` prints a table of every piece: OK, missing (required / recommended / optional), with what to do
about each. It exits 0 only when everything required is OK, so you can rerun it any time (`--json` for scripts).
It also looks for LM Studio and pmjc on this machine and offers to write what it finds into `~/.agentcontroller/config.toml`
(comments and other keys are kept). `agentdoctor --scan` additionally probes the local /24 subnet for them, after asking.

**4. Open the dashboard.** Restart Claude Code, then open http://127.0.0.1:8765/ (on Windows the installer's logon task keeps it
running; elsewhere any `agentctl`/`agentask` call, or `agentdash`, starts it). The project you gave `--init` is listed right away; any other
shows up once an agent has run in it, or add its parent folder to `scan` in `~/.agentcontroller/config.toml`.

**5. Run your first agent.** From the project, either press "+ New agent" in the dashboard, or:

```sh
agentask "what does this repo do?" -m sonnet         # one API call, recorded in the dashboard
agentctl --brief briefs/task.md --name first --model sonnet --budget 2     # an agent in its own worktree
```

or in Claude Code: `/api-request agent: implement the brief in briefs/task.md`.

### What it depends on

| Tool or service | Needed for | Required? | How it gets there |
|---|---|---|---|
| [uv](https://docs.astral.sh/uv/) | installing and running everything | **required** | you install it; `install.py` stops with the link if missing |
| git | worktrees, branches, the dashboard's git info | **required** | you install it |
| This package (`agentctl agentdash agentask agentwatch agentwait agentusage agentpilot agentlocal agentdoctor`) | everything | **required** | `install.py` (`uv tool install --editable`) |
| `claude-agent-sdk`, `anthropic` | API agents, one-shot calls | **required** | dependencies of the package; the SDK bundles the Claude Code binary it drives |
| [`roadmap` CLI](https://github.com/Fwogg3r/WebRoadmap) (WebRoadmap) | the dashboard's Roadmap tab, project roadmaps | **required** for those | `install.py` asks, then installs WebRoadmap's latest main; agentdash and `install.py` keep it there (`--no-roadmap` to skip) |
| Anthropic API key | `agentctl`, `agentask`, `/batchy` | recommended | `install.py --init PROJECT` (stored in the project's gitignored `.agentcontroller/api.key`, never in a global `ANTHROPIC_API_KEY`) |
| Claude Code CLI | `/api-request`, `/batchy`, `agentpilot`, plan-session view | recommended | you install it; `install.py` prints how if it is missing |
| claude-batch-toolkit | `/batchy` (Batch API, 50% off) | optional | `install.py` fetches it at a pinned commit (`--no-batchy` to skip) |
| [aider](https://github.com/Aider-AI/aider) | `agentlocal` (local-model agents) | optional | `install.py --local`, pinned commit, Python 3.12 |
| [LM Studio](https://lmstudio.ai) | models for `agentlocal`, the Models tab | optional | you install it and start its server (port 1234); `agentdoctor` finds it |
| [pmjc](https://github.com/Fwogg3r/PersonalMCPJobController) | the Jobs tab, approvals, notifications | optional | you install it (its README); `agentdoctor` finds it and tells you how to make a `jobs`-scope token; put it in `<secrets_dir>/pmjc/agentdash.token` or point `[pmjc] token_file` at it |
| `cloudflared` + Cloudflare Access | reaching the dashboard from the internet | optional | you set it up, see [Internet access](#internet-access-cloudflare-access) |
| Windows Task Scheduler | the always-on dashboard (logon task) | optional (Windows) | `install.py` creates it (`--no-dash-task` to skip) |

Not needed: Node, npm and Firebase (nothing in AgentController uses them; Node is only one way to install Claude Code).

## What the installer does

Restart Claude Code afterwards so it picks up the skills and the MCP server. The installer:

| What | Where |
|---|---|
| The commands `agentctl agentdash agentask agentwatch agentwait agentusage agentpilot agentlocal agentdoctor` | `uv tool install --editable` of this checkout, so `git pull` upgrades them |
| The `roadmap` CLI and its Claude skill (asks first); with `--local` also aider | `uv tool install` from GitHub: WebRoadmap at its latest main (upgraded on every run), aider at a pinned commit; skill in `~/.claude/skills/roadmap/` |
| **`/api-request`** skill: one-shot asks and design debates with another model, background API agents, dashboard, spend | `~/.claude/skills/api-request/` (`SKILL.md` + `ask_api.py`) |
| **`/batchy`** skill + `claude-batch` MCP server: non-urgent work through the Message Batches API at 50% off | Fetched from [s2-streamstore/claude-batch-toolkit](https://github.com/s2-streamstore/claude-batch-toolkit) at a pinned commit into `~/.claude/skills/batchy/` and `~/.claude/mcp/`, registered in `~/.claude.json` |
| Windows: the hidden Task Scheduler logon task **"AgentController dashboard"** (`agentdash --all`, so the dashboard is always up at http://127.0.0.1:8765/) and `~/.agentcontroller/config.toml` with commented defaults | Task Scheduler, `~/.agentcontroller/` |
| With `--init PATH`: `agentcontroller.toml`, a gitignored `.agentcontroller/`, your key in `.agentcontroller/api.key` | the project |

The API key never goes into a global `ANTHROPIC_API_KEY`: if it did, Claude Code itself would bill the API instead of
your plan. The batch MCP server gets it in its own `env` entry, and the agents get it through `apiKeyHelper`.
Every file the installer overwrites under `~/.claude` is backed up to `<file>.bak` first.

The key it asks for is a normal API key (`sk-ant-api…`); admin keys can't make Messages calls. The dashboard's costs
and the budget cap come from the runs' own estimates, so no admin key is needed. Optional: put an admin key in
`.agentcontroller/admin.key` (`admin_key_file` in the toml) for `agentusage`, which reports actual billed spend from
the Admin API (organisation-wide, daily, hours of lag; `--by-key` splits tokens per key). It doesn't feed the dashboard.

Re-run `uv run install.py` to refresh. On Windows close a running `agentdash` first (it locks the tool directory), or pass `--no-tool`.
The CLIs also start the dashboard if it is down (`schtasks /Run` first, then a hidden spawn).

Options: `--key-file F` (read the key from a file), `--dry-run`, `-y` (no prompts), `--no-batchy`, `--local` (also install aider for agentlocal), `--no-roadmap`, `--no-doctor`, `--no-tool`, `--no-dash-task` (skip the logon task),
`--statusline` (the toolkit's batch status line; this also writes the key to `~/.claude/env`, which only that
script sources), `--uninstall` (removes the skills, the MCP entry and the tool; keeps `~/.claude/batches`).

In Claude Code:

- `/api-request should we use a BVH or a uniform grid here? ask opus` - one-shot call, recorded in the dashboard.
- `/api-request agent: implement the brief in briefs/x.md` - a budget-capped agent in its own worktree.
- `/batchy review src/ for security issues` - submitted to the Batch API; `/batchy check` later.
- Open http://127.0.0.1:8765/ (or run `agentdash`) to see runs, costs, steering, Claude Code sessions, local agents, the roadmap and pmjc jobs for every project.

The skill is called `api-request`, not `claude-api`, because Claude Code ships a built-in `claude-api` skill
(the SDK reference). `ask_api.py` works without the tool installed (it falls back to `uvx` from GitHub):
`uv run ~/.claude/skills/api-request/ask_api.py "prompt" -f file -m opus`.

## Manual setup (new project)

```sh
# in your project repo
cp /path/to/agentcontroller/examples/agentcontroller.toml .
mkdir .agentcontroller && echo ".agentcontroller/" >> .gitignore
echo "sk-ant-..." > .agentcontroller/api.key        # your API key, never committed

uv tool install /path/to/agentcontroller             # commands: agentctl agentdash agentpilot agentwatch agentwait agentusage agentask agentlocal
agentdash                                            # http://127.0.0.1:8765/  -> "+ New agent"
```

Without installing: `uv run --project /path/to/agentcontroller agentdash --project .` (`--project X` is `--all` pinned to X).
Or from the CLI: `agentctl --brief briefs/task.md --name my-task --base main --model sonnet --budget 3`.
`agentctl --check-policy` self-tests the shell deny list.

The config file `agentcontroller.toml` is found by walking up from the current directory, or pass `--project PATH`.
See [examples/agentcontroller.toml](examples/agentcontroller.toml) for every key: secrets dir, key file, budget cap,
worktrees dir, runs dir, extra deny patterns, extra `PATH` entries, the rules appended to the agents' system prompt,
default model/base/turns and port. Project-specific instructions (build commands, "verify headless only") belong in
those rules and in the project's own `CLAUDE.md`; nothing project-specific is in the package.

## Dashboard

`agentdash --all` (the default) is one dashboard for every project, in Claude Code's terminal look (dark, monospace,
terracotta accents; works in a narrow split-screen window). Spec: [docs/unified-dash.md](docs/unified-dash.md).

- **Projects:** a list with live counts (working / waiting / crashed); discovered from the registry (`~/.agentcontroller`, filled by the CLIs), Claude Code transcript directories and `[dash] scan`. "All projects" merges them. Model checkboxes filter every list.
- **Icons per agent kind** (`AGENT_PROFILES` in `index.html`): "Clawd", the orange pixel-art critter, for Claude (bobbing while it works, dozing while it waits, asleep when finished, "!" when crashed); an original Qwen critter for local Qwen; a chip for any other local model.
- **Trees:** runs with a `parent` (`--parent`, `AGENTCONTROLLER_PARENT`, Claude plan subagents) nest under it; a parent collapses to "(n agents)".
- **Tabs:** *Agents* (transcripts, steering) | *Roadmap* (WebRoadmap's own interactive page for the project's `docs/roadmap`, editable with operate access; a button switches to a read-only summary with agents linked by `--roadmap ID`; agentdash upgrades the `roadmap` CLI to WebRoadmap's latest main at startup and when this page is loaded) | *Jobs* (pmjc jobs, watches, approvals) | *Models* (LM Studio per host).
- **Local runs:** a context gauge (tokens of the last call vs the window, per-call history), a thinking pane apart from answer/edits/tests, stage progress, and approval buttons.
- **Phone layout:** at <= 640px the page switches to a list view and a detail view (back button returns to the list), a bottom tab bar for Roadmap / Jobs / Models, a menu for spend and "+ New agent", and Filters in a bottom sheet. Desktop is unchanged.
- **Attention queue:** the header badge lists what needs you: local approvals, pmjc approvals, crashes in the last 24 h, waiting Claude sessions, questions from API agents.
- **Global config** `~/.agentcontroller/config.toml` (all keys optional):

```toml
include = "Z:/shared/agentcontroller/dash.toml"   # merged first; this file's keys win
[dash]
port = 8765
# host = "0.0.0.0"                # LAN viewing; needs remote = true
# remote = true                   # token auth, see "Watching from other devices"
scan = ["~/Documents/GitHub"]     # also list repos here that have an agentcontroller.toml
hide = ["C:/some/repo"]
[pmjc]
url = "http://127.0.0.1:8770"
# token_file = "..."              # default <secrets_dir>/pmjc/agentdash.token: a pmjc API token with the `jobs` scope (`admin` only for approvals)
[lmstudio.hosts]
local = "http://127.0.0.1:1234"
```

Writes (POSTs) also need the header `X-AC: 1`. The pmjc token never leaves the server. Planned work: [docs/roadmap](docs/roadmap) phase 2.

The per-project features below apply as before.

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
- **Live reload:** installed from a git checkout (the default `install.py` editable install), agentdash restarts itself
  when its Python source changes and the new code imports cleanly (a broken edit keeps the old version running; see
  `~/.agentcontroller/dash.log`). Open pages reload themselves when the build changes. `[dash] auto_reload = false` turns it off.
- **Markdown:** agent text in every transcript renders as markdown (headings, lists, tables, code, quotes, links), like Claude Code shows it.
- **Claude sessions:** a session whose turn ended while a background command or background agent still runs shows
  *waiting* (not *finished*) until its task notification arrives; an unanswered question shows *waiting · question*.
  Tool calls still running pulse.
- **Attachments:** *Attach* (or paste / drop) adds up to 10 files (20 MB) to a message. They are saved to
  `<runs>/attachments/<run>/`; images and PDFs reach the agent as content blocks, text files up to 200 KB are inlined,
  anything else is referenced by path.
- **Questions:** when an API agent calls `AskUserQuestion`, the run shows *waiting · question*, the question pops up once as a
  dialog (options, multi-select, free-text *Other*), sits in the attention queue and appears as a card in the transcript.
  The answer goes back to the tool; *Stop* or 4 h without an answer denies it and the agent uses its own judgment.
  Claude Code (plan) sessions are read-only: they can't be messaged or answered from the dashboard.
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
`{"text": ..., "files": [...]}` lines become user messages; `{"answer": <question id>, "answers": {...}}` answers a pending
`AskUserQuestion` (written to the events as `question` / `answer`); `{"control":"stop"}` ends the run after the current turn. The prompt is a
**streaming** generator: this is also what keeps the SDK's permission callback (`can_use_tool`) alive. A run ends by itself
2 s after a result when no message is pending. Background tasks (`run_in_background`) hold the run open until they end
and the agent has answered their notification; closing the stream earlier breaks every later tool approval ("Stream closed").

## Local agents (`agentlocal`)

Coding agents on your own models (LM Studio, with aider doing the edits). The owner's models and tiers go in a tiers file:
copy [examples/tiers.example.toml](examples/tiers.example.toml), point `AGENTCONTROLLER_TIERS` (or `[local].tiers_path` in
`agentcontroller.toml`) at it. Run inside a git repo, ideally a worktree on its own branch (it refuses `main` and a dirty tree):

```sh
agentlocal probe --model qwen-27b                          # measure context, chars/token, no-think handling
agentlocal run "add a --json flag to the CLI" --model qwen-27b          # staged pipeline: plan, stages, edit, test, fix
agentlocal run @brief.md --model qwen-27b --tier aider-edit --files a.py b.py   # one aider call with a brief
agentlocal plan "idea" --model qwen-27b           # only the stage plan, to approve first
agentlocal status | resume <run>
```

Tiers set the stage size, files per stage, thinking budget, autonomy and which steps wait for you (`gates`: plan, stage,
tests, protected files). Approve in the console or in the dashboard. The model never picks shell commands: agentlocal runs
the configured test command, and files changed outside a stage's list are reverted. Launch it from a terminal, not from
an auto-mode Claude session. Runs register themselves, so they appear in the dashboard with the gauge and thinking pane.

## One-shot calls (`agentask`)

```sh
agentask "prompt" -f file.rs -m opus -e high -o answer.md   # also: --spend, --balance, --backfill
```

**Low-balance guard.** The Anthropic API exposes no account balance, so "remaining" is `[budget] cap` minus the
larger of the logged spend and the **actual** org-wide spend from the Admin API (`agentusage`'s cost report, since
`[budget] since = "YYYY-MM-DD"`, default the first usage-log entry; cached 15 min; skipped without an admin key).
The max never under-counts: the log is real-time but misses other callers, the Admin figure sees everything but lags. `agentask --balance` prints it (exit 4 when it is below `[budget] min_remaining`, default $5).
`agentask` and `agentctl` refuse to start below that floor and tell the caller to use the plan (a Claude Code
subagent) instead; `agentask --force` (or `AGENTCONTROLLER_FORCE=1` for `agentctl`) overrides it.

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
- **LAN:** `agentdash --remote --host 0.0.0.0`, or set `host = "0.0.0.0"` and `remote = true` under `[dash]` in `~/.agentcontroller/config.toml` (flags win over config). The config route is what the always-on logon task uses: the task runs plain `agentdash --all`, so reinstalling it with `install.py` keeps LAN viewing. This is plain HTTP, so anyone on the network who can sniff traffic can read the token; on Windows also allow the port in the firewall for the private profile and local subnet only.
- **Anywhere:** keep the default host and tunnel it, for example `cloudflared tunnel --url http://127.0.0.1:8765` (HTTPS). Put Cloudflare Access in front if the URL must stay private beyond the token.

## Internet access (Cloudflare Access)

agentdash can be reached from the internet through a Cloudflare tunnel with Cloudflare Access sign-in and per-user permission levels.
Enable a second listener in `~/.agentcontroller/config.toml` (all keys in `agentcontroller/hub.py`):

```toml
[dash.access]
enabled = true
listen_port = 8766                  # 127.0.0.1 only: point the tunnel's origin here
team_domain = "myteam.cloudflareaccess.com"
aud = ["<Access application AUD tag>"]
super_admin = "you@example.com"     # always admin
grants_file = "~/.agentcontroller/grants.json"
public_origin = "https://agentdash.example.com"
audit_log = "~/.agentcontroller/access-audit.jsonl"
```

- Every request on that listener (pages, API, SSE) must carry a valid `Cf-Access-Jwt-Assertion` (RS256, checked against the team's JWKS, stdlib only), whatever the `Host`. The LAN token of `--remote` is ignored there; the normal port 8765 behaves as before.
- Run it next to the tunnel: `cloudflared` publishes a hostname whose origin is `http://127.0.0.1:<listen_port>`, and an Access application on that hostname does the sign-in. The `aud` is that application's AUD tag.
- Levels: `view` (read everything), `operate` (send messages, approve, launch, kill), `admin` (archive, delete worktrees, forget projects, anything unlisted). `grants_file` is normally written by an external sync that mirrors your own user/permission store into Access policies and this file; agentdash only reads it. Format: `{"version": 1, "services": {"agentdash": {"someone@example.com": "view"}}}`, re-read when it changes; if it is missing or invalid only `super_admin` gets in. The page shows who you are and hides what you may not do (`/api/me`); the server enforces it.
- POSTs also need `Content-Type: application/json`, `X-AC: 1` and an `Origin` equal to `public_origin`. Every POST and every refused request is appended to the audit log.
- `operate` can launch agents that spend API money; per-user budget caps are not implemented yet.

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
