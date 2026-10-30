"""Spend-free end-to-end check of the dashboard: temp git project, stub runner, every endpoint.

    python -m agentcontroller.selftest
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 8799
PKG_PARENT = str(Path(__file__).resolve().parent.parent)

STUB = r'''
import json, sys, time, pathlib
a = sys.argv; g = lambda k: a[a.index(k) + 1]
name, proj = g("--name"), pathlib.Path(g("--project"))
runs = proj / ".ac" / "agent_runs"; runs.mkdir(parents=True, exist_ok=True)
ev = runs / (name + ".events.jsonl"); inbox = runs / (name + ".inbox.jsonl")
def w(**k):
    with ev.open("a") as f: f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **k}) + "\n")
print("stub started", name, flush=True)
w(kind="start", model="stub", budget=float(g("--budget")), branch=name, base=g("--base"), worktree=str(proj / ".wt" / name))
w(kind="text", text="working"); w(kind="result", cost=0.02, turns=1, subtype="success")
while True:
    time.sleep(0.3)
    if inbox.exists() and "stop" in inbox.read_text() and not name.endswith("stubborn"):
        break
w(kind="end")
with (proj / ".ac" / "api_usage.jsonl").open("a") as f: f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "name": name, "usd": 0.02}) + "\n")
(runs / (name + ".pid")).unlink(missing_ok=True)
'''


def req(path, body=None, host=None, timeout=10):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method="POST" if body is not None else "GET",
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json", **({"Host": host} if host else {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            return f.status, json.loads(f.read().decode() or "null") if "json" in f.headers.get("Content-Type", "") else f.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


def wait_for(fn, secs=15):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    proj = Path(tempfile.mkdtemp())
    (proj / "stub.py").write_text(STUB)
    (proj / "agentcontroller.toml").write_text(
        f'[paths]\nsecrets = ".ac"\nworktrees = ".wt"\n[budget]\ncap = 1.0\n[agent]\nrunner_cmd = ["{Path(sys.executable).as_posix()}", "stub.py"]\n')
    git(proj, "init", "-b", "main")
    (proj / "f.txt").write_text("x")
    git(proj, "add", "f.txt")
    git(proj, "commit", "-m", "init")
    runs = proj / ".ac" / "agent_runs"
    runs.mkdir(parents=True)
    proc = subprocess.Popen([sys.executable, "-m", "agentcontroller.dash", "--project", str(proj), "--port", str(PORT)],
                            env={**os.environ, "PYTHONPATH": PKG_PARENT}, stdout=subprocess.PIPE)
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + label, extra if not cond else "")

    try:
        wait_for(lambda: req("/api/spend")[0] == 200 if proc.poll() is None else False)
        c = req("/api/config")[1]
        check("config", "main" in c["branches"] and "haiku" in c["models"] and c["headroom"] == 1.0, c)
        launch = lambda **k: req("/api/launch", {"name": "t1", "base": "main", "model": "haiku", "budget": 0.1,
                                                 "max_turns": 3, "brief_text": "do it", **k})
        check("bad name refused", launch(name="Bad Name")[0] == 400 and launch(name="main")[0] == 400)
        check("bad base refused", launch(base="nope")[0] == 400)
        check("over headroom refused", "headroom" in launch(budget=5)[1]["error"])
        check("no brief refused", launch(brief_text="")[0] == 400)
        code, r = launch()
        check("launch ok", code == 200 and r["pid"] > 0, r)
        check("brief written", (proj / "briefs" / "t1.md").read_text().strip() == "do it")
        check("pid file", (runs / "t1.pid").read_text() == str(r["pid"]))
        check("running", wait_for(lambda: next((x for x in req("/api/runs")[1] if x["name"] == "t1"), {}).get("status") == "running"))
        check("log has stub stdout", wait_for(lambda: "stub started" in (runs / "t1.log").read_text()))
        check("headroom reserves active budget", abs(req("/api/config")[1]["headroom"] - 0.9) < 1e-6)
        check("duplicate active refused", launch()[0] == 409)
        check("archive refused while active", req("/api/archive?run=t1", {})[0] == 409)
        check("graceful stop", req("/api/inbox?run=t1", {"control": "stop"})[0] == 200)
        check("finished", wait_for(lambda: next((x for x in req("/api/runs")[1] if x["name"] == "t1"), {}).get("status") == "finished"))
        check("kill refused when not running", req("/api/kill?run=t1", {})[0] == 409)

        code, r = launch(name="t2-stubborn")
        check("launch stubborn", code == 200)
        wait_for(lambda: (runs / "t2-stubborn.events.jsonl").exists())
        pid = r["pid"]
        req("/api/inbox?run=t2-stubborn", {"control": "stop"})
        time.sleep(1.5)
        check("stubborn ignores stop", next(x for x in req("/api/runs")[1] if x["name"] == "t2-stubborn")["status"] == "running")
        check("kill", req("/api/kill?run=t2-stubborn", {})[0] == 200)
        check("process gone", wait_for(lambda: subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                                                              text=True).stdout.split().count(str(pid)) == 0) if os.name == "nt"
              else wait_for(lambda: not Path(f"/proc/{pid}").exists()))
        st = next(x for x in req("/api/runs")[1] if x["name"] == "t2-stubborn")
        check("killed run finished + spend logged", st["status"] == "finished" and "killed" in (proj / ".ac" / "api_usage.jsonl").read_text())
        check("archive", req("/api/archive?run=t2-stubborn", {})[0] == 200 and (runs / "archive" / "t2-stubborn.events.jsonl").exists())
        check("archived run hidden", "t2-stubborn" not in [x["name"] for x in req("/api/runs")[1]])

        # worktree deletion: one merged, one with an unmerged commit and a dirty file
        for n in ("wt-merged", "wt-dirty"):
            git(proj, "worktree", "add", str(proj / ".wt" / n), "-b", n, "main")
            ev = {"ts": "2026-01-01T00:00:00", "kind": "start", "model": "m", "budget": 0.1, "branch": n, "base": "main",
                  "worktree": str(proj / ".wt" / n)}
            (runs / f"{n}.events.jsonl").write_text(json.dumps(ev) + "\n" + json.dumps({"ts": "2026-01-01T00:00:01", "kind": "end"}) + "\n")
        (proj / ".wt" / "wt-dirty" / "g.txt").write_text("y")
        git(proj / ".wt" / "wt-dirty", "add", "g.txt")
        git(proj / ".wt" / "wt-dirty", "commit", "-m", "unmerged work")
        (proj / ".wt" / "wt-dirty" / "h.txt").write_text("z")
        s = req("/api/worktree?run=wt-dirty")[1]
        check("worktree state", not s["merged"] and s["unmerged"] and s["dirty"] == 1, s)
        code, r = req("/api/worktree_delete?run=wt-dirty", {})
        check("unmerged delete needs discard", code == 409 and r.get("needs_discard") and (proj / ".wt" / "wt-dirty").exists())
        check("discard delete", req("/api/worktree_delete?run=wt-dirty", {"discard": True})[0] == 200 and not (proj / ".wt" / "wt-dirty").exists())
        check("branch gone", "wt-dirty" not in git(proj, "branch", "--format=%(refname:short)"))
        check("merged delete", req("/api/worktree_delete?run=wt-merged", {})[0] == 200 and not (proj / ".wt" / "wt-merged").exists())
        (runs / "evil.events.jsonl").write_text(json.dumps({"kind": "start", "branch": "main", "base": "main", "worktree": str(proj)}) + "\n"
                                                + json.dumps({"kind": "end"}) + "\n")
        check("main/primary tree refused", req("/api/worktree_delete?run=evil", {"discard": True})[0] == 403 and (proj / "f.txt").exists())
        check("bad host refused", req("/api/runs", host="evil.com")[0] == 403)
        return 0 if ok else 1
    finally:
        proc.kill()
        for p in runs.glob("*.pid"):  # don't leave stub processes behind
            try:
                subprocess.run(["taskkill", "/PID", p.read_text().strip(), "/T", "/F"], capture_output=True) if os.name == "nt" \
                    else os.kill(int(p.read_text()), 9)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
