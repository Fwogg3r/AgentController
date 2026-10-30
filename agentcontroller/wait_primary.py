"""Block until a new line appears in primary_inbox.jsonl, print it, exit.

    agentwait [--project PATH] [--after N]

N = lines already seen (default: the current length). The coordinating session runs this in the
background; when it exits, the new message(s) are in its output. The last output line is
`[wait_primary] seen=<count>`: re-arm with `--after <count>` so nothing is missed or repeated.
"""

import argparse
import sys
import time
from pathlib import Path

from .config import load


def lines(p: Path) -> list[str]:
    try:
        return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--after", type=int, help="lines already seen (default: current length)")
    args = ap.parse_args(argv)
    p = load(args.project).runs / "primary_inbox.jsonl"
    after = len(lines(p)) if args.after is None else args.after
    while True:
        cur = lines(p)
        if len(cur) > after:
            for l in cur[after:]:
                print(l, flush=True)
            print(f"[wait_primary] seen={len(cur)}", flush=True)
            return 0
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
