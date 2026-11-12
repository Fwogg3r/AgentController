"""WebRoadmap's own page proxied under /rmui/<id>/ (needs the `roadmap` CLI), and the pmjc token lookup."""
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pytest

from dashfix import home  # noqa: F401
from agentcontroller import access, dash, hub, registry
from test_dash_http import server  # noqa: F401


def raw(port, path, body=None, ac=True):
    h = {"Content-Type": "application/json", **({"X-AC": "1"} if ac else {})}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                               data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            return f.status, f.read(), f.url
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None


@pytest.mark.skipif(not shutil.which("roadmap"), reason="the roadmap CLI is not installed")
def test_rmui_proxy(server, home):
    port = server
    p = home / "gamma"
    p.mkdir()
    subprocess.run([shutil.which("roadmap"), "init"], cwd=p, check=True, capture_output=True)
    key = registry.register_project(p)
    rid = dash.rm_id(key)
    try:
        code, body, url = raw(port, "/rmui/?p=" + urllib.parse.quote(key, safe=""))
        assert code == 200 and url.endswith(f"/rmui/{rid}/") and b"<html" in body.lower()
        code, js, _ = raw(port, f"/rmui/{rid}/app.js")
        assert code == 200 and b'fetch("api/' in js and b'fetch("/api/' not in js and b'"X-AC": "1"' in js
        code, data, _ = raw(port, f"/rmui/{rid}/api/roadmap")
        assert code == 200 and json.loads(data)["phases"]
        ph = json.loads(data)["phases"][0]["id"]
        assert raw(port, f"/rmui/{rid}/api/add", {"phase": ph, "title": "via proxy"}, ac=False)[0] == 403
        code, res, _ = raw(port, f"/rmui/{rid}/api/add", {"phase": ph, "title": "via proxy"})
        assert code == 200 and json.loads(res)["id"]
        assert "via proxy" in json.dumps(json.loads(raw(port, f"/rmui/{rid}/api/roadmap")[1]))
        assert raw(port, "/rmui/nope/")[0] == 404
    finally:
        dash.rm_stop_all()


def test_rmui_access_levels():
    assert access.required_level("GET", "/rmui/abc/api/roadmap") == "view"
    assert access.required_level("POST", "/rmui/abc/api/set") == "operate"
    assert access.required_level("POST", "/api/archive") == "admin"


def test_pmjc_token_file(tmp_path):
    assert hub.pmjc_token_file({}) is None
    assert hub.pmjc_token_file({"secrets_dir": str(tmp_path)}) == tmp_path / "pmjc" / "agentdash.token"
    assert hub.pmjc_token_file({"secrets_dir": str(tmp_path), "pmjc": {"token_file": str(tmp_path / "t")}}) == tmp_path / "t"


def test_rmui_page_load_checks_the_cli(server, home, monkeypatch):
    """A Roadmap page load (not its assets or API calls) is when agentdash checks WebRoadmap main."""
    calls = []
    monkeypatch.setattr(dash, "rm_refresh", lambda force=False: calls.append(force) or {})
    monkeypatch.setattr(dash, "rm_server", lambda key: (None, "no server in this test"))
    p = home / "delta"
    (p / "docs" / "roadmap").mkdir(parents=True)
    rid = dash.rm_id(registry.register_project(p))
    assert raw(server, f"/rmui/{rid}/")[0] == 502 and calls == [False]
    raw(server, f"/rmui/{rid}/app.js")
    raw(server, f"/rmui/{rid}/api/roadmap")
    assert calls == [False]


def test_rm_refresh_stops_serves_only_when_behind(monkeypatch):
    from agentcontroller import roadmap_tool
    stopped, ensured = [], []
    monkeypatch.setattr(dash, "rm_stop_all", lambda: stopped.append(1))
    monkeypatch.setattr(roadmap_tool, "ensure_latest", lambda log=print, latest=None: ensured.append(latest) or
                        {"ok": True, "action": "upgraded", "installed": latest, "latest": latest})
    monkeypatch.setattr(roadmap_tool, "works", lambda: True)
    monkeypatch.setattr(roadmap_tool, "latest_commit", lambda timeout=8: "b" * 40)
    monkeypatch.setattr(roadmap_tool, "installed_commit", lambda: "b" * 40)
    assert dash.rm_refresh(True)["action"] == "current" and not stopped and not ensured
    assert dash.rm_refresh()["action"] == "current"  # throttled: no second GitHub check
    monkeypatch.setattr(roadmap_tool, "installed_commit", lambda: "a" * 40)
    assert dash.rm_refresh()["action"] == "current"  # still throttled
    assert dash.rm_refresh(True)["action"] == "upgraded" and stopped == [1] and ensured == ["b" * 40]
