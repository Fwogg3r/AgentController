"""agentlocal: probe | plan | run | resume | status."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import budget, config, llm, plan as planmod
from .run import Run, _aider, approve, git, run_plan

CURRENT: Run | None = None   # the run this process works on, for the registry status at exit


def _register(run: Run, s: config.Settings, root: Path, idea: str) -> None:
    """Show the run in the unified agentdash. A registry problem never breaks a run."""
    global CURRENT
    CURRENT = run
    try:
        from .. import registry
        extra = {"parent": os.environ["AGENTCONTROLLER_PARENT"]} if os.environ.get("AGENTCONTROLLER_PARENT") else {}
        registry.register_agent(
            run.id, "local", root, name=run.id, model=s.model.name, model_id=s.model.id, provider="lmstudio",
            dir=str(run.dir), events=str(run.dir / "events.jsonl"), worktree=str(root), pid=os.getpid(),
            tier=s.tier.name, runner=s.tier.runner, title=(idea.strip().splitlines() or [""])[0][:80],
            context_tokens=s.model.context_tokens, status="running", **extra)
        registry.ensure_dash()
    except Exception as e:  # noqa: BLE001
        print(f"(agentdash registry unavailable: {e})", file=sys.stderr)


def _finish(status: str) -> None:
    if CURRENT is None:
        return
    try:
        from .. import registry
        registry.update_agent(CURRENT.id, status=status, ended=time.time(), pid=None)
    except Exception:  # noqa: BLE001
        pass


def _root() -> Path:
    out = git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
    if not out:
        raise SystemExit("run agentlocal inside a git repository (ideally a worktree on its own branch)")
    return Path(out)


def _guard_branch(root: Path) -> None:
    br = git(root, "branch", "--show-current").strip()
    if br in ("main", "master", ""):
        raise SystemExit(f"refusing to commit stages on '{br or 'detached HEAD'}': use a worktree on its own branch")
    if git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise SystemExit("the worktree has uncommitted changes; commit or stash them first")


def cmd_probe(a) -> None:
    s = config.load(a.model, _root())
    m = s.model
    q = [{"role": "user", "content": "Reply with exactly the JSON {\"ok\": true, \"n\": 7} and nothing else."}]
    res = {}
    for label, tb in (("thinking_on", None), ("thinking_off", 0)):
        r = llm.chat(m, q, 4000, tb)
        res[label] = {"secs": round(r.secs, 1), "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                      "reasoning_chars": len(r.reasoning), "json_ok": llm.last_json(r.text) == {"ok": True, "n": 7},
                      "tok_per_s": round(r.tokens_out / r.secs, 1) if r.secs else None}
    text = Path(budget.__file__).read_text(encoding="utf-8")
    r = llm.chat(m, [{"role": "user", "content": text + "\n\nReply: ok"}], 50, 0)
    if r.tokens_in:
        res["chars_per_token"] = round(len(text) / r.tokens_in, 2)
    res["no_think_works"] = res["thinking_off"]["reasoning_chars"] < 0.2 * max(1, res["thinking_on"]["reasoning_chars"])
    print(json.dumps(res, indent=2))
    print("Copy chars_per_token into the model's entry; if no_think_works is false, change its no_think method.")


def cmd_plan(a) -> Run:
    root = _root()
    s = config.load(a.model, root, a.tier)
    run = Run(root)
    idea = Path(a.idea[1:]).read_text(encoding="utf-8") if a.idea.startswith("@") else a.idea
    run.save("meta.json", {"kind": "local", "model": s.model.name, "tier": s.tier.name, "runner": s.tier.runner,
                           "idea": idea, "started": time.time(), "autonomy": s.autonomy, "sandbox": s.sandbox})
    _register(run, s, root, idea)
    run.emit("start", {"model": s.model.name, "tier": s.tier.name, "dir": str(run.dir),
                       "context_tokens": s.model.context_tokens, "autonomy": s.autonomy})
    if s.tier.runner != "staged" or s.tier.decompose == "never":
        run.save("plan.json", {"idea": idea, "stages": [], "runner": s.tier.runner,
                               "files": list(getattr(a, "files", None) or [])})
        return run
    p, errs = planmod.make(idea, s, root, lambda k, d: run.emit(k, d, 0))
    if not p:
        run.emit("plan_failed", {"errors": errs})
        raise SystemExit(1)
    p["idea"] = idea
    run.save("plan.json", p)
    for st in p["stages"]:
        print(f"  {st['id']}. {st['goal']}  edit={st.get('edit_files')} new={st.get('create_files')} "
              f"check={st.get('acceptance')}")
    if not approve(run, s, "plan", f"{len(p['stages'])} stages for: {idea[:80]}", p):
        run.emit("stopped", {"why": "plan not approved", "reason": "denied"})
        raise SystemExit(1)
    run.emit("plan_ok", {"stages": len(p["stages"]), "run": run.id})
    return run


def _execute(run: Run, root: Path, model: str, tier: str | None) -> None:
    s = config.load(model, root, tier)
    p = run.load("plan.json")
    if p.get("runner") in ("aider-single", "passthrough"):   # top tiers: one shot, no machinery
        spec = run.dir / "idea.md"
        spec.write_text(p["idea"], encoding="utf-8")
        rc = _aider(s, run, root, spec, p.get("files") or [], [], run.dir / "aider.log", passthrough=True)
        run.emit("done", {"aider_exit": rc, "diffstat": git(root, "diff", "--stat").strip()[-800:]})
        sys.exit(0 if rc == 0 else 1)
    ok = run_plan(s, run, root)
    run.emit("done", {"ok": ok})
    sys.exit(0 if ok else 1)


def cmd_run(a) -> None:
    root = _root()
    _guard_branch(root)
    meta_model, tier = a.model, a.tier
    if a.target.startswith("local-"):
        run = Run(root, a.target)
        meta = run.load("meta.json")
        if meta is None:
            raise SystemExit(f"no run {a.target} in {run.dir.parent}")
        meta_model, tier = meta_model or meta["model"], tier or meta.get("tier")
        if getattr(a, "mark_done", None):
            prog = run.load("progress.json", {"done": [], "summaries": [], "shas": []})
            for sid in a.mark_done:
                if sid not in prog["done"]:
                    prog["done"].append(sid)
                    prog["summaries"].append(f"Stage {sid}: finished by hand.")
            prog["shas"].append(git(root, "rev-parse", "HEAD").strip())
            run.save("progress.json", prog)
        s = config.load(meta_model, root, tier)
        _register(run, s, root, meta.get("idea", ""))
        run.emit("resume", {"model": s.model.name, "tier": s.tier.name, "context_tokens": s.model.context_tokens})
    else:
        a.idea = a.target
        run = cmd_plan(a)
    _execute(run, root, meta_model, tier)


def cmd_status(a) -> None:
    run = Run(_root(), a.run_id)
    prog = run.load("progress.json", {"done": []})
    p = run.load("plan.json", {"stages": []})
    print(f"{run.id}: {len(prog['done'])}/{len(p['stages'])} stages done; dir {run.dir}")
    ev = (run.dir / "events.jsonl")
    if ev.exists():
        for line in ev.read_text(encoding="utf-8").splitlines()[-8:]:
            print("  " + line[:200])


def main() -> None:
    ap = argparse.ArgumentParser(prog="agentlocal", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="measure speed, chars/token, thinking toggle, JSON compliance")
    p.add_argument("--model", required=True)
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("plan", help="make and approve a stage plan for an idea (or @file)")
    p.add_argument("idea"); p.add_argument("--model", required=True); p.add_argument("--tier")
    p.set_defaults(fn=lambda a: cmd_plan(a))
    p = sub.add_parser("run", help="run an idea (or @file) end to end, or a planned run id")
    p.add_argument("target"); p.add_argument("--model"); p.add_argument("--tier")
    p.add_argument("--files", nargs="*", default=[], help="passthrough tiers: files aider may edit")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("resume", help="continue a run from its progress")
    p.add_argument("target"); p.add_argument("--model"); p.add_argument("--tier")
    p.add_argument("--mark-done", type=int, nargs="*", default=[], help="stage ids you finished and committed by hand")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("status")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_status)
    a = ap.parse_args()
    if a.cmd == "run" and not a.target.startswith("local-") and not a.model:
        ap.error("--model is required for a new idea")
    try:
        a.fn(a)
    except KeyboardInterrupt:
        if CURRENT:
            CURRENT.emit("stopped", {"why": "interrupted", "reason": "denied"})
        _finish("stopped")
        raise SystemExit(130)
    except SystemExit as e:
        _finish(CURRENT.outcome if CURRENT and CURRENT.outcome else "done" if e.code in (0, None) else "failed")
        raise
    except BaseException:
        _finish("failed")
        raise
    else:   # `plan` (approved, waiting for `run <id>`) ends here
        _finish(CURRENT.outcome if CURRENT and CURRENT.outcome else "done")
