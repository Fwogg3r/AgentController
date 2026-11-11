"""Stage plans: the model proposes, code validates."""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path, PurePosixPath

from . import budget, llm, prompts
from .config import Settings

FIXED_OVERHEAD = 4000  # system prompt, instructions, spec scaffolding
SHELL_CHARS = set(";&|<>`$\n")  # acceptance commands run without a shell; reject anything that looks like one
REPAIR_ROUNDS = 2


def stage_budget(s: Settings) -> int:
    """Input tokens a stage's files may use in the spec pass (spec output budget comes off the window)."""
    out = s.tier.thinking.get("spec", 6000) + 4000
    return max(1000, s.model.context_tokens - out - s.model.reserve - FIXED_OVERHEAD - 3000)  # 3k for summaries


def _bad_path(f) -> str | None:
    if not isinstance(f, str) or not f.strip():
        return "is not a path"
    p = PurePosixPath(f.replace("\\", "/"))
    if p.is_absolute() or ":" in f or ".." in p.parts:
        return "must be a relative path inside the repository"
    if p.parts and p.parts[0] in (".git", ".agentcontroller"):
        return "is in a folder the agent may not touch"
    return None


def _strs(st: dict, key: str, errs: list[str], sid) -> list[str]:
    v = st.get(key) or []
    if not isinstance(v, list):
        errs.append(f"stage {sid}: {key} must be a list of paths")
        return []
    return v


def validate(plan, s: Settings, root: Path, done: list[dict] | None = None) -> list[str]:
    """Errors in a plan (empty = valid). `done`: stages already finished (a replan's plan holds only the rest:
    their ids may not be reused, later stages may depend on them, and the criteria they cover stay covered)."""
    errs: list[str] = []
    if not isinstance(plan, dict) or not isinstance(plan.get("stages"), list) or not plan["stages"]:
        return ["the answer must be a JSON object with a non-empty 'stages' list"]
    crit, stages, t = plan.get("criteria") or [], plan["stages"], s.tier
    done = done or []
    if len(stages) > t.max_stages:
        errs.append(f"{len(stages)} stages; the limit is {t.max_stages}")
    done_ids = {d.get("id") for d in done}
    ids, covered, cap = set(done_ids), set(), stage_budget(s)
    for d in done:
        covered.update(c for c in d.get("covers") or [] if isinstance(c, int))
    for st in stages:
        if not isinstance(st, dict):
            errs.append("every stage must be a JSON object")
            continue
        sid = st.get("id")
        if not isinstance(sid, int) or isinstance(sid, bool):
            errs.append(f"stage id {sid!r} must be an integer")
        elif sid in done_ids:
            errs.append(f"stage id {sid} is already used by a finished stage; use new ids")
        elif sid in ids:
            errs.append(f"stage id {sid} is used twice")
        edit, new, read = (_strs(st, k, errs, sid) for k in ("edit_files", "create_files", "read_files"))
        if not st.get("goal"):
            errs.append(f"stage {sid}: missing goal")
        if len(edit) > t.max_edit_files:
            errs.append(f"stage {sid}: {len(edit)} edit_files; the limit is {t.max_edit_files}")
        if new and not t.allow_new_files:
            errs.append(f"stage {sid}: creating files is not allowed for this tier")
        bad = [(f, why) for f in edit + new + read if (why := _bad_path(f))]
        for f, why in bad:
            errs.append(f"stage {sid}: {f!r} {why}")
        badset = {f for f, _ in bad}
        for f in edit + read:
            if f not in badset and not (root / f).is_file():
                errs.append(f"stage {sid}: file {f} does not exist")
        for f in new:
            if f not in badset and (root / f).exists():
                errs.append(f"stage {sid}: create_files has {f}, which already exists (put it in edit_files)")
        size = sum(budget.file_tokens(root, f, s.model) for f in edit + read if f not in badset)
        if size > cap:
            errs.append(f"stage {sid}: its files total ~{size} tokens; the limit is {cap}. Split it or read fewer files")
        acc = st.get("acceptance") or ""
        if not isinstance(acc, str) or SHELL_CHARS & set(acc):
            errs.append(f"stage {sid}: acceptance must be one plain command (no ; & | < > ` $)")
        elif not any(fnmatch.fnmatch(acc, p) for p in s.repo.acceptance_allow):
            errs.append(f"stage {sid}: acceptance {acc!r} must match one of {s.repo.acceptance_allow}")
        deps = st.get("depends_on") or []
        for d in deps if isinstance(deps, list) else [deps]:
            if d == sid:
                errs.append(f"stage {sid}: depends on itself")
            elif d not in ids:   # only earlier (or finished) stages: the order is a topological order, so acyclic
                errs.append(f"stage {sid}: depends_on {d}, which is not an earlier stage")
        if isinstance(sid, int):
            ids.add(sid)
        covered.update(c for c in st.get("covers") or [] if isinstance(c, int))
    missing = [i for i in range(len(crit)) if i not in covered]
    if missing:
        errs.append(f"criteria {missing} are not covered by any stage")
    return errs


