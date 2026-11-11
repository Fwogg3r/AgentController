"""agentdash: localhost dashboard for every agent in every project (127.0.0.1 only).

    agentdash [--all | --project PATH] [--port 8765] [--session-start 2026-01-31T09:00:00]

--all serves every project (registry + Claude Code sessions, see hub.py) and is the default when no
agentcontroller.toml is found from the cwd; --project pins one project (same code path, `p` optional).
The project's config is per request (ContextVar behind `CFG`). Projects without a toml are read-only.

Reads <runs>/<name>.events.jsonl (falls back to <name>.log for older runs) and the usage log.
Writes only inbox/pid/archive files, briefs, and launches/stops agents. Never reads or serves key files.
"""

import argparse
import codecs
import contextlib
import contextvars
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import claude_sessions, hub, registry
from .config import CONFIG_NAME, Config, load

PAGE = Path(__file__).resolve().parent / "static" / "index.html"
PKG_PARENT = str(Path(__file__).resolve().parent.parent)
NAME_RE = re.compile(r"^[\w.-]+$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SESSION_RE = re.compile(r"^[\w-]{6,80}$")
PROTECTED = {"main", "master", "develop", "trunk"}
# A run without a PID file (from before the runner wrote one) that has been quiet this long is over.
STALL_SECONDS = 180
OUTCOMES = {"success": "success", "error_max_turns": "max_turns", "error_max_budget_usd": "budget",
            "max_tokens": "max_tokens", "refusal": "refusal"}

_CUR: contextvars.ContextVar = contextvars.ContextVar("agentdash_cfg", default=None)


class _CfgProxy:
    """`CFG.x` reads the config of the project the current request (thread/context) selected."""

    def __getattr__(self, name):
        c = _CUR.get()
        if c is None:
            raise RuntimeError("no project selected for this request")
        return getattr(c, name)


CFG: Config = _CfgProxy()  # type: ignore[assignment]
ALL_MODE = True
PINNED: str | None = None  # project key when started with --project
SESSION_START = ""  # ISO timestamp, set in main()
# --remote: requests from any host are accepted with this token (query ?token= once, then a cookie).
TOKEN: str | None = None


# ---------- files ----------
def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for l in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def usage_records() -> list[dict]:
    return read_jsonl(CFG.usage_log)


def git(wt: str | Path, *args: str, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout if p.returncode == 0 else p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


# ---------- processes ----------
def pid_file(name: str) -> Path:
    return CFG.runs / f"{name}.pid"


def read_pid(name: str) -> int | None:
    try:
        return int(pid_file(name).read_text().strip())
    except (OSError, ValueError):
        return None


_pid_cache: dict[int, tuple[float, bool]] = {}


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":  # tasklist is slow: remember the answer for 3 s (many projects x many runs)
        hit = _pid_cache.get(pid)
        if hit and time.time() - hit[0] < 3:
            return hit[1]
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        _pid_cache[pid] = (time.time(), str(pid) in out.split())
        return _pid_cache[pid][1]
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)


# ---------- runs ----------
def run_names() -> list[str]:
    names = set()
    if CFG.runs.exists():
        for p in CFG.runs.iterdir():
            if p.name.endswith(".events.jsonl"):
                names.add(p.name[: -len(".events.jsonl")])
            elif p.suffix == ".log":
                names.add(p.stem)
    return sorted(n for n in names if NAME_RE.match(n))


def last_activity(name: str) -> float:
    ts = [p.stat().st_mtime for p in (CFG.runs / f"{name}.events.jsonl", CFG.runs / f"{name}.log") if p.exists()]
    return max(ts, default=0.0)


def log_tail(name: str) -> str:
    """Last non-empty line of <name>.log (read from the end: logs can be large)."""
    p = CFG.runs / f"{name}.log"
    try:
        with p.open("rb") as fh:
            fh.seek(max(0, p.stat().st_size - 4096))
            lines = [l for l in fh.read().decode("utf-8", errors="replace").splitlines() if l.strip()]
        return lines[-1][:300] if lines else ""
    except OSError:
        return ""


def event_state(events: list[dict]) -> dict:
    """What the last session (since the last `start`) was doing where its events stop.

    Older event files have no tool ids; their tools pair with results in order.
    """
    fresh = lambda: {"tools": {}, "tasks": {}, "result": None, "ended": False, "killed": False, "progress": None,
                     "last": None, "retry": None, "rate_limit": None, "pending_user": 0, "task_closed_at": None}
    st, anon = fresh(), 0
    for e in events:
        k = e.get("kind")
        if k == "start":
            st = fresh()
        elif k == "tool":
            tid = e.get("id")
            if not tid:
                tid, anon = f"_{anon}", anon + 1
            st["tools"][tid] = e
        elif k == "tool_result":
            tid = e.get("id") or next((t for t in st["tools"] if t.startswith("_")), None)
            st["tools"].pop(tid, None)
        elif k == "task":
            if e.get("status") == "started":
                st["tasks"][e.get("id")] = e
            elif st["tasks"].pop(e.get("id"), None) is not None and not st["tasks"]:
                st["task_closed_at"] = e.get("ts")
        elif k == "result":
            st["result"] = e.get("subtype")
            st["tools"] = {}  # a finished turn has nothing in flight (a cancelled call never gets a result)
            st["pending_user"] = max(0, st["pending_user"] - 1)
        elif k == "user":
            st["pending_user"] += 1
        elif k == "end":
            st["ended"], st["killed"] = True, bool(e.get("killed"))
        if k == "progress":
            st["progress"] = e
        elif k == "retry":
            st["retry"] = e
        elif k == "rate_limit":
            st["rate_limit"] = e if e.get("status") == "rejected" else None
        elif k not in ("session", "task", "cost"):  # task and cost events arrive between turns
            st["last"] = e
            if k in ("tool", "tool_result", "text", "result", "user"):
                st["progress"] = st["retry"] = None
    return st


def outcome_of(subtype: str | None) -> str:
    return OUTCOMES.get(subtype or "", "error")


def describe(e: dict | None) -> str:
    if not e:
        return ""
    if e.get("kind") == "tool":
        return f"{e.get('name')}: {e.get('arg', '')}"[:200]
    if e.get("kind") == "result":
        return f"result {e.get('subtype')}"
    return f"{e.get('kind')}: {str(e.get('text') or '')[:200]}".strip()


def derive_state(name: str, events: list[dict], alive: bool, has_pid: bool, idle: float) -> dict:
    """state: working | waiting | finished | crashed | ended, with `outcome` or `activity` (kind, text, since).

    `status` (running | finished | dead | ended) is kept for older clients and scripts.
    """
    def out(state: str, status: str, outcome: str | None = None, **activity) -> dict:
        return {"state": state, "status": status, "outcome": outcome, "activity": activity or None}

    if not events:  # legacy run: only a .log (and a .json once it finished)
        res = CFG.runs / f"{name}.json"
        if res.exists():
            try:
                sub = json.loads(res.read_text(encoding="utf-8")).get("subtype")
            except (OSError, ValueError, AttributeError):
                sub = None
            return out("finished", "finished", outcome_of(sub) if sub else "success")
        if alive or idle < STALL_SECONDS:
            return out("working", "running", kind="thinking", text="", since=None)
        return out("ended", "ended", "no_record", kind="ended", text=log_tail(name) or "no result file, no events log")

    st = event_state(events)
    if st["ended"]:
        return out("finished", "finished", "stopped" if st["killed"] else outcome_of(st["result"]))
    last = st["last"] or {}
    if not (alive if has_pid else idle < STALL_SECONDS):
        return out("crashed", "dead", kind="crashed", text=describe(st["last"]), log=log_tail(name),
                   since=last.get("ts"))
    prog = st["progress"] or {}
    tokens = prog.get("tokens")
    if st["rate_limit"]:
        rl = st["rate_limit"]
        return out("waiting", "running", kind="rate_limit", text=f"rate limited ({rl.get('type') or 'API'})",
                   since=rl.get("ts"), resets_at=rl.get("resets_at"))
    if st["retry"]:
        r = st["retry"]
        return out("waiting", "running", kind="retry", since=r.get("ts"), delay_ms=r.get("delay_ms"),
                   text=f"API retry {r.get('attempt')}/{r.get('max_retries')}: {r.get('error') or r.get('error_status')}")
    if st["tools"]:
        tools = list(st["tools"].values())
        t = tools[0]
        more = f" (+{len(tools) - 1} more)" if len(tools) > 1 else ""
        return out("working", "running", kind="tool", tool=t.get("name"), text=describe(t) + more,
                   since=t.get("ts"), tokens=tokens)
    if last.get("kind") == "result" and not st["pending_user"]:
        if st["tasks"]:
            descs = "; ".join(str(t.get("text") or "task") for t in st["tasks"].values())
            return out("waiting", "running", kind="task", text=f"background task: {descs}"[:220],
                       since=min(t.get("ts") or "~" for t in st["tasks"].values()))
        if st["task_closed_at"]:
            return out("waiting", "running", kind="notification", since=st["task_closed_at"],
                       text="background task done: waiting for its notification turn")
        return out("waiting", "running", kind="inbox", since=last.get("ts"),
                   text="turn done: waiting briefly for a message before ending")
    if last.get("kind") == "user":  # a message the model hasn't started answering yet
        return out("working", "running", kind="user", text=str(last.get("text") or "")[:200], since=last.get("ts"))
    # A model turn is in progress: after a tool result, the start or a text block.
    phase = prog.get("phase")
    text = prog.get("excerpt") or (f"writing {prog['tool']} call" if prog.get("tool") else "")
    return out("working", "running", kind="writing" if phase in ("text", "tool_input") else "thinking",
               text=text, since=last.get("ts") or prog.get("ts"), tokens=tokens, phase=phase)


def run_info(name: str, usage: list[dict], git_info: bool = True) -> dict:
    events = read_jsonl(CFG.runs / f"{name}.events.jsonl")
    info = {"name": name, "model": None, "cost": 0.0, "turns": 0, "worktree": None, "branch": None, "base": None,
            "budget": None, "legacy": not events, "commits": [], "dirty": None, "oneshot": False}
    activity = last_activity(name)
    idle = time.time() - activity
    has_pid = pid_file(name).exists()
    alive = pid_alive(read_pid(name)) if has_pid else False
    if events:
        for e in events:
            if e["kind"] == "start":
                info.update(model=e.get("model"), worktree=e.get("worktree"), branch=e.get("branch"),
                            base=e.get("base"), budget=e.get("budget"))
                info["ended"] = False  # a resumed run appends a new `start`
                if e.get("oneshot"):  # agentask: one Messages API call, no worktree
                    info.update(oneshot=True, effort=e.get("effort"), backfilled=bool(e.get("backfilled")))
            elif e["kind"] == "result":
                info["cost"], info["turns"] = e.get("cost", 0.0), e.get("turns", 0)
                if info["oneshot"]:
                    info.update(tokens_in=e.get("input_tokens"), tokens_out=e.get("output_tokens"), seconds=e.get("seconds"))
            elif e["kind"] == "cost":  # live estimate between results (runner.CostMeter)
                info["cost"], info["turns"] = e.get("usd", info["cost"]), e.get("turns", info["turns"])
            elif e["kind"] == "end":
                info["ended"] = True
        info["started"] = events[0].get("ts")
    else:
        rec = next((r for r in reversed(usage) if r.get("name") == name), None)
        if rec:
            info.update(model=rec.get("model"), cost=rec.get("usd", 0.0), turns=rec.get("turns") or 0)
        info["started"] = None
    # With a PID file (every run since the runner writes its own) the process state decides
    # live vs crashed; only runs without one fall back to file activity.
    info.update(derive_state(name, events, alive, has_pid, idle))
    if info["activity"] and info["activity"].get("since"):
        # Seconds, computed here so a browser in another time zone (--remote) shows the right elapsed time.
        try:
            t = time.mktime(time.strptime(info["activity"]["since"], "%Y-%m-%dT%H:%M:%S"))
            info["activity"]["age"] = max(0, round(time.time() - t))
        except ValueError:
            pass
    info["active"] = info["state"] in ("working", "waiting")
    # Time worked: start -> now while active, else start -> last write (or the one-shot's own timer).
    info.update(claude_sessions.duration_fields(
        info["started"], time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(activity)) if activity else None,
        info["active"], time.time()))
    if info["oneshot"] and not info["active"] and info.get("seconds") is not None:
        info["duration"] = round(info["seconds"])
    info["pid_alive"] = alive
    info["idle_seconds"] = round(idle)
    info["last_activity"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(activity)) if activity else None
    wt = info["worktree"] or str(CFG.worktrees / name)
    if git_info and Path(wt).exists():
        info["worktree"] = wt
        info["branch"] = info["branch"] or git(wt, "rev-parse", "--abbrev-ref", "HEAD")[1] or None
        info["commits"] = git(wt, "log", "-3", "--format=%h %s")[1].splitlines()
        info["dirty"] = len(git(wt, "status", "--porcelain")[1].splitlines())
    return info


