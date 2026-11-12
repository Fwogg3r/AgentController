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
