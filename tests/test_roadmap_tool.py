"""roadmap_tool: keep the roadmap CLI at WebRoadmap main without ever forcing past a running `roadmap`."""
import pytest

from agentcontroller import roadmap_tool as rt

A, B = "a" * 40, "b" * 40


@pytest.fixture
def fake(monkeypatch):
    st = {"installed": A, "works": True, "running": [], "installs": [], "result": B}

    def run(cmd, timeout):
        st["installs"].append(cmd)
        st["installed"] = st["result"]
        return type("P", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(rt, "installed_commit", lambda: st["installed"])
    monkeypatch.setattr(rt, "works", lambda: st["works"])
    monkeypatch.setattr(rt, "running", lambda: st["running"])
    monkeypatch.setattr(rt, "_run", run)
    monkeypatch.setattr(rt.shutil, "which", lambda exe: f"/bin/{exe}")
    return st


def test_current_does_nothing(fake):
    r = rt.ensure_latest(log=lambda m: None, latest=A)
    assert r["action"] == "current" and r["ok"] and not fake["installs"]


def test_behind_installs_mains_exact_commit(fake):
    r = rt.ensure_latest(log=lambda m: None, latest=B)
    assert r["action"] == "upgraded" and r["installed"] == B
    assert fake["installs"] == [["uv", "tool", "install", "--force", "--quiet", f"git+{rt.REPO_URL}@{B}"]]


def test_broken_install_is_repaired_even_at_main(fake):
    fake["installed"], fake["works"] = B, False
    calls = []
    fake_run = rt._run

    def run(cmd, timeout):
        calls.append(cmd)
        fake["works"] = True
        return fake_run(cmd, timeout)

    rt._run = run
    try:
        assert rt.ensure_latest(log=lambda m: None, latest=B)["action"] == "upgraded" and calls
    finally:
        rt._run = fake_run


def test_running_roadmap_blocks_the_upgrade(fake):
    fake["running"] = [1234]
    r = rt.ensure_latest(log=lambda m: None, latest=B)
    assert r["action"] == "skipped" and "1234" in r["error"] and not fake["installs"] and r["ok"]


def test_offline_keeps_the_installed_version(fake, monkeypatch):
    monkeypatch.setattr(rt, "latest_commit", lambda timeout=8: None)
    r = rt.ensure_latest(log=lambda m: None)
    assert r["action"] == "skipped" and not fake["installs"]


def test_failed_install_retries_once_then_reports(fake):
    fake["result"] = A  # uv "succeeds" but the commit doesn't move
    r = rt.ensure_latest(log=lambda m: None, latest=B)
    assert r["action"] == "failed" and len(fake["installs"]) == 2