def plan_dir() -> Path | None:
    return claude_sessions.project_dir(CFG.root, CFG.claude_projects_dir)


def active_runs(usage: list[dict]) -> list[dict]:
    return [r for r in (run_info(n, usage, git_info=False) for n in run_names()) if r["active"]]


def spend(runs: list[dict] | None = None) -> dict:
    usage = usage_records()
    total = sum(r.get("usd", 0.0) for r in usage)
    runs = runs if runs is not None else [run_info(n, usage, git_info=False) for n in run_names()]
    # Runs that haven't ended have not been logged yet, so add their live cost.
    live = sum(r["cost"] for r in runs if r["state"] != "finished" and not r["legacy"])
    session = sum(r.get("usd", 0.0) for r in usage if r.get("ts", "") >= SESSION_START) + live
    return {"total": round(total + live, 4), "logged": round(total, 4), "cap": CFG.cap, "session": round(session, 4),
            "session_start": SESSION_START, "per_run": {r["name"]: r["cost"] for r in runs}}


def headroom(usage: list[dict]) -> dict:
    """cap - logged spend - budgets of runs still active (their spend isn't logged until they end)."""
    logged = sum(r.get("usd", 0.0) for r in usage)
    reserved = sum(r.get("budget") or 0.0 for r in active_runs(usage))
    return {"cap": CFG.cap, "logged": round(logged, 4), "reserved": round(reserved, 4),
            "headroom": None if CFG.cap is None else round(CFG.cap - logged - reserved, 4)}


