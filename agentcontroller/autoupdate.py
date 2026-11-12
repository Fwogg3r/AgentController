"""Follow pmjc's and WebRoadmap's `main`: when it moves on GitHub, update the service and serve it again.

    agentupdate                # check both now, update what moved, print what happened
    agentupdate --status       # the last results (agentdash's background checks included), no network

agentdash runs the same check every [autoupdate] interval seconds (default 300) in the background:

  - roadmap (WebRoadmap): the `roadmap` uv tool is reinstalled at main's commit (roadmap_tool.ensure_latest);
    agentdash's own `roadmap serve`s are stopped first and respawn on the next Roadmap page request.
  - pmjc (PersonalMCPJobController): it runs from its checkout's .venv, so the checkout is fast-forwarded to
    origin/main (only when it is on `main` with no uncommitted changes to tracked files), `uv sync` runs when
    pyproject.toml or uv.lock changed, then the `pmjc serve` process is killed and pmjc's own supervisor
    (`pmjc service run`) starts it again on the new code. Health is checked on /api/health afterwards.
    `pmjc mcp` processes (Claude sessions' stdio servers) are left alone; new sessions get the new code.

Config (~/.agentcontroller/config.toml):

    [autoupdate]
    interval = 300                     # seconds between checks in agentdash; 0 turns the background check off
    # services = ["pmjc", "roadmap"]   # which ones to follow
    [autoupdate.pmjc]
    # checkout = "C:/.../PersonalMCPJobController"   # default: found from the running `pmjc serve` process
    # health_url = "http://127.0.0.1:8770/api/health"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import NO_WINDOW, hub, registry, roadmap_tool

SERVICES = ("pmjc", "roadmap")
DEFAULT_INTERVAL = 300
PMJC_HEALTH = "http://127.0.0.1:8770/api/health"
LOCK = threading.Lock()  # one update pass at a time (dash loop vs. a manual run in the same process)


def _run(cmd: list[str], timeout: float = 60, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=cwd, **NO_WINDOW)


def settings(cfg: dict | None = None) -> dict:
    c = (cfg if cfg is not None else hub.gconfig()).get("autoupdate") or {}
    try:
        interval = int(c.get("interval", DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL
    services = [s for s in (c.get("services") or SERVICES) if s in SERVICES]
    return {"interval": max(interval, 60) if interval > 0 else 0, "services": services,
            "pmjc": c.get("pmjc") or {}}


def state_file() -> Path:
    return registry.home() / "autoupdate.json"


def load_state() -> dict:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(name: str, res: dict) -> None:
    st = load_state()
    st[name] = {**res, "checked": datetime.now().isoformat(timespec="seconds")}
    try:
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass


# ---------- processes ----------

def processes(pattern: str) -> list[dict]:
    """[{pid, ppid, cmd}] of processes whose command line matches the regex `pattern`."""
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                  "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
            out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 30).stdout.strip()
            rows = json.loads(out) if out else []
            rows = [rows] if isinstance(rows, dict) else rows
            procs = [{"pid": r["ProcessId"], "ppid": r["ParentProcessId"], "cmd": r.get("CommandLine") or ""}
                     for r in rows]
        else:
            out = _run(["ps", "-eo", "pid=,ppid=,args="], 15).stdout
            procs = [{"pid": int(a), "ppid": int(b), "cmd": c} for a, b, c in
                     (l.strip().split(None, 2) for l in out.splitlines() if len(l.split(None, 2)) == 3)]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []
    rx = re.compile(pattern, re.I)
    return [p for p in procs if rx.search(p["cmd"]) and p["pid"] != os.getpid()]


def _kill(pid: int) -> None:
    try:
        if os.name == "nt":
            _run(["taskkill", "/T", "/F", "/PID", str(pid)], 15)
        else:
            os.kill(pid, 9)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------- pmjc ----------

SERVE_RX = r"-m\s+pmjc\s+serve\b"


def pmjc_checkout(conf: dict) -> Path | None:
    """[autoupdate.pmjc] checkout, else the checkout whose .venv runs `pmjc serve`."""
    if conf.get("checkout"):
        p = Path(os.path.expandvars(conf["checkout"])).expanduser()
        return p if (p / ".git").exists() else None
    for pr in processes(SERVE_RX):
        m = re.search(r"([A-Za-z]:\\[^\"]*?|/[^\"]*?)[\\/]\.venv[\\/]", pr["cmd"])
        if m and (Path(m.group(1)) / ".git").exists():
            return Path(m.group(1))
    return None


def remote_main(repo: str | Path, timeout: float = 15) -> str | None:
    """main's commit at the remote (`repo` is a URL or a checkout whose origin is used), or None when offline."""
    try:
        if isinstance(repo, Path):
            out = _run(["git", "-C", str(repo), "ls-remote", "origin", "refs/heads/main"], timeout).stdout
        else:
            out = _run(["git", "ls-remote", repo, "refs/heads/main"], timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"([0-9a-f]{40})\s", out)
    return m.group(1) if m else None


def _git(co: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(co), *args], timeout)


def _healthy(url: str, wait: float) -> bool:
    end = time.time() + wait
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        if time.time() >= end:
            return False
        time.sleep(2)


