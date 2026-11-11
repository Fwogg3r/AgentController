import pytest

from dashfix import home  # noqa: F401
from agentcontroller import dash, registry
from test_dash_http import call, q, server  # noqa: F401


def row(name, state, parent=None, project="p", **kw):
    act = state in ("working", "waiting")
    return {"name": name, "project": project, "parent": parent, "state": state,
            "status": "running" if act else "finished", "active": act,
            "outcome": None if act else "success", "activity": {"kind": "tool", "text": f"{name} act", "since": "2026-01-01T00:00:05"} if act else None,
            "last_activity": "2026-01-01T00:00:05", "duration": 5, "duration_live": act, "kind": "plan", **kw}


def by(rows):
    return {r["name"]: r for r in rows}


def test_simple_and_original_untouched():
    rows = [row("s", "finished"), row("s.a", "working", "s", kind="plan-agent")]
    out = by(dash.propagate_waiting(rows))
    s = out["s"]
    assert s["state"] == "waiting" and s["active"] and s["status"] == "running" and s["outcome"] is None
    assert s["own_state"] == "finished" and s["own_outcome"] == "success" and s["duration_live"]
    assert s["activity"]["kind"] == "task" and s["activity"]["text"].startswith("waiting for 1 child agent(s)")
    assert "s.a act" in s["activity"]["text"] and s["activity"]["since"] == "2026-01-01T00:00:05"
    assert out["s.a"]["state"] == "working" and out["s.a"]["kind"] == "plan-agent"
    assert rows[0]["state"] == "finished"  # cached rows are not mutated


def test_nested_and_ended():
    rows = [row("a", "ended"), row("b", "finished", "a"), row("c", "working", "b")]
    out = by(dash.propagate_waiting(rows))
    assert out["a"]["state"] == "waiting" and out["b"]["state"] == "waiting" and out["c"]["state"] == "working"
    assert out["a"]["own_state"] == "ended"


def test_crashed_parent_stays_crashed():
    out = by(dash.propagate_waiting([row("a", "crashed"), row("b", "working", "a")]))
    assert out["a"]["state"] == "crashed" and not out["a"]["active"] and out["a"]["children_active"] == 1 and out["a"]["note"]


def test_children_finished_parent_finished():
    out = by(dash.propagate_waiting([row("a", "finished"), row("b", "finished", "a"), row("c", "crashed", "a")]))
    assert out["a"]["state"] == "finished" and "own_state" not in out["a"]


def test_active_parent_untouched():
    out = by(dash.propagate_waiting([row("a", "working"), row("b", "working", "a")]))
    assert out["a"]["state"] == "working" and "own_state" not in out["a"]


def test_mixed_projects():
    rows = [row("a", "finished", project="p1"), row("a", "finished", project="p2"), row("k", "working", "a", project="p2")]
    out = dash.propagate_waiting(rows)
    assert [r["state"] for r in out] == ["finished", "waiting", "working"]


def test_cycles():
    rows = [row("a", "finished", "b"), row("b", "finished", "a"), row("c", "working", "a"), row("s", "finished", "s")]
    out = by(dash.propagate_waiting(rows))
    assert out["a"]["state"] == "waiting" and out["b"]["state"] == "waiting" and out["s"]["state"] == "finished"
    dash.propagate_waiting([row("x", "finished", "y"), row("y", "finished", "x")])


def test_http_runs_and_projects(server, home, monkeypatch):
    b = home / "beta"
    b.mkdir()
    kb = registry.register_project(b)
    rows = [row("s", "finished", project=kb, project_name="beta"), row("s.a", "working", "s", project=kb, project_name="beta", kind="plan-agent")]
    monkeypatch.setattr(dash, "extra_part", lambda key, plan, age: [dict(r) for r in rows] if key == kb else [])
    monkeypatch.setattr(dash, "api_part", lambda key, git: ([], None))
    dash._clear_caches()
    for path in ("/api/runs?p=*", "/api/runs" + q(kb)):
        got = by(call(server, path)[1])
        assert got["s"]["state"] == "waiting" and got["s"]["own_state"] == "finished" and got["s.a"]["state"] == "working"
    pj = {p["key"]: p for p in call(server, "/api/projects")[1]}[kb]
    assert pj["waiting"] == 1 and pj["total"] == 1
    att = call(server, "/api/attention")[1]
    assert not any(a["kind"] == "crashed" for a in att)