def branches() -> list[str]:
    rc, out = git(CFG.root, "branch", "--format=%(refname:short)")
    return [b for b in out.splitlines() if b.strip()] if rc == 0 else []


def briefs() -> list[str]:
    return sorted(p.name for p in CFG.briefs.glob("*.md")) if CFG.briefs.exists() else []


# ---------- actions (each returns (http_code, json)) ----------
def has_toml() -> bool:
    return (CFG.root / CONFIG_NAME).is_file()


def launch(body: dict) -> tuple[int, dict]:
    if not has_toml():
        return 400, {"ok": False, "error": f"{CFG.root.name} has no {CONFIG_NAME}: it is read-only here "
                     "(set it up with `uv run install.py --init PATH`)"}
    name = str(body.get("name", ""))
    if not SLUG_RE.match(name) or name in PROTECTED:
        return 400, {"ok": False, "error": "name must be a slug: a-z, 0-9, '-' (max 40), and not main/master"}
    usage = usage_records()
    if (CFG.runs / f"{name}.events.jsonl").exists() or (CFG.runs / f"{name}.log").exists():
        if run_info(name, usage, git_info=False)["active"]:
            return 409, {"ok": False, "error": f"'{name}' is already active"}
    base = str(body.get("base") or CFG.default_base)
    if base not in branches():
        return 400, {"ok": False, "error": f"unknown base branch '{base}'"}
    model = str(body.get("model") or CFG.default_model)
    if model not in CFG.models:
        return 400, {"ok": False, "error": f"unknown model '{model}'"}
    try:
        budget, turns = float(body.get("budget")), int(body.get("max_turns") or CFG.default_max_turns)
        assert budget > 0 and turns > 0
    except (TypeError, ValueError, AssertionError):
        return 400, {"ok": False, "error": "budget and max turns must be positive numbers"}
    resume = body.get("resume") or None
    if resume and not SESSION_RE.match(str(resume)):
        return 400, {"ok": False, "error": "bad session id"}
    h = headroom(usage)
    if h["headroom"] is not None and budget > h["headroom"]:
        return 409, {"ok": False, "error": f"budget ${budget:.2f} exceeds headroom ${h['headroom']:.2f} "
                     f"(cap ${h['cap']:.2f} - logged ${h['logged']:.2f} - active budgets ${h['reserved']:.2f})"}
    if body.get("brief_file"):
        brief = CFG.briefs / Path(str(body["brief_file"])).name  # basename only: no path traversal
        if not brief.is_file():
            return 400, {"ok": False, "error": "brief file not found"}
    elif str(body.get("brief_text", "")).strip():
        CFG.briefs.mkdir(parents=True, exist_ok=True)
        brief = CFG.briefs / f"{name}.md"
        brief.write_text(str(body["brief_text"]).strip() + "\n", encoding="utf-8")
    else:
        return 400, {"ok": False, "error": "need brief text or a brief file"}
    cmd = [*(CFG.runner_cmd or [sys.executable, "-m", "agentcontroller.runner"]), "--project", str(CFG.root),
           "--brief", str(brief), "--name", name, "--base", base, "--model", model, "--budget", str(budget),
           "--max-turns", str(turns), "--no-textlog"]
    if resume:
        cmd += ["--resume", str(resume)]
    for opt in ("parent", "roadmap"):
        if body.get(opt) and re.fullmatch(r"[\w./-]{1,120}", str(body[opt])):
            cmd += [f"--{opt}", str(body[opt])]
    CFG.runs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": PKG_PARENT + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "PYTHONIOENCODING": "utf-8"}
    kw: dict = {}
    if os.name == "nt":  # detached: no console, own process group, survives the dashboard
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kw["start_new_session"] = True
    with (CFG.runs / f"{name}.log").open("ab") as log:
        p = subprocess.Popen(cmd, cwd=CFG.root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             close_fds=True, **kw)
    pid_file(name).write_text(str(p.pid))
    return 200, {"ok": True, "pid": p.pid, "brief": str(brief)}


