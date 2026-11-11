"""agentlocal run machinery: events, approvals, scope guard, replan wiring, registry hooks. No model, no aider."""
import json
import queue
import sys
import threading
import time

import pytest
from conftest import git, reply

from agentcontroller.local import cli, run as runmod
from agentcontroller.local.run import Run, approve, scope_guard


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(runmod, "POLL", 0.02)


class NoTTY:
    def isatty(self):
        return False


class TTY(NoTTY):
    def isatty(self):
        return True


def events(run):
    return [json.loads(l) for l in (run.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


# ---------- Run.emit ----------

def test_emit_splits_thinking_and_answer(repo, capsys):
    run = Run(repo)
    run.emit("call_start", {"phase": "spec", "tokens_in": 10, "context_tokens": 100}, 2)
    run.emit("thinking", {"text": "hmm "})
    run.emit("thinking", {"text": "ok"})
    run.emit("answer", {"text": "DONE"})
    run.emit("call", {"phase": "spec", "tokens_in": 10}, 2)
    th, an = (run.dir / "thinking.txt").read_text(encoding="utf-8"), (run.dir / "answer.txt").read_text(encoding="utf-8")
    assert th.endswith("hmm ok") and "stage 2 spec" in th
    assert an.endswith("DONE") and "hmm" not in an
    evs = events(run)
    assert [e["kind"] for e in evs] == ["call_start", "call"]       # text never goes into events
    assert evs[0]["stage"] == 2 and evs[0]["run_id"] == run.id
    assert "[call]" in capsys.readouterr().out


def test_outcome_from_events(repo):
    run = Run(repo)
    run.emit("done", {"ok": True})
    assert run.outcome == "done"
    run.emit("done", {"aider_exit": 1})
    assert run.outcome == "failed"
    run.emit("stopped", {"reason": "denied"})
    assert run.outcome == "stopped"


# ---------- approvals ----------

def test_gate_not_in_tier_needs_no_human(repo, settings, monkeypatch):
    monkeypatch.setattr(sys, "stdin", NoTTY())
    run = Run(repo)
    assert approve(run, settings(), "tests", "x", {}) is True
    assert not (run.dir / "approvals").exists()


def _answer_when_pending(run, approve_value, delay=0.05):
    def go():
        adir = run.dir / "approvals"
        for _ in range(200):
            pend = list(adir.glob("*.pending.json")) if adir.exists() else []
            if pend:
                time.sleep(delay)
                aid = pend[0].name[:-len(".pending.json")]
                (adir / f"{aid}.json").write_text(json.dumps({"approve": approve_value}), encoding="utf-8")
                return
            time.sleep(0.01)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


@pytest.mark.parametrize("value", [True, False])
def test_decision_file_without_tty(repo, settings, monkeypatch, value):
    monkeypatch.setattr(sys, "stdin", NoTTY())
    run = Run(repo)
    t = _answer_when_pending(run, value)
    assert approve(run, settings(), "plan", "3 stages", {"stages": []}) is value
    t.join(1)
    adir = run.dir / "approvals"
    assert not list(adir.glob("*.pending.json"))                  # no longer shown as pending
    rec = json.loads(next(adir.glob("plan-*.json")).read_text(encoding="utf-8"))
    assert rec["approve"] is value and rec["by"] == "dashboard" and rec["gate"] == "plan"
    assert [e["kind"] for e in events(run)][-2:] == ["approval", "decision"]
    assert run.denied == (None if value else "plan")


def test_decision_file_wins_while_console_waits(repo, settings, monkeypatch):
    monkeypatch.setattr(sys, "stdin", TTY())
    monkeypatch.setattr(runmod, "_stdin_q", queue.Queue())        # a console that never answers
    run = Run(repo)
    t = _answer_when_pending(run, True)
    assert approve(run, settings(), "stage", "stage 1", {}) is True
    t.join(1)
    assert not list((run.dir / "approvals").glob("*.pending.json"))


def test_console_answer(repo, settings, monkeypatch):
    monkeypatch.setattr(sys, "stdin", TTY())
    q = queue.Queue()
    q.put("stale\n")
    monkeypatch.setattr(runmod, "_stdin_q", q)
    run = Run(repo)
    threading.Timer(0.1, lambda: q.put("y\n")).start()
    assert approve(run, settings(), "stage", "stage 1", {}) is True
    rec = json.loads(next((run.dir / "approvals").glob("stage-*.json")).read_text(encoding="utf-8"))
    assert rec["by"] == "console"


def test_hook_decides(repo, settings, monkeypatch):
    s = settings()
    s.repo.approve_hook = f'"{sys.executable}" -c "import sys, json; sys.exit(0 if json.load(sys.stdin)[\'gate\'] == \'plan\' else 1)"'
    run = Run(repo)
    assert approve(run, s, "plan", "x", {}) is True
    assert approve(run, s, "stage", "x", {}) is False
    assert not list((run.dir / "approvals").glob("*.pending.json"))


# ---------- scope guard ----------

def test_scope_guard_reverts_outside_and_reports_protected(repo):
    run = Run(repo)
    (repo / "notes.txt").write_text("mine, from before the run\n", encoding="utf-8")   # pre-existing untracked
    baseline = set(runmod.changed_paths(repo))
    (repo / "src" / "a.py").write_text("def a():\n    return 42\n", encoding="utf-8")   # allowed
    (repo / "src" / "b.py").write_text("hacked\n", encoding="utf-8")                    # stray tracked
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")  # allowed, protected
    (repo / "src" / "new").mkdir()
    (repo / "src" / "new" / "x.py").write_text("x = 1\n", encoding="utf-8")             # stray untracked, in a new dir
    (repo / "big.txt").unlink()                                                        # stray deletion
    (repo / "src" / "staged.py").write_text("y = 1\n", encoding="utf-8")
    git(repo, "add", "src/staged.py")                                                 # stray, staged by the tool
    allowed = {"src/a.py", "tests/test_a.py"}
    prot = scope_guard(repo, allowed, ["tests/**"], run, 1, baseline)
    assert prot == ["tests/test_a.py"]
    assert (repo / "src" / "b.py").read_text(encoding="utf-8") == "def b():\n    return 2\n"
    assert not (repo / "src" / "new" / "x.py").exists() and not (repo / "src" / "staged.py").exists()
    assert (repo / "big.txt").exists()
    assert "42" in (repo / "src" / "a.py").read_text(encoding="utf-8")
    assert (repo / "notes.txt").exists()                                                  # never touched
    rev = next(e for e in events(run) if e["kind"] == "scope")["reverted"]
    assert set(rev) == {"src/b.py", "src/new/x.py", "big.txt", "src/staged.py"}
    assert sorted(runmod.changed_paths(repo)) == ["notes.txt", "src/a.py", "tests/test_a.py"]


def test_check_runs_without_shell(repo):
    ok, out = runmod._check(f'"{sys.executable}" -c "print(41 + 1)"', repo)
    assert ok and "42" in out
    ok, out = runmod._check("definitely-not-a-command-xyz", repo)
    assert not ok


# ---------- run_plan: replan wiring ----------

def _plan(repo):
    st = lambda i, f, cov: {"id": i, "goal": f"stage {i}", "edit_files": [f], "create_files": [], "read_files": [],
                            "acceptance": "uv run pytest -q", "covers": [cov], "depends_on": []}
    return {"idea": "two things", "criteria": ["a", "b"], "stages": [st(1, "src/a.py", 0), st(2, "src/b.py", 1)]}


@pytest.fixture
def staged(repo, settings, monkeypatch, fake_chat):
    s = settings("nonew")                     # no gates
    s.repo.test_cmd = f'"{sys.executable}" -c "pass"'
    run = Run(repo)
    run.save("plan.json", _plan(repo))
    return s, run


def test_replan_after_failed_stage(repo, staged, monkeypatch, fake_chat):
    s, run = staged
    attempts = []

    def fake_stage(s_, run_, root, idea, st, n, summaries, baseline=frozenset()):
        attempts.append(st["id"])
        f = st["edit_files"][0]
        (root / f).write_text(f"# stage {st['id']}\n", encoding="utf-8")
        if st["id"] == 2:                      # fails every attempt
            run_.last_failure = "FAILED tests/test_b.py::test_b - AssertionError"
            return False
        return True
    monkeypatch.setattr(runmod, "run_stage", fake_stage)
    newstage = {"id": 3, "goal": "smaller step", "edit_files": ["src/b.py"], "create_files": [], "read_files": [],
                "acceptance": "uv run pytest -q", "covers": [1], "depends_on": [1]}
    fake_chat.queue += [reply("summary 1"), reply(json.dumps({"stages": [newstage]})), reply("summary 3")]
    assert runmod.run_plan(s, run, repo) is True
    assert attempts == [1, 2, 3]
    prog = run.load("progress.json")
    assert prog["done"] == [1, 3] and prog["replans"] == 1
    assert [st["id"] for st in run.load("plan.json")["stages"]] == [1, 3]
    assert run.load("plan.v1.json")["stages"][1]["id"] == 2
    ev = next(e for e in events(run) if e["kind"] == "replan")
    assert ev["result"] == "accepted" and ev["stash"]
    assert "FAILED tests/test_b.py" in fake_chat.calls[1]["messages"][1]["content"]
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert git(repo, "log", "--format=%s", "-2").splitlines() == ["agentlocal stage 3: smaller step",
                                                                  "agentlocal stage 1: stage 1"]


def test_at_most_one_replan_and_failed_changes_restored(repo, staged, monkeypatch, fake_chat):
    s, run = staged

    def fake_stage(s_, run_, root, idea, st, n, summaries, baseline=frozenset()):
        (root / st["edit_files"][0]).write_text(f"# attempt at {st['id']}\n", encoding="utf-8")
        if st["id"] == 1:
            return True
        run_.last_failure = "FAILED"
        return False
    monkeypatch.setattr(runmod, "run_stage", fake_stage)
    again = {"id": 4, "goal": "retry", "edit_files": ["src/b.py"], "create_files": [], "read_files": [],
             "acceptance": "uv run pytest -q", "covers": [1], "depends_on": []}
    fake_chat.queue += [reply("summary 1"), reply(json.dumps({"stages": [again]}))]
    assert runmod.run_plan(s, run, repo) is False
    replans = [e for e in events(run) if e["kind"] == "replan"]
    assert len(replans) == 1                               # the second failure does not replan again
    assert events(run)[-1]["kind"] == "stopped" and events(run)[-1]["reason"] == "failed"
    assert (repo / "src" / "b.py").read_text(encoding="utf-8") == "# attempt at 4\n"   # left for a human


def test_replan_continue_restores_failed_stage(repo, staged, monkeypatch, fake_chat):
    s, run = staged

    def fake_stage(s_, run_, root, idea, st, n, summaries, baseline=frozenset()):
        (root / st["edit_files"][0]).write_text("# partial\n", encoding="utf-8")
        if st["id"] == 1:
            return True
        run_.last_failure = "FAILED"
        return False
    monkeypatch.setattr(runmod, "run_stage", fake_stage)
    fake_chat.queue += [reply("summary 1"), reply("continue")]
    assert runmod.run_plan(s, run, repo) is False
    assert next(e for e in events(run) if e["kind"] == "replan")["result"] == "continue"
    assert (repo / "src" / "b.py").read_text(encoding="utf-8") == "# partial\n"
    assert git(repo, "stash", "list") == ""


def test_no_replan_when_stage_fails_without_test_failure(repo, staged, monkeypatch, fake_chat):
    s, run = staged
    monkeypatch.setattr(runmod, "run_stage", lambda *a, **k: False)   # e.g. preflight or empty reply
    assert runmod.run_plan(s, run, repo) is False
    assert not [e for e in events(run) if e["kind"] == "replan"]


def test_resume_refuses_foreign_head_but_accepts_descendant(repo, staged, monkeypatch, fake_chat):
    s, run = staged
    head = git(repo, "rev-parse", "HEAD").strip()
    run.save("progress.json", {"done": [1, 2], "summaries": [], "shas": ["0" * 40]})
    assert runmod.run_plan(s, run, repo) is False
    (repo / "src" / "a.py").write_text("# by hand\n", encoding="utf-8")
    git(repo, "commit", "-qam", "manual fix")
    run.save("progress.json", {"done": [1, 2], "summaries": [], "shas": [head]})
    assert runmod.run_plan(s, run, repo) is True                       # HEAD descends from the last stage commit


# ---------- registry hooks ----------

def test_register_and_finish(repo, settings, monkeypatch, tmp_path):
    from agentcontroller import registry
    monkeypatch.setenv("AGENTCONTROLLER_PARENT", "api-123")
    monkeypatch.setattr(cli, "CURRENT", None)
    run = Run(repo)
    cli._register(run, settings(), repo, "Add a widget\nwith details")
    rec = registry.agents()[0]
    assert rec["id"] == run.id and rec["kind"] == "local" and rec["status"] == "running"
    assert rec["title"] == "Add a widget" and rec["context_tokens"] == 32768 and rec["model_id"] == "model-id"
    assert rec["provider"] == "lmstudio" and rec["parent"] == "api-123" and rec["events"].endswith("events.jsonl")
    cli._finish("done")
    rec = registry.agents()[0]
    assert rec["status"] == "done" and rec["pid"] is None and rec["ended"]


def test_registry_failure_never_breaks_a_run(repo, settings, monkeypatch):
    from agentcontroller import registry
    monkeypatch.setattr(registry, "register_agent", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(cli, "CURRENT", None)
    cli._register(Run(repo), settings(), repo, "x")      # prints a note, does not raise
    monkeypatch.setattr(registry, "update_agent", lambda *a, **k: 1 / 0)
    cli._finish("failed")


def test_run_stage_spec_fix_loop(repo, settings, monkeypatch, fake_chat):
    s = settings("nonew")
    run = Run(repo)
    st = _plan(repo)["stages"][0]
    edits = []

    def fake_aider(s_, run_, root, spec, edit, read, log, passthrough=False):
        edits.append(spec.read_text(encoding="utf-8"))
        (root / "src" / "a.py").write_text(f"# v{len(edits)}\n", encoding="utf-8")
        (root / "src" / "b.py").write_text("stray\n", encoding="utf-8")
        return 0
    results = iter([(False, "FAILED test_a - AssertionError"), (True, "1 passed")])
    monkeypatch.setattr(runmod, "_aider", fake_aider)
    monkeypatch.setattr(runmod, "_check", lambda cmd, root, timeout=900: next(results))
    fake_chat.queue += [reply("SPEC v1", reasoning="think 1"), reply("SPEC v2")]
    assert runmod.run_stage(s, run, repo, "idea", st, 2, []) is True
    assert edits == ["SPEC v1", "SPEC v2"]
    assert "FAILED test_a" in fake_chat.calls[1]["messages"][-1]["content"]
    calls = [e for e in events(run) if e["kind"] == "call"]
    assert [c["phase"] for c in calls] == ["spec", "fix"] and all(c["context_tokens"] == 32768 for c in calls)
    assert (repo / "src" / "b.py").read_text(encoding="utf-8") == "def b():\n    return 2\n"   # scope guard
    assert (run.dir / "stages" / "01" / "reasoning0.txt").read_text(encoding="utf-8") == "think 1"
    assert "think 1" in (run.dir / "thinking.txt").read_text(encoding="utf-8")


def test_run_stage_same_failure_twice_sets_last_failure(repo, settings, monkeypatch, fake_chat):
    s = settings("nonew")
    s.tier.max_fix_attempts = 3
    run = Run(repo)
    monkeypatch.setattr(runmod, "_aider", lambda *a, **k: 0)
    monkeypatch.setattr(runmod, "_check", lambda cmd, root, timeout=900: (False, "FAILED test_a - Error"))
    fake_chat.queue += [reply("S1"), reply("S2"), reply("S3")]
    assert runmod.run_stage(s, run, repo, "idea", _plan(repo)["stages"][0], 2, []) is False
    assert run.last_failure and len(fake_chat.calls) == 2      # stopped at the repeat, not after 4 attempts
