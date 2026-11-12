---
title: Messages with file attachments; AskUserQuestion answered from the dashboard
status: done
links: []
---
Composer: Attach button, paste or drop files (10 files, 20 MB). Saved to agent_runs/attachments/<run>/; the runner sends images and PDFs as content blocks, inlines text files up to 200 KB, and gives the path for anything else. The audit log records names and sizes only.

API agents' AskUserQuestion calls now wait for an answer from the dashboard (question event, then an inbox answer line, returned as updated_input answers). The run shows waiting · question, the question appears in Needs me and pops up once as a dialog (options, multi-select, Other), and as a card in the transcript. Stop or 4 h without an answer denies the call so the model carries on.

Notes: Claude Code (plan) sessions are read-only transcripts, so they can't be messaged or answered from the dash yet. Not verified against a live API run (unit-tested with the answer format from the Agent SDK docs, and in a sandbox dashboard).
