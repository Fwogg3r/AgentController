"""Messaging Claude Code sessions from agentdash: re-cache warning, driver launch, follow-ups, approvals."""
import json
import os

import pytest

from dashfix import home  # noqa: F401
from agentcontroller import claude_sessions, dash, registry, resume

SID = "abcdef12-0000-0000-0000-000000000000"


def transcript(path, cwd, ctx, done=True):
    usage = {"input_tokens": 10, "cache_read_input_tokens": ctx - 10, "cache_creation_input_tokens": 0, "output_tokens": 5}
    a = {"type": "assistant", "timestamp": "2026-01-01T10:00:02.000Z", "cwd": cwd,
         "message": {"id": "m1", "model": "claude-opus-5-5", "role": "assistant", "stop_reason": "end_turn" if done else None,
                     "content": [{"type": "text", "text": "done"}], "usage": usage}}
    u = {"type": "user", "timestamp": "2026-01-01T10:00:00.000Z", "cwd": cwd, "message": {"role": "user", "content": "go"}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(u) + "\n" + json.dumps(a) + "\n")


@pytest.fixture
def proj(home, monkeypatch):  # noqa: F811
    a = home / "alpha"
    a.mkdir()
    key = registry.register_project(a)
    t = home / "claude" / claude_sessions.slug_of(a) / f"{SID}.jsonl"
    launched = []

    class P:
        pid = os.getpid()  # "alive" for drive_alive

    real = dash.subprocess.Popen

    def popen(cmd, *args, **kw):  # only the driver launch is faked; git and friends run for real
        if isinstance(cmd, list) and "agentcontroller.resume" in cmd:
            launched.append((cmd, kw))
            return P()
        return real(cmd, *args, **kw)
    monkeypatch.setattr(dash.subprocess, "Popen", popen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    return key, a, t, launched


def test_warns_then_starts_then_queues(proj):
    key, a, t, launched = proj
    transcript(t, str(a), 250_000)
    name = "plan.abcdef12"
    code, res = dash.plan_inbox(key, name, {"text": "continue"})
    assert code == 409 and res["need_confirm"] and res["context_tokens"] == 250_000 and not launched

    code, res = dash.plan_inbox(key, name, {"text": "continue", "confirm_recache": True})
    assert code == 200 and res["started"]
    cmd, kw = launched[0]
    assert cmd[1:4] == ["-m", "agentcontroller.resume", "--session"] and SID in cmd and "--model" in cmd
    assert "ANTHROPIC_API_KEY" not in kw["env"]
    sd = resume.state_dir(SID)
    first = json.loads(open(cmd[cmd.index("--first") + 1], encoding="utf-8").read())
    assert first["text"] == "continue"

    code, res = dash.plan_inbox(key, name, {"text": "and more"})  # driver alive: a follow-up, no warning
    assert code == 200 and res["queued"] and len(launched) == 1
    code, _ = dash.plan_inbox(key, name, {"decision": "a1", "allow": True})
    assert code == 200
    lines = [json.loads(l) for l in (sd / "inbox.jsonl").read_text().splitlines()]
    assert lines[0]["text"] == "and more" and lines[1] == {**lines[1], "decision": "a1", "allow": True}


def test_small_context_no_warning_and_refusals(proj):
    key, a, t, launched = proj
    transcript(t, str(a), 5_000, done=False)  # mid-turn, just written: running in the app
    assert dash.plan_inbox(key, "plan.abcdef12", {"text": "x"})[0] == 409
    claude_sessions._cache.clear()  # the test rewrites the file; real transcripts only grow
    transcript(t, str(a), 5_000)
    code, res = dash.plan_inbox(key, "plan.abcdef12", {"text": "x"})
    assert code == 200 and res["started"]
    assert dash.plan_inbox(key, "plan.abcdef12.12345678", {"text": "x"})[0] == 409  # subagent


def test_overlay_and_attention(proj):
    key, a, t, launched = proj
    transcript(t, str(a), 5_000)
    sd = resume.state_dir(SID)
    sd.mkdir(parents=True)
    (sd / "pid").write_text(str(os.getpid()))
    ev = sd / "events.jsonl"
    ev.write_text(json.dumps({"kind": "drive_start"}) + "\n"
                  + json.dumps({"kind": "approval", "id": "a1", "tool": "Bash", "arg": "rm x", "ts": "2026-01-01T10:00:03"}) + "\n")
    r = next(x for x in dash.project_rows(key) if x["name"] == "plan.abcdef12")
    assert r["driven"] and r["state"] == "waiting" and r["activity"]["kind"] == "approval" and r["activity"]["aid"] == "a1"
    att = [x for x in dash.attention() if x["kind"] == "approval"]
    assert att and att[0]["action"] == {**att[0]["action"], "type": "approve", "id": "a1", "tool": "Bash"}
    with ev.open("a") as fh:
        fh.write(json.dumps({"kind": "decision", "id": "a1", "allow": True}) + "\n")
    dash._extra_cache.clear()
    r = next(x for x in dash.project_rows(key) if x["name"] == "plan.abcdef12")
    assert r["state"] == "working" and r["active"]


def test_context_tokens_parsed(proj):
    key, a, t, _ = proj
    transcript(t, str(a), 123_456)
    m = claude_sessions.session_meta(t)
    assert m["context_tokens"] == 123_456 and m["cwd"] == str(a) and m["sid"] == SID and m["state"] == "finished"


def test_new_session(proj):
    key, a, t, launched = proj
    code, res = dash.new_session(key, {"text": "start here", "model": "haiku"})
    assert code == 200 and res["run"].startswith("plan.") and res["session"].startswith(res["run"][5:])
    cmd, kw = launched[0]
    assert "--new" in cmd and cmd[cmd.index("--model") + 1] == "haiku" and cmd[cmd.index("--cwd") + 1] == str(a)
    assert "ANTHROPIC_API_KEY" not in kw["env"]
    assert dash.new_session(key, {"text": ""})[0] == 400
    assert dash.new_session(key, {"text": "x", "model": "gpt"})[0] == 400