def update_pmjc(conf: dict, log: Callable[[str], None] = print, health_wait: float = 90) -> dict:
    co = pmjc_checkout(conf)
    if co is None:
        return {"ok": False, "action": "skipped",
                "error": "pmjc checkout not found (no running `pmjc serve`; set [autoupdate.pmjc] checkout)"}
    res: dict = {"checkout": str(co)}
    latest = remote_main(co)
    head = _git(co, "rev-parse", "HEAD").stdout.strip() or None
    res.update(installed=head, latest=latest)
    if latest is None:
        return {**res, "ok": True, "action": "skipped", "error": "couldn't reach GitHub"}
    if head == latest:
        return {**res, "ok": True, "action": "current"}
    branch = _git(co, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        return {**res, "ok": False, "action": "skipped", "error": f"checkout is on '{branch}', not main"}
    if _git(co, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return {**res, "ok": False, "action": "skipped", "error": "checkout has uncommitted changes"}
    f = _git(co, "fetch", "--quiet", "origin", "main", timeout=120)
    if f.returncode:
        return {**res, "ok": False, "action": "failed", "error": f"git fetch: {f.stderr.strip()[-300:]}"}
    if _git(co, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode:
        return {**res, "ok": False, "action": "skipped", "error": "local main has commits not on origin/main"}
    changed = _git(co, "diff", "--name-only", "HEAD", "origin/main").stdout.split()
    log(f"[pmjc] main moved {head[:7]} -> {latest[:7]} ({len(changed)} files); updating {co}")
    m = _git(co, "merge", "--ff-only", "--quiet", "origin/main")
    if m.returncode:
        return {**res, "ok": False, "action": "failed", "error": f"git merge --ff-only: {m.stderr.strip()[-300:]}"}
    res["installed"] = _git(co, "rev-parse", "HEAD").stdout.strip()
    warn = []
    if any(n in ("pyproject.toml", "uv.lock") for n in changed):
        uv = shutil.which("uv")
        s = _run([uv, "sync", "--directory", str(co)], 600) if uv else None
        if s is None or s.returncode:
            warn.append("uv sync failed (Claude sessions' `pmjc mcp` may lock .venv; close them and run "
                        f"`uv sync` in the checkout): {(s.stderr.strip()[-300:] if s else 'uv not on PATH')}")
        else:
            log("[pmjc] dependencies synced")
    serves = processes(SERVE_RX)
    serves = [p for p in serves if str(co).lower() in p["cmd"].lower()] or serves
    pids = {p["pid"] for p in serves}
    tops = [p for p in serves if p["ppid"] not in pids]  # the serve launcher(s); /T takes their children
    if not tops:
        warn.append("no `pmjc serve` was running; code updated, start pmjc yourself")
        return {**res, "ok": False, "action": "updated", "restarted": False, "error": "; ".join(warn)}
    for p in tops:
        _kill(p["pid"])
    log(f"[pmjc] stopped `pmjc serve` (pid {', '.join(str(p['pid']) for p in tops)}); its supervisor restarts it")
    url = conf.get("health_url") or PMJC_HEALTH
    up = _healthy(url, health_wait)
    if not up:
        warn.append(f"pmjc didn't answer {url} within {int(health_wait)} s after the restart")
    log(f"[pmjc] {'serving' if up else 'NOT serving'} {res['installed'][:7]}")
    return {**res, "ok": not warn, "action": "updated", "restarted": True, "healthy": up,
            **({"error": "; ".join(warn)} if warn else {})}


# ---------- roadmap ----------

def update_roadmap(log: Callable[[str], None] = print, refresh: Callable[[], dict] | None = None) -> dict:
    """`refresh` is agentdash's rm_refresh(force=True), which stops its own `roadmap serve`s around the upgrade."""
    r = refresh() if refresh else roadmap_tool.ensure_latest(log=log)
    return {**r, "action": {"upgraded": "updated"}.get(r.get("action"), r.get("action"))}


# ---------- one pass and the background loop ----------

def check_all(log: Callable[[str], None] = print, roadmap_refresh: Callable[[], dict] | None = None,
              cfg: dict | None = None) -> dict:
    st = settings(cfg)
    out = {}
    with LOCK:
        for name in st["services"]:
            try:
                r = update_pmjc(st["pmjc"], log) if name == "pmjc" else update_roadmap(log, roadmap_refresh)
            except Exception as e:  # never let one service stop the loop
                r = {"ok": False, "action": "failed", "error": f"{type(e).__name__}: {e}"}
            if r.get("action") != "current" or r.get("error"):
                log(f"[autoupdate] {name}: {r.get('action')}" + (f" ({r['error']})" if r.get("error") else ""))
            _save(name, r)
            out[name] = r
    return out


def loop(log: Callable[[str], None], roadmap_refresh: Callable[[], dict] | None = None,
         stop: threading.Event | None = None, first_delay: float = 0) -> None:
    """agentdash's background thread: check, then sleep [autoupdate] interval (re-read each round)."""
    stop = stop or threading.Event()
    stop.wait(first_delay)
    while not stop.is_set():
        interval = settings()["interval"]
        if interval:
            check_all(log, roadmap_refresh)
        stop.wait(interval or 300)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="print the last results only")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = load_state() if args.status else check_all(lambda m: print(m, flush=True))
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        for name, r in res.items():
            print(f"{name:<8} {r.get('action', '?'):<8} installed {(r.get('installed') or '?')[:7]} "
                  f"main {(r.get('latest') or '?')[:7]}" + (f"  checked {r['checked']}" if r.get("checked") else "")
                  + (f"\n         {r['error']}" if r.get("error") else ""))
    return 0 if all(r.get("ok") for r in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
