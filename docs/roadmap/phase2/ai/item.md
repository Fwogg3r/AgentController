---
title: agentdash live reload from the checkout
status: done
links: [phase2/t]
---
agentdash restarts itself when its Python source changes (editable install from a git checkout): changes must be quiet for 8 s and import cleanly in a separate interpreter, else the running version keeps serving and dash.log says why. Open pages poll /api/health build and reload themselves (keeping the selected run; not while a message, attachment or dialog is pending). Opt out with [dash] auto_reload = false.

Notes: verified on the owner's dashboard host: touching a source file moved both listeners (8765 and the Access listener 8766 behind the Cloudflare tunnel) to a new process within ~15 s. The scheduled task shows the original process as ended after a reload; the dashboard keeps running.
