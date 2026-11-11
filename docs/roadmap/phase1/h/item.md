---
title: "Low-balance guard: prefer the plan over the API (agentask --balance, [budget].min_remaining)"
status: done
links: []
---

Owner request (2026-11-10). The API has no balance endpoint, so remaining = `[budget] cap` - logged spend. `agentask --balance` prints it (exit 4 below `[budget] min_remaining`, default $5); agentask and agentctl refuse below the floor (`--force` / `AGENTCONTROLLER_FORCE=1` override). The api-request skill and the owner's global CLAUDE.md now default generic debates to the plan and use the API only when asked. Selftest sets `min_remaining = 0` in its stub config.

Follow-up (2026-11-10): the spend counted against the cap is now max(logged, actual org-wide cost from the Admin API via `usage.actual_spend`, since `[budget] since`, cached 15 min, skipped without an admin key). agentctl also requires cap - spend >= the run's budget using that figure. Tested with a mocked Admin figure, then live (2026-11-10): `agentusage` and `agentask --balance` read the real cost report with the admin key from `Z:\secrets\agents\anthropic` and return LOW when spend passes the cap. No unit tests (pytest isn't installed).
