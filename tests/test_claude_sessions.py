"""Parser tests on small synthetic transcripts: uv run --frozen python -m unittest discover tests"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from agentcontroller import claude_sessions as cs


def asst(mid, blocks, ts="2026-01-01T10:00:00.000Z", out=10, model="claude-sonnet-5-5"):
    return {"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": model, "role": "assistant", "content": blocks,
            "usage": {"input_tokens": 5, "output_tokens": out, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}}}


def user(content, ts="2026-01-01T10:00:01.000Z", **kw):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": content}, **kw}


def write(path, events, raw=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
        for r in raw:
            fh.write(r)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.sid = "abcdef12-0000-0000-0000-000000000000"
        self.main = self.d / f"{self.sid}.jsonl"

    def tearDown(self):
        cs._cache.clear()
        self.tmp.cleanup()

    def test_session_and_subagent(self):
        write(self.main, [
            user("<command-name>/clear</command-name>", isMeta=True),
            user("\n  Fix the bug\nsecond line"),
            asst("m1", [{"type": "thinking", "thinking": ""}], out=7),
            asst("m1", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la\nmore"}}], out=7),
            user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
            asst("m2", [{"type": "text", "text": "done"}], out=20),
        ], raw=["not json\n", '{"type":"assistant","message":5}\n'])
        sub = self.d / self.sid / "subagents"
        write(sub / "agent-a1b2c3d4e5.jsonl", [user("Work ONLY inside the tree"), asst("s1", [{"type": "text", "text": "hi"}])])
        (sub / "agent-a1b2c3d4e5.meta.json").write_text(json.dumps({"description": "Implement thing", "model": "sonnet"}))
        runs = cs.list_runs(self.d, now=time.time())
        self.assertEqual([r["kind"] for r in runs], ["plan", "plan-agent"])
        s, a = runs
        self.assertEqual(s["name"], "plan.abcdef12")
        self.assertEqual(s["title"], "Fix the bug")            # meta/command echo skipped, first line only
        self.assertEqual(s["turns"], 2)                          # m1 logged twice, counted once
        self.assertEqual(s["tokens_out"], 7 + 20)
        self.assertEqual(s["cache_read"], 200)
        self.assertEqual(s["tool_calls"], 1)
        self.assertEqual(s["model"], "claude-sonnet-5-5")
        self.assertGreater(s["cost"], 0)
        self.assertTrue(s["active"])                            # just written
        self.assertEqual(a["parent"], s["name"])
        self.assertEqual(a["title"], "Implement thing")
        self.assertEqual(cs.find_file(self.d, a["name"]).name, "agent-a1b2c3d4e5.jsonl")
        self.assertEqual(cs.find_file(self.d, s["name"]), self.main)
        self.assertIsNone(cs.find_file(self.d, "plan.zzzzzzzz"))

    def test_incremental_append(self):
        write(self.main, [user("hello"), asst("m1", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}}])])
        r = cs.list_runs(self.d)[0]
        self.assertEqual(r["tool_calls"], 1)
        self.assertIn("Read: /x", r["activity"]["text"])         # unfinished tool call
        off = cs._cache[str(self.main)].offset
        # half-written line is not consumed until completed
        line = json.dumps(user([{"type": "tool_result", "tool_use_id": "t1", "content": "x"}])) + "\n"
        with self.main.open("a", encoding="utf-8") as fh:
            fh.write(line[:20])
        cs.list_runs(self.d)
        self.assertEqual(cs._cache[str(self.main)].offset, off)
        with self.main.open("a", encoding="utf-8") as fh:
            fh.write(line[20:])
        write(self.main, [asst("m1", [{"type": "text", "text": "x"}], out=99)])  # same message id: usage replaces, not adds
        r = cs.list_runs(self.d)[0]
        self.assertGreater(cs._cache[str(self.main)].offset, off)
        self.assertEqual(r["tokens_out"], 99)
        self.assertEqual(r["turns"], 1)
        self.assertFalse(cs._cache[str(self.main)].pending)

    def test_state_and_age(self):
        write(self.main, [user("hi"), asst("m1", [{"type": "text", "text": "x"}])])
        r = cs.list_runs(self.d, now=time.time() + 600)[0]       # quiet for 10 min, nothing pending
        self.assertEqual(r["state"], "finished")
        self.assertEqual(cs.list_runs(self.d, max_age=60, now=time.time() + 600), [])

    def test_events(self):
        first = []
        ev = lambda e: cs.to_events(e, first)
        self.assertEqual(ev(user("<system-reminder>x</system-reminder>")), [])
        self.assertEqual(ev(user("go"))[0]["kind"], "prompt")
        self.assertEqual(ev(user("again"))[0]["kind"], "user")
        kinds = [e["kind"] for e in ev(asst("m", [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "a"},
                                                  {"type": "tool_use", "id": "t", "name": "Grep", "input": {"pattern": "foo"}}]))]
        self.assertEqual(kinds, ["thinking", "text", "tool"])
        res = ev(user([{"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "y" * 900}], "is_error": True}]))
        self.assertFalse(res[0]["ok"])
        self.assertEqual(len(res[0]["text"]), 400)

    def test_project_dir(self):
        root = Path("C:/Users/X/Doc/My_Proj")
        (self.d / "c--Users-X-Doc-My-Proj").mkdir()
        self.assertEqual(cs.project_dir(root, base=self.d).name, "c--Users-X-Doc-My-Proj")  # drive-letter case differs
        self.assertIsNone(cs.project_dir(Path("D:/nope"), base=self.d))


if __name__ == "__main__":
    unittest.main()
