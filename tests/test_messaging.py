"""Messages with attachments and AskUserQuestion answered from the dashboard."""
import asyncio
import base64
import json
import os

from dashfix import home  # noqa: F401
from test_dash_http import call, q, server  # noqa: F401
from agentcontroller import dash, registry, runner


class Ev:
    def __init__(self):
        self.out = []

    def write(self, kind, **f):
        self.out.append({"kind": kind, **f})


def test_attachment_blocks(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "p.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "b.bin").write_bytes(b"\0\1\2" * 10)
    files = [{"name": n, "path": str(tmp_path / n)} for n in ("a.txt", "p.png", "b.bin", "gone.txt")]
    blocks = runner.attachment_blocks("look", files)
    assert blocks[0] == {"type": "text", "text": "look"}
    assert any(b["type"] == "text" and "hello" in b["text"] and "attached_file" in b["text"] for b in blocks)
    img = next(b for b in blocks if b["type"] == "image")
    assert img["source"]["media_type"] == "image/png" and base64.b64decode(img["source"]["data"]) == b"\x89PNG fake"
    assert any("b.bin" in b.get("text", "") and "saved at" in b["text"] for b in blocks)
    assert any("could not be read" in b.get("text", "") for b in blocks)
    assert runner.user_msg("plain")["message"]["content"] == "plain"


def _ask(tmp_path, lines, timeout=5):
    inbox = tmp_path / "r.inbox.jsonl"
    inbox.write_text(json.dumps({"text": "old"}) + "\n")
    ev = Ev()
    asker = runner.Asker(inbox, ev, timeout=timeout)
    inp = {"questions": [{"question": "Which?", "header": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]}

    async def go():
        task = asyncio.create_task(asker.ask(inp))
        await asyncio.sleep(0.2)
        qid = ev.out[0]["id"]
        with inbox.open("a") as fh:
            for l in lines(qid):
                fh.write(json.dumps(l) + "\n")
        return await task
    return asyncio.run(go()), ev


def test_asker_answer(tmp_path):
    res, ev = _ask(tmp_path, lambda qid: [{"answer": "other"}, {"answer": qid, "answers": {"Which?": "B"}}])
    assert res.updated_input["answers"] == {"Which?": "B"} and res.updated_input["questions"]
    assert [e["kind"] for e in ev.out] == ["question", "answer"]


def test_asker_stop_and_timeout(tmp_path):
    res, ev = _ask(tmp_path, lambda qid: [{"control": "stop"}])
    assert res.behavior == "deny" and ev.out[-1]["cancelled"]
    res, ev = _ask(tmp_path, lambda qid: [], timeout=0.5)
    assert res.behavior == "deny" and "best judgment" in res.message


def test_question_state():
    evs = [{"kind": "start"}, {"kind": "tool", "id": "t1", "name": "AskUserQuestion"},
           {"kind": "question", "id": "q1", "ts": "2026-01-01T00:00:00",
            "questions": [{"question": "Ship it?", "options": [{"label": "yes"}]}]}]
    st = dash.derive_state("r", evs, True, True, 0)
    assert st["state"] == "waiting" and st["activity"]["kind"] == "question"
    assert st["activity"]["qid"] == "q1" and "Ship it?" in st["activity"]["text"]
    st = dash.derive_state("r", evs + [{"kind": "answer", "id": "q1", "answers": {"Ship it?": "yes"}}], True, True, 0)
    assert st["activity"]["kind"] == "tool"


def test_inbox_files_and_answers(server, home):  # noqa: F811
    port = server
    a = home / "alpha"
    runs = a / ".ac" / "agent_runs"
    runs.mkdir(parents=True)
    (a / "agentcontroller.toml").write_text('[paths]\nsecrets = ".ac"\n[budget]\ncap = 5\n')
    (runs / "r1.events.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00", "kind": "start", "model": "m", "budget": 1}) + "\n"
        + json.dumps({"ts": "2026-01-01T00:00:01", "kind": "question", "id": "q1",
                      "questions": [{"question": "Ship it?", "options": [{"label": "yes"}]}]}) + "\n")
    (runs / "r1.pid").write_text(str(os.getpid()))
    key = registry.register_project(a)
    path = f"/api/inbox{q(key)}&run=r1"

    code, res = call(port, path, {"text": "see", "files": [{"name": "../x y.txt", "data": base64.b64encode(b"hi").decode()}]})
    assert code == 200 and res["files"] == ["x_y.txt"]
    line = json.loads((runs / "r1.inbox.jsonl").read_text().splitlines()[-1])
    assert line["text"] == "see" and open(line["files"][0]["path"], "rb").read() == b"hi"
    assert (runs / "attachments" / "r1") in [p.parent for p in (runs / "attachments" / "r1").iterdir()]

    assert call(port, path, {"files": [{"name": "x", "data": "!!notb64"}]})[0] == 400
    code, res = call(port, path, {"answer": "q1", "answers": {"Ship it?": "yes"}})
    assert code == 200
    assert json.loads((runs / "r1.inbox.jsonl").read_text().splitlines()[-1])["answers"] == {"Ship it?": "yes"}

    code, att = call(port, "/api/attention")
    qa = [x for x in att if x["kind"] == "question"]
    assert qa and qa[0]["action"]["id"] == "q1" and qa[0]["action"]["questions"][0]["question"] == "Ship it?"