def stop_graceful(name: str) -> tuple[int, dict]:
    if not run_info(name, [], git_info=False)["active"]:
        return 409, {"ok": False, "error": "run is not running"}
    append_jsonl(CFG.runs / f"{name}.inbox.jsonl", {"control": "stop", "ts": now()})
    return 200, {"ok": True}


def kill(name: str) -> tuple[int, dict]:
    pid = read_pid(name)
    if not pid_alive(pid) or run_info(name, [], git_info=False).get("ended"):
        return 409, {"ok": False, "error": "no live PID recorded for this run (was it started from the dashboard?)"}
    kill_tree(pid)
    info = run_info(name, [], git_info=False)
    # A killed runner never logs its spend, so do it here to keep the budget honest.
    append_jsonl(CFG.usage_log, {"ts": now(), "model": info["model"], "kind": "agent", "name": name,
                                 "usd": round(info["cost"], 4), "turns": info["turns"], "subtype": "killed"})
    append_jsonl(CFG.runs / f"{name}.events.jsonl", {"ts": now(), "kind": "end", "killed": True})
    pid_file(name).unlink(missing_ok=True)
    return 200, {"ok": True, "logged_usd": info["cost"]}


def archive(name: str) -> tuple[int, dict]:
    if run_info(name, [], git_info=False)["active"]:
        return 409, {"ok": False, "error": "run is still active; stop or kill it first"}
    dest = CFG.runs / "archive"
    dest.mkdir(exist_ok=True)
    moved = 0
    for p in CFG.runs.glob(f"{name}.*"):
        if p.is_file():
            target = dest / p.name
            if target.exists():
                target = dest / f"{time.strftime('%Y%m%d-%H%M%S')}-{p.name}"
            shutil.move(str(p), str(target))
            moved += 1
    return 200, {"ok": True, "moved": moved}


def worktree_state(name: str) -> tuple[int, dict]:
    """Merged/unmerged status shown before the owner confirms a worktree delete."""
    info = run_info(name, [], git_info=True)
    wt, branch = info["worktree"], info["branch"]
    base = info["base"] or CFG.default_base
    if not wt or not branch or not Path(wt).exists():
        return 404, {"ok": False, "error": "no worktree for this run"}
    if branch in PROTECTED or Path(wt).resolve() == CFG.root.resolve():
        return 403, {"ok": False, "error": "refusing: protected branch or primary working tree"}
    if CFG.worktrees.resolve() not in Path(wt).resolve().parents:
        return 403, {"ok": False, "error": "worktree is outside the configured worktrees dir"}
    unmerged = git(CFG.root, "log", f"{base}..{branch}", "--format=%h %s")[1].splitlines()
    return 200, {"ok": True, "worktree": wt, "branch": branch, "base": base, "merged": not unmerged,
                 "unmerged": unmerged, "dirty": info["dirty"], "active": info["active"]}


def worktree_delete(name: str, discard: bool) -> tuple[int, dict]:
    code, st = worktree_state(name)
    if code != 200:
        return code, st
    if st["active"]:
        return 409, {"ok": False, "error": "run is still active"}
    if (not st["merged"] or st["dirty"]) and not discard:
        return 409, {"ok": False, "needs_discard": True, **st,
                     "error": "unmerged commits or uncommitted changes; confirm 'discard unmerged work'"}
    rc, out = git(CFG.root, "worktree", "remove", *(["--force"] if discard else []), st["worktree"])
    if rc != 0:
        return 500, {"ok": False, "error": out}
    rc, out2 = git(CFG.root, "branch", "-D" if discard else "-d", st["branch"])
    return 200, {"ok": rc == 0, "branch_error": None if rc == 0 else out2}


# ---------- projects, configs and rows (one dash serves many projects) ----------
PINNED_ENTRY: dict | None = None
_pm_cache: tuple[float, dict] = (0.0, {})
_cfgs: dict[str, tuple[float, Config]] = {}
_api_cache: dict = {}
_extra_cache: dict = {}
_ROWS_TTL = 4.0


