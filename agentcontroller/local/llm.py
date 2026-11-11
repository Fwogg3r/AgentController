"""Streaming OpenAI-compatible chat client: splits reasoning from the answer and enforces a thinking budget."""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import budget
from .config import Model


@dataclass
class Reply:
    text: str
    reasoning: str
    finish: str            # stop | length | think_budget
    tokens_in: int
    tokens_out: int
    secs: float


def _no_think(messages: list[dict], m: Model, body: dict) -> None:
    nt = m.no_think or {}
    if nt.get("method") == "suffix":
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] + "\n" + nt.get("value", "/no_think")}
    elif nt.get("method") == "param":       # e.g. {"method": "param", "value": {"chat_template_kwargs": {...}}}
        body.update(nt.get("value", {}))


def chat(m: Model, messages: list[dict], max_tokens: int, think_budget: int | None = None,
         on_event: Callable[[str, dict], None] | None = None) -> Reply:
    """think_budget: None = unlimited, 0 = thinking off, N = abort and answer from the reasoning after ~N tokens."""
    messages = [dict(x) for x in messages]
    body = {"model": m.id, "messages": messages, "max_tokens": max_tokens, "stream": True,
            "stream_options": {"include_usage": True}, **m.sampling}
    if think_budget == 0:
        _no_think(messages, m, body)
    emit = on_event or (lambda kind, d: None)
    t0, text, reasoning, finish, usage = time.time(), [], [], "stop", {}
    in_think, rchars = False, 0
    tag = m.reasoning.get("tag", "think")
    req = urllib.request.Request(m.endpoint.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {m.api_key}"})
    with urllib.request.urlopen(req, timeout=m.timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ev = json.loads(line[5:])
            usage = ev.get("usage") or usage
            for ch in ev.get("choices", []):
                d = ch.get("delta", {})
                rc = d.get(m.reasoning.get("field", "reasoning_content")) or ""
                c = d.get("content") or ""
                if c and tag:                       # inline <think>...</think>
                    if f"<{tag}>" in c:
                        in_think, c = True, c.replace(f"<{tag}>", "")
                    if f"</{tag}>" in c:
                        before, _, c = c.partition(f"</{tag}>")
                        rc += before
                        in_think = False
                    elif in_think:
                        rc, c = rc + c, ""
                if rc:
                    reasoning.append(rc)
                    rchars += len(rc)
                    emit("thinking", {"text": rc})
                if c:
                    text.append(c)
                    emit("answer", {"text": c})
                finish = ch.get("finish_reason") or finish
            if think_budget and not text and rchars / m.chars_per_token * 1.15 > think_budget:
                finish = "think_budget"
                break
    rs = "".join(reasoning)
    if finish == "think_budget":            # answer from the truncated reasoning with thinking off
        emit("note", {"text": "thinking budget reached; answering from the reasoning so far"})
        follow = messages + [{"role": "user", "content": "Stop thinking. Using this reasoning, give the final answer "
                                                          "now in the required format:\n\n" + rs[-12000:]}]
        r2 = chat(m, follow, max_tokens, 0, on_event)
        return Reply(r2.text, rs + "\n[budget]\n" + r2.reasoning, r2.finish, r2.tokens_in, r2.tokens_out,
                     time.time() - t0)
    return Reply("".join(text).strip(), rs, finish if finish in ("stop", "length") else "stop",
                 usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), time.time() - t0)


def msg_tokens(messages: list[dict], m: Model) -> int:
    """Estimated input tokens of a message list (contents plus a few tokens of chat template per message)."""
    return sum(budget.tokens(str(x.get("content", "")), m) + 8 for x in messages)


def call(m: Model, messages: list[dict], max_tokens: int, think_budget: int | None, emit: Callable[[str, dict], None],
         phase: str, **extra) -> Reply:
    """chat() wrapped in events for the dashboard: "call_start" (estimate, before the stream) and "call" (measured),
    both carrying the model's context window so a gauge can be drawn while the call streams."""
    est = msg_tokens(messages, m)
    emit("call_start", {"phase": phase, "tokens_in": est, "max_tokens": max_tokens,
                        "context_tokens": m.context_tokens, **extra})
    r = chat(m, messages, max_tokens, think_budget, emit)
    tin = r.tokens_in or est
    emit("call", {"phase": phase, "tokens_in": tin, "tokens_out": r.tokens_out, "context_tokens": m.context_tokens,
                  "context_pct": round(100 * (tin + r.tokens_out) / m.context_tokens), "finish": r.finish,
                  "reasoning_chars": len(r.reasoning), "secs": round(r.secs, 1), **extra})
    return r


def last_json(text: str):
    """The last balanced {...} block in text, parsed; None if there is none."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            depth += {"}": 1, "{": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    return json.loads(text[i:end + 1])
                except json.JSONDecodeError:
                    break
        end = text.rfind("}", 0, end)
    return None
