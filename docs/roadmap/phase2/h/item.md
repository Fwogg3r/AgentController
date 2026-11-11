---
title: Logon task for the dashboard
status: done
links: [phase2/a]
---
install.py creates the hidden Task Scheduler task 'AgentController dashboard' (agentdash --all) at logon on Windows (--no-dash-task skips it) and writes ~/.agentcontroller/config.toml with commented defaults; registry.ensure_dash runs the task first, then a hidden pythonw spawn.
