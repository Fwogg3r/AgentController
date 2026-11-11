"""Fixtures for the agentlocal tests: a temp git repo and a tiers file. No model, no aider, no network."""
import os
import subprocess
from pathlib import Path

import pytest

TIERS = """
[models.m]
id = "model-id"
tier = "small"
context_tokens = 32768
max_output = 8192
reserve = 1024
chars_per_token = 4.0

[tiers.small]
runner = "staged"
max_stages = 3
max_edit_files = 2
allow_new_files = true
thinking = { plan = 2000, spec = 2000, fix = 1000, edit = 0 }
max_fix_attempts = 1
autonomy = "edit"
gates = ["plan", "stage"]

[tiers.nonew]
allow_new_files = false
gates = []

[tiers.wild]
autonomy = "full"
gates = []

[tiers.locked]
autonomy = "locked"
gates = []

[tiers.top]
runner = "passthrough"
decompose = "never"
gates = []
"""


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "commit.gpgsign", "false")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (root / "big.txt").write_text("x" * 400_000 + "\n", encoding="utf-8")  # ~115k tokens at 4 chars/token
    (root / ".gitignore").write_text(".agentcontroller/\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    tiers = tmp_path / "tiers.toml"
    tiers.write_text(TIERS, encoding="utf-8")
    monkeypatch.setenv("AGENTCONTROLLER_TIERS", str(tiers))
    monkeypatch.delenv("AGENTLOCAL_SANDBOX", raising=False)
    monkeypatch.setenv("AGENTCONTROLLER_HOME", str(tmp_path / "achome"))
    monkeypatch.setenv("AGENTCONTROLLER_NO_DASH", "1")
    monkeypatch.delenv("AGENTCONTROLLER_PARENT", raising=False)
    return root


@pytest.fixture
def settings(repo):
    from agentcontroller.local import config

    def make(tier=None, model="m"):
        return config.load(model, repo, tier)
    return make


def reply(text="", reasoning="", finish="stop", tin=100, tout=50):
    from agentcontroller.local.llm import Reply
    return Reply(text, reasoning, finish, tin, tout, 0.1)


@pytest.fixture
def fake_chat(monkeypatch):
    """Queue replies for llm.chat; records every call's messages."""
    from agentcontroller.local import llm
    calls, queue = [], []

    def chat(m, messages, max_tokens, think_budget=None, on_event=None):
        calls.append({"messages": messages, "max_tokens": max_tokens, "think": think_budget})
        r = queue.pop(0) if queue else reply("ok")
        if on_event and r.reasoning:
            on_event("thinking", {"text": r.reasoning})
        if on_event and r.text:
            on_event("answer", {"text": r.text})
        return r
    monkeypatch.setattr(llm, "chat", chat)
    chat.calls, chat.queue = calls, queue
    return chat


os.environ.setdefault("PYTHONIOENCODING", "utf-8")
