---
title: "agentdash: show local-model agents (aider on LM Studio) and every other agent in a project, with their own icon"
status: planned
links: [phase1/d]
---
Owner request (2026-11-08). agentdash lists only Claude API agents today. Add local-model agents (aider driving LM Studio models, started by an overseer session; runner pattern in pmjc's private/local-agent/run.sh) and any other agent spun up in the project: register each run (name, model, worktree, brief, log path, status, start/end, token counts from aider's "Tokens: Xk sent" lines), stream its log live in the dashboard, and show a distinct icon per agent kind so a local model never looks like Claude (e.g. a chip/GPU glyph for local, the existing icon for Claude). Free cost, but show context use against the model's window (64k for Qwen3.8-27B).

Must-haves (owner, 2026-11-08): watch the context grow live per agent (a gauge of tokens used vs the model's window, updated each turn, with the history of sent/received tokens per turn), and see the local model's thinking (reasoning_content or <think> blocks from LM Studio) separately from its answer and edits, streamed as it arrives. Same view for any agent: thinking, tool calls/edits, test runs, final report.