def _system(s: Settings) -> str:
    return (s.instructions() + "\n\n" + prompts.SYSTEM).strip()


def propose(msgs: list[dict], s: Settings, root: Path, emit, phase: str, check, accept_text=None):
    """Ask, validate with check(plan) -> errors, and repair up to REPAIR_ROUNDS times.
    accept_text(text) -> True ends early without a plan (e.g. a replan answering 'continue').
    Returns (plan | None, errors, ended_early)."""
    t = s.tier
    max_out = t.thinking.get("plan", 8000) + 4000
    why = budget.preflight(llm.msg_tokens(msgs, s.model), max_out, s.model)
    if why:
        return None, [f"the {phase} prompt does not fit: {why} (the repository map is too big for this model)"], False
    errs: list[str] = []
    for attempt in range(REPAIR_ROUNDS + 1):
        r = llm.call(s.model, msgs, max_out, t.thinking.get("plan"), emit, phase, attempt=attempt)
        if accept_text and accept_text(r.text):
            return None, [], True
        plan = llm.last_json(r.text)
        errs = check(plan)
        if not errs:
            return plan, [], False
        emit("plan_rejected", {"phase": phase, "attempt": attempt, "errors": errs[:20]})
        msgs = msgs + [{"role": "assistant", "content": r.text[-6000:]},
                       {"role": "user", "content": prompts.REPAIR.format(errors="\n".join(f"- {e}" for e in errs))}]
    return None, errs, False


def make(idea: str, s: Settings, root: Path, emit) -> tuple[dict | None, list[str]]:
    t = s.tier
    msg = prompts.PLAN.format(
        idea=idea, max_stages=t.max_stages, max_edit_files=t.max_edit_files,
        new_files=" (plus new files)" if t.allow_new_files else " (no new files)",
        stage_budget=stage_budget(s), allow=", ".join(s.repo.acceptance_allow),
        repo_map=budget.annotated_map(root, s.model))
    msgs = [{"role": "system", "content": _system(s)}, {"role": "user", "content": msg}]
    plan, errs, _ = propose(msgs, s, root, emit, "plan", lambda p: validate(p, s, root))
    return plan, errs


def replan(p: dict, done: list[dict], remaining: list[dict], failure: str, diffstat: str, s: Settings, root: Path,
           emit) -> tuple[dict | None, list[str], bool]:
    """Ask the model whether the remaining stages (starting with the one that failed) should change.
    Returns (new plan for the remaining work | None, errors, model_said_continue)."""
    t = s.tier
    msg = prompts.REPLAN.format(
        idea=p.get("idea", ""), criteria=json.dumps(p.get("criteria") or []),
        done="\n".join(f"- {d['id']}: {d.get('goal')}" for d in done) or "(none)",
        failed=json.dumps(remaining[0]), failure=failure[-3000:], remaining=json.dumps(remaining[1:]),
        diffstat=diffstat or "(no changes yet)", max_stages=t.max_stages, max_edit_files=t.max_edit_files,
        stage_budget=stage_budget(s), allow=", ".join(s.repo.acceptance_allow),
        repo_map=budget.annotated_map(root, s.model))
    msgs = [{"role": "system", "content": _system(s)}, {"role": "user", "content": msg}]

    def check(newp):
        if isinstance(newp, dict):
            newp = {**newp, "criteria": p.get("criteria") or []}
        return validate(newp, s, root, done)

    def said_continue(text: str) -> bool:
        return text.strip().strip("`*. \n").lower() == "continue"

    newp, errs, cont = propose(msgs, s, root, emit, "replan", check, said_continue)
    return newp, errs, cont
