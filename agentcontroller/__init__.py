"""AgentController: launch, watch and steer budget-capped Claude Agent SDK runs from a localhost dashboard."""

import os as _os

# Pass as **NO_WINDOW to every subprocess call. The dashboard runs under pythonw (no console), so on Windows each
# console child (git, tasklist, ...) would otherwise get a new, visible console window that flashes up for a moment.
NO_WINDOW: dict = {"creationflags": 0x08000000} if _os.name == "nt" else {}  # CREATE_NO_WINDOW
