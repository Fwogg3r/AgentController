"""Continue (or start) a Claude Code session from agentdash, billed to the plan.

agentdash starts one of these per message to a finished Claude Code session (and feeds follow-ups, answers and
approval decisions through its inbox while it runs). It resumes the session in place with the Claude Code CLI:
same session id, same transcript file, the user's own settings and plan login. No API key is passed and
ANTHROPIC_API_KEY is removed from the environment, so nothing is billed to the API.

Tool calls that would prompt in the Claude app arrive at `can_use_tool` and wait for a decision from the dashboard;
AskUserQuestion is answered there too. The run ends a couple of seconds after its last result when no follow-up
is queued (runner.Session's idle rule).

State: ~/.agentcontroller/resume/<session id>/ with events.jsonl (drive_start, approval, decision, question,
answer, drive_end, error), inbox.jsonl, pid, and attachments/.
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from claude_agent_sdk import (ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny, ResultMessage,
                              SystemMessage, TaskNotificationMessage, TaskStartedMessage, query)

from . import registry
from .runner import QUESTION_TIMEOUT, Asker, EventLog, Session, read_inbox, short_arg


def state_dir(sid: str) -> Path:
    return registry.home() / "resume" / sid


def find_cli() -> str | None:
    """A native claude.exe (the SDK refuses .cmd wrappers): `claude` on PATH, the npm package's binary, or the
    native installer's. None falls back to the SDK's bundled CLI."""
    env = os.environ.get("AGENTCONTROLLER_CLAUDE_CLI")
    if env and Path(env).is_file():
        return env
    w = shutil.which("claude")
    cands = []
    if w:
        p = Path(w)
        cands += [p] if p.suffix.lower() == ".exe" or os.name != "nt" else []
        cands.append(p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    cands += [Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"]
    for c in cands:
        if c.is_file() and (os.name != "nt" or c.suffix.lower() == ".exe"):
            return str(c)
    return None


class DriveLog(EventLog):
    """The transcript already shows the user's messages: keep them out of this log."""

    def write(self, kind: str, **fields) -> None:
        if kind != "user":
            super().write(kind, **fields)


class Approvals:
    """A tool call the Claude app would ask about: an `approval` event, then {"decision": id, "allow": bool} in the inbox."""

    def __init__(self, inbox: Path, events: EventLog, timeout: float = QUESTION_TIMEOUT):
        self.inbox, self.events, self.timeout, self.n = inbox, events, timeout, 0

    async def decide(self, tool: str, inp: dict) -> "PermissionResultAllow | PermissionResultDeny":
        self.n += 1
        aid = f"a{int(time.time())}-{self.n}"
        _, seen = read_inbox(self.inbox)
        detail = json.dumps(inp, ensure_ascii=False)
        self.events.write("approval", id=aid, tool=tool, arg=short_arg(inp), detail=detail[:3000])
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            await asyncio.sleep(0.7)
            items, seen = read_inbox(self.inbox, seen)
            for it in items:
                if it.get("control") == "stop":
                    self.events.write("decision", id=aid, allow=False, cancelled=True)
                    return PermissionResultDeny(message="The owner stopped this turn.", interrupt=True)
                if it.get("decision") == aid:
                    allow = bool(it.get("allow"))
                    self.events.write("decision", id=aid, allow=allow)
                    if allow:
                        return PermissionResultAllow()
                    return PermissionResultDeny(message=str(it.get("message") or "The owner denied this tool call."))
        self.events.write("decision", id=aid, allow=False, cancelled=True)
        return PermissionResultDeny(message="No decision from the owner in time.")


async def drive(sid: str, cwd: str, first: dict, model: str | None, sd: Path, new: bool = False) -> int:
    events = DriveLog(sd / "events.jsonl")
    inbox = sd / "inbox.jsonl"
    asker, approvals = Asker(inbox, events), Approvals(inbox, events)

    async def approve(tool: str, inp: dict, _ctx):
        if tool == "AskUserQuestion":
            return await asker.ask(inp)
        return await approvals.decide(tool, inp)

    session = Session(first.get("text") or "", inbox, events, first_files=first.get("files") or None)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # the plan login, never the API
    options = ClaudeAgentOptions(
        resume=None if new else sid, session_id=sid if new else None, cwd=cwd, cli_path=find_cli(), model=model, permission_mode="default", can_use_tool=approve,
        setting_sources=["user", "project", "local"], env=env, max_buffer_size=64 * 1024 * 1024,
    )
    events.write("drive_start", session=sid, model=model, new=new)
    code = 0
    try:
        async for msg in query(prompt=session.prompts(), options=options):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                src = msg.data.get("apiKeySource")
                if src not in (None, "none"):  # an API key would bill the API: stop before any model call
                    events.write("error", text=f"refused: the CLI authenticated with an API key ({src}), not the plan")
                    return 3
            elif isinstance(msg, TaskStartedMessage):
                session.task_started(msg.task_id)
            elif isinstance(msg, TaskNotificationMessage):
                session.task_ended(msg.task_id)
            elif isinstance(msg, ResultMessage):
                session.on_result()
                if msg.subtype != "success":
                    events.write("error", text=f"turn ended: {msg.subtype}")
    except Exception as e:  # noqa: BLE001 - reported in the dashboard, not swallowed
        events.write("error", text=f"{type(e).__name__}: {e}"[:500])
        code = 1
    finally:
        session.done = True
        events.write("drive_end")
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True, help="full Claude Code session id (with --new: the id to give it)")
    ap.add_argument("--new", action="store_true", help="start a new session instead of resuming one")
    ap.add_argument("--cwd", required=True, help="the session's working directory")
    ap.add_argument("--first", required=True, help="JSON file: {text, files: [{name, path}]}")
    ap.add_argument("--model", help="model id (default: the session's last model)")
    args = ap.parse_args(argv)
    sd = state_dir(args.session)
    sd.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:  # started windowless: log to a file
        log = open(sd / "drive.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    first = json.loads(Path(args.first).read_text(encoding="utf-8"))
    (sd / "pid").write_text(str(os.getpid()))
    try:
        return asyncio.run(drive(args.session, args.cwd, first, args.model, sd, new=args.new))
    finally:
        (sd / "pid").unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
