# Unified agentdash: second opinion (Opus subagent, on the plan), 2026-11-10

Proposal given: registry (`~/.agentcontroller`) + scan of `~/.claude/projects`; one `agentdash --all` process loading
each project's config lazily; project list + filters + model checkboxes; tabs Agents | Roadmap | Jobs; roadmap parsed
by agentdash; pmjc read over HTTP; LM Studio via pmjc; scans throttled.

## Verdicts

1. **Registry + scan: agree, registry as pointers only.** Status comes from the run's own files; a crashed CLI never
   writes "finished". Also scan `<project>/.agentcontroller/runs`. `projects.json` read-modify-replace loses concurrent
   updates: one file per project or a lock. Un-slugging is lossy: use the transcript `cwd`. Cache `main_checkout()`;
   handle deleted worktrees.
2. **`--all` as the only code path.** `CFG` is used ~52 times; make config per request. `--project X` = `--all` pinned.
   `dash_up()` must probe `/api/health` (`mode: all`), not just a TCP connect.
3. **Roadmap: render ourselves, WebRoadmap owns the format** (use its `--json` output). Ranking: CLI JSON > own parser >
   iframe `roadmap serve`. Importing it fails (isolated uv tool env). Writes only via `roadmap set`. Escape markdown:
   agents write roadmap files, and the page can launch/kill agents (XSS).
4. **Logon task: agree; copy pmjc's `service.py` pattern** (`schtasks /Create /XML`, `pythonw.exe`, "only when logged
   on" so Z: is mapped, no execution time limit, restart on failure, IgnoreNew). `ensure_dash()` runs the task first.
   Never open a browser. A Windows service is wrong (session 0).
5. **pmjc over HTTP, not SQLite** (`journal_mode=DELETE`, `synchronous=FULL`: a reader stalls its scheduler). Auth:
   `Authorization: Bearer`/`X-PMJC-Token`, scopes sources/actions/jobs/admin. Jobs/watches/runs need `jobs`;
   **`GET /api/approvals` needs admin**, which can approve actions and mint tokens: don't store one; add a read-only
   scope to pmjc. Token server-side only. Query LM Studio directly, not pmjc's audited tool call. "Agent assigned to a
   job" needs a defined link (e.g. a job field on the agent record).
6. **Caching: agree**; one background snapshot thread is better still; tail-read big transcripts; reload configs on
   toml mtime.

## Risks raised

- Registry files are writable by any local process: serve only paths under `.agentcontroller`/`~/.claude/projects`;
  never `taskkill` a registry PID without checking a pid file and start time (Windows reuses PIDs).
- Agents (running model-written code) are the threat: suggested a mandatory token + custom POST header + launch only in
  registered toml projects. (Decision: header yes, mandatory localhost token no; see DECISIONS.md.)
- `--remote` on the all-projects instance exposes everything; moved/renamed projects duplicate keys (add forget);
  agents on other machines are invisible; caps are per project (no global cap).

## Extra features, ranked

1. A global "needs me" queue (local approvals, pmjc approvals, waiting Claude sessions, crashed runs) + notification.
2. A global daily budget enforced by the CLIs.
3. Roadmap <-> agent links (`--roadmap <id>` at launch, agents on roadmap cards, launch from an item).
4. Per-machine GPU arbitration for local agents.
5. Worktree actions (diff vs main, open in editor, copy resume command).
6. Retention/archiving of registry records.
7. Search across transcripts and run logs.
