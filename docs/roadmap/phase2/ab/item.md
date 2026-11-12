---
title: pmjc token from a configurable secrets_dir
status: done
links: []
---
New global key secrets_dir (per machine, in ~/.agentcontroller/config.toml, not the shared include). When [pmjc] token_file is unset the token is read from <secrets_dir>/pmjc/agentdash.token; agentdash and agentdoctor share the lookup (hub.pmjc_token_file). Nothing is hardcoded; unset means no default.
