"""Text view of agent runs in the terminal.

    agentwatch [--project PATH]            # list runs: status, cost, turns
    agentwatch NAME [-f]                   # print a run's events (-f: follow until it ends)
"""

import argparse
import json
import sys
import time

from .config import load


def fmt(e: dict) -> str | None:
    k, ts = e.get("kind"), e.get("ts", "")[11:]
    if k == "text":
        return f"{ts} | {e['text']}"
    if k == "user":
        return f"{ts} >> USER: {e['text']}"
    if k == "tool":
        return f"{ts}   > {e['name']}: {e.get('arg', '')}"
    if k == "tool_result":
        return f"{ts}     {'ok' if e.get('ok') else 'ERR'}: {e.get('text', '')[:120]!r}"
    if k == "result":
        return f"{ts} -- turn done ${e.get('cost', 0):.2f}, {e.get('turns')} turns ({e.get('subtype')})"
    if k in ("start", "end"):
        return f"{ts} == {k} {'' if k == 'end' else e.get('model', '')}"
    return None


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?")
    ap.add_argument("--project")
    ap.add_argument("-f", "--follow", action="store_true")
    args = ap.parse_args(argv)
    runs = load(args.project).runs
    if not args.name:
        for p in sorted(runs.glob("*.events.jsonl")):
            ev = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            last = next((e for e in reversed(ev) if e["kind"] == "result"), {})
            state = "finished" if ev and ev[-1]["kind"] == "end" else "active?"
            print(f"{p.name[:-13]:<30} {state:<9} ${last.get('cost', 0):.2f}  {last.get('turns', 0)} turns")
        return 0
    path = runs / f"{args.name}.events.jsonl"
    pos = 0
    while True:
        if path.exists():
            data = path.read_bytes()[pos:]
            end = data.rfind(b"\n") + 1
            pos += end
            for l in data[:end].decode("utf-8", errors="replace").splitlines():
                e = json.loads(l)
                if (s := fmt(e)):
                    print(s, flush=True)
                if e["kind"] == "end":
                    return 0
        if not args.follow:
            return 0
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
