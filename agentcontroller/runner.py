"""agentctl: run a headless agent through the Claude Agent SDK, billed to an API key.

    agentctl --brief briefs/task.md --name my-task --base main --model sonnet --budget 3 [--resume SESSION]

The Agent SDK is used (not `claude -p`) because API/Agent SDK credits are billed separately from
Claude Code plan usage; see README.md. Everything project-specific comes from agentcontroller.toml.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny,
                              ResultMessage, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
                              query)

from .config import Config, load

IDLE_GRACE = 2.0  # seconds to wait after the last result for an inbox message before ending

# Static allow-patterns proved unreliable headless, so every tool call goes through `approve`:
# allow by default, deny dangerous shell patterns, confine file writes to the agent's worktree.
BASE_DENY = [
    r"\bgit\s+(push|reset\s+--hard|checkout|switch|worktree|clean|rebase|branch\s+-D|stash)\b",
    r"ANTHROPIC_API_KEY", r"\b(curl|wget|Invoke-WebRequest|iwr|ssh|scp)\b",
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(/|~|\.\.|[A-Za-z]:)", r"\b(shutdown|format|reg\s+(add|delete)|setx)\b",
    r"\bnpm\s+(i|install|publish)\b",
]


def deny_patterns(cfg: Config) -> list[str]:
    # The secrets dir and key file names are denied so an agent can't read the key.
    own = [re.escape(cfg.secrets.name), re.escape(cfg.key_file.name)]
    return BASE_DENY + own + cfg.deny


def denied(cmd: str, patterns: list[str]) -> str | None:
    return next((p for p in patterns if re.search(p, cmd, re.IGNORECASE)), None)


def make_approver(worktree: Path, patterns: list[str]):
    root = worktree.resolve()

    async def approve(tool: str, inp: dict, _ctx) -> "PermissionResultAllow | PermissionResultDeny":
        if tool == "Bash" and (pat := denied(inp.get("command", ""), patterns)):
            return PermissionResultDeny(message=f"blocked by runner policy ({pat})")
        if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            target = Path(inp.get("file_path") or inp.get("notebook_path") or "").resolve()
            if root not in target.parents and target != root:
                return PermissionResultDeny(message=f"writes are limited to {root}")
        if tool in ("WebFetch", "WebSearch"):
            return PermissionResultDeny(message="no network access in this runner")
        return PermissionResultAllow()

    return approve


def logged_spend(cfg: Config) -> float:
    try:
        return sum(json.loads(l).get("usd", 0.0) for l in cfg.usage_log.read_text(encoding="utf-8").splitlines() if l.strip())
    except OSError:
        return 0.0


class EventLog:
    """Structured JSONL event log (read by the dashboard) next to the plain text log."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, kind: str, **fields) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def short_arg(inp: dict) -> str:
    for k in ("command", "file_path", "path", "pattern", "notebook_path", "description"):
        if inp.get(k):
            return str(inp[k]).replace("\n", " ")[:160]
    return json.dumps(inp, ensure_ascii=False)[:160]


def user_msg(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}


