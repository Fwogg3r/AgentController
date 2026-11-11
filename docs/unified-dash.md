# Unified agentdash: spec

One always-on dashboard (`agentdash --all`, 127.0.0.1:8765) for every agent in every project.
Decision record: [DECISIONS.md](DECISIONS.md) "Unified dashboard"; review: [reviews/2026-11-10-unified-dash-opus.md](reviews/2026-11-10-unified-dash-opus.md).

## Pieces

- `agentcontroller/registry.py` (done): `~/.agentcontroller/projects/<hash>.json`, `agents/<id>.json`, `register_agent`,
  `update_agent`, `ensure_dash` (health-checked; `schtasks /Run` first, then a hidden `pythonw` spawn).
- `agentcontroller/hub.py` (done, extend as needed): project discovery (registry + Claude Code transcript cwd + `[dash] scan`),
  `local_info` for agentlocal runs, `roadmap(path)` via the `roadmap --json list` CLI plus item bodies, `pmjc()`,
  `lmstudio()`, `decide_local()`. Global config `~/.agentcontroller/config.toml` (see hub.py docstring).
- `agentcontroller/dash.py`: one code path. `--all` serves every project; `--project X` = `--all` pinned to X.
  The module-global `CFG` becomes per-request: a `ContextVar` holding the project's `Config`, set from the request's
  `p` query parameter (registry key) and a proxy object so existing `CFG.x` code keeps working. Configs cached per
  project, reloaded when the toml's mtime changes. `config.load(dir)` must use `dir` as root when it has no toml.
- `agentcontroller/static/index.html`: the UI.

## HTTP API (all JSON; `p` = project key from `/api/projects`)

| Method/path | Returns / does |
|---|---|
| GET `/api/health` | `{"mode": "all"|"project", "version", "pid", "projects": N}` (no auth beyond Host check) |
| GET `/api/projects` | `[{key, name, path, toml, roadmap, exists, sources, active, waiting, crashed, total, last_activity, models:[...], spend:{total,cap}|null}]`, sorted active first then last activity |
| GET `/api/runs?p=KEY&plan_age=S&plan=0|1` | the project's runs: API (agentctl/agentask/pilot, as today), Claude Code sessions from **all** its `~/.claude/projects` dirs (main checkout + worktrees), and registry runs (`hub.registered_runs`). Every row gets `project` (key) and `project_name`, plus `provider` (`anthropic`, `lmstudio`, `plan`, other) |
| GET `/api/runs?p=*` | all projects merged (same row shape) |
| GET `/api/stream?p=KEY&run=NAME` | SSE as today. For a local run: replay + tail `events.jsonl` (lines as-is), and tail `thinking.txt` / `answer.txt` as `{"kind":"local_thinking","text":chunk}` / `{"kind":"local_answer","text":chunk}` |
| GET `/api/spend?p=KEY` | as today, for that project (no `p` or `*`: sum over projects with a toml, cap null) |
| GET `/api/config?p=KEY` | as today, plus `can_launch` (project has a toml and a key file) |
| GET `/api/roadmap?p=KEY` | `hub.roadmap(path)`; items gain `agents: [run names]` whose registry record or brief names that roadmap id (field `roadmap`) |
| GET `/api/jobs` | `hub.pmjc()` |
| GET `/api/lmstudio` | `hub.lmstudio()` |
| GET `/api/attention` | the "needs me" queue across projects: local approvals, pmjc pending approvals, crashed runs (last 24 h), Claude sessions idle in a waiting state; `[{project, run, kind, text, ts, action?}]` |
| GET `/api/worktree`, `/api/billed` | as today, with `p` |
| POST `/api/launch`, `/api/inbox`, `/api/kill`, `/api/archive`, `/api/worktree_delete`, `/api/primary` | as today, with `p`; launch only in projects with a toml |
| POST `/api/local_approve?p=KEY` | body `{run, id, approve}` -> `hub.decide_local` |
| POST `/api/forget_project` | body `{p}`: hide a stale project (registry forget + add to an in-registry hide list) |

Security (from the review): every POST needs header `X-AC: 1` in addition to the Host/Origin check (blocks cross-site
form posts); paths from registry records are served only if they contain a `.agentcontroller` path segment
(local runs) and never via user-supplied paths; kill only acts on PIDs from the run's own pid file (local runs: no kill
button in v1). The pmjc token stays server-side. `--remote` on the all-projects instance prints a warning.

## UI

- Header: title "Agents", global spend (sum of projects with a cap), LM Studio chips (host: loaded model or "off"),
  an attention badge (count from `/api/attention`, click = list), "+ New agent" (enabled when the selected project can launch).
- Left column, top: **project list** ("All projects" first, then projects with counts of working/waiting/crashed,
  last activity, a dot for live agents). Persist the selection. "Forget" on hover for projects with no runs.
- Left column, below: the existing filters, unchanged in behaviour, applied to the selected project's runs (or all),
  plus a **Models** block: one checkbox per model seen in the current rows (normalised: `opus`, `sonnet`, `haiku`,
  `fable`, local model names, `other`), all checked by default, persisted; "all / none" links. New models appear checked.
  The same filters drive the project counts shown in the project list.
- Run rows: as today; a **chip icon** (GPU/chip glyph) for local runs instead of Clawd, a small provider tag, the
  project name when "All projects" is selected, and for local runs `stage x/y`, `ctx NN%`.
- Main pane tabs for the selected project: **Agents** (today's transcript view) | **Roadmap** | **Jobs** (global) | **Models** (LM Studio hosts).
  - Agents, local run selected: header with model/tier/stages, a **context gauge** (tokens of the last call vs the
    window) with a sparkline of per-call `in`/`out`, pending approvals with Approve/Deny buttons, and two panes:
    **Thinking** (streamed, muted) and **Answer/edits/events** (calls, edits with diffstat, tests, stage_done, final).
  - Roadmap: phases as columns (cards: title, status pill, nickname), click a card to expand its body (markdown,
    escaped), agents linked to the item as chips that select the run. A "refresh" button. Read-only in v1.
  - Jobs: pmjc jobs (name, enabled, trigger, model preference = the assigned agent/model, tagged AgentController
    agents, last run), watches (status, kind, created), pending pmjc approvals with a link to pmjc's `/approve/<id>`.
    If pmjc isn't configured or reachable, show the reason from `/api/jobs`.
- Mobile width (<640px) keeps working.
