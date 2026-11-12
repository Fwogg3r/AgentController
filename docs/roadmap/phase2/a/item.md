---
title: "Unified agentdash: one always-on dashboard for every project"
status: done
links: [phase2/ae, phase2/aj, phase2/c, phase2/h, phase2/k, phase2/n, phase2/o, phase2/p, phase2/t, phase2/v, phase2/w, phase2/y, phase2/z]
---
agentdash --all (default) serves every project from one process on 127.0.0.1:8765: project list with live counts, model filter checkboxes, per-request project config, /api/projects /runs?p=* /attention. Discovery from the registry (~/.agentcontroller), Claude Code transcript cwds and [dash] scan. Global config ~/.agentcontroller/config.toml (include, [dash], [pmjc], [lmstudio.hosts]). The CLIs (agentctl, agentask, agentpilot, agentlocal) register runs (--parent, --roadmap, AGENTCONTROLLER_PARENT) and start the dashboard if it is down. Spec docs/unified-dash.md, decision in DECISIONS.md.
