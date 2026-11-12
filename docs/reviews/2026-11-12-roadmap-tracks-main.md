# roadmap CLI tracks WebRoadmap main (2026-11-12)

Debate with an Opus-level plan subagent. Roadmap phase2/ad.

## Question

The owner wants AgentController to use the latest WebRoadmap every time. `install.py` pinned a commit and skipped
installing when `roadmap` existed; agentdash (running for days) calls `roadmap --json list` and spawns `roadmap serve`
per project. On Windows, running `roadmap` processes lock the uv tool dir: `uv tool install --force` then deleted the
package before failing, leaving `roadmap` broken (happened today).

## Proposal

A helper `roadmap_tool.py`: installed commit (uv's `direct_url.json`), main's commit (`git ls-remote`), and
`ensure_latest` that stops agentdash's own serves, refuses to run while any other `roadmap` process runs, installs
main's exact SHA, verifies and retries once. Called by install.py, agentdash (startup + periodically), agentdoctor.

## Critique (summary)

- Agreed on the helper, install.py on every run, agentdash at startup before any serve exists, agentdoctor reporting
  stale as a warning.
- Against a timed loop that kills serves: silently stopping a server the owner is looking at is the wrong trade.
  Upgrade only at safe points (startup, opening the Roadmap tab, a manual action); a background poll may only flag.
- Rejected alternatives: `uvx` per call (network and resolve cost on every `roadmap --json list`; its cache envs have
  their own locks); `uv tool upgrade` (doesn't move a git source reliably, same lock failure); a WebRoadmap post-merge
  hook (misses merges done on GitHub, puts AgentController logic in another repo); install + doctor only (the
  dashboard runs for days).
- Notes: install `@<sha>` so the compared commit is exactly the one installed; check processes with `tasklist` (no
  psutil); verify-and-retry is mandatory because another session can start `roadmap` between the check and the
  install; offline keeps the installed version; tracking main means a broken WebRoadmap commit goes live at once
  (acceptable for the owner's own tool, document the exception to the pinning rule).

## Decision

As critiqued, with one change: the Roadmap page load (not its asset or API requests) is treated as a safe point, so
agentdash checks main there at most every 10 minutes and, when behind, restarts its own serves around the upgrade.
The page is being reloaded anyway, so nothing visible is lost. No timed loop and no manual button for now. agentdash
also kills each serve's whole process tree, because the `roadmap.exe` launcher's python children kept the lock.
