"""Keep the `roadmap` CLI (WebRoadmap, the owner's own tool) at WebRoadmap's latest main.

WebRoadmap is a single-source shared tool, so unlike third-party tools it is not pinned: install.py, agentdash (at
startup and when the Roadmap page is loaded) and agentdoctor all compare the installed commit with main and upgrade.

On Windows a running `roadmap` locks its uv tool dir, and `uv tool install --force` then deletes the package before
failing on the locked Scripts dir, leaving `roadmap` broken. So an upgrade only runs when no `roadmap` process is
running (callers stop their own `roadmap serve`s first), installs main's exact commit, verifies the result and
retries once if a race broke it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import NO_WINDOW

REPO_URL = "https://github.com/Fwogg3r/WebRoadmap"
TOOL = "webroadmap"
LOCK = threading.Lock()  # held during an upgrade; hold it around `roadmap` calls that must not overlap one


def spec(commit: str | None = None) -> str:
    return f"git+{REPO_URL}" + (f"@{commit}" if commit else "")


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          **NO_WINDOW)


def tool_dir() -> Path | None:
    try:
        return Path(_run(["uv", "tool", "dir"], 15).stdout.strip()) / TOOL
    except (OSError, subprocess.SubprocessError):
        return None


def installed_commit() -> str | None:
    """The commit the installed tool was built from (uv records it in the dist-info's direct_url.json)."""
    d = tool_dir()
    if not d or not d.is_dir():
        return None
    for f in d.glob("[Ll]ib/**/webroadmap-*.dist-info/direct_url.json"):
        try:
            return json.loads(f.read_text(encoding="utf-8"))["vcs_info"]["commit_id"]
        except (OSError, ValueError, KeyError):
            return None
    return None


def latest_commit(timeout: float = 8) -> str | None:
    """main's commit on GitHub, or None when offline."""
    try:
        out = _run(["git", "ls-remote", REPO_URL, "refs/heads/main"], timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"([0-9a-f]{40})\s", out)
    return m.group(1) if m else None


def running() -> list[int]:
    """PIDs of running `roadmap` processes (their files lock the tool dir on Windows)."""
    try:
        if os.name == "nt":
            out = _run(["tasklist", "/FI", "IMAGENAME eq roadmap.exe", "/FO", "CSV", "/NH"], 15).stdout
            return [int(m) for m in re.findall(r'^"roadmap\.exe","(\d+)"', out, re.I | re.M)]
        out = _run(["pgrep", "-f", r"bin/roadmap( |$)"], 15).stdout
        return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]
    except (OSError, subprocess.SubprocessError):
        return []


def works() -> bool:
    exe = shutil.which("roadmap")
    if not exe:
        return False
    try:
        return _run([exe, "--help"], 30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status(timeout: float = 8) -> dict:
    """{installed, latest, current, works}; current is None when main can't be reached."""
    inst, latest = installed_commit(), latest_commit(timeout)
    return {"installed": inst, "latest": latest, "current": (inst == latest) if inst and latest else None,
            "works": works()}


def ensure_latest(log=print, latest: str | None = None) -> dict:
    """Install main's commit if the tool is missing, broken or behind. Never forces past a running `roadmap`.

    Returns {"ok", "action": "current"|"upgraded"|"skipped"|"failed", "installed", "latest", "error"?}.
    """
    with LOCK:
        latest = latest or latest_commit()
        inst = installed_commit()
        ok_now = works()
        res = {"installed": inst, "latest": latest}
        if latest is None:
            return {**res, "ok": ok_now, "action": "skipped", "error": "couldn't reach GitHub; kept the installed version"}
        if inst == latest and ok_now:
            return {**res, "ok": True, "action": "current"}
        if pids := running():
            return {**res, "ok": ok_now, "action": "skipped",
                    "error": f"`roadmap` is running (pid {', '.join(map(str, pids))}); upgrade when it has stopped"}
        if not shutil.which("uv"):
            return {**res, "ok": ok_now, "action": "failed", "error": "uv is not on PATH"}
        log(f"[roadmap] {'upgrading' if inst else 'installing'} WebRoadmap {inst and inst[:7]} -> {latest[:7]}")
        err = ""
        for _ in range(2):  # a second try repairs a half-done install (another process started mid-install)
            try:
                p = _run(["uv", "tool", "install", "--force", "--quiet", spec(latest)], 600)
                err = (p.stderr or p.stdout).strip()[-400:]
            except (OSError, subprocess.SubprocessError) as e:
                err = str(e)
            if installed_commit() == latest and works():
                return {**res, "ok": True, "action": "upgraded", "installed": latest}
        return {**res, "ok": works(), "action": "failed", "installed": installed_commit(),
                "error": f"uv tool install failed: {err}"}
