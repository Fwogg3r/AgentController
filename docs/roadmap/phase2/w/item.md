---
title: agentdash LAN listener from config ([dash] host/remote)
status: done
links: [phase2/a]
---
The always-on logon task runs plain `agentdash --all`; `[dash] host` and `remote` in ~/.agentcontroller/config.toml (read when the flags are not given) make it listen on the LAN with the token cookie, so reinstalling the task keeps LAN viewing. Flags still win. Test: test_resolve_listen_flags_and_config.
