---
title: Roadmap tab shows WebRoadmap's own page, toggle to the summary view
status: done
links: [phase2/ad]
---
The Roadmap tab embeds WebRoadmap's interactive page (one roadmap serve per project on a loopback port, proxied under /rmui/<id>/ so it stays behind the LAN token and Cloudflare Access: GET view, POST operate; the folder path is scrubbed for non-admins). A button switches to the dashboard's read-only summary (with agent chips); the choice is remembered per browser. Edits through the page cover the write half of phase2/m. Tested: tests/test_dash_rmui.py, and checked in Chrome on a throwaway dashboard.