@contextlib.contextmanager
def using(cfg: Config):
    t = _CUR.set(cfg)
    try:
        yield
    finally:
        _CUR.reset(t)


def projects_map() -> dict[str, dict]:
    """{key: entry} for every listed project (cached 5 s); only the pinned one with --project."""
    global _pm_cache
    if time.time() - _pm_cache[0] < 5 and _pm_cache[1]:
        return _pm_cache[1]
    if PINNED_ENTRY:
        m = {PINNED_ENTRY["key"]: {**PINNED_ENTRY, "roadmap": (Path(PINNED_ENTRY["path"]) / "docs/roadmap/roadmap.md").is_file()}}
    else:
        m = hub.projects()
    _pm_cache = (time.time(), m)
    return m


def cfg_for(key: str) -> Config | None:
    """The project's Config (reloaded when its toml changes); None for unknown keys or vanished directories."""
    e = projects_map().get(key)
    if not e or not Path(e["path"]).is_dir():
        return None
    toml = Path(e["path"]) / CONFIG_NAME
    try:
        mt = toml.stat().st_mtime if toml.is_file() else 0.0
    except OSError:
        mt = 0.0
    hit = _cfgs.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    cfg = load(Path(e["path"]))
    _cfgs[key] = (mt, cfg)
    return cfg


def _fin(rows: list[dict], key: str, name: str) -> list[dict]:
    for r in rows:
        r["project"], r["project_name"] = key, name
        r.setdefault("parent", None)
        if not r.get("provider"):
            r["provider"] = "plan" if r.get("kind") in ("plan", "plan-agent") else "anthropic"
    return rows


def api_part(key: str, git: bool) -> tuple[list[dict], dict | None]:
    """(API runs of the project's own runs dir, spend dict or None without a toml); cached 4 s."""
    hit = _api_cache.get((key, git))
    if hit and time.time() - hit[0] < _ROWS_TTL:
        return hit[1], hit[2]
    cfg = cfg_for(key)
    rows: list[dict] = []
    sp = None
    if cfg is not None:
        with using(cfg):
            usage = usage_records()
            rows = [run_info(n, usage, git_info=git) for n in run_names()]
            if has_toml():
                sp = spend(rows)
        recs = {(r.get("name") or r.get("id")): r for r in registry.agents(key) if r.get("kind") in ("api", "oneshot", "pilot")}
        for r in rows:
            rec = recs.get(r["name"]) or {}
            r["parent"], r["roadmap"] = rec.get("parent"), rec.get("roadmap")
            if rec.get("provider") and rec["provider"] != "anthropic":
                r["provider"] = rec["provider"]
        _fin(rows, key, projects_map().get(key, {}).get("name", key))
    _api_cache[(key, git)] = (time.time(), rows, sp)
    return rows, sp


def plan_dirs(key: str, cfg: Config | None) -> list[Path]:
    dirs = list(hub.claude_dirs().get(key, []))
    if cfg is not None:
        extra = claude_sessions.project_dir(cfg.root, cfg.claude_projects_dir, hub.CLAUDE_BASE)
        if extra and all(extra.resolve() != d.resolve() for d in dirs):
            dirs.append(extra)
    return dirs


def extra_part(key: str, plan: bool, age: float) -> list[dict]:
    """Claude Code sessions (all the project's ~/.claude/projects dirs) + registry and local runs; cached 4 s."""
    ck = (key, plan, age)
    hit = _extra_cache.get(ck)
    if hit and time.time() - hit[0] < _ROWS_TTL:
        return hit[1]
    rows: list[dict] = []
    if plan:
        for d in plan_dirs(key, cfg_for(key)):
            rows += claude_sessions.list_runs(d, age)
    rows += hub.registered_runs(key)
    _fin(rows, key, projects_map().get(key, {}).get("name", key))
    _extra_cache[ck] = (time.time(), rows)
    return rows


def _sort(rows: list[dict]) -> list[dict]:
    rows.sort(key=lambda r: r.get("last_activity") or "", reverse=True)
    rows.sort(key=lambda r: not r.get("active"))
    return rows


def project_rows(key: str, plan: bool = True, age: float = claude_sessions.DEFAULT_AGE, git: bool = False) -> list[dict]:
    return _sort([*api_part(key, git)[0], *extra_part(key, plan, age)])


def all_rows(plan: bool, age: float) -> list[dict]:
    rows: list[dict] = []
    for key in list(projects_map()):
        rows += project_rows(key, plan, age)
    return _sort(rows)


def project_entry(key: str, e: dict) -> dict:
    rows = [r for r in project_rows(key) if r.get("kind") != "plan-agent"]
    sp = api_part(key, False)[1]
    live = lambda st: sum(1 for r in rows if r.get("state") == st)
    return {"key": key, "name": e.get("name") or key, "path": e["path"], "toml": bool(e.get("toml")),
            "roadmap": bool(e.get("roadmap")), "exists": bool(e.get("exists")), "sources": e.get("sources", []),
            "active": live("working"), "waiting": live("waiting"), "crashed": live("crashed"), "total": len(rows),
            "last_activity": max((r.get("last_activity") or "" for r in rows), default="") or None,
            "models": sorted({r["model"] for r in rows if r.get("model")}),
            "spend": {"total": sp["total"], "cap": sp["cap"]} if sp else None}


def projects_list() -> list[dict]:
    out = [project_entry(k, e) for k, e in projects_map().items()]
    out.sort(key=lambda p: p["last_activity"] or "", reverse=True)
    out.sort(key=lambda p: not (p["active"] or p["waiting"]))
    return out


