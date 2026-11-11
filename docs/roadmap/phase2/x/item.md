---
title: Parent agents show waiting while children run
status: done
links: [phase2]
notes: "Server-side propagate_waiting() in dash.py (used by project_rows and /api/runs?p=KEY): a finished/ended row with an active descendant (same project, parent=run name, recursive, cycle-safe) becomes waiting/active/running with a 'waiting for N child agent(s)' task activity; own_state/own_outcome keep the original; crashed parents stay crashed with children_active + note. Counts, attention and the UI follow. Tests: tests/test_parent_waiting.py."
---
