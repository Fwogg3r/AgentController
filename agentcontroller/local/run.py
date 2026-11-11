"""Run state, events, approvals, and the staged runner."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import budget, llm, plan as planmod, prompts
from .config import Settings

POLL = 0.5  # seconds between checks for a dashboard decision file


class Run:
    """.agentcontroller/runs/<id>/: plan.json, progress.json, events.jsonl, thinking.txt, answer.txt, stages/NN/*."""

    def __init__(self, root: Path, run_id: str | None = None):
        self.id = run_id or time.strftime("local-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.dir = root / ".agentcontroller" / "runs" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.outcome: str | None = None       # done | failed | stopped, from the events; read by the CLI
        self.last_failure: str | None = None  # test output of a stage that failed every attempt (replan input)
        self.denied: str | None = None        # the last gate a human said no to

    def _append(self, name: str, text: str) -> None:
        with open(self.dir / name, "a", encoding="utf-8") as f:   # closed per chunk, so readers see it at once
            f.write(text)
            f.flush()

    def emit(self, kind: str, d: dict, stage: int | None = None) -> None:
        if kind in ("thinking", "answer"):        # stream text to files; events carry only sizes
            self._append(f"{kind}.txt", d["text"])
            return
        if kind == "call_start":                  # mark where each call's stream begins in the text files
            hdr = f"\n\n=== {time.strftime('%H:%M:%S')} stage {stage} {d.get('phase')} ===\n"
            self._append("thinking.txt", hdr)
            self._append("answer.txt", hdr)
        if kind == "done":
            self.outcome = "done" if d.get("ok", True) and not d.get("aider_exit") else "failed"
        elif kind == "stopped":
            self.outcome = "stopped" if d.get("reason") == "denied" else "failed"
        elif kind == "plan_failed":
            self.outcome = "failed"
        ev = {"ts": round(time.time(), 2), "run_id": self.id, "stage": stage, "kind": kind, **d}
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        if kind != "call_start":
            print(f"[{kind}] " + json.dumps(d)[:300], flush=True)

    def load(self, name: str, default=None):
        p = self.dir / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def save(self, name: str, data) -> None:
        tmp = self.dir / f"{name}.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / name)


# ---------- approvals ----------

_stdin_q: queue.Queue | None = None


def _console() -> queue.Queue:
    """One background reader for stdin, so a console prompt and a dashboard decision file can race."""
    global _stdin_q
    if _stdin_q is None:
        _stdin_q = queue.Queue()

        def read():
            for line in sys.stdin:
                _stdin_q.put(line)
            _stdin_q.put(None)
        threading.Thread(target=read, daemon=True, name="agentlocal-stdin").start()
    return _stdin_q


def _read_decision(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):   # not there yet, or half written
        return None


def approve(run: Run, s: Settings, gate: str, summary: str, detail: dict) -> bool:
    """Gates in the tier's gates need a human. A pending file approvals/<id>.pending.json is written; the answer comes
    from the repo's approve_hook (exit 0 = yes) if set, else from whichever comes first: a decision file
    approvals/<id>.json {"approve": true|false} (written by the dashboard) or a console answer (when stdin is a tty).
    Once decided, the pending file is removed and approvals/<id>.json holds the request and the decision."""
    if gate not in s.gates():
        return True
    aid = f"{gate}-{uuid.uuid4().hex[:6]}"
    req = {"id": aid, "run_id": run.id, "gate": gate, "summary": summary, "detail": detail, "asked": time.time()}
    adir = run.dir / "approvals"
    adir.mkdir(exist_ok=True)
    pending, decision = adir / f"{aid}.pending.json", adir / f"{aid}.json"
    pending.write_text(json.dumps(req, indent=2), encoding="utf-8")
    run.emit("approval", {"gate": gate, "id": aid, "summary": summary})
    by, note = "", ""
    if s.repo.approve_hook:
        ok, by = subprocess.run(s.repo.approve_hook, input=json.dumps(req), text=True, shell=True).returncode == 0, "hook"
    else:
        tty = sys.stdin is not None and sys.stdin.isatty()
        q = _console() if tty else None
        if q:
            while not q.empty():      # drop lines typed before this question
                q.get_nowait()
            print(f"\nApprove {gate}: {summary}? [y/N] (or answer in the dashboard) ", end="", flush=True)
        else:
            print(f"waiting for {decision} with {{\"approve\": true|false}} (the dashboard writes it)", flush=True)
        ok = None
        while ok is None:
            d = _read_decision(decision)
            if isinstance(d, dict) and "approve" in d:
                ok, by, note = bool(d["approve"]), d.get("by") or "dashboard", d.get("note") or ""
                if q:
                    print(f"\n(answered in the dashboard: {'yes' if ok else 'no'})", flush=True)
                break
            if q:
                try:
                    line = q.get(timeout=POLL)
                except queue.Empty:
                    continue
                if line is None:          # stdin closed: only the dashboard can answer now
                    q = None
                    continue
                ok, by = line.strip().lower() in ("y", "yes"), "console"
            else:
                time.sleep(POLL)
    rec = {**req, "approve": ok, "by": by, "note": note, "decided": time.time()}
    tmp = adir / f"{aid}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    os.replace(tmp, decision)
    pending.unlink(missing_ok=True)
    if not ok:
        run.denied = gate
    run.emit("decision", {"id": aid, "gate": gate, "approve": ok, "by": by})
    return ok


# ---------- git and tools ----------

def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout


def git_ok(root: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=root, capture_output=True).returncode == 0


def changed_paths(root: Path) -> list[str]:
    """Every changed, deleted or untracked file (individual files, both sides of a rename), repo-relative."""
    parts = git(root, "status", "--porcelain", "-z", "--untracked-files=all").split("\0")
    out, i = [], 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        out.append(e[3:])
        if e[0] in "RC" and i < len(parts):   # the original path follows a rename/copy
            out.append(parts[i])
            i += 1
    return list(dict.fromkeys(out))


def _files_block(root: Path, files: list[str]) -> str:
    out = []
    for f in files:
        p = root / f
        body = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else "(new file)"
        out.append(f"## {f}\n```\n{body}\n```")
    return "\n\n".join(out) or "(none)"


def _aider(s: Settings, run: Run, root: Path, spec_file: Path, edit: list[str], read: list[str], log: Path,
           passthrough: bool = False) -> int:
    m, t = s.model, s.tier
    env = {**os.environ, "OPENAI_API_BASE": m.endpoint, "OPENAI_API_KEY": m.api_key, "PYTHONIOENCODING": "utf-8"}
    settings = run.dir / "aider.model.settings.yml"
    name = m.aider_model or f"openai/{m.id}"
    extras = {"extra_params": {"max_tokens": m.max_output, **m.sampling}, **m.aider_settings}
    extra = "".join(f"  {k}: {json.dumps(v)}\n" for k, v in extras.items()
                    if k not in ("name", "edit_format", "use_repo_map", "reasoning_tag"))
    settings.write_text(f"- name: {name}\n  edit_format: {t.edit_format}\n"
                        f"  use_repo_map: {'true' if t.map_tokens else 'false'}\n"
                        f"  reasoning_tag: {m.reasoning.get('tag', 'think')}\n{extra}", encoding="utf-8")
    meta = run.dir / "aider.model.metadata.json"
    meta.write_text(json.dumps({name: {"max_input_tokens": m.context_tokens, "max_output_tokens": m.max_output,
                                       "max_tokens": m.max_output, "input_cost_per_token": 0,
                                       "output_cost_per_token": 0, "litellm_provider": "openai", "mode": "chat"}}))
    # Tests run model-written code: only a passthrough run whose autonomy allows it and that has no tests gate lets
    # aider run them itself (the old run.sh "edit" profile); staged runs test through agentlocal's gated check.
    autotest = passthrough and s.autonomy != "locked" and "tests" not in s.gates()
    tests = ["--test-cmd", s.repo.test_cmd, "--auto-test"] if autotest else ["--no-auto-test"]
    cmd = ["aider", "--model", name, "--model-settings-file", str(settings), "--model-metadata-file", str(meta),
           "--no-auto-commits", "--no-dirty-commits", "--no-gitignore", "--no-check-update", "--analytics-disable",
           "--no-show-model-warnings", "--no-detect-urls", "--no-pretty", "--no-stream", "--no-fancy-input",
           "--yes-always", "--no-suggest-shell-commands", "--no-auto-lint", *tests,
           "--map-tokens", str(t.map_tokens), "--timeout", str(m.timeout),
           "--chat-history-file", str(log) + ".chat.md", "--input-history-file", str(run.dir / "aider.hist"),
           "--message-file", str(spec_file), *edit]
    instr = Path(os.path.expanduser(t.instructions)) if t.instructions else None
    if passthrough and instr and instr.is_file():   # staged edit passes get an exact spec instead
        read = [*read, str(instr)]
    for f in read:
        cmd += ["--read", f]
    with open(log, "w", encoding="utf-8") as out:
        return subprocess.run(cmd, cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL).returncode


def scope_guard(root: Path, allowed: set[str], protected: list[str], run: Run, sid: int,
                baseline: set[str] = frozenset()) -> list[str]:
    """Revert changes outside the stage's files (files already changed before the run, `baseline`, are left alone);
    return the changed protected paths (e.g. tests) so they can be gated."""
    changed = [f for f in changed_paths(root) if f not in baseline and not f.startswith(".agentcontroller/")]
    stray = [f for f in changed if f not in allowed]
    for f in stray:
        if git_ok(root, "cat-file", "-e", f"HEAD:{f}"):            # tracked: restore (also undoes a deletion)
            git(root, "checkout", "HEAD", "--", f)
        else:                                                      # new: unstage if aider added it, then delete
            git(root, "rm", "-q", "--cached", "--ignore-unmatch", "--", f)
            if (root / f).is_file():
                (root / f).unlink()
    if stray:
        run.emit("scope", {"reverted": stray}, sid)
    return [f for f in changed if f not in stray and any(fnmatch.fnmatch(f, p) for p in protected)]


def _check(cmd: str, root: Path, timeout: int = 900) -> tuple[bool, str]:
    """Run a configured command without a shell (on Windows the string goes to CreateProcess as is)."""
    args = cmd if os.name == "nt" else shlex.split(cmd)
    try:
        p = subprocess.run(args, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode == 0, (p.stdout + p.stderr)[-6000:]
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as e:
        return False, f"could not run {cmd!r}: {e}"


def _sig(output: str) -> str:
    lines = [l for l in output.splitlines() if "FAILED" in l or "Error" in l][:10]
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:12]


# ---------- staged runner ----------

def run_stage(s: Settings, run: Run, root: Path, idea: str, st: dict, n: int, summaries: list[str],
              baseline: set[str] = frozenset()) -> bool:
    """Spec -> aider edit -> scope guard -> gated test -> fix. On test failures run.last_failure holds the output."""
    sid, t = st["id"], s.tier
    sdir = run.dir / "stages" / f"{sid:02d}"
    sdir.mkdir(parents=True, exist_ok=True)
    edit, new, read = st.get("edit_files") or [], st.get("create_files") or [], st.get("read_files") or []
    allowed = {f.replace("\\", "/") for f in edit + new}
    system = (s.instructions() + "\n\n" + prompts.SYSTEM).strip()
    emit = lambda k, d: run.emit(k, d, sid)
    msg = prompts.SPEC.format(idea=idea, sid=sid, n=n, goal=st["goal"], edit_files=edit, create_files=new,
                              acceptance=st["acceptance"], summaries="\n".join(summaries) or "(first stage)",
                              files=_files_block(root, edit + new + read))
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": msg}]
    seen: set[str] = set()
    run.last_failure = None
    for attempt in range(t.max_fix_attempts + 1):
        phase = "spec" if attempt == 0 else "fix"
        max_out = t.thinking.get(phase, 6000) + 4000
        why = budget.preflight(llm.msg_tokens(msgs, s.model), max_out, s.model)
        if why:
            run.emit("error", {"phase": phase, "why": why}, sid)
            return False
        r = llm.call(s.model, msgs, max_out, t.thinking.get(phase), emit, phase, attempt=attempt)
        spec = sdir / f"spec{attempt}.md"
        spec.write_text(r.text, encoding="utf-8")
        (sdir / f"reasoning{attempt}.txt").write_text(r.reasoning, encoding="utf-8")
        if r.finish == "length" or not r.text:
            run.emit("error", {"phase": phase, "why": "reply cut off or empty"}, sid)
            return False
        for f in new:
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).touch()
        rc = _aider(s, run, root, spec, edit + new, read, sdir / f"aider{attempt}.log")
        run.emit("edit", {"attempt": attempt, "aider_exit": rc, "diffstat": git(root, "diff", "--stat").strip()[-800:]}, sid)
        touched_protected = scope_guard(root, allowed, s.repo.protected, run, sid, baseline)
        if touched_protected and not approve(run, s, "protected", f"stage {sid} changed {touched_protected}",
                                             {"diff": git(root, "diff", "--", *touched_protected)[:20000]}):
            return False
        if not approve(run, s, "tests", f"run `{st['acceptance']}` (runs model-written code)", {}):
            return False
        ok, out = _check(st["acceptance"], root)
        (sdir / f"test{attempt}.log").write_text(out, encoding="utf-8")
        run.emit("test", {"attempt": attempt, "ok": ok}, sid)
        if ok:
            return True
        run.last_failure = out
        sig = _sig(out)
        if sig in seen:
            run.emit("error", {"why": "same failure twice; stopping"}, sid)
            return False
        seen.add(sig)
        msgs = msgs[:1] + [{"role": "user", "content": msg},
                           {"role": "assistant", "content": r.text[-8000:]},
                           {"role": "user", "content": prompts.FIX.format(
                               output=out[-4000:], files=_files_block(root, edit + new),
                               edit_files=edit, create_files=new)}]
    return False


def _try_replan(s: Settings, run: Run, root: Path, p: dict, prog: dict, todo: list[dict], baseline: set[str]):
    """After a stage failed all its attempts: park its changes in a git stash, ask the model for a new split of the
    remaining work, validate it like the first plan, and gate it like the first plan. Returns the new plan or None
    (then the failed changes are restored for a human)."""
    st = todo[0]
    sid = st["id"]
    prog["replans"] = prog.get("replans", 0) + 1
    run.save("progress.json", prog)         # counted before asking, so a resume never replans twice
    mine = [f for f in changed_paths(root) if f not in baseline and not f.startswith(".agentcontroller/")]
    stash = f"agentlocal {run.id} failed stage {sid}"
    stashed = bool(mine) and git_ok(root, "stash", "push", "--include-untracked", "-m", stash, "--", *mine)
    done = [d for d in p["stages"] if d["id"] in prog["done"]]
    diffstat = git(root, "diff", "--stat", f"{prog['base']}..HEAD").strip() if prog.get("base") else ""
    newp, errs, cont = planmod.replan(p, done, todo, run.last_failure or "", diffstat, s, root,
                                      lambda k, d: run.emit(k, d, sid))
    accepted = False
    if newp:
        accepted = approve(run, s, "plan", f"replan after stage {sid} failed: {len(newp['stages'])} new stages", newp)
    run.emit("replan", {"result": "accepted" if accepted else "continue" if cont else "denied" if newp else "invalid",
                        "errors": errs[:10], "stash": stash if stashed else None}, sid)
    if not accepted:
        if stashed and not git_ok(root, "stash", "pop"):
            run.emit("error", {"why": f"could not restore the failed stage's changes; they are in git stash '{stash}'"})
        return None
    run.save(f"plan.v{prog['replans']}.json", p)
    newplan = {**p, "stages": done + newp["stages"], "replanned_after": sid}
    run.save("plan.json", newplan)
    return newplan


def run_plan(s: Settings, run: Run, root: Path) -> bool:
    p = run.load("plan.json")
    prog = run.load("progress.json", {"done": [], "summaries": [], "shas": []})
    head = git(root, "rev-parse", "HEAD").strip()
    if prog["shas"] and prog["shas"][-1] != head and not git_ok(root, "merge-base", "--is-ancestor", prog["shas"][-1], head):
        run.emit("error", {"why": f"HEAD {head[:8]} does not contain the last stage commit {prog['shas'][-1][:8]}; refusing"})
        return False
    prog.setdefault("base", head)
    prog.setdefault("replans", 0)
    baseline = set(changed_paths(root))     # pre-existing untracked files are never touched
    while True:
        stages = p["stages"]
        todo = [st for st in stages if st["id"] not in prog["done"]]
        if not todo:
            break
        st = todo[0]
        if not approve(run, s, "stage", f"stage {st['id']}: {st['goal']}", st):
            run.emit("stopped", {"stage": st["id"], "reason": "denied"})
            return False
        run.denied = None
        if not run_stage(s, run, root, p["idea"], st, len(stages), prog["summaries"], baseline):
            if run.denied:
                run.emit("stopped", {"stage": st["id"], "reason": "denied", "gate": run.denied})
                return False
            if run.last_failure and prog["replans"] < min(1, s.tier.max_replans):
                newp = _try_replan(s, run, root, p, prog, todo, baseline)
                if newp:
                    p = newp
                    continue
            run.emit("stopped", {"stage": st["id"], "reason": "failed",
                                 "hint": f"fix by hand, commit, then `agentlocal resume {run.id} --mark-done {st['id']}`"
                                         f" (or plain resume to retry the stage)"})
            return False
        diffstat = git(root, "diff", "--stat").strip()
        r = llm.call(s.model, [{"role": "user", "content": prompts.SUMMARY.format(diffstat=diffstat) +
                                "\n\nStage goal: " + st["goal"]}], 1500, 0, lambda k, d: run.emit(k, d, st["id"]),
                     "summary")
        git(root, "add", "-A", "--", *(st.get("edit_files") or []), *(st.get("create_files") or []))
        git(root, "commit", "-q", "-m", f"agentlocal stage {st['id']}: {st['goal'][:60]}")
        prog["done"].append(st["id"])
        prog["summaries"].append(f"Stage {st['id']} ({st['goal']}): {r.text.strip()}\nFiles: {diffstat[-400:]}")
        prog["shas"].append(git(root, "rev-parse", "HEAD").strip())
        run.save("progress.json", prog)
        run.emit("stage_done", {"stage": st["id"]})
    ok, out = _check(s.repo.test_cmd, root, 1800)
    run.emit("final_tests", {"ok": ok, "tail": out[-600:]})
    return ok
