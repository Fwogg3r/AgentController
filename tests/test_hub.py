import json
import time

from dashfix import home  # noqa: F401
from agentcontroller import hub, registry


def mkrun(proj, rid="r1", events=(), approvals=()):
    d = proj / ".agentcontroller" / "runs" / rid
    (d / "approvals").mkdir(parents=True)
    now = time.time()
    (d / "events.jsonl").write_text("".join(json.dumps({"ts": now, **e}) + "\n" for e in events), encoding="utf-8")
    for a in approvals:
        (d / "approvals" / f"{a}.pending.json").write_text(json.dumps({"id": a, "summary": "edit x"}))
    return d


def rec(d, **kw):
    return {"id": d.name, "kind": "local", "dir": str(d), "status": "running", "started": time.time(), **kw}


def test_projects_from_claude_dirs(home):
    proj = home / "repo"
    proj.mkdir()
    slug = home / "claude" / "C--x-repo"
    slug.mkdir()
    (slug / "s1.jsonl").write_text(json.dumps({"type": "user", "cwd": str(proj)}) + "\n")
    wt = home / "repo" / ".claude" / "worktrees" / "w1"  # deleted worktree maps to its repo by convention
    slug2 = home / "claude" / "C--x-repo--claude-worktrees-w1"
    slug2.mkdir()
    (slug2 / "s2.jsonl").write_text(json.dumps({"type": "user", "cwd": str(wt)}) + "\n")
    key = registry.norm(proj)
    p = hub.projects()
    assert key in p and "claude" in p[key]["sources"] and p[key]["exists"] and not p[key]["toml"]
    assert sorted(d.name for d in hub.claude_dirs()[key]) == ["C--x-repo", "C--x-repo--claude-worktrees-w1"]


def test_hide_project(home):
    proj = home / "repo"
    proj.mkdir()
    key = registry.register_project(proj)
    assert key in hub.projects()
    hub.hide_project(key)
    assert key not in hub.projects() and key not in registry.projects()


def test_local_states(home):
    proj = home / "p"
    working = mkrun(proj, "w", [{"kind": "call_start", "stage": 1}])
    assert hub.local_info(rec(working))["state"] == "working"
    waiting = mkrun(proj, "q", [{"kind": "call_start"}], approvals=["a1"])
    i = hub.local_info(rec(waiting))
    assert i["state"] == "waiting" and i["approvals"][0]["id"] == "a1" and i["active"]
    done = mkrun(proj, "d", [{"kind": "call"}, {"kind": "done", "ok": True}])
    i = hub.local_info(rec(done, status="running"))  # registry still says running
    assert i["state"] == "finished" and i["outcome"] == "success"
    stopped = mkrun(proj, "s", [{"kind": "stopped", "reason": "denied"}])
    assert hub.local_info(rec(stopped))["outcome"] == "stopped"
    crashed = mkrun(proj, "c", [{"kind": "call_start"}])
    i = hub.local_info(rec(crashed, pid=2 ** 22 + 12345))
    assert i["state"] == "crashed" and not i["active"]


def test_dir_without_ac_segment_ignored(home):
    d = home / "elsewhere" / "run"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(json.dumps({"kind": "call", "ts": time.time()}) + "\n")
    i = hub.local_info(rec(d))
    assert i["turns"] == 0 and i["dir"] is None
    assert hub.ac_dir(str(d)) is None and hub.ac_dir(str(home / ".agentcontroller" / ".." / "x")) is None


def test_discovered_runs_and_worktrees(home):
    proj = home / "p"
    proj.mkdir()
    key = registry.register_project(proj)
    mkrun(proj, "u1", [{"kind": "call_start"}])
    mkrun(proj / ".claude" / "worktrees" / "wt", "u2", [{"kind": "done", "ok": True}])
    runs = {r["id"]: r for r in hub.registered_runs(key)}
    assert runs["u1"]["state"] == "working" and runs["u2"]["state"] == "finished"
    # a registered run is not listed twice
    registry.register_agent("u1", "local", proj, dir=str(proj / ".agentcontroller" / "runs" / "u1"), name="u1")
    assert [r["id"] for r in hub.registered_runs(key)].count("u1") == 1


def test_decide_local(home):
    proj = home / "p"
    proj.mkdir()
    key = registry.register_project(proj)
    d = mkrun(proj, "r", [{"kind": "call_start"}], approvals=["a1"])
    registry.register_agent("r", "local", proj, dir=str(d), name="r")
    assert hub.decide_local(key, "r", "a1", True)[0] == 200
    assert json.loads((d / "approvals" / "a1.json").read_text())["approve"] is True
    assert hub.decide_local(key, "r", "nope", True)[0] == 404
    assert hub.decide_local(key, "r", "../x", True)[0] == 400
    assert hub.decide_local(key, "zzz", "a1", True)[0] == 404
    bad = home / "plain"
    (bad / "approvals").mkdir(parents=True)
    (bad / "approvals" / "a1.pending.json").write_text("{}")
    registry.register_agent("bad", "local", proj, dir=str(bad), name="bad")
    assert hub.decide_local(key, "bad", "a1", True)[0] == 404
