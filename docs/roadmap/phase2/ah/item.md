---
title: Claude sessions waiting on background tasks show waiting, not finished; running tool calls pulse
status: done
links: [phase2/x]
---
A Claude Code session or subagent whose turn ended while a run_in_background Bash or a background agent is still going (no task-notification yet) is now waiting, with the tasks as its activity, for up to 6 h of quiet. An unanswered AskUserQuestion shows waiting · question. Bullets of tool calls still in flight pulse (static with prefers-reduced-motion); calls that never got a result stop pulsing at the next prompt or turn end.

Notes: checked on a live session (a subagent waiting on its bench run showed waiting). Waiting-on-background is not added to Needs me.
