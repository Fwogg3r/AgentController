import os
import time

from dashfix import home  # noqa: F401
from agentcontroller import registry


def test_round_trip(home):
    proj = home / "proj"
    proj.mkdir()
    key = registry.register_project(proj)
    assert key == registry.norm(proj)
    assert registry.projects()[key]["name"] == "proj" and registry.projects()[key]["toml"] is None
    (proj / "agentcontroller.toml").write_text("")
    registry.register_project(proj)
    assert registry.projects()[key]["toml"]
    registry.register_agent("a1", "api", proj, name="a1", model="m", roadmap="phase1/b", parent="boss")
    registry.update_agent("a1", status="done", ended=1.0)
    (rec,) = registry.agents(key)
    assert rec["id"] == "a1" and rec["status"] == "done" and rec["roadmap"] == "phase1/b" and rec["parent"] == "boss"
    assert registry.agents("other") == []
    assert registry.forget_project(key) and key not in registry.projects()
    registry.update_agent("missing", status="x")  # no record: no error


def test_hooks_respect_no_dash(home, monkeypatch):
    monkeypatch.setenv("AGENTCONTROLLER_NO_DASH", "1")
    registry.hook_start(home, "x", "api", name="x")
    assert registry.agents() == []
    monkeypatch.delenv("AGENTCONTROLLER_NO_DASH")
    monkeypatch.setattr(registry, "ensure_dash", lambda *a, **k: True)
    monkeypatch.setenv("AGENTCONTROLLER_PARENT", "boss")
    registry.hook_start(home, "x", "api", name="x", model="m")
    (rec,) = registry.agents()
    assert rec["parent"] == "boss" and rec["status"] == "running" and rec["pid"] == os.getpid()
    registry.hook_end("x")
    assert registry.agents()[0]["status"] == "done"
    assert time.time() - registry.agents()[0]["ended"] < 5
