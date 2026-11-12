---
title: New Claude Code session from agentdash
status: done
links: []
---
Every project gets *+ New session* (no agentcontroller.toml needed): a first message, optional files and a model (default / opus / sonnet / haiku / fable) start a Claude Code session in the project folder on the plan (agentcontroller.resume --new, with a preassigned session id; never the API). Approvals and questions pop up in the dashboard; the new session is selected once its transcript appears and can be messaged like any other. The API launcher is now labelled *+ New API agent* and still needs the toml and key file.

Also fixed: live reload missed an edit that landed while the restarted dashboard was importing (it kept the half-edited code); a restart now also checks for source files newer than its start.

Notes: checked live: a Haiku session started in a scratch project from the dashboard, replied, and was selected automatically.