def spend_all() -> dict:
    tot = logged = sess = 0.0
    per: dict = {}
    for key, e in projects_map().items():
        sp = api_part(key, False)[1]
        if sp:
            tot, logged, sess = tot + sp["total"], logged + sp["logged"], sess + sp["session"]
            per.update({f"{e.get('name') or key}:{n}": c for n, c in sp["per_run"].items()})
    return {"total": round(tot, 4), "logged": round(logged, 4), "cap": None, "session": round(sess, 4),
            "session_start": SESSION_START, "per_run": per}


def attention() -> list[dict]:
    """What needs the owner: local approvals, pmjc approvals, runs that crashed in the last 24 h, waiting Claude sessions."""
    out = []
    for key in list(projects_map()):
        for r in project_rows(key):
            base = {"project": key, "project_name": r.get("project_name"), "run": r["name"], "ts": r.get("last_activity")}
            for a in r.get("approvals") or []:
                out.append({**base, "kind": "approval", "text": f"approve? {a.get('summary') or a.get('id') or ''}"[:200],
                            "action": {"type": "local_approve", "run": r["name"], "id": a.get("id")}})
            if r.get("state") == "crashed" and (r.get("idle_seconds") or 0) < 86400:
                out.append({**base, "kind": "crashed", "text": f"{r.get('title') or r['name']} crashed"[:200]})
            elif r.get("plan") and r.get("state") == "waiting":
                out.append({**base, "kind": "waiting", "text": f"{r.get('title') or r['name']} is waiting"[:200]})
    pm = hub.pmjc()
    for a in pm.get("approvals") or []:
        if isinstance(a, dict) and a.get("status") in (None, "pending"):
            out.append({"project": None, "run": None, "kind": "pmjc_approval", "ts": a.get("created") or a.get("ts"),
                        "text": str(a.get("summary") or a.get("title") or a.get("action") or a.get("id"))[:200],
                        "action": {"type": "link", "url": f"{pm.get('url', '')}/approve/{a.get('id')}"}})
    out.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return out


_rm_cache: dict[str, tuple[float, dict]] = {}


def roadmap_for(key: str, fresh: bool = False) -> dict:
    e = projects_map()[key]
    hit = _rm_cache.get(key)
    if hit and not fresh and time.time() - hit[0] < 8:
        data = hit[1]
    else:
        data = hub.roadmap(e["path"])
        _rm_cache[key] = (time.time(), data)
    by: dict[str, list[str]] = {}
    for rec in registry.agents(key):
        if rec.get("roadmap"):
            by.setdefault(str(rec["roadmap"]), []).append(rec.get("name") or rec["id"])
    data = json.loads(json.dumps(data))  # copy: the cache stays free of per-call fields

    def walk(items):
        for it in items:
            if not isinstance(it, dict):
                continue
            ids = {str(it.get(k)) for k in ("id", "nickname", "path") if it.get(k)}
            it["agents"] = sorted({n for i in ids for n in by.get(i, [])})
            for k in ("items", "children"):
                if isinstance(it.get(k), list):
                    walk(it[k])

    for ph in data.get("phases", []) or []:
        walk(ph.get("items", []) or [])
        walk(ph.get("groups", []) or [])
    return data


