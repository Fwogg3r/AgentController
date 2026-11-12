---
title: No console flashes from the background dashboard
status: done
links: []
notes: "Found 2026-11-11: agentdash runs under pythonw (no console), so every git/tasklist/taskkill it started without CREATE_NO_WINDOW got a new console that Windows Terminal showed for about a second (caught: git branch from the dash process). Fix: agentcontroller.NO_WINDOW passed to every background subprocess call (dash, hub, registry, config, doctor, pilot, local), and agent/dashboard launches drop DETACHED_PROCESS, which overrode CREATE_NO_WINDOW and left agents with no console, so their own git calls flashed too. 116 tests pass; a pythonw probe running git/tasklist with the flag opened no terminal window."
---
