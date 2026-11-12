---
title: Dashboard listeners can double-bind a busy port on Windows
status: planned
links: [phase2/t]
---
http.server.HTTPServer sets allow_reuse_address, and on Windows SO_REUSEADDR lets a second agentdash bind a port the first already holds (seen: a second instance started its Access listener on 8766 while the real one was running). Set allow_reuse_address = False on win32 (as WebRoadmap's Server does) so a second instance fails loudly.
