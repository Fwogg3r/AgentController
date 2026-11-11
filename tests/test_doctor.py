"""agentdoctor: every check is driven through monkeypatched seams; no real network, no real subnet scan."""
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agentcontroller import doctor


def _refuse(url, timeout=2.0):
    raise OSError("refused")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCONTROLLER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.delenv("AGENTDASH_PORT", raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "user").mkdir()
    # Nothing is installed and nothing answers, unless a test says so.
    monkeypatch.setattr(doctor, "_which", lambda exe: None)
    monkeypatch.setattr(doctor, "_version", lambda path, *a: "1.2.3")
    monkeypatch.setattr(doctor, "_http_json", _refuse)
    monkeypatch.setattr(doctor, "_task_exists", lambda: False)
    monkeypatch.setattr(doctor, "_dash_up", lambda port: False)
    monkeypatch.setattr(doctor, "_tool_bin_dir", lambda: None)
    monkeypatch.setattr(doctor, "_port_open", lambda *a: pytest.fail("a real socket was opened"))
    return tmp_path


def by_id(rep):
    return {c.id: c for c in rep.checks}


def test_nothing_installed_fails_required(env):
    rep = doctor.run_checks(cwd=env)
    c = by_id(rep)
    assert not rep.ok
    assert c["uv"].label == "MISSING" and c["git"].status == "missing" and c["commands"].status == "missing"
    assert c["roadmap"].label == "MISSING"
    assert c["claude"].label == "missing (recommended)" and c["aider"].label == "missing (optional)"
    assert c["lmstudio"].status == "missing" and c["pmjc"].status == "missing"
    table = doctor.format_table(rep)
    assert "Required pieces missing" in table and "What to do:" in table


def test_all_required_ok_exit_zero(env, monkeypatch):
    present = set(doctor.COMMANDS) | {"uv", "git", "roadmap"}
    monkeypatch.setattr(doctor, "_which", lambda exe: f"/bin/{exe}" if exe in present else None)
    rep = doctor.run_checks(cwd=env)
    # claude-agent-sdk is a dependency of the package, so it is importable in the test env
    assert all(x.status == "ok" for x in rep.checks if x.category == "required"), \
        [x for x in rep.checks if x.category == "required" and x.status != "ok"]
    assert rep.ok and by_id(rep)["claude"].status == "missing"  # recommended only: doesn't fail the run
    assert doctor.main(["--no-write"]) == 0


def test_commands_not_on_path_points_to_update_shell(env, monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "agentctl.exe").write_text("")
    monkeypatch.setattr(doctor, "_tool_bin_dir", lambda: bindir)
    c = doctor.check_commands()
    assert c.status == "missing" and "update-shell" in c.hint


def test_api_key_presence_only(env):
    proj = env / "proj"
    (proj / ".agentcontroller").mkdir(parents=True)
    (proj / "agentcontroller.toml").write_text("")
    assert doctor.check_keys(proj)[0].status == "missing"
    (proj / ".agentcontroller" / "api.key").write_text("")
    assert doctor.check_keys(proj)[0].status == "missing"  # empty file
    (proj / ".agentcontroller" / "api.key").write_text("sk-ant-api-SECRET\n")
    (row,) = doctor.check_keys(proj)
    assert row.status == "ok" and row.detail == "set"
    assert "SECRET" not in json.dumps(row.__dict__)


def test_keys_found_through_registry_projects(env):
    proj = env / "reg"
    (proj / ".agentcontroller").mkdir(parents=True)
    (proj / "agentcontroller.toml").write_text("")
    (proj / ".agentcontroller" / "api.key").write_text("k")
    d = env / "home" / "projects"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"key": "k", "name": "reg", "toml": str(proj / "agentcontroller.toml")}))
    rows = doctor.check_keys(env)  # cwd has no toml: found via the registry
    assert [r.status for r in rows] == ["ok"] and rows[0].name == "API key: reg"


def test_dashboard_and_task(env, monkeypatch):
    monkeypatch.setattr(doctor, "_dash_up", lambda port: port == 9999)
    (env / "home" / "config.toml").write_text("[dash]\nport = 9999\n")
    rows = {c.id: c for c in doctor.check_dashboard(doctor.load_config())}
    assert rows["dash"].status == "ok" and "9999" in rows["dash"].detail
    assert rows["config"].status == "ok"
    monkeypatch.setattr(doctor, "_task_exists", lambda: None)  # non-Windows
    assert {c.id: c for c in doctor.check_dashboard({})}["task"].label == "n/a"


def test_skills(env):
    cd, cj = env / "claude", env / "claude.json"
    assert [c.status for c in doctor.check_skills(cd, cj)] == ["missing", "missing"]
    (cd / "skills" / "api-request").mkdir(parents=True)
    (cd / "skills" / "api-request" / "SKILL.md").write_text("x")
    (cd / "skills" / "batchy").mkdir(parents=True)
    (cd / "skills" / "batchy" / "SKILL.md").write_text("x")
    assert [c.status for c in doctor.check_skills(cd, cj)] == ["ok", "warn"]
    cj.write_text(json.dumps({"mcpServers": {"claude-batch": {}}}))
    assert [c.status for c in doctor.check_skills(cd, cj)] == ["ok", "ok"]


