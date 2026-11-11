"""Machine-wide registry of projects and agents, read by the unified agentdash.

    ~/.agentcontroller/            (AGENTCONTROLLER_HOME overrides)
      projects/<hash>.json         one file per project: {"key", "name", "path", "toml", "last_seen"}
      agents/<id>.json             one file per agent run started by agentctl/agentask/agentlocal/agentpilot
      dash.json                    {"port", "pid", "started"} of the running unified dashboard

Every CLI that starts an agent calls `register_agent()` (which also registers the project) and
`ensure_dash()` (starts the dashboard hidden if nothing answers on its port). The dashboard itself
also auto-discovers Claude Code sessions under ~/.claude/projects, so those need no registration.
Writes are atomic (temp file + replace); readers tolerate missing or half-written files.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = 8765


def home() -> Path:
    h = Path(os.environ.get("AGENTCONTROLLER_HOME") or Path.home() / ".agentcontroller")
    (h / "agents").mkdir(parents=True, exist_ok=True)
    return h


def _write(path: Path, data) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def norm(p: str | Path) -> str:
    """Canonical key for a path: resolved, forward slashes, lower-case drive on Windows."""
    s = str(Path(p).resolve()).replace("\\", "/")
    return s[0].lower() + s[1:] if len(s) > 1 and s[1] == ":" else s


def main_checkout(path: str | Path) -> Path:
    """The main checkout a path (or a git worktree of it) belongs to; the path itself outside git."""
    p = Path(path).resolve()
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=p,
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if common:
            c = Path(common)
            return c.parent if c.name == ".git" else p
    except (OSError, subprocess.SubprocessError):
        pass
    return p


# ---------- projects ----------

def _pfile(key: str) -> Path:
    import hashlib
    d = home() / "projects"
    d.mkdir(exist_ok=True)
    return d / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")


def projects() -> dict[str, dict]:
    """{key: entry}. One file per project, so two CLIs registering at once can't drop each other's entry."""
    out = {}
    for f in (home() / "projects").glob("*.json") if (home() / "projects").is_dir() else []:
        e = _read(f)
        if isinstance(e, dict) and e.get("key"):
            out[e["key"]] = e
    return out


def register_project(path: str | Path, name: str | None = None) -> str:
    root = main_checkout(path)
    key = norm(root)
    f = _pfile(key)
    entry = _read(f, {}) or {}
    toml = root / "agentcontroller.toml"
    entry.update({"key": key, "name": name or entry.get("name") or root.name, "path": str(root),
                  "toml": str(toml) if toml.is_file() else None, "last_seen": time.time()})
    _write(f, entry)
    return key


def forget_project(key: str) -> bool:
    f = _pfile(key)
    if not f.exists():
        return False
    f.unlink()
    return True


# ---------- agents ----------

def register_agent(agent_id: str, kind: str, project: str | Path, **fields) -> dict:
    """kind: api | oneshot | pilot | local | other. Fields commonly used by the dashboard:
    name, model, provider, dir (run folder), log, events, worktree, pid, brief, started (epoch)."""
    key = register_project(project)
    rec = {"id": agent_id, "kind": kind, "project": key, "started": time.time(), **fields}
    old = _read(home() / "agents" / f"{_safe(agent_id)}.json", {}) or {}
    rec = {**old, **rec}
    if old.get("started"):  # a resumed run keeps its first start time
        rec["started"] = old["started"]
    _write(home() / "agents" / f"{_safe(agent_id)}.json", rec)
    return rec


def update_agent(agent_id: str, **fields) -> None:
    path = home() / "agents" / f"{_safe(agent_id)}.json"
    rec = _read(path)
    if rec is None:
        return
    rec.update(fields)
    _write(path, rec)


def agents(project: str | None = None) -> list[dict]:
    out = []
    for f in (home() / "agents").glob("*.json"):
        rec = _read(f)
        if isinstance(rec, dict) and (project is None or rec.get("project") == project):
            out.append(rec)
    return sorted(out, key=lambda r: r.get("started") or 0, reverse=True)


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:120]


# ---------- CLI hooks (never raise, skipped with AGENTCONTROLLER_NO_DASH=1) ----------

def hook_start(root: str | Path, agent_id: str, kind: str, **fields) -> None:
    """Register the project and the agent, and make sure the dashboard is up. `parent` defaults to
    $AGENTCONTROLLER_PARENT (the run that spawned this one)."""
    if os.environ.get("AGENTCONTROLLER_NO_DASH"):
        return
    try:
        parent = fields.pop("parent", None) or os.environ.get("AGENTCONTROLLER_PARENT") or None
        register_agent(agent_id, kind, root, provider=fields.pop("provider", "anthropic"), pid=os.getpid(),
                       status="running", **({"parent": parent} if parent else {}),
                       **{k: v for k, v in fields.items() if v is not None})
        ensure_dash()
    except Exception:  # noqa: BLE001  (a dashboard problem must never break the CLI)
        pass


def hook_end(agent_id: str, status: str = "done") -> None:
    if os.environ.get("AGENTCONTROLLER_NO_DASH"):
        return
    try:
        update_agent(agent_id, status=status, ended=time.time(), pid=None)
    except Exception:  # noqa: BLE001
        pass


# ---------- the always-on dashboard ----------

def dash_port() -> int:
    try:
        return int(os.environ.get("AGENTDASH_PORT") or (_read(home() / "dash.json", {}) or {}).get("port") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


TASK_NAME = "AgentController dashboard"


def dash_up(port: int | None = None, timeout: float = 0.6) -> bool:
    """True only if the unified dashboard (not any other server, nor a per-project agentdash) answers."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port or dash_port()}/api/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("mode") == "all"
    except (OSError, ValueError):
        return False


def ensure_dash(port: int | None = None) -> bool:
    """Start the unified dashboard in the background, without a window, if nothing answers on its port.
    Never raises; returns whether a dashboard is (now) answering. AGENTCONTROLLER_NO_DASH=1 disables it."""
    if os.environ.get("AGENTCONTROLLER_NO_DASH"):
        return False
    port = port or dash_port()
    if dash_up(port):
        return True
    if os.name == "nt":  # the logon task owns the process when it is installed (install.py --dash-task)
        try:
            if subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, timeout=10).returncode == 0:
                for _ in range(30):
                    if dash_up(port):
                        return True
                    time.sleep(0.2)
        except (OSError, subprocess.SubprocessError):
            pass
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe") and Path(exe[:-10] + "pythonw.exe").exists():
        exe = exe[:-10] + "pythonw.exe"  # no console window, ever
    cmd = [exe, "-m", "agentcontroller.dash", "--all", "--port", str(port)]
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000200 | 0x00000008  # NO_WINDOW | NEW_PROCESS_GROUP | DETACHED
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kw)
    except OSError:
        return False
    for _ in range(20):
        if dash_up(port):
            return True
        time.sleep(0.15)
    return False
