---
title: "First-run install: installer, agentdoctor, quick start"
status: done
links: [phase2/a]
---
Fresh-machine install that ends in a working product. `install.py` now also installs the roadmap CLI (WebRoadmap, pinned commit 108eb29, asks first) and its skill, aider at pinned commit 253f036 with `--local`, checks for the Claude Code CLI (prints how to install it, never installs system software), and runs `agentdoctor` last; `--dry-run` prints the whole plan without cloning or writing.

`agentdoctor` (agentcontroller/doctor.py): table of required / recommended / optional pieces (uv, git, the commands, claude-agent-sdk, roadmap, Claude Code, skills, dashboard, logon task, global config, API key presence per project, aider, LM Studio, pmjc, cloudflared), exit 0 only if every required one is OK, `--json`. It finds LM Studio and pmjc on localhost and in the config (include honoured); `--scan` asks, then probes the local private /24 only (TCP connect, then GET /api/v0/models for LM Studio and the unauthenticated /api/health for pmjc). Found services can be added to ~/.agentcontroller/config.toml by a targeted text edit (comments and other keys kept, .bak made, refuses invalid TOML); tokens are never written, pmjc token instructions are printed.

Classification: required = uv, git, the package and SDK, roadmap CLI; recommended = API key, Claude Code CLI, skills, dashboard, logon task; optional = batch toolkit, aider, LM Studio, pmjc, cloudflared. Node and Firebase are not used. Follow-up: show doctor results in the dashboard (dash.py was off limits for this change). Tests: tests/test_doctor.py, tests/test_install.py.
