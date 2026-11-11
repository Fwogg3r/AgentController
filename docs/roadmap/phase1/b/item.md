---
title: "agentdash: show local-model agents (aider on LM Studio) and every other agent in a project, with their own icon"
status: done
links: [phase1/d]
---
Owner request (2026-11-08): agentdash lists local-model agents and every other agent in a project, each with its own icon. Must-haves: context gauge and thinking shown live per agent.

Done 2026-11-10 (shipped in the unified dashboard, see phase2): agentlocal runs are registered in ~/.agentcontroller/agents and listed with a chip icon (AGENT_PROFILES: Clawd for Claude, an original Qwen critter for local Qwen, chip for other local models). A local run shows a context gauge (tokens of the last call vs the model window) with per-call history, the thinking stream separately from the answer/edits/test events, stage progress and approvals (Approve/Deny). Cost is free; no kill button yet (planned in phase2).
