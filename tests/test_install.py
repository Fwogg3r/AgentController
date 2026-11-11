"""install.py --dry-run on a fresh HOME: prints the plan, writes and runs nothing."""
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def dry(tmp_path, *extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "AGENTCONTROLLER_HOME": str(home / ".agentcontroller"),
           "AGENTCONTROLLER_NO_DASH": "1"}
    r = subprocess.run([sys.executable, str(REPO / "install.py"), "--dry-run", "-y",
                        "--claude-dir", str(home / ".claude"), "--claude-json", str(home / ".claude.json"), *extra],
                       capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
    return r, home


def test_fresh_home_plan(tmp_path):
    r, home = dry(tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "uv tool install --force --editable" in out
    # the roadmap CLI is either planned at its pinned commit or already on this machine
    assert "Fwogg3r/WebRoadmap@" in out or "the roadmap CLI is already installed" in out
    assert "agentdoctor" in out
    assert "@ feb9ff9 (pinned)" in out                      # batchy: planned, not cloned
    assert not (home / ".claude").exists() and not (home / ".agentcontroller").exists()   # a dry run writes nothing


def test_flags_shape_the_plan(tmp_path):
    r, _ = dry(tmp_path, "--no-roadmap", "--no-batchy", "--no-doctor", "--no-tool", "--no-dash-task")
    assert r.returncode == 0, r.stderr
    assert "WebRoadmap" not in r.stdout and "agentdoctor" not in r.stdout and "claude-batch-toolkit" not in r.stdout
    assert "uv tool install --force --editable" not in r.stdout


def test_local_flag_plans_pinned_aider(tmp_path):
    r, _ = dry(tmp_path, "--local", "--no-batchy")
    assert r.returncode == 0, r.stderr
    assert "aider.git@253f0368b873ba30d8ee26e463718f0c03614ddf" in r.stdout or "aider is already installed" in r.stdout


def test_pins_are_full_commits():
    src = (REPO / "install.py").read_text(encoding="utf-8")
    for name in ("ROADMAP_SPEC", "AIDER_SPEC"):
        assert re.search(rf'{name} = "[^"]+@[0-9a-f]{{40}}"', src), name


def test_yes_does_not_install_aider_and_init_registers_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    r, _ = dry(tmp_path, "--init", str(proj))
    assert r.returncode == 0, r.stderr
    assert "uv tool install --python" not in r.stdout       # -y alone must not pull in aider (only --local)
    assert "register" in r.stdout and "so the dashboard lists it" in r.stdout
    assert not (proj / "agentcontroller.toml").exists()     # dry run
