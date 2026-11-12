---
title: roadmap CLI always at WebRoadmap main
status: done
links: [phase2/al]
notes: "Owner request (2026-11-12): AgentController must use the latest WebRoadmap every time. Before this, install.py pinned 108eb29 and skipped installing when `roadmap` existed, so the installed CLI fell one commit behind main. New agentcontroller/roadmap_tool.py compares the installed commit (uv's direct_url.json) with main (git ls-remote) and installs main's exact commit. Called by install.py on every run, by agentdash at startup and on Roadmap page loads (at most every 10 min; it stops its own `roadmap serve`s first, which respawn on the next request), and by agentdoctor (warn when behind, missing when broken). It never forces past a running `roadmap` (on Windows that lock made `uv tool install --force` delete the package and then fail, leaving `roadmap` broken, seen today), verifies `roadmap --help` and retries once. agentdash now kills each serve's whole process tree (the roadmap.exe launcher left two python children holding the lock). Design debated with an Opus-level subagent: docs/reviews/2026-11-12-roadmap-tracks-main.md, DECISIONS.md. 128 tests pass (new test_roadmap_tool.py, doctor, install and rmui tests); real check: status at aeaaab2 = main, a live serve detected and its tree killed."
---
**What:** The `roadmap` CLI tracks WebRoadmap's main instead of a pinned commit, and AgentController upgrades it whenever main moves.

**Why:** WebRoadmap is the owner's own single-source tool; a pin plus "skip when installed" left the dashboard on an old version.

**How:** `agentcontroller/roadmap_tool.py` (`status`, `ensure_latest`); `install.py install_roadmap`; `dash.rm_refresh` (startup thread, Roadmap page loads); `doctor.check_roadmap`; `roadmap_tool.LOCK` held around `roadmap --json list` and serve spawns so they don't overlap an upgrade.
