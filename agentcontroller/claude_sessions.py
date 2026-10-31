"""Claude Code sessions and their Agent-tool subagents, read from the transcripts on disk.

Plan-billed work (the interactive session and the subagents it spawns) shows up in the dashboard next to
the agentctl runs. Nothing here is billed to the API: the cost is an *API-equivalent estimate* from the same
price table, and these runs are never part of the spend totals or the budget. No hooks: transcripts are only
read, never written.

Layout (`~/.claude/projects/<slug>/`): `<session>.jsonl` and `<session>/subagents/agent-<id>.jsonl`
(+ `.meta.json` with the description). A file is parsed incrementally: per file we keep (offset, summary
state) and read only the bytes appended since the last refresh, up to the last complete line.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .runner import call_usd

RUNNING_IDLE = 120        # file written within this many seconds: running
PENDING_IDLE = 30 * 60    # an unfinished tool call keeps a quiet session "running" this long (a subagent may be working)
DEFAULT_AGE = 7 * 86400
MAX_SESSIONS = 40
PREFIX = "plan."
_LOCK = threading.Lock()


def slug_of(root: Path) -> str:
    """Claude Code names the project dir after the path with every non-alphanumeric turned into `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


def project_dir(root: Path, override: Path | None = None, base: Path | None = None) -> Path | None:
    if override:
        return override if override.is_dir() else None
    base = base or Path.home() / ".claude" / "projects"
    want = slug_of(root).lower()  # Windows: the drive letter's case differs between tools
    try:
        for d in base.iterdir():
            if d.is_dir() and d.name.lower() == want:
                return d
    except OSError:
        pass
    return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _first_line(s: str, n: int = 120) -> str:
    for l in s.splitlines():
        l = l.strip()
        if l:
            return l[:n]
    return ""


def _is_prompt(e: dict) -> bool:
    """A real user prompt: not meta, not a slash-command echo or injected <system-reminder> block."""
    if e.get("isMeta") or e.get("type") != "user":
        return False
    t = _prompt_text((e.get("message") or {}).get("content")).lstrip()
    return bool(t) and not t.startswith("<")


def _prompt_text(content) -> str:
    """The prompt's text; a pasted image with no text still counts as a message (tool results nest theirs)."""
    t = _text_of(content)
    if not t.strip() and isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in content):
        return "[image]"
    return t


def tool_arg(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return ""
    key = {"Bash": "command", "PowerShell": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
           "Grep": "pattern", "Glob": "pattern", "Agent": "description", "WebFetch": "url", "WebSearch": "query"}.get(name)
    v = inp.get(key) if key else None
    if v is None:
        v = next((x for x in inp.values() if isinstance(x, str)), "")
    return _first_line(str(v), 140)


@dataclass
class Parsed:
    """Summary of one transcript, grown line by line."""
    offset: int = 0
    size: int = 0
    mtime: float = 0.0
    title: str = ""
    model: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    tool_calls: int = 0
    tool_ids: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)   # tool_use id -> (name, arg)
    msgs: dict = field(default_factory=dict)      # message id -> (model, usage): a message is logged once per content block
    tot: dict = field(default_factory=lambda: {"in": 0, "out": 0, "cr": 0, "cw": 0, "usd": 0.0})
    last_kind: str = ""
    last_text: str = ""
    turn_done: bool = False  # the last assistant message ended the turn: idle, however recently it was written

    def feed(self, e: dict) -> None:
        ts = e.get("timestamp")
        if isinstance(ts, str):
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        msg = e.get("message")
        if not isinstance(msg, dict):
            return
        t = e.get("type")
        content = msg.get("content")
        if t == "assistant":
            self.turn_done = msg.get("stop_reason") == "end_turn"
            if self.turn_done:
                self.pending.clear()  # a finished turn has nothing in flight (an interrupted call never gets a result)
            mid, usage, model = msg.get("id"), msg.get("usage"), msg.get("model")
            if model and model != "<synthetic>":
                self.model = model
            if isinstance(usage, dict) and mid:
                old = self.msgs.get(mid)
                if old:
                    self._add(*old, sign=-1)
                self.msgs[mid] = (model, usage)
                self._add(model, usage, sign=1)
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("id") not in self.tool_ids:
                    self.tool_ids.add(b.get("id"))
                    self.tool_calls += 1
                    self.pending[b.get("id")] = (b.get("name", "?"), tool_arg(b.get("name", ""), b.get("input")))
                    self.last_kind = "tool"
                elif b.get("type") == "text" and b.get("text"):
                    self.last_kind, self.last_text = "writing", _first_line(b["text"], 160)
                elif b.get("type") == "thinking":
                    self.last_kind = "thinking"
        elif t == "user":
            self.turn_done = False  # a prompt or a background-task notification starts a turn
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    self.pending.pop(b.get("tool_use_id"), None)
            if _is_prompt(e):
                # shown on the card until the model starts answering
                self.last_kind, self.last_text = "user", _first_line(_prompt_text(content), 160)
                if not self.title:
                    self.title = _first_line(_text_of(content))

    def _add(self, model, u: dict, sign: int) -> None:
        g = lambda k: int(u.get(k) or 0)
        self.tot["in"] += sign * g("input_tokens")
        self.tot["out"] += sign * g("output_tokens")
        self.tot["cr"] += sign * g("cache_read_input_tokens")
        self.tot["cw"] += sign * g("cache_creation_input_tokens")
        self.tot["usd"] += sign * call_usd(model or "", u)

    def refresh(self, path: Path) -> bool:
        """Read appended complete lines. False if the file is gone."""
        try:
            st = path.stat()
        except OSError:
            return False
        if st.st_size < self.offset:  # rewritten: start over
            fresh = Parsed()
            self.__dict__.update(fresh.__dict__)
        self.size, self.mtime = st.st_size, st.st_mtime
        if st.st_size > self.offset:
            try:
                with path.open("rb") as fh:
                    fh.seek(self.offset)
                    chunk = fh.read(st.st_size - self.offset)
            except OSError:
                return True
            end = chunk.rfind(b"\n") + 1  # a half-written last line is picked up next time
            self.offset += end
            for raw in chunk[:end].splitlines():
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    try:
                        self.feed(e)
                    except Exception:  # unknown shape: skip the line, keep the rest
                        continue
        return True


_cache: dict[str, Parsed] = {}


def _parsed(path: Path) -> Parsed | None:
    p = _cache.setdefault(str(path), Parsed())
    return p if p.refresh(path) else None


def _meta(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _local(ts: str | None) -> str | None:
    """UTC ISO ("...Z") -> local "YYYY-MM-DDTHH:MM:SS", the format the other runs use."""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _summary(name: str, kind: str, p: Parsed, title: str, parent: str | None, now: float, meta: dict | None = None) -> dict:
    idle = max(0.0, now - p.mtime)
    running = not p.turn_done and (idle < RUNNING_IDLE or (bool(p.pending) and idle < PENDING_IDLE))
    if p.pending:
        n, a = next(iter(reversed(p.pending.values())))
        act = {"kind": "tool", "text": f"{n}: {a}"}
    else:
        act = {"kind": p.last_kind if p.last_kind in ("thinking", "writing", "user") else "thinking",
               "text": p.last_text if p.last_kind == "user" else ""}
    act["since"] = _local(p.last_ts)
    if act["since"]:
        try:
            act["age"] = max(0, round(now - time.mktime(time.strptime(act["since"], "%Y-%m-%dT%H:%M:%S"))))
        except ValueError:
            pass
    model = p.model or (meta or {}).get("model")
    return {
        "name": name, "kind": kind, "parent": parent, "title": title or name, "plan": True,
        "model": model, "cost": round(p.tot["usd"], 4), "cost_estimate": True, "turns": len(p.msgs),
        "tool_calls": p.tool_calls, "tokens_in": p.tot["in"], "tokens_out": p.tot["out"],
        "cache_read": p.tot["cr"], "cache_write": p.tot["cw"],
        "state": "working" if running else "finished", "outcome": None if running else "success",
        "active": running, "activity": act if running else None,
        "started": _local(p.first_ts), "last_activity": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.mtime)),
        "idle_seconds": round(idle), "oneshot": False, "legacy": False, "commits": [], "worktree": None, "branch": None,
        "dirty": None, "pid_alive": False, "budget": None,
    }


def _locate(d: Path, max_age: float, now: float):
    """(session path, mtime, [(agent path, mtime)]) for sessions touched within max_age, newest first."""
    out = []
    try:
        files = [f for f in d.iterdir() if f.suffix == ".jsonl" and f.is_file()]
    except OSError:
        return out
    for f in files:
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        subs = []
        sd = d / f.stem / "subagents"
        if sd.is_dir():
            for a in sd.glob("agent-*.jsonl"):
                try:
                    subs.append((a, a.stat().st_mtime))
                except OSError:
                    pass
        newest = max([m, *(x[1] for x in subs)])  # a quiet main session can still have a working subagent
        if now - newest <= max_age:
            out.append((f, m, subs, newest))
    out.sort(key=lambda x: -x[3])
    return out[:MAX_SESSIONS]


def list_runs(d: Path | None, max_age: float = DEFAULT_AGE, now: float | None = None) -> list[dict]:
    """Run summaries (sessions, each followed by its subagents), same shape as `run_info`."""
    if d is None:
        return []
    now = now or time.time()
    runs = []
    with _LOCK:
        for f, _m, subs, _n in _locate(d, max_age, now):
            sid = f.stem
            p = _parsed(f)
            if not p:
                continue
            sname = PREFIX + sid[:8]
            runs.append(_summary(sname, "plan", p, p.title or "(session)", None, now))
            for a, am in sorted(subs, key=lambda x: -x[1]):
                if now - am > max_age:
                    continue
                ap = _parsed(a)
                if not ap:
                    continue
                meta = _meta(a.with_suffix(".meta.json"))
                aid = a.stem.removeprefix("agent-")
                runs.append(_summary(f"{sname}.{aid[:8]}", "plan-agent", ap, meta.get("description") or ap.title or aid,
                                     sname, now, meta))
    return runs


def find_file(d: Path | None, name: str) -> Path | None:
    """Transcript path for a run name from `list_runs` (`plan.<sid8>` or `plan.<sid8>.<agent8>`)."""
    if d is None or not name.startswith(PREFIX):
        return None
    parts = name[len(PREFIX):].split(".")
    if not re.fullmatch(r"[0-9a-f]{8}", parts[0]) or len(parts) > 2:
        return None
    try:
        for f in d.glob(parts[0] + "*.jsonl"):
            if len(parts) == 1:
                return f
            for a in (d / f.stem / "subagents").glob(f"agent-{parts[1]}*.jsonl"):
                return a
    except OSError:
        pass
    return None


def to_events(e: dict, first_prompt: list) -> list[dict]:
    """Transcript line -> dashboard event(s) (the shapes agentctl writes: prompt/user/text/thinking/tool/tool_result)."""
    msg = e.get("message")
    if not isinstance(msg, dict) or e.get("type") not in ("user", "assistant"):
        return []
    ts = _local(e.get("timestamp"))
    content = msg.get("content")
    out = []
    if e["type"] == "user":
        if _is_prompt(e):
            text = _prompt_text(content)
            if not first_prompt:
                first_prompt.append(1)
                return [{"kind": "prompt", "text": text[:20000], "ts": ts, "truncated": len(text) > 20000}]
            return [{"kind": "user", "text": text[:2000], "ts": ts}]
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else _text_of(c)
                out.append({"kind": "tool_result", "id": b.get("tool_use_id"), "ok": not b.get("is_error"),
                            "text": text.strip()[:400], "ts": ts})
        return out
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        k = b.get("type")
        if k == "text" and b.get("text", "").strip():
            out.append({"kind": "text", "text": b["text"], "ts": ts})
        elif k == "thinking":
            # newer models record only a signature: show the (collapsed) block empty rather than hide that it happened
            out.append({"kind": "thinking", "text": b.get("thinking") or "(thinking not recorded)", "ts": ts})
        elif k == "tool_use":
            out.append({"kind": "tool", "id": b.get("id"), "name": b.get("name", "?"),
                        "arg": tool_arg(b.get("name", ""), b.get("input")), "ts": ts})
    return out
