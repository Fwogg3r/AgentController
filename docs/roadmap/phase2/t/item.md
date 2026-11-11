---
title: "agentdash internet access: Cloudflare Access sign-in and per-user levels"
status: done
links: [phase2/a]
---
Second listener (127.0.0.1:<listen_port>, `[dash.access]` in the global config) for the Cloudflare tunnel: every request needs a valid Cloudflare Access JWT (RS256, stdlib), levels view/operate/admin per e-mail from a grants file (super admin from config), CSRF (Origin = public_origin), audit log, default-deny endpoint table, /api/me drives UI gating. The LAN `--remote` token is ignored there. Design: docs/DECISIONS.md.

Follow-up: per-user budget caps for `operate` (launch spends API money).