LMS = {"data": [{"id": "m1", "type": "llm", "state": "loaded"}, {"id": "m2", "type": "llm", "state": "not-loaded"},
                {"id": "emb", "type": "embeddings"}]}
PMJC = {"ok": True, "version": "0.1.0", "connectors": 4, "tools": 30, "jobs": 2}


def fake_http(routes):
    def f(url, timeout=2.0):
        if url in routes:
            return routes[url]
        raise OSError("refused")
    return f


def test_probes_identify_services(monkeypatch):
    monkeypatch.setattr(doctor, "_http_json", fake_http({"http://h:1234/api/v0/models": LMS, "http://h:8770/api/health": PMJC,
                                                         "http://x:1234/api/v0/models": {"unrelated": 1},
                                                         "http://x:8770/api/health": {"ok": True}}))
    s = doctor.probe_lmstudio("http://h:1234/")
    assert s and s.url == "http://h:1234" and "2 model(s), 1 loaded" in s.detail
    p = doctor.probe_pmjc("http://h:8770")
    assert p and "0.1.0" in p.detail
    assert doctor.probe_lmstudio("http://x:1234") is None and doctor.probe_pmjc("http://x:8770") is None
    assert doctor.probe_pmjc("http://nobody:8770") is None


def test_local_services_and_token_hints(env, monkeypatch):
    monkeypatch.setattr(doctor, "_http_json", fake_http({doctor.LOCAL_LMSTUDIO + "/api/v0/models": LMS,
                                                         doctor.LOCAL_PMJC + "/api/health": PMJC}))
    found = doctor.find_local_services({})
    assert {s.kind for s in found} == {"lmstudio", "pmjc"} and all(s.local for s in found)
    rows = {c.id: c for c in doctor.service_checks({}, found)}
    assert rows["lmstudio"].status == "ok"
    assert rows["pmjc"].status == "warn" and "--scopes jobs" in rows["pmjc"].hint
    tok = env / "pm.token"
    cfg = {"pmjc": {"token_file": str(tok)}}
    assert {c.id: c for c in doctor.service_checks(cfg, found)}["pmjc"].status == "warn"
    tok.write_text("secret")
    assert {c.id: c for c in doctor.service_checks(cfg, found)}["pmjc"].status == "ok"


def test_subnet_hosts_is_a_slash24_and_private_only():
    h = doctor.subnet_hosts("192.168.7.20")
    assert len(h) == 253 and "192.168.7.20" not in h and h[0] == "192.168.7.1" and h[-1] == "192.168.7.254"
    assert doctor.subnet_hosts("8.8.8.8") == []          # never scan public space
    assert doctor.subnet_hosts("10.0.0.5", limit=10) == [f"10.0.0.{i}" for i in range(1, 12) if i != 5]
    assert doctor.subnet_hosts("not-an-ip") == []


def test_scan_subnet_is_mocked_and_limited(monkeypatch):
    opened = []
    http = {"http://10.0.0.3:1234/api/v0/models": LMS, "http://10.0.0.4:8770/api/health": PMJC}

    def port_open(host, port, timeout):
        opened.append((host, port))
        return (host, port) in {("10.0.0.3", 1234), ("10.0.0.4", 8770), ("10.0.0.5", 1234)}

    monkeypatch.setattr(doctor, "_port_open", port_open)
    monkeypatch.setattr(doctor, "_http_json", fake_http(http))
    hosts = doctor.subnet_hosts("10.0.0.2", limit=5)  # .1 .3 .4 .5 .6
    found = doctor.scan_subnet(hosts, workers=4)
    assert {(s.kind, s.url) for s in found} == {("lmstudio", "http://10.0.0.3:1234"), ("pmjc", "http://10.0.0.4:8770")}
    assert {h for h, _ in opened} <= set(hosts)             # nothing outside the given list was touched
    assert doctor.scan_subnet([]) == []


def test_scan_asks_first_and_declining_probes_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "subnet_hosts", lambda *a, **k: ["10.0.0.1"])
    monkeypatch.setattr(doctor, "scan_subnet", lambda hosts: pytest.fail("scanned without consent"))
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: False})())
    doctor.main(["--scan"])
    assert "scan skipped" in capsys.readouterr().out


def test_scan_with_yes_adds_and_writes_config(env, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "subnet_hosts", lambda *a, **k: ["10.0.0.3"])
    monkeypatch.setattr(doctor, "scan_subnet", lambda hosts: [doctor.Service("lmstudio", "http://10.0.0.3:1234", "x"),
                                                              doctor.Service("pmjc", "http://10.0.0.4:8770", "y")])
    doctor.main(["--scan", "-y"])
    cfg = tomllib.loads((env / "home" / "config.toml").read_text())
    assert cfg["lmstudio"]["hosts"] == {"lan-3": "http://10.0.0.3:1234"}
    assert cfg["pmjc"] == {"url": "http://10.0.0.4:8770"}
    assert "token" in capsys.readouterr().out.lower()


