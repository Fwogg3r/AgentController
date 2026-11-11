"""The unified view behind `agentdash --all`: every project, every agent, one dashboard.

Sources (all read-only here):
  - registry.py: projects and agents registered by agentctl / agentask / agentpilot / agentlocal
  - ~/.claude/projects/<slug>/: Claude Code sessions, mapped to the repo they ran in (worktrees included)
  - <project>/agentcontroller.toml: that project's API runs and usage log (through config.load)
  - <project>/docs/roadmap: the roadmap, via the `roadmap` CLI (WebRoadmap) when it is installed
  - the global config (~/.agentcontroller/config.toml): pmjc URL + token file, LM Studio hosts

Global config, all keys optional:

    include = "Z:/shared/agentcontroller/dash.toml"   # merged first; this file's keys win
    [dash]
    port = 8765
    scan = ["~/Documents/GitHub"]       # also list repos here that have an agentcontroller.toml
    hide = ["C:/some/repo"]             # never list these projects
    [pmjc]
    url = "http://127.0.0.1:8770"
    token_file = "Z:/secrets/pmjc/agentdash.token"   # a pmjc API token with the jobs (+ admin for approvals) scope
    [lmstudio.hosts]
    local = "http://127.0.0.1:1234"
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import claude_sessions, registry

CLAUDE_BASE = Path.home() / ".claude" / "projects"
_lock = threading.Lock()


# ---------- global config ----------

def _load_toml(p: Path) -> dict:
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_gcfg: tuple[float, dict] = (0.0, {})


def gconfig() -> dict:
    """~/.agentcontroller/config.toml (+ its `include`), re-read at most every 10 s."""
    global _gcfg
    if time.time() - _gcfg[0] < 10:
        return _gcfg[1]
    own = _load_toml(registry.home() / "config.toml")
    inc = own.get("include")
    base = _load_toml(Path(os.path.expandvars(inc)).expanduser()) if inc else {}
    _gcfg = (time.time(), _merge(base, own))
    return _gcfg[1]


def _path(v: str) -> Path:
    return Path(os.path.expandvars(v)).expanduser()


# ---------- Claude Code session dirs -> projects ----------

_slug_cwd: dict[str, str | None] = {}
_main_of: dict[str, str] = {}


def _cwd_of_slug(d: Path) -> str | None:
    """The cwd recorded in the newest transcript of a ~/.claude/projects/<slug> dir (read from its head)."""
    if d.name in _slug_cwd:
        return _slug_cwd[d.name]
    cwd = None
    try:
        files = sorted((f for f in d.glob("*.jsonl")), key=lambda f: -f.stat().st_mtime)[:3]
    except OSError:
        files = []
    for f in files:
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for _ in range(40):
                    line = fh.readline()
                    if not line:
                        break
                    m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', line)
                    if m:
                        cwd = json.loads(f'"{m.group(1)}"')
                        break
        except OSError:
            continue
        if cwd:
            break
    if files:  # only cache a real answer or a dir that has transcripts (a new dir may get one soon)
        _slug_cwd[d.name] = cwd
    return cwd


def _main(path: str) -> str:
    """registry key of the main checkout of `path` (cached: git is slow on Windows)."""
    if path not in _main_of:
        p = Path(path)
        # A worktree that has been deleted still maps to its repo by the .claude/worktrees convention.
        if not p.exists():
            parts = re.split(r"[\\/]\.claude[\\/]worktrees[\\/]", path)
            p = Path(parts[0]) if len(parts) > 1 else p
        _main_of[path] = registry.norm(registry.main_checkout(p) if p.exists() else p)
    return _main_of[path]


_claude_cache: tuple[float, dict] = (0.0, {})


def claude_dirs(max_age: float = 30 * 86400) -> dict[str, list[Path]]:
    """{project key: [~/.claude/projects/<slug> dirs]} for dirs touched within max_age; cached 20 s."""
    global _claude_cache
    if time.time() - _claude_cache[0] < 20:
        return _claude_cache[1]
    out: dict[str, list[Path]] = {}
    now = time.time()
    try:
        dirs = [d for d in CLAUDE_BASE.iterdir() if d.is_dir()]
    except OSError:
        dirs = []
    for d in dirs:
        try:
            if now - d.stat().st_mtime > max_age and not any(now - f.stat().st_mtime < max_age for f in d.glob("*.jsonl")):
                continue
        except OSError:
            continue
        cwd = _cwd_of_slug(d)
        if not cwd:
            continue
        out.setdefault(_main(cwd), []).append(d)
    _claude_cache = (time.time(), out)
    return out


# ---------- projects ----------

def _scan_tomls() -> dict[str, str]:
    found = {}
    for root in gconfig().get("dash", {}).get("scan", []):
        try:
            for d in _path(root).iterdir():
                if (d / "agentcontroller.toml").is_file():
                    found[registry.norm(d)] = str(d)
        except OSError:
            pass
    return found


def _hidden_file() -> Path:
    return registry.home() / "hidden.json"


def hidden() -> set[str]:
    try:
        v = json.loads(_hidden_file().read_text(encoding="utf-8"))
        return {str(x) for x in v} if isinstance(v, list) else set()
    except (OSError, ValueError):
        return set()


def hide_project(key: str) -> None:
    """Forget a project for good: drop its registry entry and keep scans from listing it again."""
    h = hidden() | {key}
    f = _hidden_file()
    tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(sorted(h)), encoding="utf-8")
    os.replace(tmp, f)
    registry.forget_project(key)


def projects() -> dict[str, dict]:
    """{key: {"key", "name", "path", "toml", "sources"}}: registry + Claude Code dirs + scanned tomls."""
    hide = {registry.norm(_path(h)) for h in gconfig().get("dash", {}).get("hide", [])} | hidden()
    out: dict[str, dict] = {}

    def add(key: str, path: str, src: str, name: str | None = None):
        if key in hide:
            return
        e = out.setdefault(key, {"key": key, "path": path, "name": name or Path(path).name, "sources": []})
        if src not in e["sources"]:
            e["sources"].append(src)

    for key, p in registry.projects().items():
        add(key, p.get("path") or key, "registry", p.get("name"))
    for key, p in _scan_tomls().items():
        add(key, p, "scan")
    for key, dirs in claude_dirs().items():
        add(key, key, "claude")
    for e in out.values():
        e["toml"] = (Path(e["path"]) / "agentcontroller.toml").is_file()
        e["roadmap"] = (Path(e["path"]) / "docs" / "roadmap" / "roadmap.md").is_file()
        e["exists"] = Path(e["path"]).is_dir()
    return out


# ---------- local (and other registered) agents ----------

def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else None


def _read_events(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def pending_approvals(run_dir: Path) -> list[dict]:
    out = []
    try:
        for f in sorted((run_dir / "approvals").glob("*.pending.json")):
            aid = f.name[: -len(".pending.json")]
            if (run_dir / "approvals" / f"{aid}.json").exists():
                continue  # decided, the runner hasn't picked it up yet
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return out


def ac_dir(v) -> Path | None:
    """A run dir from a registry record, only if its path has a `.agentcontroller` segment (never serve other paths)."""
    if not v:
        return None
    p = Path(os.path.normpath(str(v)))
    return p if ".agentcontroller" in p.parts and ".." not in p.parts else None


def local_info(rec: dict) -> dict:
    """A registered local (agentlocal) run in the same shape as dash.run_info, plus local-only fields."""
    ad = ac_dir(rec.get("dir"))
    rec = {**rec, "dir": str(ad) if ad else None}
    d = ad or Path("")
    evs = _read_events(d / "events.jsonl") if ad else []
    meta = {}
    try:
        meta = {} if not ad else json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    plan = {}
    try:
        plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    prog = {}
    try:
        prog = json.loads((d / "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    mtimes = [p.stat().st_mtime for p in (d / "events.jsonl", d / "thinking.txt", d / "answer.txt") if ad and p.exists()]
    last = max(mtimes, default=rec.get("started") or 0)
    idle = time.time() - last
    alive = _pid_alive(rec.get("pid"))
    status = rec.get("status") or "running"
    calls = [e for e in evs if e.get("kind") == "call"]
    last_call = calls[-1] if calls else {}
    ctx = rec.get("context_tokens") or last_call.get("context_tokens")
    tok_in = sum(int(e.get("tokens_in") or 0) for e in calls)
    tok_out = sum(int(e.get("tokens_out") or 0) for e in calls)
    approvals = pending_approvals(d) if rec.get("dir") else []
    lastev = evs[-1] if evs else {}
    kinds = {e.get("kind") for e in evs}
    ends = [e for e in evs if e.get("kind") in ("done", "stopped")]
    if ends:  # the run's own events decide, whatever the registry says
        e = ends[-1]
        state = "finished"
        if e["kind"] == "stopped":
            outcome = "stopped"
        else:
            outcome = "success" if e.get("ok", True) and not e.get("aider_exit") else "error"
    elif status in ("done", "failed", "stopped"):
        state = "finished"
        outcome = {"done": "success", "failed": "error", "stopped": "stopped"}[status]
    elif rec.get("pid"):
        state, outcome = (("waiting" if approvals else "working") if alive else "crashed"), None
    elif idle < (3600 if approvals else 180):
        state, outcome = ("waiting" if approvals else "working"), None
    else:
        state, outcome = "crashed", None
    active = state in ("working", "waiting")
    if approvals:
        act = {"kind": "approval", "text": f"waiting for approval: {approvals[0].get('summary', '')}"[:200]}
    elif lastev.get("kind") == "call_start":
        act = {"kind": "thinking", "text": f"{lastev.get('phase', '')} call, stage {lastev.get('stage')}"}
    else:
        act = {"kind": "thinking", "text": f"{lastev.get('kind', '')} {json.dumps({k: v for k, v in lastev.items() if k not in ('ts', 'run_id', 'kind')})[:160]}"}
    act["since"] = _iso(lastev.get("ts")) if lastev.get("ts") else None
    if lastev.get("ts"):
        act["age"] = max(0, round(time.time() - float(lastev["ts"])))
    started = rec.get("started") or meta.get("started")
    stages = plan.get("stages") or []
    return {
        "name": rec.get("name") or rec["id"], "id": rec["id"], "kind": "local", "local": True, "plan": False,
        "provider": rec.get("provider") or "lmstudio", "model": rec.get("model") or meta.get("model"),
        "model_id": rec.get("model_id"), "tier": rec.get("tier") or meta.get("tier"),
        "title": rec.get("title") or (meta.get("idea") or "").split("\n")[0][:80] or rec["id"],
        "cost": 0.0, "turns": len(calls), "tokens_in": tok_in, "tokens_out": tok_out,
        "context_tokens": ctx, "context_pct": last_call.get("context_pct"),
        "context_history": [{"stage": e.get("stage"), "phase": e.get("phase"), "in": e.get("tokens_in"),
                             "out": e.get("tokens_out"), "pct": e.get("context_pct")} for e in calls[-60:]],
        "stages_total": len(stages), "stages_done": len(prog.get("done") or []),
        "approvals": approvals, "dir": str(d) if rec.get("dir") else None,
        "worktree": rec.get("worktree"), "branch": None, "commits": [], "dirty": None,
        "state": state, "outcome": outcome, "active": active, "activity": act if active else None,
        "started": _iso(started), "last_activity": _iso(last), "idle_seconds": round(idle),
        "pid_alive": alive, "oneshot": False, "legacy": False, "budget": None, "job": rec.get("job"),
        "parent": rec.get("parent"), "roadmap": rec.get("roadmap"),
        **claude_sessions.duration_fields(_iso(started), _iso(last), active, time.time()),
    }


def registered_info(rec: dict) -> dict | None:
    """Agents of other kinds that only exist in the registry (kind 'other': e.g. a script calling another API)."""
    if rec.get("kind") == "local":
        return local_info(rec)
    if rec.get("kind") != "other":
        return None  # api/oneshot/pilot runs are read from their project's runs dir (richer events)
    alive = _pid_alive(rec.get("pid"))
    status = rec.get("status") or ("running" if alive else "done")
    active = status == "running" and (alive or not rec.get("pid"))
    started, ended = rec.get("started"), rec.get("ended")
    return {"name": rec.get("name") or rec["id"], "id": rec["id"], "kind": "other", "plan": False,
            "provider": rec.get("provider"), "model": rec.get("model"), "title": rec.get("title") or rec["id"],
            "cost": rec.get("cost") or 0.0, "turns": rec.get("turns") or 0,
            "state": "working" if active else ("crashed" if status == "running" else "finished"),
            "outcome": None if active else ("success" if status == "done" else status), "active": active,
            "activity": {"kind": "thinking", "text": rec.get("activity") or ""} if active else None,
            "started": _iso(started), "last_activity": _iso(ended or started),
            "idle_seconds": round(time.time() - (ended or started or time.time())), "pid_alive": alive,
            "oneshot": False, "legacy": False, "commits": [], "job": rec.get("job"), "log": rec.get("log"),
            "parent": rec.get("parent"), "roadmap": rec.get("roadmap"),
            **claude_sessions.duration_fields(_iso(started), _iso(ended), active, time.time())}


def discovered_local(key: str, known_dirs: set[str]) -> list[dict]:
    """agentlocal runs that never got registered: <project>/.agentcontroller/runs/* and the same in its
    .claude/worktrees/*. Treated like registry records of kind local without a pid."""
    base = Path((registry.projects().get(key) or {}).get("path") or key)
    roots = [base / ".agentcontroller" / "runs"]
    try:
        roots += sorted((base / ".claude" / "worktrees").glob("*/.agentcontroller/runs"))
    except OSError:
        pass
    out = []
    for r in roots:
        try:
            dirs = [d for d in r.iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in dirs:
            if registry.norm(d) in known_dirs or not (d / "events.jsonl").exists():
                continue
            meta = {}
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            try:
                started = meta.get("started") or (d / "events.jsonl").stat().st_ctime
            except OSError:
                started = None
            out.append({"id": d.name, "name": d.name, "kind": "local", "project": key, "dir": str(d), "started": started,
                        "model": meta.get("model"), "tier": meta.get("tier"), "worktree": str(d.parent.parent.parent),
                        "discovered": True})
    return out


def local_records(key: str) -> list[dict]:
    recs = registry.agents(key)
    known = {registry.norm(r["dir"]) for r in recs if r.get("dir")}
    return recs + discovered_local(key, known)


def registered_runs(key: str) -> list[dict]:
    out = []
    for rec in local_records(key):
        try:
            info = registered_info(rec)
        except (OSError, ValueError, KeyError):
            info = None
        if info:
            info["project"] = key
            out.append(info)
    return out


def find_agent(key: str, name: str) -> dict | None:
    return next((r for r in local_records(key) if (r.get("name") or r.get("id")) == name or r.get("id") == name), None)


def local_dir(key: str, name: str) -> Path | None:
    """The run folder of a local run, only under a `.agentcontroller` segment."""
    rec = find_agent(key, name)
    return ac_dir(rec.get("dir")) if rec and rec.get("kind") == "local" else None


def decide_local(key: str, name: str, aid: str, approve: bool) -> tuple[int, dict]:
    rec = find_agent(key, name)
    d = ac_dir(rec.get("dir")) if rec else None
    if not rec or rec.get("kind") != "local" or not d:
        return 404, {"ok": False, "error": "no such local run"}
    if not re.fullmatch(r"[\w.-]{1,80}", aid):
        return 400, {"ok": False, "error": "bad approval id"}
    adir = d / "approvals"
    if not (adir / f"{aid}.pending.json").exists():
        return 404, {"ok": False, "error": "no such pending approval"}
    tmp = adir / f"{aid}.json.tmp"
    tmp.write_text(json.dumps({"approve": bool(approve), "by": "agentdash", "ts": time.time()}), encoding="utf-8")
    os.replace(tmp, adir / f"{aid}.json")
    return 200, {"ok": True}


# ---------- roadmap ----------

def roadmap(path: str) -> dict:
    """The project's docs/roadmap as JSON (WebRoadmap's own model through its CLI) plus each item's body."""
    rdir = Path(path) / "docs" / "roadmap"
    if not (rdir / "roadmap.md").is_file():
        return {"ok": False, "error": "this project has no docs/roadmap (create one with `roadmap init`)"}
    exe = shutil.which("roadmap")
    if not exe:
        return {"ok": False, "error": "the `roadmap` CLI isn't installed: uv tool install git+https://github.com/Fwogg3r/WebRoadmap"}
    try:
        p = subprocess.run([exe, "--dir", str(rdir), "--json", "list"], capture_output=True, text=True,
                           encoding="utf-8", timeout=20)
        data = json.loads(p.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"ok": False, "error": f"roadmap list failed: {e}"}

    def body(rel: str, fname: str) -> str:
        try:
            txt = (rdir / rel / fname).read_text(encoding="utf-8")
        except OSError:
            return ""
        return txt.split("---", 2)[2].strip() if txt.startswith("---") and txt.count("---") >= 2 else txt

    def walk(items: list[dict]):
        for it in items:
            if it.get("id") and "body" not in it:
                rel = it["id"]
                it["body"] = body(rel, "item.md") or body(rel, "group.md")
            for k in ("items", "children"):
                if isinstance(it.get(k), list):
                    walk(it[k])

    for ph in data.get("phases", []):
        walk(ph.get("items", []))
        for g in ph.get("groups", []) or []:
            walk([g])
    data["ok"] = True
    return data


# ---------- pmjc (jobs, watches, approvals) and LM Studio ----------

def _http_json(url: str, token: str | None = None, timeout: float = 4.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (configured local URL)
        return json.loads(r.read().decode("utf-8"))


_pmjc_cache: tuple[float, dict] = (0.0, {})


def pmjc() -> dict:
    """Jobs, watches and pending approvals from pmjc's HTTP API (cached 10 s). Never raises."""
    global _pmjc_cache
    if time.time() - _pmjc_cache[0] < 10:
        return _pmjc_cache[1]
    c = gconfig().get("pmjc", {})
    url = (c.get("url") or "http://127.0.0.1:8770").rstrip("/")
    token = None
    if c.get("token_file"):
        try:
            token = _path(c["token_file"]).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    out: dict = {"url": url, "configured": bool(token), "ok": False}
    if not token:
        out["error"] = ("no pmjc token: create one in pmjc's dashboard (Tokens: scopes jobs, plus admin to list "
                        "approvals) and set [pmjc] token_file in ~/.agentcontroller/config.toml")
    else:
        for key, path in (("jobs", "/api/jobs"), ("watches", "/api/watches?all=true"), ("approvals", "/api/approvals"),
                          ("runs", "/api/runs")):
            try:
                out[key] = _http_json(url + path, token)
                out["ok"] = True
            except urllib.error.HTTPError as e:
                out[key + "_error"] = f"HTTP {e.code}" + (" (token lacks the scope)" if e.code in (401, 403) else "")
            except (OSError, ValueError) as e:
                out[key + "_error"] = str(e)[:200]
        if not out["ok"]:
            out["error"] = out.get("jobs_error") or "pmjc unreachable"
    # Which jobs have an agent: the job's own model preference, and registry agents tagged with job=<name>.
    tagged: dict[str, list[str]] = {}
    for rec in registry.agents():
        if rec.get("job"):
            tagged.setdefault(rec["job"], []).append(rec.get("name") or rec["id"])
    for j in out.get("jobs") or []:
        if isinstance(j, dict):
            j["agents"] = tagged.get(j.get("name"), [])
    _pmjc_cache = (time.time(), out)
    return out


_lms_cache: tuple[float, dict] = (0.0, {})


def lmstudio() -> dict:
    """{host: {"url", "ok", "models": [{"id", "state", "type", "max_context_length", ...}]}} (cached 15 s)."""
    global _lms_cache
    if time.time() - _lms_cache[0] < 15:
        return _lms_cache[1]
    hosts = gconfig().get("lmstudio", {}).get("hosts") or {"local": "http://127.0.0.1:1234"}
    out = {}
    for name, url in hosts.items():
        try:
            data = _http_json(url.rstrip("/") + "/api/v0/models", timeout=2.5).get("data", [])
            out[name] = {"url": url, "ok": True,
                         "models": [{k: m.get(k) for k in ("id", "state", "type", "arch", "quantization",
                                                         "max_context_length", "loaded_context_length")}
                                    for m in data if m.get("type") in ("llm", "vlm", None)]}
        except (OSError, ValueError) as e:
            out[name] = {"url": url, "ok": False, "error": str(e)[:160]}
    _lms_cache = (time.time(), out)
    return out
