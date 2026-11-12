"""agentdash live reload: restart on a settled source change that imports; keep running on a broken one."""
import pytest

from agentcontroller import dash


class Exited(Exception):
    pass


def run_reloader(monkeypatch, stamps, ok):
    seq = iter(stamps)
    calls = {"close": 0, "popen": None, "ok": 0}
    monkeypatch.setattr(dash, "RELOAD_POLL", 0)
    monkeypatch.setattr(dash, "RELOAD_SETTLE", 0)
    monkeypatch.setattr(dash, "py_stamp", lambda: next(seq))

    def code_ok():
        calls["ok"] += 1
        return None if ok else "SyntaxError: boom"
    monkeypatch.setattr(dash, "code_ok", code_ok)
    monkeypatch.setattr(dash.subprocess, "Popen", lambda cmd, **kw: calls.update(popen=cmd))

    def exit_(code):
        raise Exited(code)
    monkeypatch.setattr(dash.os, "_exit", exit_)

    def close():
        calls["close"] += 1
    with pytest.raises((Exited, StopIteration)):
        dash.reloader(close)
    return calls


def test_restart_after_settled_change(monkeypatch):
    # start a; poll a; poll b -> settle check c (still editing); poll c -> settle c -> restart
    calls = run_reloader(monkeypatch, ["a", "a", "b", "c", "c", "c"], ok=True)
    assert calls["close"] == 1 and calls["ok"] == 1 and calls["popen"]


def test_broken_code_keeps_running(monkeypatch):
    calls = run_reloader(monkeypatch, ["a", "b", "b"], ok=False)  # runs out of stamps instead of restarting
    assert calls["close"] == 0 and calls["popen"] is None and calls["ok"] == 1


def test_code_stamp_tracks_static(tmp_path, monkeypatch):
    (tmp_path / "static").mkdir()
    (tmp_path / "x.py").write_text("a")
    page = tmp_path / "static" / "index.html"
    page.write_text("1")
    monkeypatch.setattr(dash, "PKG_DIR", tmp_path)
    before, py = dash.code_stamp(), dash.py_stamp()
    page.write_text("22")
    assert dash.code_stamp() != before and dash.py_stamp() == py
