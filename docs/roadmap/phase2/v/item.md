---
title: Mobile layout for agentdash
status: done
links: []
---

Phones (<= 640px) now use two views instead of the side-by-side layout that piled up the horizontal run strip: a list view (project chips, Filters button, vertical run rows) and a detail view (sticky "‹ Agents" back bar, collapsible run header, transcript, composer pinned above the safe area). Selecting a run pushes a history entry, so the browser back button returns to the list. Roadmap/Jobs/Models are in a bottom tab bar (hidden in the detail view); a project select sits in the Roadmap header.

**Notes**
- Run rows: icon, one-line name, pills, ellipsised meta line, activity line; children are indented with a thin left border, 46px collapse toggle, "(n agents · k working)" line.
- Header is one row (logo, title, needs-me badge, menu); spend, LM Studio chips, user/level and "+ New agent" live in the menu. Filters (with model checkboxes) are a bottom sheet. The project "forget" button is always visible in its chip.
- Touch: 44px targets, 16px inputs, safe-area insets, `viewport-fit=cover`, dvh height, tap highlight off, no horizontal page scroll. Local-run view is stacked with a collapsible Thinking pane.
- Desktop is unchanged (verified by screenshots at 1400x900); added an empty `data:` favicon so the page stops logging a 404/400 for favicon.ico.
- Verified headless with playwright (iPhone 14 descriptor) against a second server; the initial auto-selected run no longer jumps to the detail view on a phone.