class Session:
    """Streaming prompt: the brief, then inbox lines as they arrive.

    A streaming prompt (rather than a plain string) is also what keeps the SDK's permission channel
    (`can_use_tool`) alive. Idle rule: every user message yields one ResultMessage. When all sent
    messages have a result and nothing new arrives within IDLE_GRACE, the generator returns and the
    run ends. A {"control":"stop"} line ends the run after the current turn's result.
    """

    def __init__(self, brief: str, inbox: Path, events: EventLog):
        self.brief, self.inbox, self.events = brief, inbox, events
        self.pending = 0
        self.stop = False
        self.idle_since: float | None = None
        self.done = False
        # Lines left over from an earlier run (e.g. before --resume) must not be replayed.
        self.seen = len(inbox.read_text(encoding="utf-8").splitlines()) if inbox.exists() else 0

    def _new_lines(self) -> list[dict]:
        if not self.inbox.exists():
            return []
        lines = self.inbox.read_text(encoding="utf-8").splitlines()
        fresh, self.seen = lines[self.seen:], len(lines)
        out = []
        for l in fresh:
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out

    def on_result(self) -> None:
        self.pending -= 1
        if self.pending <= 0:
            self.idle_since = time.time()

    async def prompts(self):
        self.pending = 1
        yield user_msg(self.brief)
        while not self.done:
            await asyncio.sleep(1.0)
            for item in self._new_lines():
                if item.get("control") == "stop":
                    self.stop = True
                elif item.get("text") and not self.stop:
                    self.pending += 1
                    self.idle_since = None
                    self.events.write("user", text=item["text"])
                    yield user_msg(item["text"])
            if self.stop and self.pending <= 0:
                return
            if self.idle_since is not None and time.time() - self.idle_since >= IDLE_GRACE:
                return


