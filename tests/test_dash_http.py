import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dashfix import home  # noqa: F401
from agentcontroller import dash, registry
from test_hub import mkrun


@pytest.fixture
def server(home, monkeypatch):
    monkeypatch.setattr(dash, "ALL_MODE", True)
    monkeypatch.setattr(dash, "PINNED", None)
    monkeypatch.setattr(dash, "PINNED_ENTRY", None)
    monkeypatch.setattr(dash, "TOKEN", None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def call(port, path, body=None, ac=True, host=None):
    h = {"Content-Type": "application/json", **({"X-AC": "1"} if ac else {}), **({"Host": host} if host else {})}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                               data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=20) as f:
            return f.status, json.loads(f.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def q(key):
    return "?p=" + urllib.parse.quote(key, safe="")


def test_http(server, home):
    port = server
    # a toml project with one API run, a toml-less project with a local run waiting for approval
    a = home / "alpha"
    (a / ".ac" / "agent_runs").mkdir(parents=True)
    (a / "agentcontroller.toml").write_text('[paths]\nsecrets = ".ac"\n[budget]\ncap = 5\n')
    (a / ".ac" / "agent_runs" / "r1.events.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00", "kind": "start", "model": "claude-sonnet-5-5", "budget": 1}) + "\n"
        + json.dumps({"ts": "2026-01-01T00:00:01", "kind": "end"}) + "\n")
    b = home / "beta"
    b.mkdir()
    ka, kb = registry.register_project(a), registry.register_project(b)
    d = mkrun(b, "loc", [{"kind": "call_start", "stage": 1}], approvals=["ap1"])
    registry.register_agent("loc", "local", b, dir=str(d), name="loc", model="qwen", provider="lmstudio", parent="boss")

    code, h = call(port, "/api/health")
    assert code == 200 and h["mode"] == "all" and h["pid"]
    code, pl = call(port, "/api/projects")
    by = {p["key"]: p for p in pl}
    assert code == 200 and by[ka]["toml"] and by[ka]["spend"]["cap"] == 5 and by[ka]["total"] == 1
    assert not by[kb]["toml"] and by[kb]["waiting"] == 1 and by[kb]["spend"] is None
    assert pl[0]["key"] == kb  # live projects first
    code, rows = call(port, "/api/runs?p=*")
    names = {r["name"]: r for r in rows}
    assert names["r1"]["project"] == ka and names["r1"]["project_name"] == "alpha" and names["r1"]["provider"] == "anthropic"
    assert names["loc"]["provider"] == "lmstudio" and names["loc"]["parent"] == "boss" and names["loc"]["state"] == "waiting"
    assert [r["name"] for r in call(port, "/api/runs" + q(kb))[1]] == ["loc"]
    assert call(port, "/api/runs?p=nope")[0] == 404
    assert call(port, "/api/config")[0] == 400  # needs a project in --all mode
    code, cfg = call(port, "/api/config" + q(kb))
    assert code == 200 and cfg["can_launch"] is False
    assert call(port, "/api/attention")[1][0]["kind"] == "approval"

    # X-AC is required, Host is checked, launch is refused without a toml
    body = {"run": "loc", "id": "ap1", "approve": True}
    assert call(port, "/api/local_approve" + q(kb), body, ac=False)[0] == 403
    assert call(port, "/api/runs?p=*", host="evil.com")[0] == 403
    code, r = call(port, "/api/launch" + q(kb), {"name": "x", "brief_text": "hi", "budget": 1})
    assert code == 400 and "agentcontroller.toml" in r["error"]
    assert call(port, "/api/local_approve" + q(kb), {**body, "approve": False}) == (200, {"ok": True})
    assert json.loads((d / "approvals" / "ap1.json").read_text())["approve"] is False
    assert call(port, "/api/local_approve" + q(kb), {"run": "loc", "id": "zz"})[0] == 404

    assert call(port, "/api/forget_project", {"p": kb}) == (200, {"ok": True})
    assert kb not in {p["key"] for p in call(port, "/api/projects")[1]}
