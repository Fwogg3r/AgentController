---
title: "agentdash: run titles with the ID below, fast transcript loading"
status: done
links: []
---
Run header shows the session title with the run ID in small italics below (plan sessions; API runs keep their name).

Notes: transcripts were slow to open because /api/stream slept 0.5 s per 1 MB chunk while catching up (a 16 MB session took 8 s+), then the page rendered and scrolled once per event. Now the backlog is read back to back in 4 MB chunks and events render in one batch per frame. 16 MB session: server 0.19 s, page ~0.4 s (was ~2.4 s after the server fix alone). Preloading was not needed.