async def run_agent(brief: str, options: "ClaudeAgentOptions", events: EventLog, inbox: Path,
                    textlog: Path | None) -> tuple[dict, str]:
    """Streams the run; returns the last result fields (cost is cumulative) + key source."""
    result, key_source = {}, "unknown"
    session = Session(brief, inbox, events)

    def say(text: str) -> None:
        print(text, file=sys.stderr, flush=True)
        if textlog:
            with textlog.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")

    try:
        async for msg in query(prompt=session.prompts(), options=options):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                key_source = msg.data.get("apiKeySource", "unknown")
                # Printed immediately so a crashed run can be resumed with --resume.
                say(f"[session] {msg.data.get('session_id')}")
                events.write("session", session_id=msg.data.get("session_id"))
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        say(block.text.strip()[:400])
                        events.write("text", text=block.text.strip())
                    elif isinstance(block, ToolUseBlock):
                        events.write("tool", name=block.name, arg=short_arg(block.input or {}))
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        c = block.content
                        if isinstance(c, list):
                            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                        events.write("tool_result", ok=not block.is_error, text=str(c or "")[:300])
            elif isinstance(msg, ResultMessage):
                result = {
                    "subtype": msg.subtype, "result": msg.result, "total_cost_usd": msg.total_cost_usd or 0.0,
                    "num_turns": msg.num_turns, "session_id": msg.session_id, "usage": msg.usage or {},
                    "is_error": msg.is_error, "api_error_status": msg.api_error_status,
                }
                events.write("result", cost=result["total_cost_usd"], turns=msg.num_turns, subtype=msg.subtype)
                session.on_result()
                if msg.subtype != "success":
                    break  # budget/turn cap: no point waiting for more input
    finally:
        session.done = True
    return result, key_source


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="agentcontroller.toml (or its directory); default: walk up from the cwd")
    ap.add_argument("--brief", help="markdown file with the task brief")
    ap.add_argument("--name", help="worktree + branch name")
    ap.add_argument("--base", help="branch/commit to start from")
    ap.add_argument("--model", help="model alias from the config")
    ap.add_argument("--budget", type=float, help="hard USD cap for this run")
    ap.add_argument("--max-turns", type=int)
    ap.add_argument("--resume", help="session id to continue (the brief becomes the follow-up message)")
    ap.add_argument("--no-textlog", action="store_true", help="don't write <name>.log (stderr is redirected there)")
    ap.add_argument("--check-policy", action="store_true", help="self-test the deny list and exit")
    args = ap.parse_args(argv)
    cfg = load(args.project)
    patterns = deny_patterns(cfg)

    if args.check_policy:
        cases = {"git push --dry-run": True, "git stash push -u": True, f"cat {cfg.secrets.name}/x": True,
                 "curl http://x": True, "rm -rf /c/Users": True, "git status": False, "ls -la": False,
                 "rm -rf target/x": False}
        bad = [c for c, want in cases.items() if bool(denied(c, patterns)) != want]
        print("policy OK" if not bad else f"policy FAILED for: {bad}")
        return 1 if bad else 0

    for req in ("brief", "name", "budget"):
        if getattr(args, req) is None:
            ap.error(f"--{req} is required")
    model = args.model or cfg.default_model
    if model not in cfg.models:
        ap.error(f"unknown model {model!r}; configured: {', '.join(cfg.models)}")
    model_id = cfg.models[model]["id"]

    spent = logged_spend(cfg)
    if cfg.cap is not None and spent + args.budget > cfg.cap:
        print(f"[refused] logged ${spent:.2f} + run budget ${args.budget:.2f} exceeds cap ${cfg.cap:.2f}", file=sys.stderr)
        return 3

    base = args.base or cfg.default_base
    wt = cfg.worktrees / args.name
    if not wt.exists():
        subprocess.run(["git", "worktree", "add", str(wt), "-b", args.name, base], cwd=cfg.root, check=True)

    key_file = cfg.key_file.as_posix()
    options = ClaudeAgentOptions(
        model=model_id,
        cwd=str(wt),
        max_turns=args.max_turns or cfg.default_max_turns,
        max_budget_usd=args.budget,
        permission_mode="default",  # every non-read tool call reaches `approve`
        allowed_tools=["Read", "Glob", "Grep", "TodoWrite"],
        can_use_tool=make_approver(wt, patterns),
        system_prompt={"type": "preset", "preset": "claude_code", "append": cfg.rules},
        setting_sources=["project"],  # loads the worktree's CLAUDE.md
        # apiKeyHelper is used without the interactive approval an env var needs, and keeps
        # the key scoped to this child process.
        settings=json.dumps({"apiKeyHelper": f"cat {key_file}"}),
        # Tool results can carry base64 screenshots; the 1 MB default aborts the run.
        max_buffer_size=64 * 1024 * 1024,
        env={"PATH": os.pathsep.join([*cfg.path_extra, os.environ.get("PATH", "")])},
        resume=args.resume,
    )
    start = time.time()
    cfg.runs.mkdir(parents=True, exist_ok=True)
    # Every run records its own PID (not just dashboard launches), so the dashboard can tell
    # a quiet-but-alive run from a dead one. A hard kill skips the `finally`, leaving the
    # file behind with a dead PID and no `end` event, which the dashboard reports as "dead".
    (cfg.runs / f"{args.name}.pid").write_text(str(os.getpid()))
    events = EventLog(cfg.runs / f"{args.name}.events.jsonl")
    events.write("start", model=model_id, budget=args.budget, session_id=args.resume, brief=Path(args.brief).name,
                 worktree=str(wt), branch=args.name, base=base)
    try:
        out, key_source = asyncio.run(run_agent(
            Path(args.brief).read_text(encoding="utf-8"), options, events, cfg.runs / f"{args.name}.inbox.jsonl",
            None if args.no_textlog else cfg.runs / f"{args.name}.log"))
    finally:
        events.write("end")
        (cfg.runs / f"{args.name}.pid").unlink(missing_ok=True)
    if key_source != "apiKeyHelper":
        print(f"[warn] agent authenticated via {key_source!r}, not the API key", file=sys.stderr)

    usd = float(out.get("total_cost_usd", 0.0))
    usage = out.get("usage", {})
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model_id, "kind": "agent", "name": args.name,
        "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
        "usd": round(usd, 4), "turns": out.get("num_turns"), "seconds": round(time.time() - start),
        "subtype": out.get("subtype"), "session_id": out.get("session_id"),
    }
    with cfg.usage_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    (cfg.runs / f"{args.name}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")

    print(out.get("result", ""))
    print(f"\n[agent {args.name}] {record['subtype']} turns={record['turns']} ${usd:.2f} "
          f"(logged total ${spent + usd:.2f}" + (f" / cap ${cfg.cap:.2f})" if cfg.cap else ")") + f" worktree={wt}",
          file=sys.stderr)
    return 0 if out.get("subtype") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
