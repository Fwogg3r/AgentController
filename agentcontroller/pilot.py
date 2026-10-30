"""agentpilot: run coordinator sessions back to back, each with a fresh context (like /clear).

    agentpilot [--name NAME] [--sessions 5] [--hours 8] [--model opus]
    agentpilot --stop NAME          (stop after the current session)

Each session is `claude -p` billed to the Claude plan (ANTHROPIC_API_KEY is removed from its
environment), in the project root, with a fresh context. It resumes from the project's handoff
file, does the next item, follows the project's boundary protocol, and ends its final message with
`AUTOPILOT: CONTINUE` or `AUTOPILOT: STOP <reason>`. `claude -p` waits for its background tasks
(agentctl runs) before it exits.

The pilot shows up in agentdash like an agent run (events + PID file in the runs directory):
- Stop: finish the current session, then end.
- Kill: end now, and kill the session's process tree too.
- A message: added to the next session's prompt as an owner note.
Cost is reported as plan usage (the API-equivalent estimate), never logged as API spend.

The pilot stops on: `AUTOPILOT: STOP`, a session that exits non-zero (usage limit, crash) or
without the AUTOPILOT line, the session or hour limit, or a stop request.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load
from .runner import EventLog, short_arg

DEFAULT_PROMPT = """You are running unattended under agentpilot (a fresh session; the owner is away).
Read CLAUDE.md and {handoff}, then do the next open item, following CLAUDE.md.
Run long jobs (e.g. agentctl) with run_in_background and wait for their notification; this session
stays open until they finish.
Decisions that are genuinely the owner's (spending past a budget, deleting work, changing their
standing preferences, a design choice that review does not settle): write the question in {handoff}
under "Questions for the owner" and pick other work, or stop.
At the boundary: commit, push, and update {handoff}. Then end your final message with exactly one
line: `AUTOPILOT: CONTINUE` if more work remains that needs no owner decision, otherwise
`AUTOPILOT: STOP <reason>`."""


def prompt_for(cfg: Config, notes: list[str]) -> str:
    p = cfg.pilot_prompt_file.read_text(encoding="utf-8") if cfg.pilot_prompt_file else DEFAULT_PROMPT
    p = p.replace("{handoff}", cfg.pilot_handoff)
    if notes:
        p += "\n\nNotes from the owner (sent from the dashboard since the last session):\n" + "\n".join(
            f"- {n}" for n in notes)
    return p


class Inbox:
    """Reads new lines of <name>.inbox.jsonl (written by agentdash and `agentpilot --stop`)."""

    def __init__(self, path: Path):
        self.path = path
        self.seen = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
        self.stop = False
        self.notes: list[str] = []

    def poll(self) -> None:
        if not self.path.exists():
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        fresh, self.seen = lines[self.seen:], len(lines)
        for line in fresh:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("control") == "stop":
                self.stop = True
            elif isinstance(item.get("text"), str) and item["text"].strip():
                self.notes.append(item["text"].strip())


def run_session(cmd: list[str], prompt: str, cfg: Config, env: dict, events: EventLog,
                inbox: Inbox) -> tuple[int, dict]:
    """Streams one session's stream-json output into the event log; returns (exit code, result fields).

    The prompt goes in on stdin: on Windows `claude` is a .cmd shim, and cmd.exe cuts a
    command-line argument at its first newline."""
    result: dict = {}
    # stderr to a file: a full stderr pipe nobody reads would block the session.
    err_path = Path(events.path.with_suffix("").with_suffix(".stderr.txt"))
    err = err_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=cfg.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=err, text=True, encoding="utf-8", errors="replace")
    proc.stdin.write(prompt)
    proc.stdin.close()
    for line in proc.stdout:
        inbox.poll()  # notes and stop requests are noted as they arrive; they act between sessions
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        t = msg.get("type")
        if t == "system" and msg.get("subtype") == "init":
            events.write("session", session_id=msg.get("session_id"))
        elif t == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    events.write("text", text=block["text"].strip())
                elif block.get("type") == "tool_use":
                    events.write("tool", id=block.get("id"), name=block.get("name"), arg=short_arg(block.get("input") or {}))
        elif t == "user":
            content = msg.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    events.write("tool_result", id=block.get("tool_use_id"), ok=not block.get("is_error"),
                                 text=str(c or "")[:300])
        elif t == "result":
            result = msg
    proc.wait()
    err.close()
    if proc.returncode:
        tail = err_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        events.write("text", text=f"[pilot] claude exited {proc.returncode}: {tail}")
    return proc.returncode, result


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="agentcontroller.toml (or its directory); default: walk up from the cwd")
    ap.add_argument("--name", help="run name in the dashboard (default pilot-YYYYMMDD-HHMM)")
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--model", help="claude model alias (default: [pilot] model, else opus)")
    ap.add_argument("--stop", metavar="NAME", help="ask a running pilot to stop after its current session")
    args = ap.parse_args(argv)
    cfg = load(args.project)
    cfg.runs.mkdir(parents=True, exist_ok=True)

    if args.stop:
        with (cfg.runs / f"{args.stop}.inbox.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"control": "stop", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
        print(f"[pilot] stop requested for {args.stop}", file=sys.stderr)
        return 0

    name = args.name or datetime.now().strftime("pilot-%Y%m%d-%H%M")
    model = args.model or cfg.pilot_model
    events = EventLog(cfg.runs / f"{name}.events.jsonl")
    inbox = Inbox(cfg.runs / f"{name}.inbox.jsonl")
    pid = cfg.runs / f"{name}.pid"
    # Plan billing: with an API key in the environment, claude would bill the API instead.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    if cfg.path_extra:
        env["PATH"] = os.pathsep.join([*cfg.path_extra, env.get("PATH", "")])
    pid.write_text(str(os.getpid()))
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cfg.root, capture_output=True,
                            text=True).stdout.strip() or None
    events.write("start", model=model, budget=None, billing="plan", brief="agentpilot", worktree=str(cfg.root),
                 branch=branch, base=None)
    print(f"[pilot] {name}: up to {args.sessions} sessions / {args.hours} h on {model} in {cfg.root}",
          file=sys.stderr, flush=True)
    deadline = time.time() + args.hours * 3600
    plan_usd, turns, reason = 0.0, 0, "session limit"
    try:
        for n in range(1, args.sessions + 1):
            inbox.poll()
            if inbox.stop:
                reason = "stop requested"
                break
            if time.time() > deadline:
                reason = "time limit"
                break
            notes, inbox.notes = inbox.notes, []
            events.write("text", text=f"[pilot] session {n}/{args.sessions}" + (f" with {len(notes)} owner note(s)" if notes else ""))
            print(f"[pilot] session {n}/{args.sessions}", file=sys.stderr, flush=True)
            cmd = [shutil.which("claude", path=env.get("PATH")) or "claude", "-p", "--model", model, "--permission-mode",
                   cfg.pilot_permission_mode, "--output-format", "stream-json", "--verbose"]
            code, res = run_session(cmd, prompt_for(cfg, notes), cfg, env, events, inbox)
            plan_usd += res.get("total_cost_usd") or 0.0
            turns += res.get("num_turns") or 0
            # cost stays 0: the dashboard's kill path would log it as API spend.
            events.write("result", cost=0.0, plan_usd=round(plan_usd, 4), turns=turns, subtype=res.get("subtype"))
            text = (res.get("result") or "").strip()
            last = text.splitlines()[-1].strip() if text else ""
            print(f"[pilot] session {n}: exit {code}, {last or '(no final line)'}", file=sys.stderr, flush=True)
            if code != 0:
                reason = f"session exited {code}"
                break
            if not last.startswith("AUTOPILOT: CONTINUE"):
                reason = last or "no AUTOPILOT line"
                break
    finally:
        events.write("text", text=f"[pilot] ended: {reason} (plan usage ≈ ${plan_usd:.2f} API-equivalent)")
        events.write("end", reason=reason)
        pid.unlink(missing_ok=True)
    print(f"[pilot] ended: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
