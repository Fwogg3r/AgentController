---
title: Message and answer Claude Code (plan) sessions from agentdash
status: done
links: [phase2/ak]
---
*Send to agent* (with attachments) works for a finished Claude Code session: agentdash starts `agentcontroller.resume`, which continues the session in place through the Agent SDK and the native Claude Code CLI (same session id and transcript, the user's settings, the plan login; ANTHROPIC_API_KEY is removed from its environment and a run that reports an API key source stops before any model call). Tool calls that would prompt in the Claude app pop up in the dashboard (Allow / Deny, also under Needs me and as a card in the transcript); AskUserQuestion is answered the same way. Follow-ups while the turn runs are queued into it; *Stop* ends agentdash's turn.

Re-cache warning: when the session's context (the last call's input + cache read + cache write) is over `[dash] recache_warn_tokens` (default 100k), the first message asks first, showing the size and how long the session has been idle. Follow-ups to a running turn skip it.

Not allowed: subagents, and sessions still working or waiting in the Claude app (two writers would branch the transcript). A session left open in the Claude app doesn't show agentdash's turns until it is reopened there.

Notes: checked end to end on a throwaway Haiku session through the live dashboard: message, Write approval popup, Allow, file written, reply in the transcript, back to finished. The SDK refuses npm's `claude.cmd` wrapper, so the driver looks for the native `claude.exe` (npm package bin, native installer, or AGENTCONTROLLER_CLAUDE_CLI) and falls back to the SDK's bundled CLI. State per session in `~/.agentcontroller/resume/<session id>/`.

Questions asked inside the Claude app (AskUserQuestion in a session the app runs) can only be answered there. They now show in the transcript as a card with every question, its options and, once answered, the chosen answers (answer results are kept whole instead of cut at 400 characters), pop up once in the dashboard with "answer it in the Claude app", and are listed under Needs me with the same note. Before, they showed only as a truncated one-line tool call.
