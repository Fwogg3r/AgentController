---
title: Auto-update pmjc and WebRoadmap when their main moves
status: done
links: []
---
agentdash (the `--all` logon dashboard) checks pmjc's and WebRoadmap's `main` on GitHub every `[autoupdate] interval` s (default 300, 0 = off) and, when it moved, updates and re-serves the service (`agentcontroller/autoupdate.py`, CLI `agentupdate`, state in `~/.agentcontroller/autoupdate.json`, `GET /api/autoupdate`).

- **roadmap:** the `roadmap` uv tool is reinstalled at main's commit through the existing rm_refresh (stops agentdash's own `roadmap serve`s, which respawn on the next request; skips while another `roadmap` runs).
- **pmjc:** runs from its checkout's `.venv` (found from the running `pmjc serve`, or `[autoupdate.pmjc] checkout`). Fast-forwards to origin/main only when the checkout is on main with no uncommitted tracked changes and no local-only commits; `uv sync` only when pyproject.toml/uv.lock changed; then kills the `pmjc serve` launcher so pmjc's supervisor restarts it, and waits for /api/health. `pmjc mcp` stdio processes are left alone.

Notes: 6 new tests (temp bare remote + checkout: current, fast-forward + restart, dirty, other branch, state, settings); full suite 150 passed. Live `agentupdate` on SHEILA: pmjc 1f4efaf and roadmap d2950f4 both current, checkout found with no config. Not yet seen live: an actual pmjc restart after a real push. Known limits: `uv sync` can fail while Claude sessions' `pmjc mcp` hold the .venv (reported, restart still happens); the supervisor (`pmjc service run`) itself keeps old code until next logon.
