"""Actual API spend and token usage from the Anthropic Admin API (optional; needs an admin key).

    python -m agentcontroller.usage [--project PATH] [--days 7] [--json] [--by-key]

The admin key file is `admin_key_file` in agentcontroller.toml (default <secrets>/admin.key). Cost data
is daily and lags by hours; the usage log written by the runner is the real-time estimate.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import load

BASE = "https://api.anthropic.com/v1/organizations"


def get(path: str, params: list[tuple[str, str]], key: str) -> dict:
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def paged(path: str, params: list[tuple[str, str]], key: str) -> list[dict]:
    buckets, page = [], None
    while True:
        body = get(path, params + ([("page", page)] if page else []), key)
        buckets += body.get("data", [])
        if not body.get("has_more"):
            return buckets
        page = body["next_page"]


def print_by_key(since: str, key: str, own_key: str) -> int:
    names = {k["id"]: (k.get("name", "?"), k.get("partial_key_hint", "")) for k in get("api_keys", [("limit", "100")], key).get("data", [])}
    usage = paged("usage_report/messages", [("starting_at", since), ("group_by[]", "api_key_id"), ("group_by[]", "model"),
                                            ("bucket_width", "1d"), ("limit", "31")], key)
    for bucket in usage:
        for r in bucket.get("results", []):
            name, hint = names.get(r.get("api_key_id"), ("(no key / console)", ""))
            # Hints look like "sk-ant-api03-abc...wxyz"; match both ends without printing our key.
            head, _, tail = hint.partition("...")
            mine = bool(hint) and own_key.startswith(head) and own_key.endswith(tail)
            print(f"{bucket['starting_at'][:10]}  {name[:24]:<24}{' (this project)' if mine else '':<15} "
                  f"{r.get('model', '?'):<20} in={r.get('uncached_input_tokens', 0):>9} out={r.get('output_tokens', 0):>8}")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by-key", action="store_true", help="token usage per day per API key")
    args = ap.parse_args(argv)
    cfg = load(args.project)
    if not cfg.admin_key_file.exists():
        print(f"missing admin key file {cfg.admin_key_file}", file=sys.stderr)
        return 2
    key = cfg.admin_key_file.read_text(encoding="utf-8").strip()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)
    since = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        cost = paged("cost_report", [("starting_at", since), ("group_by[]", "description"), ("limit", "31")], key)
        usage = paged("usage_report/messages", [("starting_at", since), ("group_by[]", "model"), ("bucket_width", "1d"),
                                                ("limit", "31")], key)
    except urllib.error.HTTPError as e:
        print(f"[error] HTTP {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return 1
    if args.by_key:
        return print_by_key(since, key, cfg.key_file.read_text(encoding="utf-8").strip())

    by_model: dict[str, float] = {}
    total_usd = 0.0
    for bucket in cost:
        for r in bucket.get("results", []):
            usd = float(r.get("amount", "0")) / 100.0  # decimal strings in cents
            total_usd += usd
            label = r.get("model") or r.get("description") or "other"
            by_model[label] = by_model.get(label, 0.0) + usd
    tokens: dict[str, dict[str, int]] = {}
    for bucket in usage:
        for r in bucket.get("results", []):
            t = tokens.setdefault(r.get("model") or "unknown", {"input": 0, "output": 0, "cache_read": 0})
            t["input"] += r.get("uncached_input_tokens", 0)
            t["output"] += r.get("output_tokens", 0)
            t["cache_read"] += r.get("cache_read_input_tokens", 0)
    if args.json:
        print(json.dumps({"since": since, "total_usd": round(total_usd, 4), "usd_by_model": by_model, "tokens_by_model": tokens}))
        return 0
    print(f"Actual API cost since {since[:10]}: ${total_usd:.4f}")
    for m, usd in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"  {m:<40} ${usd:.4f}")
    print("Tokens by model:")
    for m, t in sorted(tokens.items()):
        print(f"  {m:<40} in={t['input']:>9}  out={t['output']:>9}  cache_read={t['cache_read']:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
