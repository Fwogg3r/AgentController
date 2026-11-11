import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated ~/.agentcontroller and ~/.claude/projects, with hub's caches reset."""
    from agentcontroller import dash, hub
    monkeypatch.setenv("AGENTCONTROLLER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTCONTROLLER_NO_DASH", "1")  # tests must never spawn a dashboard
    monkeypatch.setattr(hub, "CLAUDE_BASE", tmp_path / "claude")
    (tmp_path / "claude").mkdir()
    for name in ("_claude_cache", "_gcfg", "_pmjc_cache", "_lms_cache"):
        monkeypatch.setattr(hub, name, (0.0, {}))
    monkeypatch.setattr(hub, "_slug_cwd", {})
    monkeypatch.setattr(hub, "_main_of", {})
    dash._clear_caches()
    dash._cfgs.clear()
    return tmp_path
