---
title: Move the local-agent runner from pmjc's private folder into AgentController (single source)
status: done
links: [phase1/d]
---
Today: pmjc/private/local-agent (run.sh with locked/edit profiles, model settings, LOCAL-AGENT.md instructions, briefs). Aider 0.86.2 is installed as a uv tool at a pinned commit (253f036). Claude Code's auto mode blocks aider's auto-confirm/auto-test on the host (sandbox, phase1/c).

Done 2026-11-10: run.sh's profiles map to passthrough tiers in the owner's Z:/shared/agentcontroller/tiers.toml: `aider-edit` (PROFILE=edit: edits, aider runs the repo's test_cmd) and `aider-locked` (PROFILE=locked: named files only, no tests); both pass LOCAL-AGENT.md via `--read` and a 4096-token repo map like run.sh. model.settings.yml / model.metadata.json values (diff format, examples_as_sys_msg, max_tokens 16384, temperature 0.6, top_p 0.95, 64k window, 1800 s timeout) live in `[models.qwen-27b]` and are generated per run. LOCAL-AGENT.md in Z:/shared is identical to pmjc's copy. pmjc's private/local-agent is gitignored (private/); it keeps run.sh, briefs and logs and got a README.md mapping old commands to agentlocal. Gaps: the test command is the project's `[local].test_cmd` instead of run.sh's `-x -p no:cacheprovider` variant (set it per project); aider output is no longer streamed into the log; agentlocal refuses main/master and a dirty worktree, which run.sh did not.
