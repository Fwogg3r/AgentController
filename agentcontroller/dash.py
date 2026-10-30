"""agentdash: localhost dashboard for the agents started by agentctl (127.0.0.1 only).

    agentdash [--project PATH] [--port 8765] [--session-start 2026-01-31T09:00:00]

Reads <runs>/<name>.events.jsonl (falls back to <name>.log for older runs) and the usage log.
Writes only inbox/pid/archive files, briefs, and launches/stops agents. Never reads or serves key files.
"""

import argparse
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

from .config import Config, load

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

CFG: Config
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


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
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
    # A model turn is in progress: after a tool result, the start, a user message or a text block.
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


def active_runs(usage: list[dict]) -> list[dict]:
    return [r for r in (run_info(n, usage, git_info=False) for n in run_names()) if r["active"]]


def spend() -> dict:
    usage = usage_records()
    total = sum(r.get("usd", 0.0) for r in usage)
    runs = [run_info(n, usage, git_info=False) for n in run_names()]
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
def launch(body: dict) -> tuple[int, dict]:
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
        if u.path == "/api/runs":
            usage = usage_records()
            runs = [run_info(n, usage) for n in run_names()]
            runs.sort(key=lambda r: (not r["active"], -last_activity(r["name"])))
            return self._json(runs)
        if u.path == "/api/spend":
            return self._json(spend())
        if u.path == "/api/config":
            return self._json({"project": CFG.root.name, "models": CFG.models, "default_model": CFG.default_model,
                               "default_base": CFG.default_base, "max_turns": CFG.default_max_turns,
                               "branches": branches(), "briefs": briefs(), **headroom(usage_records())})
        if u.path == "/api/worktree":
            name = self._run_arg(q)
            return self._json(*reversed(worktree_state(name))) if name else self._json({"ok": False}, 400)
        if u.path == "/api/billed":
            try:
                p = subprocess.run([sys.executable, "-m", "agentcontroller.usage", "--project", str(CFG.root), "--json"],
                                   capture_output=True, text=True, timeout=90, cwd=CFG.root,
                                   env={**os.environ, "PYTHONPATH": PKG_PARENT})
                return self._json({"ok": p.returncode == 0, "output": p.stdout.strip()[:20000] or p.stderr.strip()[-2000:]})
            except (OSError, subprocess.SubprocessError) as e:
                return self._json({"ok": False, "output": str(e)})
        if u.path == "/api/stream":
            name = self._run_arg(q)
            return self._stream(name) if name else self._json({"ok": False, "error": "bad run"}, 400)
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._local_ok() or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json({"ok": False, "error": "forbidden"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        body = self._body()
        name = self._run_arg(q) or (body.get("run") if isinstance(body.get("run"), str) and NAME_RE.match(body["run"]) else None)

        def reply(res):
            self._json(res[1], res[0])

        if u.path == "/api/launch":
            return reply(launch(body))
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

    def _stream(self, name: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        path = CFG.runs / f"{name}.events.jsonl"
        pos, last_beat = 0, time.time()
        try:
            if not path.exists() and (CFG.runs / f"{name}.log").exists():
                for l in (CFG.runs / f"{name}.log").read_text(encoding="utf-8", errors="replace").splitlines():
                    if l.strip():
                        self.wfile.write(f"data: {json.dumps({'kind': 'text', 'text': l}, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            while True:
                if path.exists() and path.stat().st_size > pos:
                    with path.open("rb") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                    end = chunk.rfind(b"\n") + 1  # only complete lines; a partial write is picked up next poll
                    pos += end
                    for l in chunk[:end].decode("utf-8", errors="replace").splitlines():
                        if l.strip():
                            self.wfile.write(f"data: {l}\n\n".encode("utf-8"))
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
    global CFG, SESSION_START, TOKEN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="agentcontroller.toml (or its directory); default: walk up from the cwd")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 for other devices on the LAN (needs --remote)")
    ap.add_argument("--remote", action="store_true",
                    help="accept any Host with a token (kept in <secrets>/dash_token.txt); for LAN or a tunnel")
    ap.add_argument("--session-start", help="ISO timestamp for the session spend figure (default: today 00:00)")
    args = ap.parse_args(argv)
    CFG = load(args.project)
    SESSION_START = args.session_start or datetime.now().strftime("%Y-%m-%dT00:00:00")
    port = args.port or CFG.port
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.host not in ("127.0.0.1", "localhost") and not args.remote:
        ap.error("--host other than 127.0.0.1 needs --remote (token auth)")
    if args.remote:
        tok = CFG.secrets / "dash_token.txt"
        if not tok.exists():
            CFG.secrets.mkdir(parents=True, exist_ok=True)
            tok.write_text(secrets.token_urlsafe(24), encoding="utf-8")
        TOKEN = tok.read_text(encoding="utf-8").strip()
    srv = ThreadingHTTPServer((args.host, port), Handler)
    srv.daemon_threads = True
    print(f"AgentController dashboard: http://127.0.0.1:{port}/  (project {CFG.root}, runs {CFG.runs})", flush=True)
    if TOKEN:
        print(f"Remote access on {args.host}:{port}: open http://<this-host>:{port}/?token=<{CFG.secrets / 'dash_token.txt'}>"
              " once per device (the token is then kept in a cookie)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
