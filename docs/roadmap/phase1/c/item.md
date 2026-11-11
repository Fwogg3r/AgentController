---
title: Per-session permission profiles and a disposable sandbox for local agents
status: planned
links: []
---

Owner request (2026-11-08). Let the owner pick a permission profile per agent session: locked (edit only named files), edit (any repo file + fixed test command), shell (batch/shell commands), full. Dangerous profiles run only inside a disposable sandbox so a bad run can't touch the PC: options are Windows Sandbox (.wsb with only the worktree mapped, LM Studio reached over the host IP; resets on close), a WSL2 distro or container with only the worktree mounted, or a Docker container. The model itself stays in LM Studio on the host; only the agent harness and its commands run inside. Claude Code's auto mode blocks aider's auto-confirm/auto-test on the host, which is another reason to run those profiles in the sandbox.
