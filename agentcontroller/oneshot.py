"""agentask: one-shot Messages API calls, recorded as dashboard runs.

    agentask "prompt" [-f file ...] [-m opus|fable|sonnet|haiku] [-e effort] [-o out.md] [--max-tokens N]
    agentask --spend | --backfill

Each call writes <runs>/<name>.events.jsonl (kind "oneshot" in the `start` event) in the format the
dashboard lists: start, prompt, progress, text (the response), cost, result, end. It also appends a
line to the usage log, so spend totals and the budget cap include it. The key is read from the
configured key file, never from the environment. Project specifics (system prompt, default model and
effort) come from agentcontroller.toml [oneshot].
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load

MAX_PROMPT_CHARS = 400_000  # the prompt event keeps the text for the dashboard; files are listed by name only
PROGRESS_EVERY = 2.0
NAME_FAMILY = ("opus", "fable", "sonnet", "haiku")


def family(model: str) -> str:
    return next((k for k in NAME_FAMILY if k in (model or "")), "model")


def usd_of(model: str, u: dict) -> float:
    from .runner import call_usd
    return call_usd(model, u)


def logged_spend(cfg: Config) -> float:
    total = 0.0
    try:
        lines = cfg.usage_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    for l in lines:
        if not l.strip():
            continue
        r = json.loads(l)
        total += r["usd"] if "usd" in r else usd_of(r.get("model", ""), {"input_tokens": r.get("input_tokens", 0),
                                                                           "output_tokens": r.get("output_tokens", 0)})
    return total


def ts_str(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


class Events:
    def __init__(self, path: Path):
        self.path = path

    def write(self, kind: str, ts: str | None = None, **fields) -> None:
        rec = {"ts": ts or ts_str(time.time()), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def unique_name(cfg: Config, model_key: str, t: float) -> str:
    base = f"ask-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}-{model_key}"
    name, i = base, 2
    while (cfg.runs / f"{name}.events.jsonl").exists():
        name, i = f"{base}-{i}", i + 1
    return name


def build_content(prompt: str, files: list[str]) -> str:
    parts = [f'<file path="{Path(f).as_posix()}">\n{Path(f).read_text(encoding="utf-8")}\n</file>' for f in files]
    return "\n\n".join([*parts, prompt])


def ask(cfg: Config, prompt: str, files: list[str] | None = None, model_key: str | None = None,
        effort: str | None = None, out: str | None = None, max_tokens: int = 64000, name: str | None = None,
        echo=None, client=None) -> dict:
    """Make one call and record it. Returns {ok, name, text, usd, input_tokens, output_tokens, stop_reason, error}.

    `echo(text)` receives streamed text; `client` replaces the Anthropic client (tests). The run record
    is written even on errors, a refusal or an empty max_tokens response.
    """
    import anthropic

    files = files or []
    model_key = model_key or cfg.oneshot_model
    model_id = cfg.models.get(model_key, {}).get("id") or model_key
    effort = effort or cfg.oneshot_effort
    t0 = time.time()
    name = name or unique_name(cfg, family(model_id), t0)
    cfg.runs.mkdir(parents=True, exist_ok=True)
    ev = Events(cfg.runs / f"{name}.events.jsonl")
    pid = cfg.runs / f"{name}.pid"
    pid.write_text(str(os.getpid()))  # lets the dashboard tell a waiting call from a dead one
    ev.write("start", oneshot=True, model=model_id, effort=effort, max_tokens=max_tokens, brief=name)
    res = {"ok": False, "name": name, "text": "", "usd": 0.0, "input_tokens": 0, "output_tokens": 0,
           "stop_reason": None, "error": None}
    chars, last = 0, 0.0
    subtype, msg = "error", None
    try:
        content = build_content(prompt, files)  # inside the try: a missing file still leaves a record
        ev.write("prompt", text=prompt[:MAX_PROMPT_CHARS], truncated=len(prompt) > MAX_PROMPT_CHARS,
                 files=[{"path": Path(f).as_posix(), "bytes": Path(f).stat().st_size} for f in files],
                 system=cfg.oneshot_system or None)
        if client is None:
            client = anthropic.Anthropic(api_key=cfg.key_file.read_text(encoding="utf-8").strip())
        kwargs = dict(model=model_id, max_tokens=max_tokens, output_config={"effort": effort},
                      messages=[{"role": "user", "content": content}])
        if cfg.oneshot_system:
            kwargs["system"] = cfg.oneshot_system
        # Haiku has no server-side fallback; the others reroute policy declines automatically.
        if "haiku" not in model_id:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        outf = open(out, "w", encoding="utf-8") if out else None
        pieces: list[str] = []
        try:
            # Stream into the output file as text arrives so a crash never loses paid output.
            with client.beta.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    pieces.append(text)
                    chars += len(text)
                    if echo:
                        echo(text)
                    if outf:
                        outf.write(text)
                        outf.flush()
                    if time.time() - last >= PROGRESS_EVERY:
                        last = time.time()
                        ev.write("progress", phase="text", tokens=chars // 4)
                msg = stream.get_final_message()
        finally:
            if outf:
                outf.close()
            res["text"] = "".join(pieces)
        res["stop_reason"] = msg.stop_reason
        u = msg.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        res.update(ok=True, usd=round(usd_of(msg.model, usage), 6), input_tokens=u.input_tokens,
                   output_tokens=u.output_tokens, model=msg.model, message_id=msg.id)
        subtype = "success" if msg.stop_reason not in ("refusal", "max_tokens") else msg.stop_reason
        if msg.stop_reason == "max_tokens" and not res["text"].strip():
            res["error"] = "max_tokens reached with an empty response (thinking used the whole budget)"
    except anthropic.AuthenticationError:
        res["error"] = "invalid API key"
    except anthropic.RateLimitError as e:
        res["error"] = f"rate limited: {e.message}"
    except anthropic.APIStatusError as e:
        res["error"] = f"API {e.status_code}: {e.message}"
    except anthropic.APIConnectionError:
        res["error"] = "network error"
    except OSError as e:
        res["error"] = str(e)
    secs = round(time.time() - t0, 1)
    if res["text"].strip():
        ev.write("text", text=res["text"].strip())
    if res["error"]:
        ev.write("text", text=f"[error] {res['error']}")
    ev.write("cost", usd=res["usd"], turns=1)
    ev.write("result", cost=res["usd"], turns=1, subtype=subtype, input_tokens=res["input_tokens"],
             output_tokens=res["output_tokens"], seconds=secs, stop_reason=res["stop_reason"], out=out)
    ev.write("end")
    pid.unlink(missing_ok=True)
    if msg is not None:
        rec = {"ts": ts_str(time.time()), "model": res.get("model", model_id), "kind": "oneshot", "name": name,
               "effort": effort, "input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"],
               "usd": res["usd"], "seconds": secs, "message_id": res.get("message_id"), "subtype": subtype}
        with cfg.usage_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    res["seconds"] = secs
    res["subtype"] = subtype
    return res


def backfill(cfg: Config) -> int:
    """Turn old usage-log lines of one-shot calls (no `name`, no `kind`) into run records. Idempotent.

    The first log lines predate the `usd` field, so the dashboard's totals missed them; they get it
    here (the original file is kept once as <log>.bak).
    """
    try:
        lines = cfg.usage_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    cfg.runs.mkdir(parents=True, exist_ok=True)
    made, fixed, out_lines = 0, False, []
    for l in lines:
        if not l.strip():
            out_lines.append(l)
            continue
        r = json.loads(l)
        if "usd" not in r and r.get("model"):
            r["usd"] = round(usd_of(r["model"], {"input_tokens": r.get("input_tokens", 0),
                                                 "output_tokens": r.get("output_tokens", 0)}), 6)
            fixed = True
            l = json.dumps(r)
        out_lines.append(l)
        if r.get("kind") or r.get("name") or not r.get("message_id"):
            continue  # an agent run, or already a named one-shot
        end = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S").timestamp()
        secs = float(r.get("seconds") or 0)
        name = f"ask-{time.strftime('%Y%m%d-%H%M%S', time.localtime(end - secs))}-{family(r['model'])}"
        path = cfg.runs / f"{name}.events.jsonl"
        if path.exists():
            continue
        ev, t0 = Events(path), ts_str(end - secs)
        ev.write("start", ts=t0, oneshot=True, backfilled=True, model=r["model"], effort=r.get("effort"), brief=name)
        ev.write("text", ts=r["ts"], text="(imported from the usage log: the prompt and response were not "
                 f"recorded; message {r['message_id']})")
        ev.write("cost", ts=r["ts"], usd=r["usd"], turns=1)
        ev.write("result", ts=r["ts"], cost=r["usd"], turns=1, subtype="success", input_tokens=r.get("input_tokens"),
                 output_tokens=r.get("output_tokens"), seconds=secs)
        ev.write("end", ts=r["ts"])
        made += 1
    if fixed:
        bak = cfg.usage_log.with_suffix(cfg.usage_log.suffix + ".bak")
        if not bak.exists():
            bak.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cfg.usage_log.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return made


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; model output often contains symbols like Σ.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", nargs="?", help="prompt text (or pipe via stdin)")
    ap.add_argument("-f", "--file", action="append", default=[], help="attach a file (repeatable)")
    ap.add_argument("-m", "--model", help="model key from agentcontroller.toml (default [oneshot].model)")
    ap.add_argument("-e", "--effort", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("-o", "--out", help="also write the response text to this file")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--name", help="run name (default ask-<time>-<model>)")
    ap.add_argument("--project", help="agentcontroller.toml or its directory (default: walk up from the cwd)")
    ap.add_argument("--spend", action="store_true", help="print estimated spend and exit")
    ap.add_argument("--backfill", action="store_true", help="import old usage-log lines as one-shot runs and exit")
    args = ap.parse_args(argv)
    cfg = load(args.project)

    if args.backfill:
        print(f"imported {backfill(cfg)} one-shot runs")
        return 0
    spent, cap = logged_spend(cfg), cfg.cap
    if args.spend:
        print(f"estimated spend: ${spent:.4f}" + (f" of ${cap:.2f} budget" if cap is not None else " (no budget set)"))
        return 0
    if cap is not None and spent >= cap:
        print(f"[refused] estimated spend ${spent:.2f} has reached the ${cap:.2f} budget", file=sys.stderr)
        return 3
    if args.model and args.model not in cfg.models:
        ap.error(f"unknown model '{args.model}' (known: {', '.join(cfg.models)})")
    prompt = args.prompt if args.prompt else sys.stdin.read()
    if not prompt.strip():
        ap.error("empty prompt")
    if not cfg.key_file.exists():
        print(f"missing {cfg.key_file}", file=sys.stderr)
        return 2
    backfill(cfg)  # cheap and idempotent: keeps the dashboard complete without a separate step
    res = ask(cfg, prompt, args.file, args.model, args.effort, args.out, args.max_tokens, args.name,
              echo=lambda t: print(t, end="", flush=True))
    print()
    if res["error"]:
        print(f"[error] {res['error']}", file=sys.stderr)
    if res["stop_reason"] in ("refusal", "max_tokens"):
        print(f"[warn] stop_reason={res['stop_reason']}", file=sys.stderr)
    if res["ok"]:
        print(f"[usage] {res.get('model')} in={res['input_tokens']} out={res['output_tokens']} ${res['usd']:.4f} "
              f"(total ${spent + res['usd']:.4f}" + (f" / ${cap:.2f})" if cap is not None else ")") +
              f" run={res['name']}", file=sys.stderr)
    return 0 if res["ok"] and not res["error"] else 1


if __name__ == "__main__":
    sys.exit(main())
