"""agentlocal plan validation and the propose/repair loop with a fake model."""
import json

from conftest import reply

from agentcontroller.local import plan as planmod


def stage(sid=1, **kw):
    st = {"id": sid, "goal": "do it", "edit_files": ["src/a.py"], "create_files": [], "read_files": [],
          "acceptance": "uv run pytest -q tests/test_a.py", "covers": [0], "depends_on": []}
    st.update(kw)
    return st


def plan(*stages, criteria=("works",)):
    return {"criteria": list(criteria), "stages": list(stages)}


def errs(s, repo, p, **kw):
    return planmod.validate(p, s, repo, **kw)


def test_valid_plan(settings, repo):
    assert errs(settings(), repo, plan(stage(1), stage(2, edit_files=["src/b.py"], depends_on=[1]))) == []


def test_shape_errors(settings, repo):
    s = settings()
    assert errs(s, repo, None) and errs(s, repo, {"stages": []}) and errs(s, repo, [1])
    e = errs(s, repo, plan(stage(1, edit_files="src/a.py")))
    assert any("must be a list" in x for x in e)


def test_files_must_exist_and_new_files_must_not(settings, repo):
    e = errs(settings(), repo, plan(stage(1, edit_files=["src/nope.py"], create_files=["src/a.py"])))
    assert any("nope.py does not exist" in x for x in e)
    assert any("already exists" in x for x in e)


def test_paths_outside_repo_rejected(settings, repo):
    e = errs(settings(), repo, plan(stage(1, edit_files=["../outside.py"], read_files=["C:/x.py"],
                                         create_files=[".git/hooks/pre-commit"])))
    assert sum("relative path" in x for x in e) == 2 and any("may not touch" in x for x in e)


def test_file_limits(settings, repo):
    e = errs(settings(), repo, plan(stage(1, edit_files=["src/a.py", "src/b.py", "tests/test_a.py"])))
    assert any("3 edit_files; the limit is 2" in x for x in e)
    e = errs(settings("nonew"), repo, plan(stage(1, create_files=["src/c.py"])))
    assert any("not allowed" in x for x in e)
    e = errs(settings(), repo, plan(*(stage(i, depends_on=[]) for i in range(1, 5))))
    assert any("4 stages; the limit is 3" in x for x in e)


def test_token_budget(settings, repo):
    e = errs(settings(), repo, plan(stage(1, read_files=["big.txt"])))
    assert any("tokens; the limit is" in x for x in e)


def test_acceptance_allowlist_and_shell_chars(settings, repo):
    s = settings()
    assert any("must match" in x for x in errs(s, repo, plan(stage(1, acceptance="rm -rf /"))))
    for bad in ("uv run pytest; rm -rf /", "uv run pytest && curl x", "uv run pytest | sh", "uv run pytest $(x)"):
        assert any("one plain command" in x for x in errs(s, repo, plan(stage(1, acceptance=bad)))), bad


def test_dependencies_acyclic(settings, repo):
    s = settings()
    assert any("depends on itself" in x for x in errs(s, repo, plan(stage(1, depends_on=[1]))))
    e = errs(s, repo, plan(stage(1, depends_on=[2]), stage(2, edit_files=["src/b.py"], depends_on=[1])))
    assert any("depends_on 2, which is not an earlier stage" in x for x in e)
    assert any("used twice" in x for x in errs(s, repo, plan(stage(1), stage(1))))


def test_criteria_coverage(settings, repo):
    e = errs(settings(), repo, plan(stage(1, covers=[0]), criteria=["a", "b"]))
    assert any("criteria [1]" in x for x in e)


def test_replan_validation_with_done_stages(settings, repo):
    s = settings()
    done = [stage(1, covers=[0])]
    newp = plan(stage(2, edit_files=["src/b.py"], covers=[1], depends_on=[1]), criteria=["a", "b"])
    assert errs(s, repo, newp, done=done) == []
    clash = plan(stage(1, covers=[1]), criteria=["a", "b"])
    assert any("already used by a finished stage" in x for x in errs(s, repo, clash, done=done))


def test_make_repairs_then_succeeds(settings, repo, fake_chat):
    good = plan(stage(1))
    fake_chat.queue += [reply("Here: " + json.dumps(plan(stage(1, edit_files=["nope.py"])))),
                        reply("Fixed:\n" + json.dumps(good))]
    events = []
    p, e = planmod.make("add a thing", settings(), repo, lambda k, d: events.append((k, d)))
    assert p == good and e == []
    assert len(fake_chat.calls) == 2
    assert "rejected by the validator" in fake_chat.calls[1]["messages"][-1]["content"]
    assert "nope.py does not exist" in fake_chat.calls[1]["messages"][-1]["content"]
    kinds = [k for k, _ in events]
    assert kinds.count("call_start") == 2 and kinds.count("call") == 2 and "plan_rejected" in kinds
    call = next(d for k, d in events if k == "call")
    assert call["context_tokens"] == 32768 and call["phase"] == "plan"
    assert "src/a.py" in fake_chat.calls[0]["messages"][1]["content"]   # the annotated repo map is in the prompt


def test_make_gives_up_after_two_repairs(settings, repo, fake_chat):
    fake_chat.queue += [reply("no json here")] * 5
    p, e = planmod.make("x", settings(), repo, lambda k, d: None)
    assert p is None and e and len(fake_chat.calls) == 3


def test_make_refuses_prompt_that_does_not_fit(settings, repo, fake_chat):
    s = settings()
    s.model.context_tokens = 5000
    p, e = planmod.make("x", s, repo, lambda k, d: None)
    assert p is None and "does not fit" in e[0] and fake_chat.calls == []


def test_replan_continue_and_new_plan(settings, repo, fake_chat):
    s = settings()
    p = {"idea": "x", **plan(stage(1), stage(2, edit_files=["src/b.py"], covers=[1]), criteria=["a", "b"])}
    done, todo = [p["stages"][0]], p["stages"][1:]
    fake_chat.queue.append(reply("continue"))
    newp, e, cont = planmod.replan(p, done, todo, "FAILED test_b", "", s, repo, lambda k, d: None)
    assert newp is None and cont
    assert "FAILED test_b" in fake_chat.calls[0]["messages"][1]["content"]
    fake_chat.queue.append(reply(json.dumps({"stages": [stage(3, edit_files=["src/b.py"], covers=[1])]})))
    newp, e, cont = planmod.replan(p, done, todo, "FAILED", "", s, repo, lambda k, d: None)
    assert newp["stages"][0]["id"] == 3 and not cont and e == []
