"""autoupdate: pmjc's checkout follows origin/main and its `pmjc serve` is restarted; roadmap goes through ensure_latest."""
import subprocess
from pathlib import Path

import pytest

from agentcontroller import autoupdate


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A bare 'GitHub' remote, a checkout of it on main, and a second clone to push from."""
    remote, co, dev = tmp_path / "remote.git", tmp_path / "pmjc", tmp_path / "dev"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(dev)], check=True, capture_output=True)
    for d in (dev,):
        git(d, "config", "user.email", "t@t"), git(d, "config", "user.name", "t")
    (dev / "a.py").write_text("1\n")
    git(dev, "add", "."), git(dev, "commit", "-qm", "one"), git(dev, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(remote), str(co)], check=True, capture_output=True)
    killed = []
    monkeypatch.setattr(autoupdate, "processes", lambda rx: [
        {"pid": 10, "ppid": 1, "cmd": f"{co}\\.venv\\Scripts\\pythonw.exe -m pmjc serve"},
        {"pid": 11, "ppid": 10, "cmd": f"{co}\\.venv\\Scripts\\pythonw.exe -m pmjc serve"}])
    monkeypatch.setattr(autoupdate, "_kill", killed.append)
    monkeypatch.setattr(autoupdate, "_healthy", lambda url, wait: True)
    monkeypatch.setattr(autoupdate, "registry", type("R", (), {"home": staticmethod(lambda: tmp_path)}))

    def push(name="b.py"):
        (dev / name).write_text("2\n")
        git(dev, "add", "."), git(dev, "commit", "-qm", name), git(dev, "push", "-q", "origin", "HEAD:main")
        return git(dev, "rev-parse", "HEAD")
    return co, push, killed


def test_current_does_nothing(repo):
    co, _, killed = repo
    r = autoupdate.update_pmjc({"checkout": str(co)}, log=lambda m: None)
    assert r["action"] == "current" and r["ok"] and not killed


def test_main_moved_fast_forwards_and_restarts_serve(repo):
    co, push, killed = repo
    new = push()
    r = autoupdate.update_pmjc({"checkout": str(co)}, log=lambda m: None)
    assert r["action"] == "updated" and r["ok"] and r["restarted"], r
    assert git(co, "rev-parse", "HEAD") == new and (co / "b.py").exists()
    assert killed == [10]  # only the top serve process; taskkill /T takes its child


def test_dirty_checkout_is_left_alone(repo):
    co, push, killed = repo
    push()
    (co / "a.py").write_text("local edit\n")
    r = autoupdate.update_pmjc({"checkout": str(co)}, log=lambda m: None)
    assert r["action"] == "skipped" and "uncommitted" in r["error"] and not killed


def test_other_branch_is_left_alone(repo):
    co, push, killed = repo
    push()
    git(co, "checkout", "-qb", "feature")
    r = autoupdate.update_pmjc({"checkout": str(co)}, log=lambda m: None)
    assert r["action"] == "skipped" and "feature" in r["error"] and not killed


def test_check_all_records_state(repo, monkeypatch):
    co, push, _ = repo
    push()
    monkeypatch.setattr(autoupdate.roadmap_tool, "ensure_latest", lambda log: {"ok": True, "action": "upgraded"})
    cfg = {"autoupdate": {"pmjc": {"checkout": str(co)}}}
    out = autoupdate.check_all(log=lambda m: None, cfg=cfg)
    assert out["pmjc"]["action"] == "updated" and out["roadmap"]["action"] == "updated"
    st = autoupdate.load_state()
    assert set(st) == {"pmjc", "roadmap"} and st["pmjc"]["checked"]


def test_settings_defaults_and_off():
    assert autoupdate.settings({})["interval"] == 300
    assert autoupdate.settings({"autoupdate": {"interval": 0}})["interval"] == 0
    assert autoupdate.settings({"autoupdate": {"interval": 5}})["interval"] == 60
    assert autoupdate.settings({"autoupdate": {"services": ["roadmap", "x"]}})["services"] == ["roadmap"]