def test_json_output(env, capsys):
    rc = doctor.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False
    assert {c["id"] for c in out["checks"]} >= {"uv", "git", "commands", "sdk", "roadmap", "claude", "aider", "lmstudio", "pmjc"}
    assert all("label" in c for c in out["checks"])


# ---------- config merge ----------

START = '''# my comments stay
include = "Z:/shared/dash.toml"
[dash]
port = 8765   # inline comment
# [lmstudio.hosts]
# local = "http://127.0.0.1:1234"
'''


def test_merge_appends_tables_and_keeps_everything(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(START)
    done = doctor.merge_services(f, {"local": "http://127.0.0.1:1234", "lan-5": "http://10.0.0.5:1234"}, "http://10.0.0.9:8770")
    assert len(done) == 3
    text = f.read_text()
    assert text.startswith(START) and "# my comments stay" in text and "# inline comment" in text
    cfg = tomllib.loads(text)
    assert cfg["include"] == "Z:/shared/dash.toml" and cfg["dash"]["port"] == 8765
    assert cfg["lmstudio"]["hosts"]["lan-5"] == "http://10.0.0.5:1234" and cfg["pmjc"]["url"] == "http://10.0.0.9:8770"
    assert (tmp_path / "config.toml.bak").read_text() == START
    assert doctor.merge_services(f, {"lan-5": "http://10.0.0.5:1234"}, "http://x:1") == []  # idempotent, never replaces


def test_merge_inserts_into_existing_tables(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[lmstudio.hosts]\nme = "http://127.0.0.1:1234"   # mine\n\n[pmjc]\ntoken_file = "t"  # keep\n\n[dash]\nport = 1\n')
    doctor.merge_services(f, {"lan-2": "http://10.0.0.2:1234", "again": "http://localhost:1234"}, "http://10.0.0.9:8770")
    text = f.read_text()
    cfg = tomllib.loads(text)
    assert cfg["lmstudio"]["hosts"] == {"lan-2": "http://10.0.0.2:1234", "me": "http://127.0.0.1:1234"}  # localhost dup skipped
    assert cfg["pmjc"] == {"url": "http://10.0.0.9:8770", "token_file": "t"} and cfg["dash"]["port"] == 1
    assert "# mine" in text and "# keep" in text


def test_merge_keeps_existing_pmjc_url_and_creates_missing_file(tmp_path):
    f = tmp_path / "sub" / "config.toml"
    doctor.merge_services(f, None, "http://a:8770")
    assert tomllib.loads(f.read_text())["pmjc"]["url"] == "http://a:8770"
    assert doctor.merge_services(f, None, "http://b:8770") == []
    assert tomllib.loads(f.read_text())["pmjc"]["url"] == "http://a:8770"


def test_merge_refuses_invalid_toml_and_writes_nothing(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("[dash\nbroken")
    with pytest.raises(ValueError):
        doctor.merge_services(f, {"a": "http://x:1234"}, None)
    assert f.read_text() == "[dash\nbroken" and not (tmp_path / "config.toml.bak").exists()
    f.write_text('lmstudio = { hosts = { a = "http://x:1234" } }\n')
    with pytest.raises(ValueError):
        doctor.merge_services(f, {"b": "http://y:1234"}, None)


def test_plan_config_writes():
    def lms(url, local=False):
        return doctor.Service("lmstudio", url, "", local)
    # a lone local server is the built-in default: nothing to write
    assert doctor.plan_config_writes({}, [lms(doctor.LOCAL_LMSTUDIO, True)]) == ({}, None)
    # a LAN server replaces the default, so local is added too
    add, pm = doctor.plan_config_writes({}, [lms(doctor.LOCAL_LMSTUDIO, True), lms("http://10.0.0.5:1234")])
    assert add == {"local": doctor.LOCAL_LMSTUDIO, "lan-5": "http://10.0.0.5:1234"} and pm is None
    # already configured: skipped
    cfg = {"lmstudio": {"hosts": {"x": "http://10.0.0.5:1234/"}}, "pmjc": {"url": "http://p:8770"}}
    assert doctor.plan_config_writes(cfg, [lms("http://10.0.0.5:1234"), doctor.Service("pmjc", "http://q:8770")]) == ({}, None)
    assert doctor.plan_config_writes({}, [doctor.Service("pmjc", "http://q:8770"),
                                          doctor.Service("pmjc", "http://127.0.0.1:8770", "", True)])[1] == "http://127.0.0.1:8770"


def test_module_runs_as_script():
    r = subprocess.run([sys.executable, "-m", "agentcontroller.doctor", "--help"], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parents[1])
    assert r.returncode == 0 and "--scan" in r.stdout