def _clear_caches() -> None:
    global _pm_cache
    _pm_cache = (0.0, {})
    _api_cache.clear()
    _extra_cache.clear()


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("agentcontroller")
    except Exception:  # noqa: BLE001
        return "dev"


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentDash"

    def log_message(self, fmt, *args):  # keep stderr quiet; SSE would flood it
        pass

    def _token_ok(self) -> bool:
        """--remote: the token from the query or the cookie, and POSTs same-origin."""
        q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        cookie = next((c.split("=", 1)[1] for c in self.headers.get("Cookie", "").split("; ")
                       if c.startswith("ac_token=")), "")
        if not any(hmac.compare_digest(t, TOKEN) for t in (q, cookie) if t):
            return False
        origin = self.headers.get("Origin")
        return self.command == "GET" or origin is None or urlparse(origin).netloc == self.headers.get("Host", "")

    def _local_ok(self) -> bool:
        """Blocks DNS-rebinding and cross-site POSTs: Host and Origin must be this server."""
        if TOKEN:
            return self._token_ok()
        port = self.server.server_address[1]
        ok_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in ok_hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in ok_hosts}

    def _send(self, code: int, body: bytes, ctype: str = "application/json", cookie: str | None = None) -> None:
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            obj = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _run_arg(q: dict) -> str | None:
        name = (q.get("run") or [""])[0]
        return name if NAME_RE.match(name) else None

    def _project(self, q: dict, key: str | None = None) -> tuple[str, Config] | None:
        """Select the request's project (query `p`, else the pinned one) and bind its config; replies on error."""
        key = key if key is not None else ((q.get("p") or [""])[0] or PINNED or "")
        if not key or key == "*":
            self._json({"ok": False, "error": "this endpoint needs a project: ?p=<key from /api/projects>"}, 400)
            return None
        cfg = cfg_for(key)
        if cfg is None:
            self._json({"ok": False, "error": "unknown project (or its directory is gone)"}, 404)
            return None
        _CUR.set(cfg)  # one context per handler thread; every request sets it again
        return key, cfg

    @staticmethod
    def _plan_args(q: dict) -> tuple[bool, float]:
        try:
            age = float((q.get("plan_age") or [claude_sessions.DEFAULT_AGE])[0]) or claude_sessions.DEFAULT_AGE
        except ValueError:
            age = claude_sessions.DEFAULT_AGE
        return (q.get("plan") or ["1"])[0] != "0", age

    def do_GET(self):
        if not self._local_ok():
            return self._json({"ok": False, "error": "forbidden"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            try:
                # --remote: the first visit carries ?token=; later requests (fetch, SSE) use the cookie.
                cookie = (f"ac_token={TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
                          if TOKEN else None)
                return self._send(200, PAGE.read_bytes(), "text/html", cookie)
            except OSError:
                return self._json({"ok": False, "error": "static/index.html missing"}, 500)
        if u.path == "/api/health":
            return self._json({"mode": "all" if ALL_MODE else "project", "version": _version(), "pid": os.getpid(),
                               "projects": len(_pm_cache[1])})
        if u.path == "/api/projects":
            return self._json(projects_list())
        if u.path == "/api/jobs":
            return self._json(hub.pmjc())
        if u.path == "/api/lmstudio":
            return self._json(hub.lmstudio())
        if u.path == "/api/attention":
            return self._json(attention())
        if u.path == "/api/runs":
            plan, age = self._plan_args(q)
            p = (q.get("p") or [""])[0] or PINNED or "*"
            if p == "*":
                return self._json(all_rows(plan, age))
            if not self._project(q, p):
                return
            return self._json(_sort([*api_part(p, True)[0], *extra_part(p, plan, age)]))
        if u.path == "/api/spend":
            p = (q.get("p") or [""])[0] or PINNED or "*"
            if p == "*":
                return self._json(spend_all())
            if not self._project(q, p):
                return
            return self._json(spend())
        sel = self._project(q)
        if not sel:
            return
        key, cfg = sel
        if u.path == "/api/config":
            return self._json({"project": CFG.root.name, "key": key, "models": CFG.models,
                               "default_model": CFG.default_model, "default_base": CFG.default_base,
                               "max_turns": CFG.default_max_turns, "branches": branches(), "briefs": briefs(),
                               "can_launch": has_toml() and CFG.key_file.is_file(), **headroom(usage_records())})
        if u.path == "/api/roadmap":
            return self._json(roadmap_for(key, (q.get("refresh") or ["0"])[0] == "1"))
        if u.path == "/api/worktree":
            name = self._run_arg(q)
            return self._json(*reversed(worktree_state(name))) if name else self._json({"ok": False}, 400)
        if u.path == "/api/billed":
            if not has_toml():
                return self._json({"ok": False, "output": f"{CFG.root.name} has no {CONFIG_NAME}"})
            try:
                p = subprocess.run([sys.executable, "-m", "agentcontroller.usage", "--project", str(CFG.root), "--json"],
                                   capture_output=True, text=True, timeout=90, cwd=CFG.root,
                                   env={**os.environ, "PYTHONPATH": PKG_PARENT})
                return self._json({"ok": p.returncode == 0, "output": p.stdout.strip()[:20000] or p.stderr.strip()[-2000:]})
            except (OSError, subprocess.SubprocessError) as e:
                return self._json({"ok": False, "output": str(e)})
        if u.path == "/api/stream":
            name = self._run_arg(q)
            return self._stream(key, name) if name else self._json({"ok": False, "error": "bad run"}, 400)
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._local_ok() or not self.headers.get("Content-Type", "").startswith("application/json") \
                or self.headers.get("X-AC") != "1":
            return self._json({"ok": False, "error": "forbidden (POSTs need Content-Type: application/json and X-AC: 1)"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        body = self._body()
        if u.path == "/api/forget_project":
            key = body.get("p")
            if not isinstance(key, str) or key not in projects_map():
                return self._json({"ok": False, "error": "unknown project"}, 404)
            hub.hide_project(key)
            _clear_caches()
            return self._json({"ok": True})
        sel = self._project(q)
        if not sel:
            return
        key, cfg = sel
        name = self._run_arg(q) or (body.get("run") if isinstance(body.get("run"), str) and NAME_RE.match(body["run"]) else None)

        def reply(res):
            _api_cache.clear()
            _extra_cache.clear()
            self._json(res[1], res[0])

        if u.path == "/api/launch":
            return reply(launch(body))
        if u.path == "/api/local_approve":
            aid = body.get("id")
            if not name or not isinstance(aid, str):
                return self._json({"ok": False, "error": "need run and id"}, 400)
            return reply(hub.decide_local(key, name, aid, bool(body.get("approve"))))
        if u.path == "/api/primary":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return self._json({"ok": False, "error": "need text"}, 400)
            append_jsonl(CFG.runs / "primary_inbox.jsonl", {"ts": now(), "from": name or "owner", "text": text.strip()})
            return self._json({"ok": True})
        if not name:
            return self._json({"ok": False, "error": "bad run"}, 400)
        if u.path == "/api/inbox":
            if not run_info(name, [], git_info=False)["active"]:
                return self._json({"ok": False, "error": "run is not running"}, 409)
            if body.get("control") == "stop":
                return reply(stop_graceful(name))
            if isinstance(body.get("text"), str) and body["text"].strip():
                append_jsonl(CFG.runs / f"{name}.inbox.jsonl", {"text": body["text"].strip(), "ts": now()})
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "need text or control=stop"}, 400)
        if u.path == "/api/kill":
            return reply(kill(name))
        if u.path == "/api/archive":
            return reply(archive(name))
        if u.path == "/api/worktree_delete":
            return reply(worktree_delete(name, bool(body.get("discard"))))
        self._json({"ok": False, "error": "not found"}, 404)

    def _sources(self, key: str, name: str) -> list[dict]:
        """What to tail for a run: Claude transcript (any of the project's dirs), API events file, or a local run."""
        if name.startswith(claude_sessions.PREFIX):
            for d in plan_dirs(key, cfg_for(key)):
                f = claude_sessions.find_file(d, name)
                if f:
                    return [{"mode": "plan", "path": f}]
        api = CFG.runs / f"{name}.events.jsonl"
        if not api.exists():
            ld = hub.local_dir(key, name)
            if ld:
                return [{"mode": "jsonl", "path": ld / "events.jsonl"},
                        {"mode": "local_thinking", "path": ld / "thinking.txt"},
                        {"mode": "local_answer", "path": ld / "answer.txt"}]
        return [{"mode": "jsonl", "path": api}]

    def _stream(self, key: str, name: str) -> None:
        srcs = self._sources(key, name)
        log = CFG.runs / f"{name}.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for st in srcs:
            st.update(pos=0, dec=codecs.getincrementaldecoder("utf-8")("replace"), first=[])
        last_beat = time.time()

        def emit(obj_or_line):
            line = obj_or_line if isinstance(obj_or_line, str) else json.dumps(obj_or_line, ensure_ascii=False)
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))

        try:
            if len(srcs) == 1 and srcs[0]["mode"] == "jsonl" and not srcs[0]["path"].exists() and log.exists():
                for l in log.read_text(encoding="utf-8", errors="replace").splitlines():
                    if l.strip():
                        emit({"kind": "text", "text": l})
                self.wfile.flush()
            while True:
                sent = False
                for st in srcs:
                    path = st["path"]
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    if size < st["pos"]:
                        st["pos"] = 0
                    if size <= st["pos"]:
                        continue
                    with path.open("rb") as fh:
                        fh.seek(st["pos"])
                        chunk = fh.read(1 << 20)
                    if st["mode"] in ("jsonl", "plan"):
                        end = chunk.rfind(b"\n") + 1  # only complete lines; a partial write is picked up next poll
                        st["pos"] += end
                        for l in chunk[:end].decode("utf-8", errors="replace").split("\n"):
                            if not l.strip():
                                continue
                            if st["mode"] == "plan":  # transcript lines -> the event shapes the page renders
                                try:
                                    evs = claude_sessions.to_events(json.loads(l), st["first"])
                                except ValueError:
                                    continue
                                for ev in evs:
                                    emit(ev)
                            else:
                                emit(l)
                    else:  # thinking.txt / answer.txt: raw text chunks
                        st["pos"] += len(chunk)
                        text = st["dec"].decode(chunk)
                        if text:
                            emit({"kind": st["mode"], "text": text})
                    sent = True
                if sent:
                    self.wfile.flush()
                    last_beat = time.time()
                elif time.time() - last_beat > 15:
                    self.wfile.write(b": hb\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.5)
        except OSError:
            return


def main(argv: list[str] | None = None) -> int:
    global SESSION_START, TOKEN, ALL_MODE, PINNED, PINNED_ENTRY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="serve every project (default when no agentcontroller.toml is found)")
    ap.add_argument("--project", help="pin one project: agentcontroller.toml (or its directory)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 for other devices on the LAN (needs --remote)")
    ap.add_argument("--remote", action="store_true",
                    help="accept any Host with a token (kept in <secrets>/dash_token.txt); for LAN or a tunnel")
    ap.add_argument("--session-start", help="ISO timestamp for the session spend figure (default: today 00:00)")
    args = ap.parse_args(argv)
    from .config import find_project
    found = find_project()
    pin = args.project or (None if args.all else (str(found) if found else None))
    ALL_MODE = pin is None
    pcfg = load(pin) if pin else None
    if pcfg is not None:
        PINNED = registry.norm(registry.main_checkout(pcfg.root))
        PINNED_ENTRY = {"key": PINNED, "path": str(pcfg.root), "name": pcfg.root.name, "sources": ["pinned"],
                        "toml": (pcfg.root / CONFIG_NAME).is_file(), "exists": True}
    SESSION_START = args.session_start or datetime.now().strftime("%Y-%m-%dT00:00:00")
    port = args.port or (registry.dash_port() if ALL_MODE else pcfg.port)
    if sys.stdout is None or sys.stderr is None:  # pythonw (the logon task): no console, log to a file instead
        log = open(registry.home() / "dash.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.host not in ("127.0.0.1", "localhost") and not args.remote:
        ap.error("--host other than 127.0.0.1 needs --remote (token auth)")
    tok = None
    if args.remote:
        tokdir = registry.home() if ALL_MODE else pcfg.secrets
        tok = tokdir / "dash_token.txt"
        if not tok.exists():
            tokdir.mkdir(parents=True, exist_ok=True)
            tok.write_text(secrets.token_urlsafe(24), encoding="utf-8")
        TOKEN = tok.read_text(encoding="utf-8").strip()
        if ALL_MODE:
            print("WARNING: --remote with --all exposes EVERY project (runs, transcripts, roadmaps) to anyone holding "
                  "the token", flush=True)
    srv = ThreadingHTTPServer((args.host, port), Handler)
    srv.daemon_threads = True
    import threading
    threading.Thread(target=lambda: (projects_map(), hub.claude_dirs()), daemon=True).start()  # warm the caches
    what = "all projects" if ALL_MODE else f"project {pcfg.root}"
    print(f"AgentController dashboard: http://127.0.0.1:{port}/  ({what})", flush=True)
    if TOKEN:
        print(f"Remote access on {args.host}:{port}: open http://<this-host>:{port}/?token=<{tok}>"
              " once per device (the token is then kept in a cookie)", flush=True)
    dashfile = registry.home() / "dash.json"
    if ALL_MODE:
        try:
            registry._write(dashfile, {"port": port, "pid": os.getpid(), "started": time.time()})
        except OSError:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if ALL_MODE:
            try:
                if (registry._read(dashfile, {}) or {}).get("pid") == os.getpid():
                    dashfile.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
